"""Deterministic weighted evidence coverage for HR review, not candidate ranking.

The result measures the fraction of *approved job criteria weights* for
which literal verified evidence was found. Missing evidence is unknown,
not evidence that a candidate lacks the qualification.
"""

from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    CandidateScoreSnapshot,
    CriteriaConfiguration,
    JobApplication,
    JobCriterion,
    JobMatchResult,
    Vacancy,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import description_hash

METHOD_VERSION = "verified-weight-coverage-v1"
STATES = {"EVIDENCED", "NOT_EVIDENCED", "REVIEW_REQUIRED"}


class CandidateScoringService:
    """Save an immutable, traceable breakdown tied to a specific matching snapshot."""

    def __init__(self, db):
        self.db = db

    def score_document(self, document_id: int) -> CandidateScoreSnapshot:
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None:
                raise ATSError("CV no longer exists.")
            application = session.get(JobApplication, document.application_id)
            if application is None or application.status != "Uploaded":
                raise ATSError("Removed applications cannot be scored.")
            vacancy = session.get(Vacancy, document.vacancy_id)
            configuration = session.scalar(
                select(CriteriaConfiguration).where(
                    CriteriaConfiguration.vacancy_id == document.vacancy_id,
                    CriteriaConfiguration.status == "APPROVED",
                    CriteriaConfiguration.job_description_hash
                    == description_hash(vacancy.description),
                )
            )
            if configuration is None:
                raise ATSError("Current job criteria are not approved.")
            latest_profile_id = session.scalar(
                select(CandidateProfile.id)
                .where(CandidateProfile.document_id == document_id)
                .order_by(CandidateProfile.id.desc())
                .limit(1)
            )
            match = session.scalar(
                select(JobMatchResult).where(
                    JobMatchResult.document_id == document_id,
                    JobMatchResult.vacancy_id == vacancy.id,
                    JobMatchResult.configuration_id == configuration.id,
                    JobMatchResult.profile_id == latest_profile_id,
                )
            )
            if match is None:
                raise ATSError("Matching for the current approved criteria is required.")
            existing = session.scalar(
                select(CandidateScoreSnapshot).where(
                    CandidateScoreSnapshot.match_result_id == match.id,
                    CandidateScoreSnapshot.method_version == METHOD_VERSION,
                )
            )
            if existing is not None:
                return existing
            criteria = list(
                session.scalars(
                    select(JobCriterion)
                    .where(JobCriterion.configuration_id == configuration.id)
                    .order_by(JobCriterion.display_order, JobCriterion.id)
                )
            )
            if not criteria or any(
                row.weight is None or not isinstance(row.weight, int)
                or not 0 <= row.weight <= 100 for row in criteria
            ):
                raise ATSError("Approved criteria have invalid or missing weights.")
            if sum(criterion.weight for criterion in criteria) != 100:
                raise ATSError("Approved criteria weights must total 100.")
            findings = match.criteria_results
            if not isinstance(findings, list) or len(findings) != len(criteria):
                raise ATSError("Stored matching breakdown is incomplete.")
            matched = {}
            for finding in findings:
                if not isinstance(finding, dict):
                    raise ATSError("Stored matching breakdown is malformed.")
                criterion_id = finding.get("criterion_id")
                if criterion_id in matched or finding.get("status") not in STATES:
                    raise ATSError("Stored matching breakdown has duplicate/unknown criteria.")
                matched[criterion_id] = finding
            breakdown = []
            totals = {"EVIDENCED": 0, "NOT_EVIDENCED": 0, "REVIEW_REQUIRED": 0}
            for criterion in criteria:
                finding = matched.get(criterion.id)
                if (
                    finding is None
                    or finding.get("criterion_key") != criterion.criterion_key
                    or finding.get("name") != criterion.name
                    or finding.get("requirement_type") != criterion.requirement_type
                ):
                    raise ATSError("Matching snapshot does not match approved job criteria.")
                state = finding["status"]
                evidence = finding.get("evidence")
                if not isinstance(evidence, list):
                    raise ATSError("Match evidence must be a list.")
                if state == "EVIDENCED" and not evidence:
                    raise ATSError("Evidenced criterion lacks source references.")
                if state != "EVIDENCED" and evidence:
                    raise ATSError("Non-evidenced criterion cannot carry positive evidence.")
                totals[state] += criterion.weight
                breakdown.append({
                    "criterion_id": criterion.id,
                    "criterion_key": criterion.criterion_key,
                    "name": criterion.name,
                    "requirement_type": criterion.requirement_type,
                    "weight": criterion.weight,
                    "evidence_status": state,
                    "documented_points": criterion.weight if state == "EVIDENCED" else 0,
                    "evidence": evidence,
                })
            # These are observed evidence-coverage buckets, NOT a suitability
            # score; unknown claims never become negative hiring conclusions.
            result = CandidateScoreSnapshot(
                match_result_id=match.id,
                document_id=document_id,
                vacancy_id=vacancy.id,
                configuration_id=configuration.id,
                profile_id=match.profile_id,
                method_version=METHOD_VERSION,
                documented_weight=totals["EVIDENCED"],
                unverified_weight=totals["NOT_EVIDENCED"],
                review_weight=totals["REVIEW_REQUIRED"],
                criterion_breakdown=breakdown,
            )
            session.add(result)
            session.flush()
            return result

    def list_for_vacancy(self, vacancy_id: int) -> list[CandidateScoreSnapshot]:
        with self.db.transaction() as session:
            vacancy = session.get(Vacancy, vacancy_id)
            if vacancy is None:
                return []
            return list(
                session.scalars(
                    select(CandidateScoreSnapshot)
                    .join(
                        CandidateDocument,
                        CandidateScoreSnapshot.document_id == CandidateDocument.id,
                    )
                    .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                    .join(
                        CriteriaConfiguration,
                        CandidateScoreSnapshot.configuration_id == CriteriaConfiguration.id,
                    )
                    .where(
                        CandidateScoreSnapshot.vacancy_id == vacancy_id,
                        JobApplication.status == "Uploaded",
                        CriteriaConfiguration.status == "APPROVED",
                        CriteriaConfiguration.job_description_hash
                        == description_hash(vacancy.description),
                    )
                    .order_by(CandidateScoreSnapshot.document_id, CandidateScoreSnapshot.id.desc())
                )
            )
