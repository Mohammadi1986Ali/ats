"""Requeue only failed CV profile stages; never modify uploaded CVs or embeddings."""

from sqlalchemy import select

from job_hire_ai.database.models import CandidateDocument, DocumentPipelineStage
from job_hire_ai.retry_profiles import requeue_profiles


def test_retry_failed_profiles_preserves_embedded_documents(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("recovery.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        document_id = session.scalar(select(CandidateDocument.id))
        stages = {
            stage.stage: stage for stage in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == document_id
                )
            )
        }
        stages["EMBEDDING"].status = "COMPLETED"
        profile = stages["PROFILE_EXTRACTION"]
        profile.status = "FAILED"
        profile.error_summary = "Truncated output"
        profile.attempts = 1
    assert requeue_profiles(ats.db, [document_id, document_id]) == {
        document_id: "Requeued"
    }
    with ats.db.transaction() as session:
        stages = {
            stage.stage: stage for stage in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == document_id
                )
            )
        }
        assert stages["PROFILE_EXTRACTION"].status == "PENDING"
        assert stages["PROFILE_EXTRACTION"].attempts == 1
        assert stages["PROFILE_EXTRACTION"].error_summary is None
        assert stages["EMBEDDING"].status == "COMPLETED"
        assert stages["TEXT_EXTRACTION"].status == "COMPLETED"
    assert requeue_profiles(ats.db, [document_id]) == {
        document_id: "Not failed (PENDING); unchanged"
    }


def test_retry_refuses_exhausted_profile_attempts(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("limits.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        document_id = session.scalar(select(CandidateDocument.id))
        stages = {
            stage.stage: stage for stage in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == document_id
                )
            )
        }
        stages["EMBEDDING"].status = "COMPLETED"
        stages["PROFILE_EXTRACTION"].status = "FAILED"
        stages["PROFILE_EXTRACTION"].attempts = 3
    assert requeue_profiles(ats.db, [document_id])[document_id] == (
        "Retry limit reached; inspect failure"
    )
