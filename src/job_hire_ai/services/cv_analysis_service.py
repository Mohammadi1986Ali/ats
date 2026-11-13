import hashlib
import json
import time
from collections import Counter
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from job_hire_ai.ai.cv_prompts import CV_PROMPT_TOKEN_RESERVE, CV_PROMPT_VERSION
from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, SCHEMA_VERSION
from job_hire_ai.ai.cv_wire import WIRE_VERSION
from job_hire_ai.ai.extraction_merger import merge_extractions
from job_hire_ai.ai.literal_cv_fallback import FAST_MODE_WARNING, extract_literal_skills
from job_hire_ai.ai.provider import InferenceMetrics
from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    CVAnalysisBatch,
    CVAnalysisJob,
    CVAnalysisRun,
    DocumentTextExtraction,
    EvidenceReference,
    JobApplication,
    Vacancy,
    now,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.candidate_profile_service import CandidateProfileService
from job_hire_ai.services.evidence_service import claim_issues, verify_quote
from job_hire_ai.text_extraction import (
    EXTRACTOR_VERSION,
    ExtractedText,
    TextExtractor,
    chunk_document,
)
from job_hire_ai.workers.local_lock import worker_lock

ACTIVE_STATUSES = {"EXTRACTING", "ANALYZING", "VALIDATING"}
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "NEEDS_REVIEW"}


class CVAnalysisService:
    def __init__(self, db, documents, provider, settings, lock_path):
        self.db, self.documents, self.provider = db, documents, provider
        self.settings, self.lock_path = settings, lock_path
        self.extractor = TextExtractor(documents.validator)

    def list_documents(self, vacancy_id: int):
        with self.db.transaction() as session:
            documents = list(
                session.scalars(
                    select(CandidateDocument)
                    .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                    .where(
                        JobApplication.vacancy_id == vacancy_id, JobApplication.status == "Uploaded"
                    )
                    .options(
                        selectinload(CandidateDocument.application).selectinload(
                            JobApplication.candidate
                        )
                    )
                    .order_by(CandidateDocument.id)
                )
            )
            jobs = list(session.scalars(select(CVAnalysisJob).order_by(CVAnalysisJob.id.desc())))
            profiles = list(
                session.scalars(
                    select(CandidateProfile)
                    .options(selectinload(CandidateProfile.reviews))
                    .order_by(CandidateProfile.id.desc())
                )
            )
            rows = []
            for document in documents:
                latest_job = next((j for j in jobs if j.document_id == document.id), None)
                latest_profile = next((p for p in profiles if p.document_id == document.id), None)
                state = latest_job.status if latest_job else "PENDING"
                if latest_profile and state not in ACTIVE_STATUSES | {"FAILED"}:
                    state = CandidateProfileService.status(latest_profile)
                rows.append(
                    {
                        "Document": document.id,
                        "Candidate": document.application.candidate.reference,
                        "Filename": document.original_filename,
                        "Status": state,
                        "Profile": latest_profile.id if latest_profile else None,
                        "Version": latest_profile.extraction_version if latest_profile else None,
                        "Profile review": CandidateProfileService.status(latest_profile)
                        if latest_profile
                        else None,
                        "Error": latest_job.error_summary if latest_job else None,
                    }
                )
            return rows

    def create_batch(self, vacancy_id: int, document_ids: list[int], force: bool = False) -> int:
        document_ids = list(dict.fromkeys(document_ids))
        if not 1 <= len(document_ids) <= 100:
            raise ATSError("Select between 1 and 100 CVs for analysis.")
        available = {row["Document"] for row in self.list_documents(vacancy_id)}
        if not set(document_ids) <= available:
            raise ATSError("Select existing CVs belonging to this vacancy's current applications.")
        with self.db.transaction() as session:
            if session.get(Vacancy, vacancy_id) is None:
                raise ATSError("Vacancy no longer exists.")
            batch = CVAnalysisBatch(vacancy_id=vacancy_id)
            session.add(batch)
            session.flush()
            for document_id in document_ids:
                batch.jobs.append(CVAnalysisJob(document_id=document_id, force_reanalysis=force))
            session.flush()
            return batch.id

    def batches(self, vacancy_id: int):
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(CVAnalysisBatch)
                    .where(CVAnalysisBatch.vacancy_id == vacancy_id)
                    .options(selectinload(CVAnalysisBatch.jobs))
                    .order_by(CVAnalysisBatch.id.desc())
                )
            )

    def summary(self, batch_id: int):
        with self.db.transaction() as session:
            batch = session.get(CVAnalysisBatch, batch_id)
            if batch is None:
                raise ATSError("Batch no longer exists.")
            counts = Counter(job.status for job in batch.jobs)
            return {
                "Total": len(batch.jobs),
                "Pending": counts["PENDING"],
                "Completed": counts["COMPLETED"],
                "Failed": counts["FAILED"],
                "Needs review": counts["NEEDS_REVIEW"],
                "Processing": sum(counts[s] for s in ACTIVE_STATUSES),
                "Elapsed processing (s)": round(
                    sum(j.duration_ms or 0 for j in batch.jobs) / 1000, 2
                ),
            }

    def jobs(self, batch_id: int):
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(CVAnalysisJob)
                    .where(CVAnalysisJob.batch_id == batch_id)
                    .options(selectinload(CVAnalysisJob.runs))
                    .order_by(CVAnalysisJob.id)
                )
            )

    def retry_failed(self, batch_id: int):
        with worker_lock(self.lock_path), self.db.transaction() as session:
            for job in session.scalars(
                select(CVAnalysisJob).where(
                    CVAnalysisJob.batch_id == batch_id, CVAnalysisJob.status == "FAILED"
                )
            ):
                if job.document_id is not None:
                    job.status, job.error_summary = "PENDING", None
                    job.worker_token, job.lease_until = None, None

    def process_batch(self, batch_id: int, progress=None):
        """Explicit, sequential worker invocation. Ordinary page reruns never call this."""
        with worker_lock(self.lock_path):
            self._recover(batch_id)
            while self._process_next(batch_id, progress):
                if progress:
                    progress(self.summary(batch_id), None, None)
        return self.summary(batch_id)

    def _recover(self, batch_id):
        # The OS lock proves no other local worker is still executing this analysis.
        with self.db.transaction() as session:
            for job in session.scalars(
                select(CVAnalysisJob).where(
                    CVAnalysisJob.batch_id == batch_id, CVAnalysisJob.status.in_(ACTIVE_STATUSES)
                )
            ):
                for run in job.runs:
                    if run.status in ACTIVE_STATUSES:
                        run.status, run.completed_at = "FAILED", now()
                        run.error_summary = (
                            "Interrupted analysis; explicitly resumed as a new attempt."
                        )
                job.status, job.worker_token, job.lease_until = "PENDING", None, None

    def _configuration_hash(self, digest):
        return hashlib.sha256(
            json.dumps(
                {
                    "model": self.settings.model,
                    "digest": digest,
                    "options": self.settings.options(),
                    "prompt": CV_PROMPT_VERSION,
                    "schema": SCHEMA_VERSION,
                    "extractor": EXTRACTOR_VERSION,
                    "wire": WIRE_VERSION,
                    "literal_fallback": self.settings.cv_literal_fallback_enabled,
                    "fast_mode_1b": self.settings.cv_fast_mode_1b,
                    "fallback_version": "literal-skills-v1",
                    "cv_chunk_chars": self.settings.cv_chunk_chars,
                    "cv_truncation_retries": self.settings.cv_truncation_retries,
                    "cv_max_model_calls": self.settings.cv_max_model_calls,
                    "cv_max_output_tokens": self.settings.cv_max_output_tokens,
                    "chunker": "structure-v1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _stage(self, job_id, token, stage, run_id, **run_values):
        with self.db.transaction() as session:
            job = session.get(CVAnalysisJob, job_id)
            if job is None or job.worker_token != token:
                raise ATSError("Analysis ownership changed; refresh the batch.")
            job.status = stage
            job.lease_until = now() + timedelta(
                seconds=self.settings.timeout_seconds * (self.settings.retries + 1) + 120
            )
            run = session.get(CVAnalysisRun, run_id)
            run.status = stage
            for key, value in run_values.items():
                setattr(run, key, value)

    def _process_next(self, batch_id, progress):
        token, started = uuid4().hex, time.perf_counter()
        metrics = InferenceMetrics()
        with self.db.transaction() as session:
            job = session.scalar(
                select(CVAnalysisJob)
                .where(CVAnalysisJob.batch_id == batch_id, CVAnalysisJob.status == "PENDING")
                .order_by(CVAnalysisJob.id)
            )
            if job is None:
                return False
            job.status, job.worker_token = "EXTRACTING", token
            job.attempts += 1
            job.started_at, job.completed_at, job.error_summary = now(), None, None
            document = session.get(CandidateDocument, job.document_id) if job.document_id else None
            job_id, document_id = job.id, job.document_id
            run = CVAnalysisRun(
                job_id=job.id,
                document_id=document_id,
                document_checksum=document.checksum if document else "",
                model_name=self.settings.model,
                prompt_version=CV_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                config_hash=self._configuration_hash(None),
                inference_options={
                    **self.settings.options(),
                    "metrics": metrics.serialize(),
                    "cv_max_output_tokens": self.settings.cv_max_output_tokens,
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id
        try:
            if document_id is None:
                raise ATSError("The source CV has been deleted. Upload a document before retrying.")
            if progress:
                progress(self.summary(batch_id), document_id, "Extracting source text")
            filename, data, _ = self.documents.download(document_id)
            source, source_id = self._text(document_id, filename, data)
            # No model check is needed here: this mode performs no LLM calls.
            # It saves only explicitly labelled, source-quoted *partial* evidence.
            # An absent/down Ollama model must not fail background CV extraction.
            fast_mode = (
                self.settings.cv_fast_mode_1b
                and self.settings.cv_literal_fallback_enabled
                and self.settings.model in {"llama3.2:1b", "qwen2.5:0.5b"}
            )
            if fast_mode:
                config_hash = self._configuration_hash("no-model-fast-mode")
                self._stage(
                    job_id, token, "ANALYZING", run_id,
                    config_hash=config_hash,
                )
                with self.db.transaction() as session:
                    cached = (
                        None
                        if job.force_reanalysis
                        else session.scalar(
                            select(CandidateProfile)
                            .where(
                                CandidateProfile.document_id == document_id,
                                CandidateProfile.config_hash == config_hash,
                            )
                            .options(selectinload(CandidateProfile.reviews))
                            .order_by(CandidateProfile.id.desc())
                        )
                    )
                    cached_status = (
                        CandidateProfileService.status(cached) if cached else None
                    )
                if cached is not None:
                    self._finish(job_id, token, run_id, cached_status, started, reused=True)
                    return True
                extraction = extract_literal_skills(
                    [
                        {"reference": part.reference, "text": part.text}
                        for part in source.segments
                    ],
                    warning=FAST_MODE_WARNING,
                )
                self._stage(
                    job_id, token, "VALIDATING", run_id,
                    chunk_count=0, chunks_completed=0,
                    inference_options={
                        **self.settings.options(),
                        "metrics": metrics.serialize(),
                        "extraction_mode": "literal-fast-1b",
                    },
                )
                self._save_profile(
                    job_id, token, run_id, document_id, source_id,
                    source, extraction, config_hash, started,
                )
                return True

            status = self.provider.check()
            if not status.available:
                raise ATSError(status.message)
            config_hash = self._configuration_hash(status.digest)
            self._stage(
                job_id,
                token,
                "ANALYZING",
                run_id,
                model_digest=status.digest,
                config_hash=config_hash,
            )
            with self.db.transaction() as session:
                cached = (
                    None
                    if job.force_reanalysis
                    else session.scalar(
                        select(CandidateProfile)
                        .where(
                            CandidateProfile.document_id == document_id,
                            CandidateProfile.config_hash == config_hash,
                        )
                        .options(selectinload(CandidateProfile.reviews))
                        .order_by(CandidateProfile.id.desc())
                    )
                )
                cached_status = CandidateProfileService.status(cached) if cached else None
            if cached:
                self._finish(job_id, token, run_id, cached_status, started, reused=True)
                return True
            # Reserve prompt/schema and response tokens before budgeting chunk text.
            budget = min(
                self.settings.cv_chunk_chars,
                max(0, self.settings.num_ctx - self.settings.num_predict - CV_PROMPT_TOKEN_RESERVE)
                * 2,
            )
            chunks = chunk_document(source, budget)
            outputs = []
            for index, chunk in enumerate(chunks, 1):
                if progress:
                    progress(
                        self.summary(batch_id),
                        document_id,
                        f"Analyzing chunk {index}/{len(chunks)}",
                    )
                self._stage(job_id, token, "ANALYZING", run_id, chunk_count=len(chunks))
                try:
                    output = self.provider.analyze_cv(chunk)
                except ATSError as exc:
                    # Never treat an unavailable model/timeout as successful
                    # inference. Only schema/truncation failures qualify for
                    # an explicitly incomplete, evidence-only fallback.
                    message = str(exc).lower()
                    recoverable = (
                        "valid structured output" in message
                        or "invalid structured output" in message
                        or "truncated" in message
                    )
                    if not self.settings.cv_literal_fallback_enabled or not recoverable:
                        raise
                    if getattr(exc, "metrics", None) is not None:
                        metrics.add(exc.metrics)
                    fallback = extract_literal_skills(chunk)
                    outputs.append(fallback)
                    continue
                if output.model != status.model or output.digest != status.digest:
                    raise ATSError(
                        "Model changed during CV analysis. Retry with one stable local model."
                    )
                outputs.append(output.extraction)
                metrics.add(output.metrics)
                self._stage(
                    job_id,
                    token,
                    "ANALYZING",
                    run_id,
                    chunks_completed=index,
                    inference_options={
                        **self.settings.options(),
                        "metrics": metrics.serialize(),
                        "cv_max_output_tokens": self.settings.cv_max_output_tokens,
                    },
                )
            self._stage(job_id, token, "VALIDATING", run_id)
            extraction = merge_extractions(outputs)
            if metrics.truncated_responses:
                extraction.extraction_warnings.append(
                    "Output-limit recovery split/retried sections; inspect record boundaries."
                )
            self._save_profile(
                job_id,
                token,
                run_id,
                document_id,
                source_id,
                source,
                extraction,
                config_hash,
                started,
            )
        except Exception as exc:
            if getattr(exc, "metrics", None) is not None:
                metrics.add(exc.metrics)
            self._record_metrics(run_id, metrics)
            message = (
                str(exc)
                if isinstance(exc, ATSError)
                else (
                    "CV analysis failed. Check the document, local model and database, then retry."
                )
            )
            self._finish(job_id, token, run_id, "FAILED", started, error=message[:500])
        return True

    def _record_metrics(self, run_id, metrics):
        with self.db.transaction() as session:
            run = session.get(CVAnalysisRun, run_id)
            if run is not None:
                run.inference_options = {**run.inference_options, "metrics": metrics.serialize()}

    def _text(self, document_id, filename, data):
        checksum = hashlib.sha256(data).hexdigest()
        with self.db.transaction() as session:
            saved = session.scalar(
                select(DocumentTextExtraction).where(
                    DocumentTextExtraction.document_id == document_id,
                    DocumentTextExtraction.checksum == checksum,
                    DocumentTextExtraction.extractor_version == EXTRACTOR_VERSION,
                )
            )
            if saved:
                return ExtractedText.deserialize(saved.source), saved.id
        extracted = self.extractor.extract(filename, data)
        with self.db.transaction() as session:
            row = DocumentTextExtraction(
                document_id=document_id,
                checksum=checksum,
                extractor_version=EXTRACTOR_VERSION,
                source=extracted.serialize(),
            )
            session.add(row)
            session.flush()
            return extracted, row.id

    def _save_profile(
        self,
        job_id,
        token,
        run_id,
        document_id,
        source_id,
        source,
        extraction,
        config_hash,
        started,
    ):
        warnings = list(source.warnings) + list(extraction.extraction_warnings)
        evidence = []
        for section in PROFILE_SECTIONS:
            for index, claim in enumerate(getattr(extraction, section)):
                warnings.extend(f"{section}.{index}: {issue}" for issue in claim_issues(claim))
                if claim.claim_status == "UNCERTAIN":
                    warnings.append(f"{section}.{index}: uncertain source information.")
                for quote in claim.evidence:
                    verified = verify_quote(quote, source, document_id)
                    evidence.append((f"{section}.{index}", verified))
                    if verified.verification_status != "VERIFIED":
                        warnings.append(
                            f"{section}.{index}: {verified.verification_status.lower()} evidence."
                        )
        if not any(getattr(extraction, key) for key in PROFILE_SECTIONS):
            warnings.append("No documented qualifications extracted; inspect the original CV.")
        state = "NEEDS_REVIEW" if warnings else "COMPLETED"
        with self.db.transaction() as session:
            job = session.get(CVAnalysisJob, job_id)
            document = session.get(CandidateDocument, document_id)
            if (
                job.worker_token != token
                or document is None
                or document.application.status != "Uploaded"
            ):
                raise ATSError("CV was removed or analysis ownership changed before saving.")
            version = (
                session.scalar(
                    select(func.max(CandidateProfile.extraction_version)).where(
                        CandidateProfile.document_id == document_id
                    )
                )
                or 0
            )
            profile = CandidateProfile(
                candidate_id=document.application.candidate_id,
                document_id=document_id,
                text_extraction_id=source_id,
                analysis_run_id=run_id,
                extraction_version=version + 1,
                config_hash=config_hash,
                analysis_status=state,
                original_extraction=extraction.model_dump(mode="json"),
                warnings=list(dict.fromkeys(warnings)),
            )
            session.add(profile)
            session.flush()
            for path, verified in evidence:
                values = verified.serialize()
                values["id"] = values.pop("evidence_id")
                session.add(EvidenceReference(profile_id=profile.id, field_path=path, **values))
            self._finish_rows(session, job, run_id, state, started)

    @staticmethod
    def _finish_rows(session, job, run_id, state, started, error=None, reused=False):
        duration = int((time.perf_counter() - started) * 1000)
        job.status, job.completed_at, job.duration_ms = state, now(), duration
        job.error_summary, job.reused_profile = error, reused
        job.worker_token, job.lease_until = None, None
        run = session.get(CVAnalysisRun, run_id)
        run.status, run.completed_at, run.duration_ms, run.error_summary = (
            state,
            now(),
            duration,
            error,
        )

    def _finish(self, job_id, token, run_id, state, started, error=None, reused=False):
        with self.db.transaction() as session:
            job = session.get(CVAnalysisJob, job_id)
            if job and job.worker_token == token:
                self._finish_rows(session, job, run_id, state, started, error, reused)

    def dashboard(self, vacancy_id: int | None = None):
        with self.db.transaction() as session:
            vacancies = list(session.scalars(select(Vacancy).order_by(Vacancy.id)))
            durations = list(
                session.scalars(
                    select(CVAnalysisRun.duration_ms)
                    .where(
                        CVAnalysisRun.status.in_({"COMPLETED", "NEEDS_REVIEW"}),
                        CVAnalysisRun.chunks_completed > 0,
                    )
                    .join(CVAnalysisJob)
                    .join(CVAnalysisBatch)
                    .where(True if vacancy_id is None else CVAnalysisBatch.vacancy_id == vacancy_id)
                )
            )
        per_vacancy = []
        for vacancy in vacancies:
            if vacancy_id is not None and vacancy.id != vacancy_id:
                continue
            rows = self.list_documents(vacancy.id)
            per_vacancy.append(
                {
                    "Vacancy": f"{vacancy.id} · {vacancy.title}",
                    "Total uploaded CVs": len(rows),
                    "Analyzed CVs": sum(r["Profile"] is not None for r in rows),
                    "Pending analysis": sum(
                        r["Status"] in ACTIVE_STATUSES | {"PENDING"} for r in rows
                    ),
                    "Failed analyses": sum(r["Status"] == "FAILED" for r in rows),
                    "Profiles requiring review": sum(
                        r["Profile review"] == "NEEDS_REVIEW" for r in rows
                    ),
                }
            )
        metrics = {
            key: sum(row[key] for row in per_vacancy)
            for key in (
                "Total uploaded CVs",
                "Analyzed CVs",
                "Pending analysis",
                "Failed analyses",
                "Profiles requiring review",
            )
        }
        metrics["Average processing duration (s)"] = (
            round(sum(value for value in durations if value is not None) / len(durations) / 1000, 2)
            if durations
            else 0
        )
        return {"metrics": metrics, "vacancies": per_vacancy}
