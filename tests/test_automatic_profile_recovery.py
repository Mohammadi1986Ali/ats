"""Upgraded workers recover pre-fix JSON failures once, without CLI intervention."""

from sqlalchemy import select

from job_hire_ai.database.models import CandidateDocument, DocumentPipelineStage
from job_hire_ai.workers.automatic_profile_recovery import (
    RECOVERY_VERSION,
    recover_legacy_profiles_once,
)


def prepare(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("previous.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        document_id = session.scalar(select(CandidateDocument.id))
        stages = {
            row.stage: row for row in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == document_id
                )
            )
        }
        stages["EMBEDDING"].status = "COMPLETED"
        return document_id


def stage(ats, document_id, name):
    with ats.db.transaction() as session:
        return session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == name,
            )
        )


def test_automatic_legacy_recovery_only_once(ats, vacancy, pdf, tmp_path):
    doc_id = prepare(ats, vacancy, pdf)
    with ats.db.transaction() as session:
        profile = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == doc_id,
                DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
            )
        )
        profile.status = "FAILED"
        profile.attempts = 3
        profile.error_summary = (
            "CV extraction could not produce complete valid structured output "
            "within bounded retries for source splitting."
        )
    directory = tmp_path / "recover"
    assert recover_legacy_profiles_once(
        ats.db, directory, fallback_enabled=True
    ) == {doc_id: "Requeued"}
    assert stage(ats, doc_id, "PROFILE_EXTRACTION").status == "PENDING"
    assert stage(ats, doc_id, "EMBEDDING").status == "COMPLETED"
    assert (directory / f".{RECOVERY_VERSION}.done").exists()
    with ats.db.transaction() as session:
        profile = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == doc_id,
                DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
            )
        )
        profile.status = "FAILED"
        profile.error_summary = "Same bad JSON again"
    assert recover_legacy_profiles_once(
        ats.db, directory, fallback_enabled=True
    ) == {}
    assert stage(ats, doc_id, "PROFILE_EXTRACTION").status == "FAILED"


def test_recovery_refuses_other_failures_and_respects_disabled_mode(
    ats, vacancy, pdf, tmp_path
):
    doc_id = prepare(ats, vacancy, pdf)
    with ats.db.transaction() as session:
        profile = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == doc_id,
                DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
            )
        )
        profile.status = "FAILED"
        profile.error_summary = "Ollama timed out"
    directory = tmp_path / "disabled"
    assert not recover_legacy_profiles_once(
        ats.db, directory, fallback_enabled=False
    )
    assert not (directory / f".{RECOVERY_VERSION}.done").exists()
    assert not recover_legacy_profiles_once(
        ats.db, directory, fallback_enabled=True
    )
    assert stage(ats, doc_id, "PROFILE_EXTRACTION").status == "FAILED"
