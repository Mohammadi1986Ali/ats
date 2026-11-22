"""JD-criterion-specific HR reviews of source quotations.

The extracted CV profile may associate a genuine source block with the wrong
profile field (e.g. Docker text tagged as a language). Review the source quote
as evidence for the JD criterion, never as approval of that extracted field.
"""
import hashlib

from sqlalchemy import select

from job_hire_ai.database.models import ProfileReviewEvent
from job_hire_ai.errors import ATSError
from job_hire_ai.text_extraction import normalize_text

PREFIX = "__jd_evidence__."
DECISIONS = {"APPROVE", "REJECT", "CORRECT"}


def quote_key(criterion_id, quote):
    raw = "\x1f".join([
        str(criterion_id), str(quote.get("source_reference") or ""),
        str(quote.get("page_number") or ""),
        str(quote.get("source_excerpt") or ""),
    ])
    return f"{PREFIX}{criterion_id}." + hashlib.sha256(raw.encode()).hexdigest()[:32]


def source_contains(segment_text, excerpt):
    normalized = " ".join(normalize_text(excerpt).casefold().split())
    original = " ".join(normalize_text(segment_text).casefold().split())
    return bool(normalized and normalized in original)


class JDReviewService:
    def __init__(self, db, profiles, llama_scoring):
        self.db = db
        self.profiles = profiles
        self.llama_scoring = llama_scoring

    def _current(self, document_id, criterion_id, quote):
        score = self.llama_scoring.current(document_id)
        if score is None or score.status != "COMPLETED":
            raise ATSError("Current completed JD evidence scoring is required.")
        profile = self.profiles.get(score.profile_id)
        if profile.document_id != document_id:
            raise ATSError("Scoring profile does not match the selected CV.")
        finding = next(
            (f for f in score.breakdown or []
             if str(f.get("criterion_id")) == str(criterion_id)), None
        )
        if finding is None or not any(
            q.get("source_reference") == quote.get("source_reference")
            and q.get("page_number") == quote.get("page_number")
            and q.get("source_excerpt") == quote.get("source_excerpt")
            for q in finding.get("evidence") or []
        ):
            raise ATSError("Quotation is not linked to this JD requirement.")
        return profile

    def history(self, document_id, criterion_id, quote):
        profile = self._current(document_id, criterion_id, quote)
        path = quote_key(criterion_id, quote)
        return sorted(
            (event for event in profile.reviews if event.field_path == path),
            key=lambda event: (event.created_at, event.id),
        )

    def record(
        self, document_id, criterion_id, quote, decision, reviewer, note,
        corrected_excerpt="", expected_last_event_id=None,
    ):
        if decision not in DECISIONS:
            raise ATSError("Choose Approve, Reject or Correct.")
        reviewer = reviewer.strip()
        note = note.strip()
        corrected_excerpt = corrected_excerpt.strip()
        if not 1 <= len(reviewer) <= 200:
            raise ATSError("Reviewer name must be 1–200 characters.")
        if not 1 <= len(note) <= 2000:
            raise ATSError("Review reason must be 1–2000 characters.")
        if decision == "CORRECT" and not 1 <= len(corrected_excerpt) <= 1000:
            raise ATSError("Corrected quotation must be 1–1000 characters.")
        if decision != "CORRECT" and corrected_excerpt:
            raise ATSError("Only Correct accepts a corrected quotation.")
        profile = self._current(document_id, criterion_id, quote)
        source = self.profiles.source(profile.id)
        segments = [
            s for s in source.segments
            if s.reference == quote.get("source_reference")
            and s.page_number == quote.get("page_number")
        ]
        if not any(source_contains(s.text, quote.get("source_excerpt") or "") for s in segments):
            raise ATSError("The selected JD quotation is not found in the original CV.")
        if decision == "CORRECT" and not any(
            source_contains(s.text, corrected_excerpt) for s in segments
        ):
            raise ATSError("Correction must be a literal excerpt in the same CV source block.")
        path = quote_key(criterion_id, quote)
        with self.db.transaction() as session:
            previous = session.scalar(
                select(ProfileReviewEvent).where(
                    ProfileReviewEvent.profile_id == profile.id,
                    ProfileReviewEvent.field_path == path,
                ).order_by(ProfileReviewEvent.id.desc()).limit(1)
            )
            if (previous.id if previous else None) != expected_last_event_id:
                raise ATSError("JD evidence review changed. Refresh and retry.")
            event = ProfileReviewEvent(
                profile_id=profile.id, field_path=path,
                original_value={
                    "criterion_id": criterion_id,
                    "document_id": document_id,
                    "source_reference": quote.get("source_reference"),
                    "page_number": quote.get("page_number"),
                    "source_excerpt": quote.get("source_excerpt"),
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
