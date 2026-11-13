"""Automatic Phase 3.4 orchestration tests; no network or fabricated AI output."""

from dataclasses import dataclass

from sqlalchemy import select

from job_hire_ai.database.models import CandidateDocument, DocumentPipelineStage
from job_hire_ai.services.pipeline_service import PipelineService


@dataclass
class FinishedJob:
    status: str
    error_summary: str | None = None


class FakeAnalyzer:
    def __init__(self, status="NEEDS_REVIEW", error=None):
        self.status = status
        self.error = error
        self.calls = []
        self.batch_id = 0

    def create_batch(self, vacancy_id, document_ids):
        self.calls.append((vacancy_id, list(document_ids)))
        self.batch_id += 1
        return self.batch_id

    def process_batch(self, batch_id):
        assert batch_id == self.batch_id

    def jobs(self, batch_id):
        assert batch_id == self.batch_id
        return [FinishedJob(self.status, self.error)]


def prepared_document(ats, vacancy, pdf):
    result = ats.documents.upload_batch(vacancy.id, [("profile.pdf", pdf)])
    assert result[0].success
    assert ats.pipeline.process_next()  # Existing automatic text extraction.
    with ats.db.transaction() as session:
        doc_id = session.scalar(
            select(CandidateDocument.id).where(CandidateDocument.vacancy_id == vacancy.id)
        )
        embedding = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == doc_id,
                DocumentPipelineStage.stage == "EMBEDDING",
            )
        )
        embedding.status = "COMPLETED"  # Existing successful embedding, without Ollama.
    return doc_id


def get_stage(ats, document_id, name):
    with ats.db.transaction() as session:
        return session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == name,
            )
        )


def test_automatic_profile_started_after_embedding_and_left_review_separate(
    ats, vacancy, pdf,
):
    document_id = prepared_document(ats, vacancy, pdf)
    analyzer = FakeAnalyzer()
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        profile_extraction_enabled=True, profile_analyzer=analyzer,
    )
    assert pipeline.process_profile_next()
    assert analyzer.calls == [(vacancy.id, [document_id])]
    assert get_stage(ats, document_id, "PROFILE_EXTRACTION").status == "COMPLETED"
    assert get_stage(ats, document_id, "EVIDENCE_VALIDATION").status == "WAITING"
    assert not pipeline.process_profile_next()  # Must never reprocess a completed stage.


def test_automatic_profile_failure_is_visible_and_not_retried_forever(ats, vacancy, pdf):
    document_id = prepared_document(ats, vacancy, pdf)
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        profile_extraction_enabled=True,
        profile_analyzer=FakeAnalyzer("FAILED", "Model was unavailable."),
    )
    assert pipeline.process_profile_next()
    stage = get_stage(ats, document_id, "PROFILE_EXTRACTION")
    assert stage.status == "FAILED"
    assert stage.error_summary == "Model was unavailable."
    assert not pipeline.process_profile_next()


def test_interrupted_profile_is_requeued_without_rewriting_embeddings(ats, vacancy, pdf):
    document_id = prepared_document(ats, vacancy, pdf)
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
            )
        )
        stage.status = "RUNNING"
        stage.attempts = 1
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        profile_extraction_enabled=True, profile_analyzer=FakeAnalyzer(),
    )
    pipeline.recover_interrupted()
    assert get_stage(ats, document_id, "PROFILE_EXTRACTION").status == "PENDING"
    assert get_stage(ats, document_id, "EMBEDDING").status == "COMPLETED"


def test_profile_waits_for_embedding_and_respects_removed_applications(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("early.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.vacancy_id == vacancy.id)
        )
        doc_id, application_id = doc.id, doc.application_id
    analyzer = FakeAnalyzer()
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        profile_extraction_enabled=True, profile_analyzer=analyzer,
    )
    assert not pipeline.process_profile_next()  # Text/embedding have not completed yet.
    assert analyzer.calls == []
    with ats.db.transaction() as session:
        session.get(JobApplication, application_id).status = "Removed"
        for stage in session.scalars(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == doc_id,
                DocumentPipelineStage.stage.in_(["TEXT_EXTRACTION", "EMBEDDING"]),
            )
        ):
            stage.status = "COMPLETED"
    assert not pipeline.process_profile_next()
    assert analyzer.calls == []
