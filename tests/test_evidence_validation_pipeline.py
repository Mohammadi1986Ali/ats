"""Phase 3.5: audit persisted evidence without LLM calls or automatic HR approval."""

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, EvidenceQuote
from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    CVAnalysisBatch,
    CVAnalysisJob,
    CVAnalysisRun,
    DocumentPipelineStage,
    DocumentTextExtraction,
    EvidenceReference,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.evidence_service import verify_quote
from job_hire_ai.services.evidence_validation_service import EvidenceValidationService
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.text_extraction import ExtractedText


def seed_profile(ats, vacancy, pdf, *, quote_text, save_quote=True):
    assert ats.documents.upload_batch(vacancy.id, [("audit.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        document = session.scalar(
            select(CandidateDocument).where(CandidateDocument.vacancy_id == vacancy.id)
        )
        extraction = session.scalar(
            select(DocumentTextExtraction).where(
                DocumentTextExtraction.document_id == document.id
            )
        )
        source = ExtractedText.deserialize(extraction.source)
        quote = EvidenceQuote(
            source_excerpt=quote_text,
            source_reference=source.segments[0].reference,
        )
        record = {
            "name": "Java", "original_wording": "Java", "category": "technology",
            "evidence": [quote.model_dump()],
            "claim_status": "SUPPORTED", "uncertainty": "",
        }
        payload = {section: [] for section in PROFILE_SECTIONS}
        payload["technical_skills"] = [record]
        payload["extraction_warnings"] = []
        batch = CVAnalysisBatch(vacancy_id=vacancy.id)
        session.add(batch)
        session.flush()
        analysis_job = CVAnalysisJob(
            batch_id=batch.id, document_id=document.id, status="COMPLETED"
        )
        session.add(analysis_job)
        session.flush()
        run = CVAnalysisRun(
            job_id=analysis_job.id, document_id=document.id,
            document_checksum=document.checksum, model_name="offline-test",
            prompt_version="offline-test", schema_version="candidate-profile-v1",
            config_hash="offline-test", inference_options={}, status="COMPLETED",
        )
        session.add(run)
        session.flush()
        profile = CandidateProfile(
            candidate_id=document.application.candidate_id, document_id=document.id,
            text_extraction_id=extraction.id, analysis_run_id=run.id,
            extraction_version=1, config_hash="offline-test",
            analysis_status="COMPLETED", original_extraction=payload, warnings=[],
        )
        session.add(profile)
        session.flush()
        if save_quote:
            checked = verify_quote(quote, source, document.id).serialize()
            checked["id"] = checked.pop("evidence_id")
            session.add(
                EvidenceReference(
                    profile_id=profile.id,
                    field_path="technical_skills.0",
                    **checked,
                )
            )
        return document.id, profile.id


@dataclass
class AuditResult:
    issues: tuple[str, ...]


class FakeValidator:
    def __init__(self, issues=()):
        self.issues = issues
        self.calls = []

    def validate_document(self, document_id):
        self.calls.append(document_id)
        return AuditResult(tuple(self.issues))


def set_profile_stage(ats, document_id, status="COMPLETED"):
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
            )
        )
        stage.status = status


def stage_status(ats, document_id, stage_name):
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == stage_name,
            )
        )
        return stage.status, stage.error_summary


def test_source_verified_audit_does_not_create_human_approval(ats, vacancy, pdf):
    document_id, profile_id = seed_profile(
        ats, vacancy, pdf, quote_text="Java and SQL experience."
    )
    result = EvidenceValidationService(ats.db).validate_document(document_id)
    assert result.profile_id == profile_id
    assert (result.claims_checked, result.quotes_checked) == (1, 1)
    assert not result.needs_review
    with ats.db.transaction() as session:
        profile = session.get(CandidateProfile, profile_id)
        assert profile.analysis_status == "COMPLETED"
        assert profile.warnings == []
        assert profile.reviews == []


def test_unverifiable_quote_requires_review_not_approval(ats, vacancy, pdf):
    document_id, profile_id = seed_profile(
        ats, vacancy, pdf, quote_text="Java and Kubernetes experience."
    )
    result = EvidenceValidationService(ats.db).validate_document(document_id)
    assert result.needs_review
    assert any("unverified" in issue for issue in result.issues)
    with ats.db.transaction() as session:
        profile = session.get(CandidateProfile, profile_id)
        assert profile.analysis_status == "NEEDS_REVIEW"
        assert any("Evidence audit:" in warning for warning in profile.warnings)
        assert profile.reviews == []
    # Repeated audit refreshes its own warnings instead of accumulating duplicates.
    EvidenceValidationService(ats.db).validate_document(document_id)
    with ats.db.transaction() as session:
        profile = session.get(CandidateProfile, profile_id)
        assert len(profile.warnings) == len(set(profile.warnings))


def test_incomplete_persisted_evidence_fails_closed(ats, vacancy, pdf):
    document_id, _ = seed_profile(
        ats, vacancy, pdf, quote_text="Java and SQL experience.", save_quote=False
    )
    with pytest.raises(ATSError, match="incomplete"):
        EvidenceValidationService(ats.db).validate_document(document_id)


def test_audit_pipeline_requires_completed_profile(ats, vacancy, pdf):
    doc_id, _ = seed_profile(
        ats, vacancy, pdf, quote_text="Java and SQL experience."
    )
    fake = FakeValidator()
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        evidence_validation_enabled=True, evidence_validator=fake,
    )
    assert not pipeline.process_evidence_next()
    assert fake.calls == []

    set_profile_stage(ats, doc_id)
    assert pipeline.process_evidence_next()
    assert fake.calls == [doc_id]
    assert stage_status(ats, doc_id, "EVIDENCE_VALIDATION") == ("COMPLETED", None)
    assert not pipeline.process_evidence_next()
    assert stage_status(ats, doc_id, "MATCHING")[0] == "WAITING"


def test_review_flags_do_not_fail_audit_stage(ats, vacancy, pdf):
    doc_id, _ = seed_profile(
        ats, vacancy, pdf, quote_text="Java and SQL experience."
    )
    set_profile_stage(ats, doc_id)
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        evidence_validation_enabled=True,
        evidence_validator=FakeValidator(("claim has an ambiguous source",)),
    )
    assert pipeline.process_evidence_next()
    status, note = stage_status(ats, doc_id, "EVIDENCE_VALIDATION")
    assert status == "COMPLETED"
    assert "human review required" in note


def test_crashed_evidence_audit_is_requeued(ats, vacancy, pdf):
    doc_id, _ = seed_profile(
        ats, vacancy, pdf, quote_text="Java and SQL experience."
    )
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == doc_id,
                DocumentPipelineStage.stage == "EVIDENCE_VALIDATION",
            )
        )
        stage.status, stage.attempts = "RUNNING", 1
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        evidence_validation_enabled=True,
    )
    pipeline.recover_interrupted()
    assert stage_status(ats, doc_id, "EVIDENCE_VALIDATION")[0] == "PENDING"


def test_clean_evidence_does_not_clear_existing_extraction_review(ats, vacancy, pdf):
    document_id, profile_id = seed_profile(
        ats, vacancy, pdf, quote_text="Java and SQL experience."
    )
    with ats.db.transaction() as session:
        profile = session.get(CandidateProfile, profile_id)
        profile.warnings = ["Source extraction requires manual inspection."]
        profile.analysis_status = "NEEDS_REVIEW"
    audit = EvidenceValidationService(ats.db).validate_document(document_id)
    assert audit.issues == ()
    assert audit.needs_review
    with ats.db.transaction() as session:
        profile = session.get(CandidateProfile, profile_id)
        assert profile.warnings == ["Source extraction requires manual inspection."]
        assert profile.analysis_status == "NEEDS_REVIEW"
