"""Conservative job-criterion to CV evidence matching, without suitability scores.

Only exact, explicitly documented criterion wording within a supported profile
claim AND its verified original source quote can establish EVIDENCED. Semantic
similarity is not treated as proof, and absence is not a negative conclusion.
"""

import json
import re

from sqlalchemy import select

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction
from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    CriteriaConfiguration,
    EvidenceReference,
    JobApplication,
    JobCriterion,
    JobMatchResult,
    Vacancy,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import description_hash

MATCH_VERSION = "literal-verified-v1"
SEARCHABLE_FIELDS = (
    "name", "original_wording", "employer", "job_title", "degree",
    "field_of_study", "institution", "project_name", "description",
    "technologies", "responsibilities", "certifications", "outcomes",
    "language", "proficiency", "information",
)


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Case-insensitive literal phrase with word boundaries, not fuzzy inference."""
    needle = " ".join(phrase.casefold().split())
    text = " ".join(haystack.casefold().split())
    return bool(needle) and bool(
        re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", text)
    )


def _claim_values(claim) -> list[str]:
    result = []
    for field in SEARCHABLE_FIELDS:
        value = getattr(claim, field, None)
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, list):
            result.extend(item for item in value if isinstance(item, str))
    return result


class JobMatchingService:
    """Persist reproducible, candidate-scoped matches against approved JD criteria."""

    def __init__(self, db):
        self.db = db

    def ready(self, vacancy_id: int) -> bool:
        with self.db.transaction() as session:
            vacancy = session.get(Vacancy, vacancy_id)
            if vacancy is None:
                return False
            configuration = session.scalar(
                select(CriteriaConfiguration).where(
                    CriteriaConfiguration.vacancy_id == vacancy_id,
                    CriteriaConfiguration.status == "APPROVED",
                    CriteriaConfiguration.job_description_hash
                    == description_hash(vacancy.description),
                )
            )
            return configuration is not None

    def match_document(self, document_id: int) -> JobMatchResult:
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None:
                raise ATSError("Candidate CV no longer exists.")
            application = session.get(JobApplication, document.application_id)
            if application is None or application.status != "Uploaded":
                raise ATSError("Removed applications cannot be matched.")
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
                raise ATSError("Approve criteria for the current job description first.")

            profile = session.scalar(
                select(CandidateProfile)
                .where(CandidateProfile.document_id == document_id)
                .order_by(CandidateProfile.id.desc())
                .limit(1)
            )
            if profile is None:
                raise ATSError("Extract a source-backed CV profile before job matching.")
            prior = session.scalar(
                select(JobMatchResult).where(
                    JobMatchResult.document_id == document_id,
                    JobMatchResult.configuration_id == configuration.id,
                    JobMatchResult.profile_id == profile.id,
                )
            )
            if prior is not None:
                return prior
            try:
                payload = CVExtraction.model_validate_json(
                    json.dumps(profile.original_extraction)
                )
            except (ValueError, TypeError) as exc:
                raise ATSError("Candidate profile is invalid; matching was not saved.") from exc

            saved_evidence = list(
                session.scalars(
                    select(EvidenceReference).where(
                        EvidenceReference.profile_id == profile.id,
                        EvidenceReference.document_id == document_id,
                        EvidenceReference.verification_status == "VERIFIED",
                    )
                )
            )
            references = {}
            for evidence in saved_evidence:
                references.setdefault(evidence.field_path, []).append(evidence)
            claims = []
            for section in PROFILE_SECTIONS:
                for index, claim in enumerate(getattr(payload, section)):
                    if claim.claim_status != "SUPPORTED":
                        continue
                    claims.append((f"{section}.{index}", _claim_values(claim)))

            criteria = list(
                session.scalars(
                    select(JobCriterion)
                    .where(JobCriterion.configuration_id == configuration.id)
                    .order_by(JobCriterion.display_order, JobCriterion.id)
                )
            )
            if not criteria:
                raise ATSError("Approved job configuration has no criteria.")
            findings = []
            for criterion in criteria:
                hits = []
                if criterion.evidence_status == "VERIFIED" or (
                    criterion.evidence_acknowledged
                ):
                    for path, claim_values in claims:
                        if not any(
                            _contains_phrase(value, criterion.name)
                            for value in claim_values
                        ):
                            continue
                        for evidence in references.get(path, []):
                            if _contains_phrase(evidence.source_excerpt, criterion.name):
                                hits.append({
                                    "field_path": path,
                                    "evidence_id": evidence.id,
                                    "document_id": document_id,
                                    "source_reference": evidence.source_reference,
                                    "page_number": evidence.page_number,
                                    "source_excerpt": evidence.source_excerpt,
                                })
                findings.append({
                    "criterion_id": criterion.id,
                    "criterion_key": criterion.criterion_key,
                    "name": criterion.name,
                    "requirement_type": criterion.requirement_type,
                    "status": (
                        "REVIEW_REQUIRED"
                        if criterion.evidence_status != "VERIFIED"
                        and not criterion.evidence_acknowledged
                        else "EVIDENCED" if hits else "NOT_EVIDENCED"
                    ),
                    "evidence": hits,
                    "method": MATCH_VERSION,
                })
            # This is a traceable lookup, not a prediction of job suitability.
            result = JobMatchResult(
                document_id=document_id,
                vacancy_id=document.vacancy_id,
                configuration_id=configuration.id,
                profile_id=profile.id,
                criteria_results=findings,
            )
            session.add(result)
            session.flush()
            return result

    def list_for_vacancy(self, vacancy_id: int) -> list[JobMatchResult]:
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(JobMatchResult)
                    .join(CandidateDocument, JobMatchResult.document_id == CandidateDocument.id)
                    .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                    .where(
                        JobMatchResult.vacancy_id == vacancy_id,
                        JobApplication.status == "Uploaded",
                    )
                    .order_by(JobMatchResult.id.desc())
                )
            )
