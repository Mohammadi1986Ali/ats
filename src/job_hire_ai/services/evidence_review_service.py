"""Append-only human review of existing, source-backed CV evidence.

Phase 4.2 does not alter original CV bytes, verified extraction, AI scores, or hiring decisions.
Review events live in the existing profile_review_events audit table.
"""
from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateProfile, EvidenceReference, ProfileReviewEvent,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.text_extraction import normalize_text

PREFIX = "__evidence_review__."
DECISIONS = {"APPROVE", "REJECT", "CORRECT"}


class EvidenceReviewService:
    def __init__(self, db, profiles):
        self.db, self.profiles = db, profiles

    def history(self, profile_id: int, evidence_id: str | None = None):
        # get() enforces nonremoved applications and loads existing review events.
        profile = self.profiles.get(profile_id)
        prefix = PREFIX + evidence_id if evidence_id else PREFIX
        rows = [
            row for row in profile.reviews
            if (row.field_path == prefix if evidence_id
                else row.field_path.startswith(PREFIX))
        ]
        return sorted(rows, key=lambda row: (row.created_at, row.id))

    def latest(self, profile_id: int):
        return {
            row.field_path[len(PREFIX):]: row
            for row in self.history(profile_id)
        }

    def record(
        self, profile_id: int, evidence_id: str, decision: str,
        reviewer: str, note: str, corrected_excerpt: str = "",
        expected_last_event_id: int | None = None,
    ):
        if decision not in DECISIONS:
            raise ATSError("Select Approve, Reject or Correct.")
        reviewer, note, corrected_excerpt = (
            reviewer.strip(), note.strip(), corrected_excerpt.strip()
        )
        if not 1 <= len(reviewer) <= 200:
            raise ATSError("Reviewer name must have 1–200 characters.")
        if not 1 <= len(note) <= 2000:
            raise ATSError("Enter a review reason (1–2000 characters).")
        if decision == "CORRECT" and not 1 <= len(corrected_excerpt) <= 1000:
            raise ATSError("Corrected quotation must contain 1–1000 characters.")
        if decision != "CORRECT" and corrected_excerpt:
            raise ATSError("Corrected text is permitted only for Correct.")
        # Validate against persisted source text, not against LLM output.
        self.profiles.get(profile_id)
        source = self.profiles.source(profile_id) if decision == "CORRECT" else None
        with self.db.transaction() as session:
            profile = session.get(CandidateProfile, profile_id)
            if profile is None or profile.document.application.status != "Uploaded":
                raise ATSError("Candidate profile is not available.")
            evidence = session.get(EvidenceReference, evidence_id)
            if evidence is None or evidence.profile_id != profile_id:
                raise ATSError("Evidence does not belong to this CV profile.")
            if decision == "CORRECT":
                matching = [
                    segment for segment in source.segments
                    if segment.reference == evidence.source_reference
                ]
                if not any(
                    normalize_text(corrected_excerpt).casefold()
                    in normalize_text(segment.text).casefold()
                    for segment in matching
                ):
                    raise ATSError(
                        "Corrected quotation must appear verbatim in the same "
                        "original CV source block. No correction was saved."
                    )
            path = PREFIX + evidence_id
            previous = session.scalar(
                select(ProfileReviewEvent)
                .where(
                    ProfileReviewEvent.profile_id == profile_id,
                    ProfileReviewEvent.field_path == path,
                )
                .order_by(ProfileReviewEvent.id.desc())
                .limit(1)
            )
            if (previous.id if previous is not None else None) != expected_last_event_id:
                raise ATSError("Evidence review was updated elsewhere. Refresh and retry.")
            event = ProfileReviewEvent(
                profile_id=profile_id, field_path=path,
                original_value={
                    "source_excerpt": evidence.source_excerpt,
                    "source_reference": evidence.source_reference,
                    "verification_status": evidence.verification_status,
                },
                corrected_value={
                    "decision": decision,
                    "corrected_excerpt": corrected_excerpt if decision == "CORRECT" else None,
                },
                reviewer=reviewer, note=note,
            )
            session.add(event)
            session.flush()
            return event.id
