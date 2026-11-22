"""Phase 4.3: append-only, deterministic HR-reviewed JD evidence coverage.

Never mutate original AI scores, source quotes or the CV. Human approval alone
cannot invent qualifications; positive changes require literal JD technology
coverage in an explicitly reviewed original-source quotation.
"""
import re

from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateDocument, CandidateProfile, CriteriaConfiguration,
    EvidenceReference, JobCriterion, LlamaScoreResult, ProfileReviewEvent,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import description_hash

REVIEW_PREFIX = "__evidence_review__."
SCORE_PREFIX = "__hr_score__."


def _has_term(text, term):
    if not text or not term:
        return False
    return bool(re.search(
        r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", text, re.I
    ))


def _source_matches(quote, evidence):
    return (
        quote.get("source_reference") == evidence.source_reference
        and quote.get("page_number") == evidence.page_number
        and quote.get("source_excerpt") == evidence.source_excerpt
    )


def reviewed_breakdown(criteria, original, evidence, decisions):
    """Compute documented/review buckets; unknown remains review, never a rejection."""
    by_id = {int(row["criterion_id"]): row for row in original}
    rows = []
    documented = review = 0
    if len(by_id) != len(criteria) or sum(row.weight for row in criteria) != 100:
        raise ATSError("Current JD scoring criteria are incomplete.")
    for criterion in criteria:
        finding = by_id.get(criterion.id)
        if finding is None:
            raise ATSError("AI score is not aligned with the current JD criteria.")
        cited = finding.get("evidence") or []
        reviewed = []
        surviving = []
        rejected = []
        for quote in cited:
            matches = [e for e in evidence if _source_matches(quote, e)]
            if not matches:
                # Never silently infer an identity for an unmatched citation.
                surviving.append(quote)
                continue
            related = []
            for e in matches:
                event = decisions.get(e.id)
                if event is not None:
                    related.append((e, event.corrected_value or {}))
            if related and all(action.get("decision") == "REJECT" for _, action in related):
                rejected.append(quote)
                continue
            surviving.append(quote)
            reviewed.extend(
                (e, action) for e, action in related
                if action.get("decision") in {"APPROVE", "CORRECT"}
            )
        baseline_state = finding.get("status")
        state = baseline_state
        if baseline_state == "EVIDENCED" and not surviving:
            state = "REVIEW_REQUIRED"
        # Additional positive coverage is allowed only for the legacy *literal*
        # single-technology criterion, and only when explicitly reviewed source
        # text contains the exact criterion. No general competence inference.
        approved_tech_sources = [
            (e, event.corrected_value or {})
            for e in evidence
            if (event := decisions.get(e.id)) is not None
            and (event.corrected_value or {}).get("decision") in {"APPROVE", "CORRECT"}
            and _has_term(
                ((event.corrected_value or {}).get("corrected_excerpt")
                 if (event.corrected_value or {}).get("decision") == "CORRECT"
                 else e.source_excerpt),
                criterion.name,
            )
        ]
        if (
            baseline_state in {"REVIEW_REQUIRED", "NOT_EVIDENCED"}
            and criterion.description == "Explicit technology mentioned in the job description."
            and approved_tech_sources
        ):
            state = "EVIDENCED"
        if state == "EVIDENCED":
            documented += criterion.weight
        else:
            review += criterion.weight
        rows.append({
            "criterion_id": criterion.id, "criterion": criterion.name,
            "weight": criterion.weight, "AI status": baseline_state,
            "HR status": state, "reviewed_sources": len(reviewed) + len(approved_tech_sources),
            "excluded_sources": len(rejected),
            "reason": "HR source review applied" if reviewed or rejected or approved_tech_sources
                      else "Unchanged from AI evidence assessment",
        })
    return {"score": documented, "review_weight": review, "breakdown": rows}


class HRScoreService:
    def __init__(self, db):
        self.db = db

    def _context(self, session, document_id):
        document = session.get(CandidateDocument, document_id)
        if document is None or document.application.status != "Uploaded":
            raise ATSError("CV is not available for HR scoring.")
        vacancy = document.application.vacancy
        config = session.scalar(select(CriteriaConfiguration).where(
            CriteriaConfiguration.vacancy_id == vacancy.id,
            CriteriaConfiguration.status == "APPROVED",
            CriteriaConfiguration.job_description_hash == description_hash(vacancy.description),
        ))
        profile = session.scalar(select(CandidateProfile).where(
            CandidateProfile.document_id == document_id
        ).order_by(CandidateProfile.id.desc()).limit(1))
        if config is None or profile is None:
            raise ATSError("Current approved JD criteria and CV profile are required.")
        score = session.scalar(select(LlamaScoreResult).where(
            LlamaScoreResult.document_id == document_id,
            LlamaScoreResult.configuration_id == config.id,
            LlamaScoreResult.profile_id == profile.id,
            LlamaScoreResult.status == "COMPLETED",
        ).order_by(LlamaScoreResult.id.desc()).limit(1))
        if score is None or not score.breakdown:
            raise ATSError("Completed, source-backed Llama scoring is required first.")
        return config, profile, score

    def history(self, document_id):
        with self.db.transaction() as session:
            config, profile, _ = self._context(session, document_id)
            return list(session.scalars(select(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id == profile.id,
                ProfileReviewEvent.field_path == SCORE_PREFIX + str(config.id),
            ).order_by(ProfileReviewEvent.id.desc())))

    def calculate(self, document_id, reviewer, note):
        if not 1 <= len(reviewer.strip()) <= 200 or not 1 <= len(note.strip()) <= 2000:
            raise ATSError("HR score requires a reviewer and a reason (up to 2000 characters).")
        with self.db.transaction() as session:
            config, profile, score = self._context(session, document_id)
            criteria = list(session.scalars(select(JobCriterion).where(
                JobCriterion.configuration_id == config.id,
            ).order_by(JobCriterion.display_order, JobCriterion.id)))
            evidence = list(session.scalars(select(EvidenceReference).where(
                EvidenceReference.profile_id == profile.id,
                EvidenceReference.document_id == document_id,
                EvidenceReference.verification_status == "VERIFIED",
            )))
            events = list(session.scalars(select(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id == profile.id,
                ProfileReviewEvent.field_path.like(REVIEW_PREFIX + "%"),
            ).order_by(ProfileReviewEvent.id)))
            if not events:
                raise ATSError("Review at least one source quotation before recalculating.")
            latest = {
                event.field_path[len(REVIEW_PREFIX):]: event for event in events
            }
            computed = reviewed_breakdown(criteria, score.breakdown, evidence, latest)
            source_event_ids = sorted(event.id for event in latest.values())
            last = session.scalar(select(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id == profile.id,
                ProfileReviewEvent.field_path == SCORE_PREFIX + str(config.id),
            ).order_by(ProfileReviewEvent.id.desc()).limit(1))
            if (
                last is not None
                and last.corrected_value.get("source_event_ids") == source_event_ids
                and last.corrected_value.get("ai_score_id") == score.id
                and last.corrected_value.get("ai_score_completed_at") == str(score.completed_at)
            ):
                return last.id
            event = ProfileReviewEvent(
                profile_id=profile.id, field_path=SCORE_PREFIX + str(config.id),
                original_value={"ai_score_id": score.id, "score": score.score,
                                "review_weight": score.review_weight},
                corrected_value={
                    **computed, "ai_score_id": score.id,
                    "ai_score_completed_at": str(score.completed_at),
                    "source_event_ids": source_event_ids,
                    "configuration_id": config.id,
                    "document_id": document_id,
                },
                reviewer=reviewer.strip(), note=note.strip(),
            )
            session.add(event)
            session.flush()
            return event.id
