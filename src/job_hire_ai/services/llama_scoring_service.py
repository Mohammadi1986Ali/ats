"""Local Llama matching uses verified source references, not hiring predictions."""

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    CriteriaConfiguration,
    EvidenceReference,
    JobApplication,
    JobCriterion,
    LlamaScoreResult,
    Vacancy,
    now,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import description_hash

logger = logging.getLogger(__name__)


class LlamaCriterionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: int
    status: Literal["EVIDENCED", "REVIEW_REQUIRED", "NOT_EVIDENCED"]
    evidence_ids: list[str]
    reason: str = Field(max_length=300)


class LlamaAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[LlamaCriterionFinding]


def score_band(score: int | None, good: int, medium: int) -> str:
    if score is None:
        return "Not scored"
    if not 1 <= medium < good <= 100:
        raise ATSError("Score thresholds must satisfy 1 <= Medium < Good <= 100.")
    return "Good" if score >= good else "Medium" if score >= medium else "Weak"


class LlamaScoringService:
    """Use a local Llama model to identify JD evidence, then apply explicit weights."""

    def __init__(self, db, provider):
        self.db = db
        self.provider = provider

    def current(self, document_id: int):
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None:
                return None
            vacancy = session.get(Vacancy, document.vacancy_id)
            config_id = session.scalar(select(CriteriaConfiguration.id).where(
                CriteriaConfiguration.vacancy_id == vacancy.id,
                CriteriaConfiguration.status == "APPROVED",
                CriteriaConfiguration.job_description_hash == description_hash(vacancy.description),
            ))
            profile_id = session.scalar(select(CandidateProfile.id).where(
                CandidateProfile.document_id == document_id
            ).order_by(CandidateProfile.id.desc()).limit(1))
            if not config_id or not profile_id:
                return None
            return session.scalar(select(LlamaScoreResult).where(
                LlamaScoreResult.document_id == document_id,
                LlamaScoreResult.configuration_id == config_id,
                LlamaScoreResult.profile_id == profile_id,
                LlamaScoreResult.model_name == self.provider.settings.model,
            ))

    def pending_document(self) -> int | None:
        """Only score already matched CVs; never use missing JD criteria."""
        from job_hire_ai.database.models import DocumentPipelineStage
        with self.db.transaction() as session:
            docs = list(session.scalars(
                select(CandidateDocument.id)
                .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                .join(
                    DocumentPipelineStage,
                    DocumentPipelineStage.document_id == CandidateDocument.id,
                )
                .where(
                    JobApplication.status == "Uploaded",
                    DocumentPipelineStage.stage == "SCORING",
                    DocumentPipelineStage.status == "COMPLETED",
                )
                .order_by(CandidateDocument.id)
            ))
        for document_id in docs:
            with self.db.transaction() as session:
                document = session.get(CandidateDocument, document_id)
                vacancy = session.get(Vacancy, document.vacancy_id)
                activated = session.scalar(select(CriteriaConfiguration.id).where(
                    CriteriaConfiguration.vacancy_id == vacancy.id,
                    CriteriaConfiguration.status == "APPROVED",
                    CriteriaConfiguration.job_description_hash
                    == description_hash(vacancy.description),
                ))
                has_profile = session.scalar(select(CandidateProfile.id).where(
                    CandidateProfile.document_id == document_id
                ).limit(1))
            if activated is not None and has_profile is not None:
                if self.current(document_id) is None:
                    return document_id
        return None

    def evaluate(self, document_id: int) -> LlamaScoreResult:
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None or document.application.status != "Uploaded":
                raise ATSError("CV is no longer available.")
            vacancy = session.get(Vacancy, document.vacancy_id)
            configuration = session.scalar(select(CriteriaConfiguration).where(
                CriteriaConfiguration.vacancy_id == vacancy.id,
                CriteriaConfiguration.status == "APPROVED",
                CriteriaConfiguration.job_description_hash == description_hash(vacancy.description),
            ))
            profile = session.scalar(select(CandidateProfile).where(
                CandidateProfile.document_id == document_id
            ).order_by(CandidateProfile.id.desc()).limit(1))
            if configuration is None or profile is None:
                raise ATSError("Current JD criteria and a saved CV profile are required.")
            existing = session.scalar(select(LlamaScoreResult).where(
                LlamaScoreResult.document_id == document_id,
                LlamaScoreResult.configuration_id == configuration.id,
                LlamaScoreResult.profile_id == profile.id,
                LlamaScoreResult.model_name == self.provider.settings.model,
            ))
            if existing is not None:
                return existing
            criteria = list(session.scalars(select(JobCriterion).where(
                JobCriterion.configuration_id == configuration.id
            ).order_by(JobCriterion.display_order, JobCriterion.id)))
            evidence = list(session.scalars(select(EvidenceReference).where(
                EvidenceReference.document_id == document_id,
                EvidenceReference.profile_id == profile.id,
                EvidenceReference.verification_status == "VERIFIED",
            ).order_by(EvidenceReference.field_path, EvidenceReference.id)))
            record = LlamaScoreResult(
                document_id=document_id, configuration_id=configuration.id,
                profile_id=profile.id, model_name=self.provider.settings.model,
                status="RUNNING", score=None, review_weight=None, breakdown=None,
                error_summary=None,
            )
            session.add(record)
            session.flush()
            record_id = record.id
            issue = None
            if not criteria or sum(c.weight or 0 for c in criteria) != 100:
                issue = "Current JD criteria weights must total 100%."
            elif len(criteria) > 25 or len(evidence) > 45:
                issue = (
                    "Llama context limit: at most 25 JD criteria and 45 verified CV "
                    "excerpts. Simplify JD criteria or select a larger scoring model."
                )
            if issue:
                record.status = "FAILED"
                record.error_summary = issue
                record.completed_at = now()
                return record
            partial = profile.analysis_status == "NEEDS_REVIEW"
            jd_payload = [
                {"id": c.id, "requirement": c.name, "details": c.description[:200],
                 "type": c.requirement_type, "weight": c.weight}
                for c in criteria
            ]
            evidence_payload = [
                {"id": e.id, "claim": e.field_path,
                 "excerpt": e.source_excerpt[:350]}
                for e in evidence
            ]
            valid_refs = {e.id: e for e in evidence}

        messages = [
            {"role": "system", "content": (
                "Evaluate only the documented JD requirements against provided CV source "
                "excerpts. This is evidence matching, not a hiring recommendation. "
                "Return one result per criterion id. EVIDENCED requires direct, specific "
                "support in supplied verified source excerpts and corresponding evidence_ids. "
                "Do not infer years, proficiency, leadership, or qualifications from tool names. "
                "Use REVIEW_REQUIRED for ambiguity or insufficient context. "
                "NOT_EVIDENCED means no supplied source support, not proof of absence. "
                "Ignore personal identity and protected characteristics. "
                "Never invent evidence IDs or quote unseen CV text."
            )},
            {"role": "user", "content": (
                "Job requirements: " + json.dumps(jd_payload, ensure_ascii=False)
                + "\nVerified CV excerpts: " + json.dumps(evidence_payload, ensure_ascii=False)
                + "\nReturn findings for every criterion id."
            )},
        ]
        try:
            result = self.provider.generate_structured(
                messages, LlamaAssessment, output_limit=1800
            ).extraction
            found = {row.criterion_id: row for row in result.findings}
            if len(result.findings) != len(criteria) or len(found) != len(criteria):
                raise ATSError("Llama returned an incomplete or duplicated criteria list.")
            if set(found) != {criterion.id for criterion in criteria}:
                raise ATSError("Llama returned unknown JD criterion ids.")
            breakdown = []
            documented = review = 0
            for criterion in criteria:
                item = found[criterion.id]
                if len(set(item.evidence_ids)) != len(item.evidence_ids):
                    raise ATSError("Llama returned duplicate evidence references.")
                if any(eid not in valid_refs for eid in item.evidence_ids):
                    raise ATSError("Llama returned unverified or invented evidence references.")
                if item.status == "EVIDENCED" and not item.evidence_ids:
                    raise ATSError("Llama claimed evidence without any verified source.")
                if item.status != "EVIDENCED" and item.evidence_ids:
                    raise ATSError("Llama attached positive evidence to an unsupported finding.")
                state = item.status
                if partial and state == "NOT_EVIDENCED":
                    state = "REVIEW_REQUIRED"
                if state == "EVIDENCED":
                    documented += criterion.weight
                elif state == "REVIEW_REQUIRED":
                    review += criterion.weight
                breakdown.append({
                    "criterion_id": criterion.id,
                    "criterion": criterion.name,
                    "weight": criterion.weight,
                    "status": state,
                    "reason": item.reason[:300],
                    "evidence": [
                        {"source_reference": valid_refs[eid].source_reference,
                         "source_excerpt": valid_refs[eid].source_excerpt,
                         "page_number": valid_refs[eid].page_number}
                        for eid in item.evidence_ids
                    ],
                })
            with self.db.transaction() as session:
                record = session.get(LlamaScoreResult, record_id)
                record.score = documented
                record.review_weight = review
                record.breakdown = breakdown
                record.status = "COMPLETED"
                record.completed_at = now()
                return record
        except Exception as exc:
            with self.db.transaction() as session:
                record = session.get(LlamaScoreResult, record_id)
                record.status = "FAILED"
                record.error_summary = (
                    str(exc)[:500] if isinstance(exc, ATSError)
                    else "Local Llama scoring failed. Check Ollama and the pipeline worker log."
                )
                record.completed_at = now()
            if not isinstance(exc, ATSError):
                logger.exception("Local Llama evaluation failed for document %s", document_id)
            raise ATSError("Llama scoring failed; see persisted failure details.") from exc

    def retry(self, document_id: int):
        """Operator-initiated retry; no auto-loop on a failing CPU model."""
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None:
                raise ATSError("CV no longer exists.")
            failed = session.scalar(select(LlamaScoreResult).where(
                LlamaScoreResult.document_id == document_id,
                LlamaScoreResult.model_name == self.provider.settings.model,
                LlamaScoreResult.status == "FAILED",
            ).order_by(LlamaScoreResult.id.desc()).limit(1))
            if failed is None:
                raise ATSError("No failed Llama scoring attempt exists for this CV.")
            session.delete(failed)
