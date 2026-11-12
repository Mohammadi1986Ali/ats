import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from job_hire_ai.ai.cv_schemas import CVExtraction, ReviewInput
from job_hire_ai.ai.provider import AnalysisOutput, ProviderError
from job_hire_ai.database.models import (
    CandidateProfile,
    CVAnalysisJob,
    CVAnalysisRun,
    DocumentTextExtraction,
    EvidenceReference,
    ProfileReviewEvent,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.runtime import Runtime
from job_hire_ai.settings import Settings
from job_hire_ai.workers.local_lock import worker_lock
from tests.cv_fixtures import docx_bytes, empty_profile, mock_provider, pdf_bytes


@pytest.fixture
def cv(ats, vacancy):
    provider = mock_provider()
    ats.cv_analysis.provider = provider
    results = ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf_bytes("Java services experience."))]
    )
    assert results[0].success
    document_id = ats.applications.list()[0].documents[0].id
    return document_id, provider


def process(ats, vacancy, document_ids, force=False):
    batch_id = ats.cv_analysis.create_batch(vacancy.id, document_ids, force)
    ats.cv_analysis.process_batch(batch_id)
    return batch_id


def test_profile_evidence_persistence_and_versions(ats, vacancy, cv):
    document_id, provider = cv
    batch = process(ats, vacancy, [document_id])
    assert ats.cv_analysis.summary(batch)["Completed"] == 1
    profile = ats.profiles.list(vacancy.id)[0]
    assert profile.original_extraction["technical_skills"][0]["name"] == "Java"
    assert profile.original_extraction["education"] == []
    assert profile.evidence[0].verification_status == "VERIFIED"
    assert profile.evidence[0].document_id == document_id
    assert profile.extraction_version == 1
    second_batch = process(ats, vacancy, [document_id])
    assert ats.cv_analysis.jobs(second_batch)[0].reused_profile
    assert provider.analyze_cv.call_count == 1
    assert len(ats.profiles.list()) == 1
    process(ats, vacancy, [document_id], force=True)
    assert [p.extraction_version for p in ats.profiles.list()] == [2, 1]
    assert provider.analyze_cv.call_count == 2
    restarted = Runtime(Settings(data_dir=ats.storage.directory.parent))
    try:
        assert len(restarted.profiles.list()) == 2
        assert restarted.profiles.get(profile.id).original_extraction == profile.original_extraction
    finally:
        restarted.db.engine.dispose()


def test_ten_independent_synthetic_cvs_and_resume_no_repeat(ats, vacancy):
    ats.cv_analysis.provider = mock_provider()
    ats.documents.upload_batch(
        vacancy.id, [(f"cv{i}.pdf", pdf_bytes(f"Java CV {i}")) for i in range(10)]
    )
    ids = [row["Document"] for row in ats.cv_analysis.list_documents(vacancy.id)]
    batch = process(ats, vacancy, ids)
    assert ats.cv_analysis.summary(batch)["Completed"] == 10
    assert len(ats.profiles.list()) == 10
    ats.cv_analysis.process_batch(batch)
    assert ats.cv_analysis.provider.analyze_cv.call_count == 10


def test_failed_document_does_not_discard_success_and_retries(ats, vacancy):
    provider = mock_provider()
    analyze = provider.analyze_cv.side_effect
    calls = 0

    def fail_once(segments):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("Synthetic model timeout")
        return analyze(segments)

    provider.analyze_cv.side_effect = fail_once
    ats.cv_analysis.provider = provider
    ats.documents.upload_batch(
        vacancy.id,
        [
            ("first.pdf", pdf_bytes("Java one")),
            ("second.pdf", pdf_bytes("Java two")),
        ],
    )
    ids = [row["Document"] for row in ats.cv_analysis.list_documents(vacancy.id)]
    batch = process(ats, vacancy, ids)
    assert ats.cv_analysis.summary(batch)["Failed"] == 1
    assert ats.cv_analysis.summary(batch)["Completed"] == 1
    ats.cv_analysis.retry_failed(batch)
    ats.cv_analysis.process_batch(batch)
    assert ats.cv_analysis.summary(batch)["Completed"] == 2
    assert len(ats.profiles.list()) == 2


def test_unavailable_model_failed_no_fake_profile(ats, vacancy, cv):
    document_id, provider = cv
    provider.check.return_value = provider.check.return_value.__class__(
        False, False, "llama3.2:1b", "Ollama disconnected"
    )
    batch = process(ats, vacancy, [document_id])
    assert ats.cv_analysis.summary(batch)["Failed"] == 1
    assert ats.profiles.list() == []
    provider.analyze_cv.assert_not_called()


def test_unsupported_claim_evidence_flagged(ats, vacancy, cv):
    document_id, provider = cv
    payload = empty_profile()
    payload["technical_skills"] = [
        {
            "name": "AWS",
            "original_wording": "AWS",
            "category": None,
            "evidence": [
                {"source_excerpt": "Certified in AWS", "source_reference": "pdf:p1:block1"}
            ],
            "claim_status": "SUPPORTED",
            "uncertainty": "",
        }
    ]
    provider.analyze_cv.side_effect = None
    provider.analyze_cv.return_value = AnalysisOutput(
        CVExtraction.model_validate_json(json.dumps(payload)), "llama3.2:1b", "cv-test-digest", 1
    )
    batch = process(ats, vacancy, [document_id])
    assert ats.cv_analysis.summary(batch)["Needs review"] == 1
    assert ats.profiles.list()[0].evidence[0].verification_status == "UNVERIFIED"


def test_separate_hr_corrections_and_validation(ats, vacancy, cv):
    process(ats, vacancy, [cv[0]])
    profile = ats.profiles.list()[0]
    correction = ReviewInput(
        field_path="technical_skills.0.name",
        corrected_value="Java (HR clarified)",
        reviewer="hr-test",
        note="Confirmed terminology with source CV.",
    )
    ats.profiles.correct(profile.id, correction, 0)
    corrected = ats.profiles.get(profile.id)
    assert corrected.original_extraction == profile.original_extraction
    assert corrected.evidence[0].source_excerpt == profile.evidence[0].source_excerpt
    assert ats.profiles.effective(corrected)["technical_skills"][0]["name"] == "Java (HR clarified)"
    assert corrected.reviews[0].original_value == "Java"
    assert ats.profiles.status(corrected) == "NEEDS_REVIEW"
    with pytest.raises(ATSError):
        ats.profiles.validate(profile.id, "hr-test", "Reviewed", False, 1)
    ats.profiles.validate(profile.id, "hr-test", "Reviewed against source", True, 1)
    assert ats.profiles.status(ats.profiles.get(profile.id)) == "COMPLETED"


def test_bad_correction_and_stale_review_rejected(ats, vacancy, cv):
    process(ats, vacancy, [cv[0]])
    profile = ats.profiles.list()[0]
    with pytest.raises(ATSError):
        ats.profiles.correct(
            profile.id,
            ReviewInput(field_path="technical_skills.0.evidence", corrected_value=[], note="bad"),
            0,
        )
    with pytest.raises(ATSError):
        ats.profiles.correct(
            profile.id,
            ReviewInput(
                field_path="technical_skills.99.name", corrected_value="Unknown", note="bad"
            ),
            0,
        )


def test_deletion_cascades_all_extracted_personal_data(ats, vacancy, cv):
    process(ats, vacancy, [cv[0]])
    profile = ats.profiles.list()[0]
    ats.profiles.correct(
        profile.id,
        ReviewInput(
            field_path="technical_skills.0.name", corrected_value="Java corrected", note="Review"
        ),
        0,
    )
    ats.applications.remove(ats.applications.list()[0].id)
    assert ats.profiles.list() == []
    with ats.db.transaction() as session:
        for model in (
            CandidateProfile,
            DocumentTextExtraction,
            EvidenceReference,
            ProfileReviewEvent,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(CVAnalysisRun)).document_id is None


def test_worker_lock_prevents_concurrent_inference(ats, vacancy, cv):
    batch = ats.cv_analysis.create_batch(vacancy.id, [cv[0]])
    with (
        worker_lock(ats.cv_analysis.lock_path),
        pytest.raises(ATSError, match="already processing"),
    ):
        ats.cv_analysis.process_batch(batch)


def test_resume_interrupted_attempt_keeps_completed_profiles(ats, vacancy, cv):
    batch = ats.cv_analysis.create_batch(vacancy.id, [cv[0]])
    with ats.db.transaction() as session:
        job = session.scalar(select(CVAnalysisJob))
        job.status, job.worker_token = "ANALYZING", "interrupted"
    ats.cv_analysis.process_batch(batch)
    assert len(ats.profiles.list()) == 1
    assert ats.cv_analysis.summary(batch)["Completed"] == 1


def test_docx_omitted_content_requires_review(ats, vacancy):
    ats.cv_analysis.provider = mock_provider()
    ats.documents.upload_batch(vacancy.id, [("cv.docx", docx_bytes())])
    doc_id = ats.cv_analysis.list_documents(vacancy.id)[0]["Document"]
    batch = process(ats, vacancy, [doc_id])
    assert ats.cv_analysis.summary(batch)["Needs review"] == 1


def test_dashboard_persisted_analysis_metrics(ats, vacancy, cv):
    assert ats.cv_analysis.dashboard()["metrics"]["Pending analysis"] == 1
    process(ats, vacancy, [cv[0]])
    metrics = ats.cv_analysis.dashboard()["metrics"]
    assert metrics["Total uploaded CVs"] == metrics["Analyzed CVs"] == 1
    assert metrics["Pending analysis"] == metrics["Failed analyses"] == 0
    assert metrics["Average processing duration (s)"] >= 0


def test_immutable_source_profile_and_review_rows(ats, vacancy, cv):
    process(ats, vacancy, [cv[0]])
    profile = ats.profiles.list()[0]
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        session.get(CandidateProfile, profile.id).original_extraction = empty_profile()
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        session.get(EvidenceReference, profile.evidence[0].id).source_excerpt = "Rewritten"


def test_new_model_digest_invalidates_cache(ats, vacancy, cv):
    document_id, provider = cv
    process(ats, vacancy, [document_id])
    status = provider.check.return_value
    provider.check.return_value = status.__class__(
        True, True, status.model, "Updated", "new-digest"
    )
    original = provider.analyze_cv.side_effect

    def updated(segments):
        result = original(segments)
        return AnalysisOutput(result.extraction, result.model, "new-digest", 1)

    provider.analyze_cv.side_effect = updated
    process(ats, vacancy, [document_id])
    assert len(ats.profiles.list()) == 2
    assert provider.analyze_cv.call_count == 2


def test_long_cv_chunk_count_and_every_chunk_processed(ats, vacancy):
    from job_hire_ai.text_extraction import ExtractedText, TextSegment

    ats.cv_analysis.provider = mock_provider()
    ats.documents.upload_batch(vacancy.id, [("long.pdf", pdf_bytes("Java"))])
    document_id = ats.applications.list()[0].documents[0].id
    # Synthetic normalized long-CV source, using the same persistent source cache as the worker.
    segments = [
        TextSegment(
            f"p{i}", "Java experience. " * 80, "Java experience. " * 80, i, "paragraph", i * 1400
        )
        for i in range(1, 9)
    ]
    with ats.db.transaction() as session:
        from job_hire_ai.database.models import CandidateDocument
        from job_hire_ai.text_extraction import EXTRACTOR_VERSION

        document = session.get(CandidateDocument, document_id)
        session.add(
            DocumentTextExtraction(
                document_id=document_id,
                checksum=document.checksum,
                extractor_version=EXTRACTOR_VERSION,
                source=ExtractedText(segments, []).serialize(),
            )
        )
    batch = process(ats, vacancy, [document_id])
    run = ats.cv_analysis.jobs(batch)[0].runs[0]
    assert run.chunk_count > 1 and run.chunks_completed == run.chunk_count
    assert ats.cv_analysis.provider.analyze_cv.call_count == run.chunk_count
    assert len(ats.profiles.list()[0].original_extraction["technical_skills"]) == 1
