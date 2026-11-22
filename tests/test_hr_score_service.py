"""Phase 4.3 HR evidence score: deterministic snapshots and provenance."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from job_hire_ai.database.models import EvidenceReference, JobCriterion, LlamaScoreResult
from job_hire_ai.errors import ATSError
from job_hire_ai.services.hr_score_service import reviewed_breakdown
from tests.test_job_matching import approve_criteria, make_data


def _setup(ats, vacancy, pdf):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        criterion_id = session.scalar(select(JobCriterion.id).where(
            JobCriterion.configuration_id == config_id
        ))
        evidence_id = session.scalar(select(EvidenceReference.id).where(
            EvidenceReference.profile_id == profile_id
        ))
        session.add(LlamaScoreResult(
            document_id=doc_id, profile_id=profile_id,
            configuration_id=config_id, model_name="llama3.2:1b",
            status="COMPLETED", score=100, review_weight=0,
            breakdown=[{
                "criterion_id": criterion_id, "criterion": "Java",
                "weight": 100, "status": "EVIDENCED",
                "reason": "Verified CV contains Java.",
                "evidence": [{
                    "source_reference": "pdf:p1:block1",
                    "page_number": 1,
                    "source_excerpt": "Java and SQL experience.",
                }],
            }], error_summary=None,
        ))
    return doc_id, profile_id, evidence_id


def test_hr_rejection_reduces_score_without_changing_ai(ats, vacancy, pdf):
    doc_id, profile_id, evidence_id = _setup(ats, vacancy, pdf)
    ats.evidence_reviews.record(
        profile_id, evidence_id, "REJECT", "HR", "Source evidence not sufficient."
    )
    snapshot_id = ats.hr_scores.calculate(doc_id, "HR", "After checking original CV.")
    assert snapshot_id
    latest = ats.hr_scores.history(doc_id)[0]
    assert latest.original_value["score"] == 100
    assert latest.corrected_value["score"] == 0
    assert latest.corrected_value["review_weight"] == 100
    with ats.db.transaction() as session:
        model = session.scalar(select(LlamaScoreResult).where(
            LlamaScoreResult.document_id == doc_id
        ))
        assert model.score == 100
    assert ats.documents.download(doc_id)[1] == pdf


def test_hr_approval_keeps_supported_score_and_idempotent_snapshot(ats, vacancy, pdf):
    doc_id, profile_id, evidence_id = _setup(ats, vacancy, pdf)
    ats.evidence_reviews.record(profile_id, evidence_id, "APPROVE", "HR", "Verified.")
    first = ats.hr_scores.calculate(doc_id, "HR", "Reviewed source quote.")
    second = ats.hr_scores.calculate(doc_id, "HR", "Same review, do not duplicate.")
    assert first == second
    history = ats.hr_scores.history(doc_id)
    assert len(history) == 1
    assert history[0].corrected_value["score"] == 100
    assert ats.profiles.effective(ats.profiles.get(profile_id))


def test_new_hr_review_creates_new_score_snapshot(ats, vacancy, pdf):
    doc_id, profile_id, evidence_id = _setup(ats, vacancy, pdf)
    first_review = ats.evidence_reviews.record(
        profile_id, evidence_id, "APPROVE", "HR", "Verified."
    )
    first_score = ats.hr_scores.calculate(doc_id, "HR", "Initial review.")
    ats.evidence_reviews.record(
        profile_id, evidence_id, "REJECT", "HR", "Correction after review.",
        expected_last_event_id=first_review,
    )
    second_score = ats.hr_scores.calculate(doc_id, "HR", "Corrected assessment.")
    assert second_score != first_score
    history = ats.hr_scores.history(doc_id)
    assert [row.corrected_value["score"] for row in history] == [0, 100]


def test_scoring_requires_a_completed_model_and_at_least_one_review(ats, vacancy, pdf):
    doc_id, profile_id, evidence_id = _setup(ats, vacancy, pdf)
    with pytest.raises(ATSError, match="Review at least one"):
        ats.hr_scores.calculate(doc_id, "HR", "No source review yet.")
    with ats.db.transaction() as session:
        model = session.scalar(select(LlamaScoreResult).where(
            LlamaScoreResult.document_id == doc_id
        ))
        model.status = "FAILED"
    ats.evidence_reviews.record(profile_id, evidence_id, "APPROVE", "HR", "Reviewed.")
    with pytest.raises(ATSError, match="Completed, source-backed"):
        ats.hr_scores.calculate(doc_id, "HR", "AI failed.")


def test_unreviewed_criterion_cannot_be_automatically_promoted():
    criterion = SimpleNamespace(
        id=11, name="Java", weight=100,
        description="Explicit technology mentioned in the job description.",
    )
    evidence = [SimpleNamespace(
        id="abc", source_excerpt="Java", source_reference="pdf:p1:block1",
        page_number=1,
    )]
    original = [{
        "criterion_id": 11, "status": "REVIEW_REQUIRED", "evidence": [],
    }]
    unreviewed = reviewed_breakdown([criterion], original, evidence, {})
    assert unreviewed["score"] == 0
    approved = SimpleNamespace(corrected_value={"decision": "APPROVE"})
    reviewed = reviewed_breakdown([criterion], original, evidence, {"abc": approved})
    assert reviewed["score"] == 100


def test_complex_requirement_cannot_be_promoted_by_skill_name_alone():
    criterion = SimpleNamespace(
        id=12, name="Java", weight=100,
        description="At least 5 years of professional Java development.",
    )
    evidence = [SimpleNamespace(
        id="abc", source_excerpt="Java", source_reference="pdf:p1:block1",
        page_number=1,
    )]
    original = [{
        "criterion_id": 12, "status": "REVIEW_REQUIRED", "evidence": [],
    }]
    approved = SimpleNamespace(corrected_value={"decision": "APPROVE"})
    score = reviewed_breakdown([criterion], original, evidence, {"abc": approved})
    assert score["score"] == 0
    assert score["review_weight"] == 100


def test_missing_or_invalid_reviewer_is_not_persisted(ats, vacancy, pdf):
    doc_id, profile_id, evidence_id = _setup(ats, vacancy, pdf)
    ats.evidence_reviews.record(profile_id, evidence_id, "APPROVE", "HR", "Checked.")
    with pytest.raises(ATSError, match="reviewer and a reason"):
        ats.hr_scores.calculate(doc_id, "", "Reviewed.")
    assert ats.hr_scores.history(doc_id) == []



def test_negative_skill_mention_does_not_become_positive_coverage():
    criterion = SimpleNamespace(
        id=11, name="Java", weight=100,
        description="Explicit technology mentioned in the job description.",
    )
    evidence = [SimpleNamespace(
        id="e1", source_excerpt="No Java", source_reference="pdf:p1:block1",
        page_number=1,
    )]
    original = [{"criterion_id": 11, "status": "REVIEW_REQUIRED", "evidence": []}]
    approved = SimpleNamespace(corrected_value={"decision": "APPROVE"})
    result = reviewed_breakdown([criterion], original, evidence, {"e1": approved})
    assert result["score"] == 0
    assert result["review_weight"] == 100


def test_score_snapshot_becomes_stale_after_new_hr_evidence_review(ats, vacancy, pdf):
    doc_id, profile_id, evidence_id = _setup(ats, vacancy, pdf)
    first_review = ats.evidence_reviews.record(
        profile_id, evidence_id, "APPROVE", "HR", "Verified."
    )
    ats.hr_scores.calculate(doc_id, "HR", "First scoring.")
    snapshot = ats.hr_scores.history(doc_id)[0]
    assert ats.hr_scores.is_current(doc_id, snapshot)
    ats.evidence_reviews.record(
        profile_id, evidence_id, "REJECT", "HR",
        "New assessment.", expected_last_event_id=first_review,
    )
    assert not ats.hr_scores.is_current(doc_id, snapshot)
    ats.hr_scores.calculate(doc_id, "HR", "Recalculated.")
    assert ats.hr_scores.is_current(doc_id, ats.hr_scores.history(doc_id)[0])


def test_jd_specific_review_rejects_docker_style_quote_without_profile_field(ats, vacancy, pdf):
    doc_id, profile_id, _ = _setup(ats, vacancy, pdf)
    score = ats.llama_scoring.current(doc_id)
    criterion = score.breakdown[0]
    quote = criterion["evidence"][0]

    # The JD-review source is the actual original CV quote, regardless of what
    # unrelated extracted qualification field the 1B model attached it to.
    event_id = ats.jd_reviews.record(
        doc_id, criterion["criterion_id"], quote, "REJECT",
        "HR", "Quote does not establish the job requirement.",
    )
    assert event_id
    assert ats.jd_reviews.history(doc_id, criterion["criterion_id"], quote)[0].id == event_id
    ats.hr_scores.calculate(doc_id, "HR", "Apply JD-specific rejection.")
    snapshot = ats.hr_scores.history(doc_id)[0]
    assert snapshot.corrected_value["score"] == 0
    assert snapshot.corrected_value["review_weight"] == 100
    assert ats.hr_scores.is_current(doc_id, snapshot)
    assert ats.llama_scoring.current(doc_id).score == 100
    assert ats.profiles.effective(ats.profiles.get(profile_id))


def test_jd_specific_approval_after_rejection_recalculates_without_mutating_cv(
    ats, vacancy, pdf
):
    doc_id, profile_id, _ = _setup(ats, vacancy, pdf)
    result = ats.llama_scoring.current(doc_id).breakdown[0]
    criterion_id, quote = result["criterion_id"], result["evidence"][0]
    original = ats.documents.download(doc_id)[1]
    rejected = ats.jd_reviews.record(
        doc_id, criterion_id, quote, "REJECT", "HR", "Needs context."
    )
    ats.hr_scores.calculate(doc_id, "HR", "First calculation.")
    ats.jd_reviews.record(
        doc_id, criterion_id, quote, "APPROVE", "HR", "Confirmed in source.",
        expected_last_event_id=rejected,
    )
    assert not ats.hr_scores.is_current(doc_id, ats.hr_scores.history(doc_id)[0])
    ats.hr_scores.calculate(doc_id, "HR", "Second calculation.")
    assert [x.corrected_value["score"] for x in ats.hr_scores.history(doc_id)] == [100, 0]
    assert ats.documents.download(doc_id)[1] == original
    assert ats.profiles.status(ats.profiles.get(profile_id)) in {
        "COMPLETED", "NEEDS_REVIEW"
    }


def test_jd_review_rejects_unlisted_or_fabricated_quote(ats, vacancy, pdf):
    doc_id, _, _ = _setup(ats, vacancy, pdf)
    finding = ats.llama_scoring.current(doc_id).breakdown[0]
    criterion_id, quote = finding["criterion_id"], finding["evidence"][0]
    with pytest.raises(ATSError, match="not linked"):
        ats.jd_reviews.record(
            doc_id, criterion_id + 12345, quote, "APPROVE", "HR", "Wrong JD."
        )
    changed = dict(quote, source_excerpt="Invented Docker years of experience.")
    with pytest.raises(ATSError, match="not linked"):
        ats.jd_reviews.record(
            doc_id, criterion_id, changed, "APPROVE", "HR", "Fabricated CV source."
        )


def test_jd_review_does_not_promote_unrelated_language_field():
    from job_hire_ai.services.jd_review_service import quote_key

    criterion = SimpleNamespace(
        id=42, name="Docker", weight=100,
        description="Explicit technology mentioned in the job description.",
    )
    quote = {"source_reference": "pdf:p3:block2", "page_number": 3,
             "source_excerpt": "Cloud: Docker, Kubernetes, Azure"}
    original = [{"criterion_id": 42, "status": "REVIEW_REQUIRED", "evidence": [quote]}]
    misplaced = SimpleNamespace(
        id="lang", field_path="language_skills.0",
        source_excerpt=quote["source_excerpt"],
        source_reference=quote["source_reference"], page_number=3,
    )
    legacy_approval = SimpleNamespace(corrected_value={"decision": "APPROVE"})
    without_jd_review = reviewed_breakdown(
        [criterion], original, [misplaced], {"lang": legacy_approval}
    )
    assert without_jd_review["score"] == 0
    jd_event = SimpleNamespace(corrected_value={"decision": "APPROVE"})
    with_jd_review = reviewed_breakdown(
        [criterion], original, [misplaced], {},
        {quote_key(criterion.id, quote): jd_event},
    )
    assert with_jd_review["score"] == 100
