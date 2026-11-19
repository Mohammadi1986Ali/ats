"""Recover legacy jobs that never received a weighting choice at JD entry.

Runs locally on pipeline-worker startup without invoking Ollama or changing CV
files, profile versions, embeddings, or past match/score snapshots.
"""

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from job_hire_ai.ai.literal_cv_fallback import KNOWN_TERMS
from job_hire_ai.ai.schemas import Category, CriterionInput, RequirementType
from job_hire_ai.database.models import (
    CandidateDocument,
    CriteriaConfiguration,
    JobApplication,
    Vacancy,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import CriteriaService, description_hash

logger = logging.getLogger(__name__)

def _word(term):
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def literal_jd_criteria(description: str) -> list[CriterionInput]:
    """Source-verified technical criteria only; no invented requirements.

    Each criterion is the verbatim technology token in a JD sentence.
    A tiny local model is not needed to support a safe evidence-coverage report.
    """
    entries = []
    used = set()
    chunks = description.splitlines()
    if len(chunks) == 1:
        chunks = re.split(r"(?<=[.!?])\s+", description)
    for chunk in chunks:
        for term in sorted(KNOWN_TERMS, key=len, reverse=True):
            match = _word(term).search(chunk)
            if match is None or term.casefold() in used:
                continue
            # Avoid overlapping aliases: "Apache Kafka" rather than "Kafka".
            if any(
                term.casefold() in prior.casefold() and term.casefold() != prior.casefold()
                for prior in used
            ):
                continue
            if any(_word(old).search(term) for old in used):
                continue
            used.add(term.casefold())
            required = bool(re.search(
                r"\b(required|must|mandatory|essential)\b", chunk, re.IGNORECASE
            ))
            entries.append(CriterionInput(
                name=match.group(),
                category=Category.TECHNICAL_SKILLS,
                description="Explicit technology mentioned in the job description.",
                requirement_type=(
                    RequirementType.REQUIRED if required else RequirementType.UNCLEAR
                ),
                source_excerpt=match.group(),
                extraction_notes="Literal JD token; conservative system-default recovery.",
                uncertainty=(
                    "JD wording alone does not establish proficiency, experience, "
                    "or hiring suitability."
                ),
                display_order=len(entries) + 1,
            ))
            if len(entries) >= 50:
                return entries
    return entries


def recover_legacy_jd_weights(db) -> dict[int, str]:
    """Activate missing default criteria for jobs with existing uploaded CVs.

    Explicit user-defined manual rules and human-edited drafts remain untouched.
    Errors are logged; re-running is idempotent once an active config exists.
    """
    outcomes = {}
    with db.transaction() as session:
        vacancy_ids = list(session.scalars(
            select(Vacancy.id)
            .join(JobApplication, JobApplication.vacancy_id == Vacancy.id)
            .join(CandidateDocument, CandidateDocument.application_id == JobApplication.id)
            .where(Vacancy.status == "Active", JobApplication.status == "Uploaded")
            .distinct()
        ))
    service = CriteriaService(db)
    for vacancy_id in vacancy_ids:
        try:
            with db.transaction() as session:
                vacancy = session.get(Vacancy, vacancy_id)
                if vacancy is None:
                    continue
                # Empty manual rules = legacy job created before JD-time weighting.
                if vacancy.weighting_mode == "MANUAL" and vacancy.weighting_rules.strip():
                    continue
                current = session.scalar(select(CriteriaConfiguration.id).where(
                    CriteriaConfiguration.vacancy_id == vacancy_id,
                    CriteriaConfiguration.status == "APPROVED",
                    CriteriaConfiguration.job_description_hash
                    == description_hash(vacancy.description),
                ))
                if current is not None:
                    continue
                draft = session.scalar(
                    select(CriteriaConfiguration)
                    .where(CriteriaConfiguration.vacancy_id == vacancy_id,
                           CriteriaConfiguration.status == "DRAFT")
                    .options(selectinload(CriteriaConfiguration.criteria))
                )
                if draft is not None and any(
                    row.created_by != "AI" or row.evidence_acknowledged
                    for row in draft.criteria
                ):
                    outcomes[vacancy_id] = "Manual draft requires owner review"
                    continue
                needs_new = (
                    draft is None
                    or draft.job_description_hash != description_hash(vacancy.description)
                    or not draft.criteria
                    or any(row.evidence_status != "VERIFIED" for row in draft.criteria)
                )
                if needs_new:
                    extracted = literal_jd_criteria(vacancy.description)
                    if not extracted:
                        outcomes[vacancy_id] = "No explicit supported JD skills found"
                        continue
                    if draft is None:
                        draft = CriteriaService._new_draft(session, vacancy)
                    else:
                        draft.criteria.clear()
                        session.flush()
                        draft.revision += 1
                        draft.job_description_hash = description_hash(vacancy.description)
                        draft.job_description_snapshot = vacancy.description
                        draft.analysis_run_id = None
                    draft.criteria.extend(
                        CriteriaService._row(value, vacancy.description, created_by="AI")
                        for value in extracted
                    )
                draft_id = draft.id
                # Persist this backfill as a system default, never HR approval.
                vacancy.weighting_mode = "DEFAULT"
            service.activate_entered_weights(draft_id, mode="DEFAULT")
            outcomes[vacancy_id] = "Activated default JD criteria"
        except ATSError as exc:
            outcomes[vacancy_id] = str(exc)
            logger.warning("JD activation skipped for job %s: %s", vacancy_id, exc)
    return outcomes
