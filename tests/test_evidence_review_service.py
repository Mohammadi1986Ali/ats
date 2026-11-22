"""Regression tests for Phase 4.2 append-only human evidence reviews."""
import pytest
from sqlalchemy import select

from job_hire_ai.database.models import EvidenceReference, ProfileReviewEvent
from job_hire_ai.errors import ATSError
from tests.test_job_matching import make_data


def _sample(ats, vacancy, pdf):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    with ats.db.transaction() as session:
        evidence_id = session.scalar(
            select(EvidenceReference.id)
            .where(EvidenceReference.profile_id == profile_id)
        )
    return doc_id, profile_id, evidence_id


def test_approve_evidence_creates_audit_without_changing_ai_or_cv(
    ats, vacancy, pdf
):
    doc_id, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    original_profile = ats.profiles.effective(ats.profiles.get(profile_id))
    original_file = ats.documents.download(doc_id)[1]
    event_id = ats.evidence_reviews.record(
        profile_id, evidence_id, "APPROVE", "HR Reviewer",
        "Original quotation confirmed against the CV.",
    )
    assert event_id
    latest = ats.evidence_reviews.latest(profile_id)[evidence_id]
    assert latest.corrected_value["decision"] == "APPROVE"
    assert ats.profiles.effective(ats.profiles.get(profile_id)) == original_profile
    assert ats.documents.download(doc_id)[1] == original_file
    with ats.db.transaction() as session:
        original = session.get(EvidenceReference, evidence_id)
        assert original.source_excerpt == "Java and SQL experience."
        assert original.verification_status == "VERIFIED"


def test_reject_then_correct_retains_both_events(ats, vacancy, pdf):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    first = ats.evidence_reviews.record(
        profile_id, evidence_id, "REJECT", "HR", "Quote taken out of context."
    )
    second = ats.evidence_reviews.record(
        profile_id, evidence_id, "CORRECT", "HR",
        "Narrower source quote is accurate.", corrected_excerpt="Java and SQL",
        expected_last_event_id=first,
    )
    history = ats.evidence_reviews.history(profile_id, evidence_id)
    assert [event.id for event in history] == [first, second]
    assert history[0].corrected_value["decision"] == "REJECT"
    assert history[1].corrected_value["corrected_excerpt"] == "Java and SQL"
    assert ats.evidence_reviews.latest(profile_id)[evidence_id].id == second


def test_correction_must_be_present_in_same_original_source(ats, vacancy, pdf):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    with pytest.raises(ATSError, match="same original CV source block"):
        ats.evidence_reviews.record(
            profile_id, evidence_id, "CORRECT", "HR",
            "Corrected quotation", corrected_excerpt="invented Kubernetes skill",
        )
    assert ats.evidence_reviews.history(profile_id) == []


def test_evidence_review_checks_optimistic_concurrency(ats, vacancy, pdf):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    ats.evidence_reviews.record(
        profile_id, evidence_id, "APPROVE", "HR", "Checked."
    )
    with pytest.raises(ATSError, match="updated elsewhere"):
        ats.evidence_reviews.record(
            profile_id, evidence_id, "REJECT", "HR", "New interpretation.",
            expected_last_event_id=None,
        )


def test_reviewer_and_note_required(ats, vacancy, pdf):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    with pytest.raises(ATSError, match="Reviewer name"):
        ats.evidence_reviews.record(
            profile_id, evidence_id, "APPROVE", "", "Source verified."
        )
    with pytest.raises(ATSError, match="review reason"):
        ats.evidence_reviews.record(
            profile_id, evidence_id, "APPROVE", "HR", ""
        )


def test_evidence_cannot_be_reviewed_for_other_profile(ats, vacancy, pdf):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    with pytest.raises(ATSError, match="does not belong"):
        ats.evidence_reviews.record(
            profile_id, "unknown-evidence-id", "APPROVE", "HR", "Reviewed."
        )
    assert ats.evidence_reviews.history(profile_id) == []


def test_existing_profile_validation_status_survives_evidence_review(
    ats, vacancy, pdf
):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    before = ats.profiles.status(ats.profiles.get(profile_id))
    ats.evidence_reviews.record(
        profile_id, evidence_id, "APPROVE", "HR", "Checked against CV."
    )
    assert ats.profiles.status(ats.profiles.get(profile_id)) == before
    # The generic correction renderer must not interpret evidence-review paths.
    assert ats.profiles.effective(ats.profiles.get(profile_id))


def test_history_contains_persisted_reviewer_and_note(ats, vacancy, pdf):
    _, profile_id, evidence_id = _sample(ats, vacancy, pdf)
    event_id = ats.evidence_reviews.record(
        profile_id, evidence_id, "REJECT", "Recruiter",
        "Quote does not establish the claimed requirement.",
    )
    with ats.db.transaction() as session:
        event = session.get(ProfileReviewEvent, event_id)
        assert event.reviewer == "Recruiter"
        assert event.note.startswith("Quote does not")
        assert event.field_path == "__evidence_review__." + evidence_id
