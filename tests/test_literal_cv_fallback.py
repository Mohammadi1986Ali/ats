"""CV fallback regression tests: evidence-only, incomplete and never fabricated."""

from sqlalchemy import select

from job_hire_ai.ai.literal_cv_fallback import (
    FALLBACK_WARNING,
    FAST_MODE_WARNING,
    extract_literal_skills,
)
from job_hire_ai.ai.provider import ProviderError
from job_hire_ai.database.models import CandidateDocument, DocumentPipelineStage
from job_hire_ai.retry_profiles import requeue_profiles
from job_hire_ai.services.evidence_service import verify_quote
from job_hire_ai.text_extraction import ExtractedText, TextSegment
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


def test_fast_1b_mode_does_not_invoke_slow_model_at_all(ats, vacancy):
    provider = mock_provider()
    provider.analyze_cv.side_effect = AssertionError("1B model must not run in fast mode")
    provider.check.side_effect = AssertionError("Fast source-only mode must not check Ollama")
    ats.cv_analysis.provider = provider
    ats.cv_analysis.settings = ats.cv_analysis.settings.model_copy(
        update={"model": "llama3.2:1b", "cv_fast_mode_1b": True}
    )
    assert ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf_bytes("Java and Spring Boot experience."))]
    )[0].success
    document_id = ats.cv_analysis.list_documents(vacancy.id)[0]["Document"]
    batch_id = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch_id)
    job = ats.cv_analysis.jobs(batch_id)[0]
    assert job.status == "NEEDS_REVIEW"
    assert job.runs[0].inference_options["extraction_mode"] == "literal-fast-1b"
    assert job.runs[0].inference_options["metrics"]["model_calls"] == 0
    provider.analyze_cv.assert_not_called()
    profile = ats.profiles.list(document_id=document_id)[0]
    assert FAST_MODE_WARNING in profile.warnings
    assert all(
        skill["claim_status"] == "UNCERTAIN"
        for skill in profile.original_extraction["technical_skills"]
    )
    assert profile.original_extraction["employment_history"] == []


def test_manual_1b_extraction_remains_available_when_fast_mode_disabled(ats, vacancy):
    provider = mock_provider()
    ats.cv_analysis.provider = provider
    ats.cv_analysis.settings = ats.cv_analysis.settings.model_copy(
        update={"model": "llama3.2:1b", "cv_fast_mode_1b": False}
    )
    assert ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf_bytes("Java services experience."))]
    )[0].success
    document_id = ats.cv_analysis.list_documents(vacancy.id)[0]["Document"]
    batch_id = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch_id)
    assert ats.cv_analysis.jobs(batch_id)[0].status in {"COMPLETED", "NEEDS_REVIEW"}
    provider.analyze_cv.assert_called()


def test_fast_profile_completes_when_ollama_model_not_installed(ats, vacancy):
    provider = mock_provider()
    provider.check.side_effect = AssertionError("Missing model check must be skipped")
    provider.analyze_cv.side_effect = AssertionError("No model inference allowed")
    ats.cv_analysis.provider = provider
    ats.cv_analysis.settings = ats.cv_analysis.settings.model_copy(
        update={
            "model": "qwen2.5:0.5b",
            "cv_fast_mode_1b": True,
            "cv_literal_fallback_enabled": True,
        }
    )
    assert ats.documents.upload_batch(
        vacancy.id, [("missing-model.pdf", pdf_bytes("Java and Kafka."))]
    )[0].success
    document_id = ats.cv_analysis.list_documents(vacancy.id)[0]["Document"]
    batch_id = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch_id)
    assert ats.cv_analysis.jobs(batch_id)[0].status == "NEEDS_REVIEW"
    profile = ats.profiles.list(document_id=document_id)[0]
    assert FAST_MODE_WARNING in profile.warnings
    assert {item["name"] for item in profile.original_extraction["technical_skills"]} == {
        "Java", "Kafka"
    }
    assert all(
        item["claim_status"] == "UNCERTAIN"
        for item in profile.original_extraction["technical_skills"]
    )
    provider.check.assert_not_called()
    provider.analyze_cv.assert_not_called()


def test_fast_mode_extracts_only_explicit_employment_and_education():
    segments = [
        {"reference": "pdf:p1:block1", "text": "EXPERIENCE ----------------"},
        {"reference": "pdf:p1:block2",
         "text": "blu Bank\nSenior Software Engineer\nDec 2022 – Jan 2026 (Full-time)"},
        {"reference": "pdf:p2:block1",
         "text": "MEBank\nSenior Software Engineer\nJun 2017 – Dec 2022 (Full-time)"},
        {"reference": "pdf:p2:block2", "text": "EDUCATION ----------------"},
        {"reference": "pdf:p2:block3",
         "text": "Master of Science: Data Driven Business\n"
                 "The Hague University of Applied Sciences\n"
                 "Master of Science: Computer Engineering\n"
                 "Bachelor of Science: Applied Mathematics"},
    ]
    # Source-line evidence must remain literal, and unknown dates are unknown.
    segments = [{**s, "text": s["text"].replace("\\\n", "\n")} for s in segments]
    result = extract_literal_skills(segments, warning=FAST_MODE_WARNING)
    assert [r.employer for r in result.employment_history] == ["blu Bank", "MEBank"]
    assert result.employment_history[0].start_date == "Dec 2022"
    assert result.employment_history[0].end_date == "Jan 2026"
    assert result.employment_history[0].claim_status == "UNCERTAIN"
    assert len(result.education) == 3
    assert result.education[0].institution == "The Hague University of Applied Sciences"
    assert result.education[1].institution is None
    assert result.education[0].graduation_date is None
    source = ExtractedText([
        TextSegment(s["reference"], s["text"], s["text"], 1, "paragraph")
        for s in segments
    ], [])
    for row in result.employment_history + result.education:
        for quote in row.evidence:
            assert verify_quote(quote, source, 1).verification_status == "VERIFIED"


def test_fast_mode_does_not_invent_jobs_from_unstructured_prose():
    result = extract_literal_skills([
        {"reference": "pdf:p1:block1",
         "text": "Worked with Java on payments. Graduated from university in 2022."},
        {"reference": "pdf:p1:block2",
         "text": "EDUCATION\nInterested in computer engineering and cloud systems."},
    ])
    assert result.employment_history == []
    assert result.education == []
