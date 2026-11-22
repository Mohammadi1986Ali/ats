"""Local Llama matching uses verified source references, not hiring predictions."""

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from job_hire_ai.ai.provider import ProviderError
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


def scoring_model_settings(model: str):
    """Use CPU-friendly context and bounded generation without changing CV extraction."""
    from job_hire_ai.ai.settings import OllamaSettings

    current = OllamaSettings(model=model)
    return current.model_copy(update={
        "num_ctx": 4096,
        "num_predict": 1024,
        "num_thread": 4,
        "retries": 0,
        "timeout_seconds": min(current.timeout_seconds, 180),
    })


def score_band(score: int | None, good: int, medium: int) -> str:
    if score is None:
        return "Not scored"
    if not 1 <= medium < good <= 100:
        raise ATSError("Score thresholds must satisfy 1 <= Medium < Good <= 100.")
    return "Good" if score >= good else "Medium" if score >= medium else "Weak"



def evidence_classification(
    score: int | None, review: int | None, good: int, medium: int
) -> str:
    """Classify only when unresolved evidence cannot change the score band."""
    if score is None:
        return "Not scored"
    if not 1 <= medium < good <= 100:
        raise ATSError("Score thresholds must satisfy 1 <= Medium < Good <= 100.")
    uncertain = max(0, review or 0)
    if uncertain and score < good and score + uncertain >= medium:
        return "Needs review"
    return score_band(score, good, medium)



def _relevant_evidence(criterion, evidence, limit=12):
    """Keep bounded original-source excerpts; omitted passages remain unassessed."""
    tokens = set(re.findall(r"[a-z0-9+#.]{2,}", criterion.name.casefold()))
    tokens -= {"and", "the", "with", "for", "years", "experience", "knowledge", "skills"}
    ranked = []
    for index, row in enumerate(evidence):
        excerpt = row.source_excerpt.casefold()
        words = set(re.findall(r"[a-z0-9+#.]{2,}", excerpt))
        ranked.append((
            int(criterion.name.casefold() in excerpt),
            len(tokens & words), -index, row,
        ))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    selected = [row for _, _, _, row in ranked[:limit]]
    return selected, len(evidence) > len(selected)


def _has_conflicting_findings(result) -> bool:
    """Identify old 1B outputs with a review status but an EVIDENCED-only reason."""
    return (
        result is not None
        and result.status == "COMPLETED"
        and isinstance(result.breakdown, list)
        and any(
            isinstance(row, dict)
            and row.get("status") != "EVIDENCED"
            and str(row.get("reason", "")).strip().upper() == "EVIDENCED"
            and bool(row.get("evidence"))
            for row in result.breakdown
        )
    )


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

    def recover_interrupted(self) -> int:
        """Make crashed/incomplete attempts retryable instead of RUNNING forever."""
        with self.db.transaction() as session:
            stuck = list(session.scalars(select(LlamaScoreResult).where(
                LlamaScoreResult.model_name == self.provider.settings.model,
                LlamaScoreResult.status == "RUNNING",
            )))
            for row in stuck:
                row.status = "FAILED"
                row.error_summary = (
                    "Llama scoring was interrupted by an application restart. "
                    "Retry from the CV details."
                )
                row.completed_at = now()
            return len(stuck)

    def recover_outdated_failures(self) -> int:
        """Requeue failed snapshots caused by the former hard limit or interruption."""
        with self.db.transaction() as session:
            failed = list(session.scalars(select(LlamaScoreResult).where(
                LlamaScoreResult.model_name == self.provider.settings.model,
                LlamaScoreResult.status == "FAILED",
            )))
            retry = [row for row in failed if row.error_summary and (
                row.error_summary.startswith("Llama context limit:")
                or row.error_summary.startswith("Llama scoring was interrupted")
                or row.error_summary.startswith("Ollama timed out or disconnected")
                or row.error_summary.startswith("Llama returned incomplete")
                or row.error_summary.startswith(
                    "Llama attached positive evidence to an unsupported finding."
                )
            )]
            for row in retry:
                session.delete(row)
            return len(retry)

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
                current = self.current(document_id)
                if current is None or _has_conflicting_findings(current):
                    return document_id
        return None

    def evaluate(self, document_id: int, *, force: bool = False) -> LlamaScoreResult:
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
            if existing is not None and not force and not _has_conflicting_findings(existing):
                return existing
            criteria = list(session.scalars(select(JobCriterion).where(
                JobCriterion.configuration_id == configuration.id
            ).order_by(JobCriterion.display_order, JobCriterion.id)))
            evidence = list(session.scalars(select(EvidenceReference).where(
                EvidenceReference.document_id == document_id,
                EvidenceReference.profile_id == profile.id,
                EvidenceReference.verification_status == "VERIFIED",
            ).order_by(EvidenceReference.field_path, EvidenceReference.id)))
            if existing is None:
                record = LlamaScoreResult(
                    document_id=document_id, configuration_id=configuration.id,
                    profile_id=profile.id, model_name=self.provider.settings.model,
                    status="RUNNING", score=None, review_weight=None, breakdown=None,
                    error_summary=None,
                )
                session.add(record)
            else:
                # Unique snapshot key: explicitly recheck a previous completed
                # evaluation without deleting its CV, profile, or job criteria.
                record = existing
                record.status = "RUNNING"
                record.score = None
                record.review_weight = None
                record.breakdown = None
                record.error_summary = None
                record.completed_at = None
            session.flush()
            record_id = record.id
            issue = None
            if not criteria or sum(c.weight or 0 for c in criteria) != 100:
                issue = "Current JD criteria weights must total 100%."
            if issue:
                record.status = "FAILED"
                record.error_summary = issue
                record.completed_at = now()
                return record
            partial = profile.analysis_status == "NEEDS_REVIEW"
            evidence_by_criterion = {}
            omitted_by_criterion = {}
            for criterion in criteria:
                selected, omitted = _relevant_evidence(criterion, evidence)
                evidence_by_criterion[criterion.id] = selected
                omitted_by_criterion[criterion.id] = omitted
            valid_refs = {f"e{index}": e for index, e in enumerate(evidence, 1)}
            refs_to_ids = {e.id: ref for ref, e in valid_refs.items()}

        system_prompt = (
            "Compare JD requirements against verified CV excerpts only. "
            "Return one finding per criterion id. EVIDENCED requires direct, "
            "specific source support and exact input evidence_ids. "
            "Use REVIEW_REQUIRED for ambiguity. NOT_EVIDENCED only means no "
            "support in provided excerpts; do not infer qualification absence. "
            "Never invent evidence IDs or qualifications; ignore personal data. "
            "This is evidence matching, not an automated hiring recommendation."
        )
        findings = {}
        batches = [criteria[i:i + 3] for i in range(0, len(criteria), 3)]
        try:
            for batch in batches:
                jd_payload = [
                    {"id": c.id, "requirement": c.name, "details": c.description[:160],
                     "type": c.requirement_type}
                    for c in batch
                ]
                chosen = {}
                for criterion in batch:
                    for row in evidence_by_criterion[criterion.id]:
                        chosen[row.id] = row
                evidence_payload = [
                    {"id": refs_to_ids[row.id], "excerpt": row.source_excerpt[:250]}
                    for row in chosen.values()
                ]
                prompt = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": (
                        "JD requirements: " + json.dumps(jd_payload, ensure_ascii=False)
                        + "\\nVerified CV excerpts: "
                        + json.dumps(evidence_payload, ensure_ascii=False)
                        + "\\nReturn one finding per JD criterion id."
                    )},
                ]
                # A 1B model can omit items in a multi-criterion response.
                # Retry only that failed batch using one short, strict output
                # per criterion instead of losing the whole CV score.
                received = {}
                try:
                    response = self.provider.generate_structured(
                        prompt, LlamaAssessment, output_limit=650
                    ).extraction
                    received = {item.criterion_id: item for item in response.findings}
                    if (
                        len(received) != len(batch)
                        or len(response.findings) != len(batch)
                        or set(received) != {criterion.id for criterion in batch}
                    ):
                        raise ATSError("Llama returned incomplete or duplicate JD criteria.")
                except (ATSError, ProviderError) as exc:
                    logger.warning(
                        "Llama batch response failed for CV %s; retrying %s criteria "
                        "individually: %s", document_id, len(batch), type(exc).__name__,
                    )
                    received = {}
                    for criterion in batch:
                        individual = evidence_by_criterion[criterion.id][:5]
                        brief_evidence = [
                            {"id": refs_to_ids[row.id], "excerpt": row.source_excerpt[:180]}
                            for row in individual
                        ]
                        single_prompt = [
                            {"role": "system", "content": (
                                "Check only the JD criterion against verified CV excerpts. "
                                "Return criterion_id, status, evidence_ids, reason. "
                                "EVIDENCED requires supplied source IDs. If unclear, use "
                                "REVIEW_REQUIRED. Do not invent sources or qualifications."
                            )},
                            {"role": "user", "content": (
                                "Criterion: " + json.dumps({
                                    "id": criterion.id, "name": criterion.name,
                                    "details": criterion.description[:100],
                                }, ensure_ascii=False)
                                + "\nVerified excerpts: "
                                + json.dumps(brief_evidence, ensure_ascii=False)
                            )},
                        ]
                        single = self.provider.generate_structured(
                            single_prompt, LlamaCriterionFinding, output_limit=260
                        ).extraction
                        if single.criterion_id != criterion.id:
                            raise ATSError("Llama returned the wrong JD criterion ID.") from exc
                        permitted_single = {row["id"] for row in brief_evidence}
                        if any(ref not in permitted_single for ref in single.evidence_ids):
                            raise ATSError("Llama invented an individual evidence ID.") from exc
                        received[criterion.id] = single

                permitted = {refs_to_ids[row.id] for row in chosen.values()}
                for criterion in batch:
                    item = received[criterion.id]
                    # Small models sometimes put EVIDENCED in the explanation
                    # but return REVIEW_REQUIRED as the structured status.
                    # Resolve the contradiction with a focused second check.
                    # Never infer positive score from free-text reasoning alone.
                    if (
                        item.status != "EVIDENCED"
                        and item.reason.strip().upper() == "EVIDENCED"
                        and item.evidence_ids
                    ):
                        relevant = [
                            {"id": refs_to_ids[row.id], "excerpt": row.source_excerpt[:250]}
                            for row in evidence_by_criterion[criterion.id][:5]
                        ]
                        try:
                            checked = self.provider.generate_structured(
                                [
                                    {"role": "system", "content": (
                                        "Your previous answer contradicted itself. "
                                        "Choose a structured status based ONLY on the cited "
                                        "CV excerpts. EVIDENCED requires direct support of "
                                        "the entire requirement; otherwise REVIEW_REQUIRED. "
                                        "Return a short specific reason, never a status word "
                                        "as the reason. Do not invent evidence IDs."
                                    )},
                                    {"role": "user", "content": json.dumps({
                                        "criterion_id": criterion.id,
                                        "requirement": criterion.name,
                                        "details": criterion.description[:160],
                                        "excerpts": relevant,
                                    }, ensure_ascii=False)},
                                ],
                                LlamaCriterionFinding, output_limit=260,
                            ).extraction
                            valid_for_check = {row["id"] for row in relevant}
                            if (
                                checked.criterion_id == criterion.id
                                and all(e in valid_for_check for e in checked.evidence_ids)
                                and (checked.status != "EVIDENCED" or checked.evidence_ids)
                            ):
                                item = checked
                                if (
                                    item.status != "EVIDENCED"
                                    and item.reason.strip().upper() == "EVIDENCED"
                                ):
                                    item = item.model_copy(update={
                                        "reason": "Conflicting model finding; human review required.",
                                    })
                            else:
                                item = item.model_copy(update={
                                    "reason": "Conflicting model finding; human review required.",
                                })
                        except (ATSError, ProviderError):
                            item = item.model_copy(update={
                                "reason": "Conflicting model finding; human review required.",
                            })
                    received[criterion.id] = item
                    if any(ref not in permitted for ref in item.evidence_ids):
                        raise ATSError(
                            "Llama invented an evidence ID or used evidence not in its input."
                        )
                    findings[criterion.id] = item
            breakdown = []
            documented = review = 0
            for criterion in criteria:
                item = findings[criterion.id]
                if len(set(item.evidence_ids)) != len(item.evidence_ids):
                    raise ATSError("Llama returned duplicate evidence references.")
                if any(eid not in valid_refs for eid in item.evidence_ids):
                    raise ATSError("Llama returned unverified or invented evidence references.")
                if item.status == "EVIDENCED" and not item.evidence_ids:
                    raise ATSError("Llama claimed evidence without any verified source.")
                # A source can be relevant but insufficient to establish a
                # qualification. Keep its verified quote for human review;
                # only an EVIDENCED finding contributes positive score weight.
                # Do not fail the entire CV merely because Llama included
                # evidence alongside REVIEW_REQUIRED or NOT_EVIDENCED.
                state = item.status
                if state == "NOT_EVIDENCED" and (
                    partial or omitted_by_criterion[criterion.id]
                ):
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
