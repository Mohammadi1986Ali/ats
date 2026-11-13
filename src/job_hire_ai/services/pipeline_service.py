"""Durable automatic CV processing pipeline with separately gated model stages."""

import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import aliased

from job_hire_ai.database.models import (
    CandidateDocument,
    DocumentPipelineStage,
    DocumentTextExtraction,
    JobApplication,
    Vacancy,
    now,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.embedding_service import (
    EmbeddingService,
    LocalEmbedder,
    chunk_cv,
    chunk_jd,
    source_hash,
)
from job_hire_ai.services.evidence_validation_service import EvidenceValidationService
from job_hire_ai.text_extraction import EXTRACTOR_VERSION, ExtractedText, TextExtractor

logger = logging.getLogger(__name__)

PIPELINE_STAGES = (
    "TEXT_EXTRACTION",
    "EMBEDDING",
    "PROFILE_EXTRACTION",
    "EVIDENCE_VALIDATION",
    "MATCHING",
    "SCORING",
    "HR_REVIEW",
)


def enqueue_document(session, document_id: int) -> None:
    """Called inside the upload transaction: no uploaded CV can miss its queue record."""
    for stage in PIPELINE_STAGES:
        session.add(
            DocumentPipelineStage(
                document_id=document_id,
                stage=stage,
                status="PENDING" if stage == "TEXT_EXTRACTION" else "WAITING",
            )
        )


class PipelineService:
    def __init__(
        self, db, documents, data_dir: Path, *, embeddings_enabled=False,
        embedding_model="nomic-embed-text", embedding_base_url="http://127.0.0.1:11434",
        embedder=None, profile_extraction_enabled=False, profile_analyzer=None,
        evidence_validation_enabled=False, evidence_validator=None,
    ):
        self.db, self.documents = db, documents
        self.data_dir = data_dir
        self.extractor = TextExtractor(documents.validator)
        self.embeddings_enabled = embeddings_enabled
        self.embedding_service = (
            EmbeddingService(db, embedder or LocalEmbedder(embedding_base_url, embedding_model))
            if embeddings_enabled else None
        )
        self.profile_extraction_enabled = profile_extraction_enabled
        self.profile_analyzer = profile_analyzer
        self.evidence_validation_enabled = evidence_validation_enabled
        self.evidence_validator = evidence_validator or EvidenceValidationService(db)
        self._jd_failed_hashes = set()

    def ensure_worker(self) -> None:
        """Start a detached local worker; an OS lock allows only one active worker.

        Invoked *after* Alembic migration validation. The worker keeps running
        independently of a Streamlit session or browser tab.
        """
        log_path = self.data_dir / "pipeline-worker.log"
        with log_path.open("ab") as log:
            options = {}
            if os.name == "nt":
                # Run the worker without opening a visible Python console window.
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            try:
                subprocess.Popen(  # noqa: S603 - fixed module and interpreter; no shell
                    [
                        sys.executable,
                        "-m",
                        "job_hire_ai.workers.pipeline_worker",
                        "--data-dir",
                        str(self.data_dir),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    **options,
                )
            except OSError as exc:
                logger.warning("Unable to start pipeline worker: %s", type(exc).__name__)
                raise ATSError(
                    "Automatic text processing worker could not start. "
                    "Check data/pipeline-worker.log and restart the ATS."
                ) from exc

    def process_next(self) -> bool:
        """Claim one pending text stage atomically; release DB while extracting."""
        with self.db.transaction() as session:
            stage_id = session.scalar(
                select(DocumentPipelineStage.id)
                .join(CandidateDocument, DocumentPipelineStage.document_id == CandidateDocument.id)
                .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                .where(
                    DocumentPipelineStage.stage == "TEXT_EXTRACTION",
                    DocumentPipelineStage.status == "PENDING",
                    JobApplication.status == "Uploaded",
                )
                .order_by(DocumentPipelineStage.id)
                .limit(1)
            )
            if stage_id is not None:
                claimed = session.execute(
                    update(DocumentPipelineStage)
                    .where(
                        DocumentPipelineStage.id == stage_id,
                        DocumentPipelineStage.status == "PENDING",
                    )
                    .values(
                        status="RUNNING",
                        started_at=now(),
                        completed_at=None,
                        error_summary=None,
                        attempts=DocumentPipelineStage.attempts + 1,
                    )
                ).rowcount
                if not claimed:
                    return True
                document_id = session.get(DocumentPipelineStage, stage_id).document_id
        if stage_id is None:
            if self.embeddings_enabled and self.process_embedding_next():
                return True
            if self.profile_extraction_enabled and self.process_profile_next():
                return True
            return self.process_evidence_next() if self.evidence_validation_enabled else False

        started = time.perf_counter()
        error = None
        try:
            filename, data, _ = self.documents.download(document_id)
            checksum = hashlib.sha256(data).hexdigest()
            with self.db.transaction() as session:
                saved = session.scalar(
                    select(DocumentTextExtraction.id).where(
                        DocumentTextExtraction.document_id == document_id,
                        DocumentTextExtraction.checksum == checksum,
                        DocumentTextExtraction.extractor_version == EXTRACTOR_VERSION,
                    )
                )
            if saved is None:
                extracted = self.extractor.extract(filename, data)
                if not extracted.segments:
                    raise ATSError("No readable text found. Scanned PDFs require OCR.")
                with self.db.transaction() as session:
                    # A completed earlier analysis may have saved the same text concurrently.
                    saved = session.scalar(
                        select(DocumentTextExtraction.id).where(
                            DocumentTextExtraction.document_id == document_id,
                            DocumentTextExtraction.checksum == checksum,
                            DocumentTextExtraction.extractor_version == EXTRACTOR_VERSION,
                        )
                    )
                    if saved is None:
                        session.add(
                            DocumentTextExtraction(
                                document_id=document_id,
                                checksum=checksum,
                                extractor_version=EXTRACTOR_VERSION,
                                source=extracted.serialize(),
                            )
                        )
        except (ATSError, OSError, ValueError) as exc:
            error = str(exc)[:500]
        except Exception:
            logger.exception("Unexpected text-extraction failure for document %s", document_id)
            error = "Unexpected document extraction failure. Inspect the worker log."

        with self.db.transaction() as session:
            stage = session.get(DocumentPipelineStage, stage_id)
            if stage is None:
                return True  # Application removed while work was in progress.
            stage.status = "FAILED" if error else "COMPLETED"
            if not error and self.embeddings_enabled:
                embedding = session.scalar(select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == document_id,
                    DocumentPipelineStage.stage == "EMBEDDING",
                ))
                if embedding is not None and embedding.status == "WAITING":
                    embedding.status = "PENDING"
            stage.error_summary = error
            stage.completed_at = now()
            stage.duration_ms = round((time.perf_counter() - started) * 1000)
        return True

    def process_embedding_next(self) -> bool:
        """Process one pending CV embedding; then index an unprocessed JD."""
        if not self.embeddings_enabled:
            return False
        with self.db.transaction() as session:
            text_stage = aliased(DocumentPipelineStage)
            stage_id = session.scalar(
                select(DocumentPipelineStage.id)
                .join(text_stage, text_stage.document_id == DocumentPipelineStage.document_id)
                .join(CandidateDocument, CandidateDocument.id == DocumentPipelineStage.document_id)
                .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                .where(
                    DocumentPipelineStage.stage == "EMBEDDING",
                    DocumentPipelineStage.status.in_(["PENDING", "WAITING"]),
                    text_stage.stage == "TEXT_EXTRACTION",
                    text_stage.status == "COMPLETED",
                    JobApplication.status == "Uploaded",
                )
                .order_by(DocumentPipelineStage.id).limit(1)
            )
            if stage_id is not None:
                claimed = session.execute(
                    update(DocumentPipelineStage)
                    .where(
                        DocumentPipelineStage.id == stage_id,
                        DocumentPipelineStage.status.in_(["PENDING", "WAITING"]),
                    )
                    .values(
                        status="RUNNING", started_at=now(), completed_at=None,
                        error_summary=None, attempts=DocumentPipelineStage.attempts + 1,
                    )
                ).rowcount
                if not claimed:
                    return True
                document_id = session.get(DocumentPipelineStage, stage_id).document_id
        if stage_id is None:
            return self._index_next_jd()
        started = time.perf_counter()
        error = None
        try:
            with self.db.transaction() as session:
                document = session.get(CandidateDocument, document_id)
                source = (
                    session.scalar(
                        select(DocumentTextExtraction)
                        .where(
                            DocumentTextExtraction.document_id == document_id,
                            DocumentTextExtraction.checksum == document.checksum,
                        )
                        .order_by(DocumentTextExtraction.id.desc()).limit(1)
                    )
                    if document is not None else None
                )
                if source is None:
                    raise ATSError("Text extraction missing; cannot embed this CV.")
                vacancy_id = document.vacancy_id
                fingerprint = source.checksum + ":" + source.extractor_version
                extracted = ExtractedText.deserialize(source.source)
            self.embedding_service.index(
                source_type="CV", vacancy_id=vacancy_id, document_id=document_id,
                source=fingerprint, chunks=chunk_cv(extracted),
            )
        except Exception as exc:
            logger.warning(
                "CV embedding failed for document %s: %s",
                document_id, type(exc).__name__,
            )
            error = (
                str(exc) if isinstance(exc, ATSError)
                else "Unexpected embedding failure; inspect the worker log."
            )[:500]
        with self.db.transaction() as session:
            stage = session.get(DocumentPipelineStage, stage_id)
            if stage is not None:
                stage.status = "FAILED" if error else "COMPLETED"
                stage.error_summary = error
                stage.completed_at = now()
                stage.duration_ms = round((time.perf_counter() - started) * 1000)
        return True


    def process_profile_next(self) -> bool:
        """Run existing source-verified CV analysis after embedding, without a UI click.

        An analysis that yields NEEDS_REVIEW still produced a persisted profile.
        Do not mark the later EVIDENCE_VALIDATION pipeline stage as completed:
        human review and separate evidence validation remain explicit.
        """
        if not self.profile_extraction_enabled or self.profile_analyzer is None:
            return False
        with self.db.transaction() as session:
            embedding = aliased(DocumentPipelineStage)
            stage_id = session.scalar(
                select(DocumentPipelineStage.id)
                .join(embedding, embedding.document_id == DocumentPipelineStage.document_id)
                .join(CandidateDocument, CandidateDocument.id == DocumentPipelineStage.document_id)
                .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                .where(
                    DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
                    DocumentPipelineStage.status.in_(["PENDING", "WAITING"]),
                    embedding.stage == "EMBEDDING",
                    embedding.status == "COMPLETED",
                    JobApplication.status == "Uploaded",
                )
                .order_by(DocumentPipelineStage.id).limit(1)
            )
            if stage_id is not None:
                claimed = session.execute(
                    update(DocumentPipelineStage)
                    .where(
                        DocumentPipelineStage.id == stage_id,
                        DocumentPipelineStage.status.in_(["PENDING", "WAITING"]),
                    )
                    .values(
                        status="RUNNING", started_at=now(), completed_at=None,
                        error_summary=None, attempts=DocumentPipelineStage.attempts + 1,
                    )
                ).rowcount
                if not claimed:
                    return True
                document = session.get(
                    CandidateDocument, session.get(DocumentPipelineStage, stage_id).document_id
                )
                document_id, vacancy_id = document.id, document.vacancy_id

        if stage_id is None:
            return False
        started = time.perf_counter()
        error = None
        try:
            # CVAnalysisService already enforces strict schemas, real evidence
            # references, bounded Ollama retries, and transactional profile saving.
            batch_id = self.profile_analyzer.create_batch(vacancy_id, [document_id])
            self.profile_analyzer.process_batch(batch_id)
            job = self.profile_analyzer.jobs(batch_id)[0]
            if job.status not in {"COMPLETED", "NEEDS_REVIEW"}:
                raise ATSError(
                    job.error_summary or "CV profile extraction failed; inspect the analysis run."
                )
        except ATSError as exc:
            error = str(exc)[:500]
        except Exception:
            logger.exception("Unexpected profile-extraction failure for document %s", document_id)
            error = "Unexpected profile extraction error; inspect the worker log."

        with self.db.transaction() as session:
            stage = session.get(DocumentPipelineStage, stage_id)
            if stage is not None:
                stage.status = "FAILED" if error else "COMPLETED"
                stage.error_summary = error
                stage.completed_at = now()
                stage.duration_ms = round((time.perf_counter() - started) * 1000)
        return True

    def process_evidence_next(self) -> bool:
        """Audit claims after a persisted profile exists; no automatic HR approval."""
        if not self.evidence_validation_enabled:
            return False
        with self.db.transaction() as session:
            profile_stage = aliased(DocumentPipelineStage)
            stage_id = session.scalar(
                select(DocumentPipelineStage.id)
                .join(
                    profile_stage,
                    profile_stage.document_id == DocumentPipelineStage.document_id,
                )
                .join(CandidateDocument, CandidateDocument.id == DocumentPipelineStage.document_id)
                .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                .where(
                    DocumentPipelineStage.stage == "EVIDENCE_VALIDATION",
                    DocumentPipelineStage.status.in_(["PENDING", "WAITING"]),
                    profile_stage.stage == "PROFILE_EXTRACTION",
                    profile_stage.status == "COMPLETED",
                    JobApplication.status == "Uploaded",
                )
                .order_by(DocumentPipelineStage.id)
                .limit(1)
            )
            if stage_id is not None:
                claimed = session.execute(
                    update(DocumentPipelineStage)
                    .where(
                        DocumentPipelineStage.id == stage_id,
                        DocumentPipelineStage.status.in_(["PENDING", "WAITING"]),
                    )
                    .values(
                        status="RUNNING", started_at=now(), completed_at=None,
                        error_summary=None, attempts=DocumentPipelineStage.attempts + 1,
                    )
                ).rowcount
                if not claimed:
                    return True
                document_id = session.get(DocumentPipelineStage, stage_id).document_id
        if stage_id is None:
            return False

        started = time.perf_counter()
        error = None
        issues = 0
        try:
            audit = self.evidence_validator.validate_document(document_id)
            issues = len(audit.issues)
        except ATSError as exc:
            error = str(exc)[:500]
        except Exception:
            logger.exception("Unexpected evidence audit error for document %s", document_id)
            error = "Unexpected evidence-validation failure; inspect the worker log."
        with self.db.transaction() as session:
            stage = session.get(DocumentPipelineStage, stage_id)
            if stage is not None:
                stage.status = "FAILED" if error else "COMPLETED"
                stage.error_summary = (
                    error if error else
                    f"Evidence audit found {issues} issue(s); human review required."
                    if issues else None
                )
                stage.completed_at = now()
                stage.duration_ms = round((time.perf_counter() - started) * 1000)
        return True

    def _index_next_jd(self) -> bool:
        """Refresh changed job descriptions without blocking CV processing."""
        with self.db.transaction() as session:
            jobs = [
                (v.id, v.description)
                for v in session.scalars(
                    select(Vacancy).where(Vacancy.status == "Active").order_by(Vacancy.id)
                )
            ]
        for vacancy_id, description in jobs:
            key = (vacancy_id, source_hash(description))
            if key in self._jd_failed_hashes:
                continue
            if self.embedding_service.current(
                source_type="JD", vacancy_id=vacancy_id,
                document_id=None, source=description,
            ):
                continue
            try:
                self.embedding_service.index(
                    source_type="JD", vacancy_id=vacancy_id, document_id=None,
                    source=description, chunks=chunk_jd(description),
                )
            except Exception as exc:
                logger.warning(
                    "JD embedding failed for vacancy %s: %s",
                    vacancy_id, type(exc).__name__,
                )
                self._jd_failed_hashes.add(key)
            return True
        return False

    def recover_interrupted(self, max_attempts: int = 3) -> None:
        """Called after obtaining the exclusive worker lock, including after a crash."""
        with self.db.transaction() as session:
            for stage in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.stage.in_(
                        ["TEXT_EXTRACTION"]
                        + (["EMBEDDING"] if self.embeddings_enabled else [])
                        + (["PROFILE_EXTRACTION"] if self.profile_extraction_enabled else [])
                        + (["EVIDENCE_VALIDATION"] if self.evidence_validation_enabled else [])
                    ),
                    DocumentPipelineStage.status == "RUNNING",
                )
            ):
                if stage.attempts >= max_attempts:
                    stage.status = "FAILED"
                    stage.error_summary = "Interrupted repeatedly; manual review required."
                    stage.completed_at = now()
                else:
                    stage.status = "PENDING"
                    stage.error_summary = "Interrupted worker; automatically queued for recovery."

    def overview(self, vacancy_id: int | None = None) -> tuple[list[dict], list[dict]]:
        """Read-only live summary and per-document status for the dashboard."""
        with self.db.transaction() as session:
            statement = (
                select(Vacancy, CandidateDocument, JobApplication, DocumentPipelineStage)
                .join(JobApplication, JobApplication.vacancy_id == Vacancy.id)
                .join(CandidateDocument, CandidateDocument.application_id == JobApplication.id)
                .join(
                    DocumentPipelineStage,
                    DocumentPipelineStage.document_id == CandidateDocument.id,
                )
                .where(JobApplication.status == "Uploaded")
            )
            if vacancy_id is not None:
                statement = statement.where(Vacancy.id == vacancy_id)
            entries = session.execute(statement).all()
            summary = {}
            details = {}
            for vacancy, document, application, stage in entries:
                key = (vacancy.id, document.id)
                if key not in details:
                    details[key] = {
                        "Job ID": vacancy.id,
                        "Job": vacancy.title,
                        "Application": application.id,
                        "CV": document.id,
                        "Filename": document.original_filename,
                    }
                details[key][stage.stage] = stage.status
                details[key][f"{stage.stage}_error"] = stage.error_summary
                details[key][f"{stage.stage}_duration_ms"] = stage.duration_ms
                details[key][f"{stage.stage}_attempts"] = stage.attempts
                details[key][f"{stage.stage}_started_at"] = stage.started_at
                if stage.stage == "TEXT_EXTRACTION":
                    info = summary.setdefault(
                        vacancy.id,
                        {"Job ID": vacancy.id, "Job": vacancy.title, "Uploaded": 0,
                         "Queued": 0, "Extracting": 0, "Text complete": 0, "Failed": 0},
                    )
                    info["Uploaded"] += 1
                    column = {"PENDING": "Queued", "RUNNING": "Extracting",
                              "COMPLETED": "Text complete", "FAILED": "Failed"}.get(stage.status)
                    if column:
                        info[column] += 1
            result = []
            for row in details.values():
                row["Current stage"] = next(
                    (
                        label
                        for label in PIPELINE_STAGES
                        if row.get(label) in {"PENDING", "RUNNING", "FAILED"}
                    ),
                    "Awaiting future pipeline stages"
                    if row.get("TEXT_EXTRACTION") == "COMPLETED"
                    else "Unknown",
                )
                result.append(row)
            return list(summary.values()), result
