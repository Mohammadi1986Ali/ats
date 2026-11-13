"""CV fallback regression tests: evidence-only, incomplete and never fabricated."""

from sqlalchemy import select

from job_hire_ai.ai.literal_cv_fallback import FALLBACK_WARNING, extract_literal_skills
from job_hire_ai.ai.provider import ProviderError
from job_hire_ai.database.models import CandidateDocument, DocumentPipelineStage
from job_hire_ai.services.evidence_service import verify_quote
from job_hire_ai.text_extraction import ExtractedText, TextSegment
from job_hire_ai.retry_profiles import requeue_profiles
from tests.cv_fixtures import mock_provider, pdf_bytes


def test_literal_extraction_preserves_exact_source_and_flags_all_as_uncertain():
    segments = [
        {"reference": "pdf:p1:block1", "text": "Built Java and Spring Boot services."},
        {"reference": "pdf:p2:block1", "text": "No claimed degrees or jobs."},
    ]
    result = extract_literal_skills(segments)
    assert [row.name for row in result.technical_skills] == ["Spring Boot", "Java"]
    assert result.employment_history == result.education == []
    assert result.extraction_warnings == [FALLBACK_WARNING]
    assert all(row.claim_status == "UNCERTAIN" for row in result.technical_skills)
    source = ExtractedText(
        [
            TextSegment("pdf:p1:block1", segments[0]["text"], segments[0]["text"], 1,
                        "paragraph"),
            TextSegment("pdf:p2:block1", segments[1]["text"], segments[1]["text"], 2,
                        "paragraph"),
        ], []
    )
    for row in result.technical_skills:
        assert verify_quote(row.evidence[0], source, 1).verification_status == "VERIFIED"


def test_fallback_never_infers_experience_or_skills_from_empty_document():
    result = extract_literal_skills([
        {"reference": "pdf:p1:block1", "text": "University graduate, contact@example.com"},
    ])
    assert not any(result.technical_skills)
    assert not result.employment_history
    assert not result.education
    assert len(result.extraction_warnings) == 2


def test_failed_local_model_can_save_partial_reviewable_profile(ats, vacancy):
    provider = mock_provider()
    provider.analyze_cv.side_effect = ProviderError(
        "CV extraction could not produce complete valid structured output within bounded retries"
    )
    ats.cv_analysis.provider = provider
    assert ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf_bytes("Java and Kafka experience."))]
    )[0].success
    document_id = ats.cv_analysis.list_documents(vacancy.id)[0]["Document"]
    batch_id = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch_id)
    assert ats.cv_analysis.jobs(batch_id)[0].status == "NEEDS_REVIEW"
    profile = ats.profiles.list(document_id=document_id)[0]
    assert FALLBACK_WARNING in profile.warnings
    assert {row["name"] for row in profile.original_extraction["technical_skills"]} == {
        "Java", "Kafka"
    }
    assert all(
        evidence.verification_status == "VERIFIED" for evidence in profile.evidence
    )
    assert all(
        row["claim_status"] == "UNCERTAIN"
        for row in profile.original_extraction["technical_skills"]
    )


def test_offline_model_error_never_creates_fallback_profile(ats, vacancy):
    provider = mock_provider()
    provider.analyze_cv.side_effect = ProviderError("Ollama disconnected")
    ats.cv_analysis.provider = provider
    assert ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf_bytes("Java and Kafka experience."))]
    )[0].success
    document_id = ats.cv_analysis.list_documents(vacancy.id)[0]["Document"]
    batch_id = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch_id)
    assert ats.cv_analysis.jobs(batch_id)[0].status == "FAILED"
    assert ats.profiles.list(document_id=document_id) == []


def test_operator_can_requeue_exhausted_profile_without_resetting_embedding(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        doc_id = session.scalar(select(CandidateDocument.id))
        stages = {
            stage.stage: stage
            for stage in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == doc_id
                )
            )
        }
        stages["EMBEDDING"].status = "COMPLETED"
        stages["PROFILE_EXTRACTION"].status = "FAILED"
        stages["PROFILE_EXTRACTION"].attempts = 3
    assert requeue_profiles(ats.db, [doc_id])[doc_id] == (
        "Retry limit reached; inspect failure"
    )
    assert requeue_profiles(ats.db, [doc_id], allow_exhausted=True)[doc_id] == "Requeued"
    with ats.db.transaction() as session:
        stages = {
            stage.stage: stage
            for stage in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == doc_id
                )
            )
        }
        assert stages["PROFILE_EXTRACTION"].attempts == 0
        assert stages["PROFILE_EXTRACTION"].status == "PENDING"
        assert stages["EMBEDDING"].status == "COMPLETED"
