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
from job_hire_ai.services.jd_review_service import PREFIX as JD_PREFIX, quote_key

REVIEW_PREFIX = "__evidence_review__."
SCORE_PREFIX = "__hr_score__."


def _has_term(text, term):
    if not text or not term:
        return False
    token = re.escape(term.strip())
    if re.search(
        r"\b(no|not|without|never|lack(?:s|ing)?)\s+"
        r"(?:experience\s+(?:in|with)\s+)?" + token + r"\b", text, re.I
    ):
        return False
    return bool(re.search(
        r"(?<!\w)" + token + r"(?!\w)", text, re.I
    ))


def _source_matches(quote, evidence):
    return (
        quote.get("source_reference") == evidence.source_reference
        and quote.get("page_number") == evidence.page_number
        and quote.get("source_excerpt") == evidence.source_excerpt
    )


def reviewed_breakdown(criteria, original, evidence, decisions, jd_reviews=None):
    """Compute documented/review buckets; unknown remains review, never a rejection."""
    jd_reviews = jd_reviews or {}
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
        reviewed_jd = []
        for quote in cited:
            jd_event = jd_reviews.get(quote_key(criterion.id, quote))
            jd_action = jd_event.corrected_value or {} if jd_event else {}
            if jd_action.get("decision") == "REJECT":
                rejected.append(quote)
                continue
            if jd_action.get("decision") in {"APPROVE", "CORRECT"}:
                reviewed_jd.append((
                    jd_action.get("corrected_excerpt")
                    if jd_action.get("decision") == "CORRECT"
                    else quote.get("source_excerpt")
                ))
                surviving.append(quote)
                continue
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
            and e.field_path.startswith((
                "technical_skills.", "employment_history.", "projects.",
                "relevant_domain_experience.",
            ))
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
            and (approved_tech_sources or any(
                _has_term(quote, criterion.name) for quote in reviewed_jd
            ))
        ):
            state = "EVIDENCED"
        if state == "EVIDENCED":
            documented += criterion.weight
        else:
            review += criterion.weight
        rows.append({
            "criterion_id": criterion.id, "criterion": criterion.name,
            "weight": criterion.weight, "AI status": baseline_state,
            "HR status": state, "reviewed_sources": len(reviewed) + len(approved_tech_sources) + len(reviewed_jd),
            "excluded_sources": len(rejected),
            "reason": "HR source review applied" if reviewed or reviewed_jd or rejected or approved_tech_sources
                      else "Unchanged from AI evidence assessment",
        })
    return {"score": documented, "review_weight": review, "breakdown": rows}


class HRScoreService:
    def __init__(self, db, model_name=None):
        self.db = db
        self.model_name = model_name

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
        query = select(LlamaScoreResult).where(
            LlamaScoreResult.document_id == document_id,
            LlamaScoreResult.configuration_id == config.id,
            LlamaScoreResult.profile_id == profile.id,
            LlamaScoreResult.status == "COMPLETED",
        )
        if self.model_name:
            query = query.where(LlamaScoreResult.model_name == self.model_name)
        score = session.scalar(query.order_by(LlamaScoreResult.id.desc()).limit(1))
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

    def is_current(self, document_id, snapshot):
        """Old HR-score snapshots are stale after new source reviews or AI rescoring."""
        with self.db.transaction() as session:
            config, profile, score = self._context(session, document_id)
            if snapshot.profile_id != profile.id:
                return False
            latest = {}
            for event in session.scalars(select(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id == profile.id,
                ProfileReviewEvent.field_path.like(REVIEW_PREFIX + "%"),
            ).order_by(ProfileReviewEvent.id)):
                latest[event.field_path] = event.id
            for event in session.scalars(select(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id == profile.id,
                ProfileReviewEvent.field_path.like(JD_PREFIX + "%"),
            ).order_by(ProfileReviewEvent.id)):
                latest[event.field_path] = event.id
            data = snapshot.corrected_value or {}
            return (
                data.get("configuration_id") == config.id
                and data.get("ai_score_id") == score.id
                and data.get("ai_score_completed_at") == str(score.completed_at)
                and data.get("source_event_ids") == sorted(latest.values())
            )

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
            jd_events = list(session.scalars(select(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id == profile.id,
                ProfileReviewEvent.field_path.like(JD_PREFIX + "%"),
            ).order_by(ProfileReviewEvent.id)))
            events.extend(jd_events)
            if not events:
                raise ATSError("Review at least one source quotation before recalculating.")
            latest_events = {event.field_path: event for event in events}
            latest = {
                path[len(REVIEW_PREFIX):]: event
                for path, event in latest_events.items()
                if path.startswith(REVIEW_PREFIX)
            }
            latest_jd = {
                path: event for path, event in latest_events.items()
                if path.startswith(JD_PREFIX)
            }
            computed = reviewed_breakdown(
                criteria, score.breakdown, evidence, latest, latest_jd
            )
            source_event_ids = sorted(event.id for event in latest_events.values())
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
