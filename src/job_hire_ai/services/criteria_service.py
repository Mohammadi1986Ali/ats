import hashlib
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from job_hire_ai.ai.schemas import CriterionInput, verify_evidence
from job_hire_ai.database.models import CriteriaConfiguration, JobCriterion, Vacancy, now
from job_hire_ai.errors import ATSError


def description_hash(description):
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


CONTENT_FIELDS = (
    "name",
    "category",
    "description",
    "requirement_type",
    "source_excerpt",
    "extraction_notes",
    "uncertainty",
)


def criterion_input(criterion):
    return CriterionInput(
        criterion_id=criterion.criterion_key,
        **{field: getattr(criterion, field) for field in CONTENT_FIELDS},
        weight=criterion.weight,
        display_order=criterion.display_order,
        evidence_acknowledged=criterion.evidence_acknowledged,
        acknowledgement_note=criterion.acknowledgement_note,
    )


class CriteriaService:
    def __init__(self, db):
        self.db = db

    def history(self, vacancy_id):
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(CriteriaConfiguration)
                    .where(CriteriaConfiguration.vacancy_id == vacancy_id)
                    .options(selectinload(CriteriaConfiguration.criteria))
                    .order_by(CriteriaConfiguration.version.desc())
                )
            )

    def get(self, configuration_id):
        with self.db.transaction() as session:
            configuration = session.scalar(
                select(CriteriaConfiguration)
                .where(CriteriaConfiguration.id == configuration_id)
                .options(selectinload(CriteriaConfiguration.criteria))
            )
            if configuration is None:
                raise ATSError("Criteria configuration no longer exists.")
            return configuration

    @staticmethod
    def _vacancy(session, vacancy_id):
        vacancy = session.get(Vacancy, vacancy_id)
        if vacancy is None:
            raise ATSError("Vacancy no longer exists.")
        return vacancy

    @staticmethod
    def _new_draft(session, vacancy):
        existing = session.scalar(
            select(CriteriaConfiguration).where(
                CriteriaConfiguration.vacancy_id == vacancy.id,
                CriteriaConfiguration.status == "DRAFT",
            )
        )
        if existing:
            raise ATSError("A draft already exists. Save or approve that draft first.")
        version = (
            session.scalar(
                select(func.max(CriteriaConfiguration.version)).where(
                    CriteriaConfiguration.vacancy_id == vacancy.id
                )
            )
            or 0
        )
        configuration = CriteriaConfiguration(
            vacancy_id=vacancy.id,
            version=version + 1,
            job_description_hash=description_hash(vacancy.description),
            job_description_snapshot=vacancy.description,
        )
        session.add(configuration)
        session.flush()
        return configuration

    def create_draft(self, vacancy_id):
        with self.db.transaction() as session:
            vacancy = self._vacancy(session, vacancy_id)
            draft = self._new_draft(session, vacancy)
            approved = session.scalar(
                select(CriteriaConfiguration).where(
                    CriteriaConfiguration.vacancy_id == vacancy_id,
                    CriteriaConfiguration.status == "APPROVED",
                )
            )
            if approved:
                draft.analysis_run_id = approved.analysis_run_id
                for old in approved.criteria:
                    values = criterion_input(old)
                    unchanged_source = approved.job_description_hash == draft.job_description_hash
                    draft.criteria.append(
                        self._row(
                            values,
                            draft.job_description_snapshot,
                            created_by=old.created_by,
                            original=old.original_extraction,
                            preserve_acknowledgement=unchanged_source,
                        )
                    )
            session.flush()
            return draft.id

    @staticmethod
    def _row(
        values, description, created_by="MANUAL", original=None, preserve_acknowledgement=True
    ):
        content = {field: getattr(values, field) for field in CONTENT_FIELDS}
        return JobCriterion(
            criterion_key=values.criterion_id or uuid4().hex,
            **content,
            weight=values.weight,
            display_order=values.display_order,
            evidence_status=verify_evidence(values.source_excerpt, description),
            evidence_acknowledged=values.evidence_acknowledged and preserve_acknowledgement,
            acknowledgement_note=values.acknowledgement_note if preserve_acknowledgement else "",
            created_by=created_by,
            original_extraction=original,
        )

    def save_draft(self, configuration_id, criteria, expected_revision):
        values = [CriterionInput.model_validate(value) for value in criteria]
        if len(values) > 50:
            raise ATSError("Use at most 50 criteria.")
        orders = [value.display_order for value in values]
        if len(set(orders)) != len(orders):
            raise ATSError("Each display order must be unique. Use 1, 2, 3… to reorder criteria.")
        keys = [value.criterion_id for value in values if value.criterion_id]
        if len(keys) != len(set(keys)):
            raise ATSError("Duplicate criterion references are not allowed.")
        with self.db.transaction() as session:
            draft = self._editable(session, configuration_id, expected_revision)
            previous = {row.criterion_key: row for row in draft.criteria}
            new_rows = []
            for value in values:
                old = previous.get(value.criterion_id)
                if value.criterion_id and old is None:
                    raise ATSError("Unknown criterion reference. Refresh the editor.")
                origin, original, keep_ack = "MANUAL", None, True
                if old:
                    original, origin = old.original_extraction, old.created_by
                    content = {field: getattr(value, field) for field in CONTENT_FIELDS}
                    if original and content != original:
                        origin = "AI_MODIFIED"
                    keep_ack = value.source_excerpt == old.source_excerpt
                    # An edited excerpt must be reviewed again. Verification is always recomputed.
                new_rows.append(
                    self._row(value, draft.job_description_snapshot, origin, original, keep_ack)
                )
            draft.criteria.clear()
            session.flush()  # release old criterion keys before inserting edited rows
            draft.criteria.extend(new_rows)
            draft.revision += 1
            session.flush()
            return draft.revision

    @staticmethod
    def _editable(session, configuration_id, expected_revision):
        draft = session.get(CriteriaConfiguration, configuration_id)
        if draft is None:
            raise ATSError("Configuration no longer exists.")
        if draft.status != "DRAFT":
            raise ATSError("Approved versions are immutable. Create a new draft to make changes.")
        if draft.revision != expected_revision:
            raise ATSError(
                "This draft changed in another session. Refresh before editing or approving."
            )
        return draft

    @staticmethod
    def issues(configuration, current_description):
        issues = []
        if configuration.job_description_hash != description_hash(current_description):
            issues.append("The job description changed. Re-analyze it before approving this draft.")
        if not configuration.criteria:
            issues.append("Add at least one criterion.")
        if any(row.weight is None for row in configuration.criteria):
            issues.append("Assign a whole-number percentage weight to every criterion.")
        if sum(row.weight or 0 for row in configuration.criteria) != 100:
            issues.append("Weights must total exactly 100%.")
        for row in configuration.criteria:
            if (
                verify_evidence(row.source_excerpt, configuration.job_description_snapshot)
                != "VERIFIED"
            ):
                if not row.evidence_acknowledged or not row.acknowledgement_note.strip():
                    issues.append(
                        f"{row.name}: correct the source excerpt or acknowledge the evidence issue "
                        "with a review note."
                    )
        return issues

    def approve(self, configuration_id, expected_revision, reviewer=None):
        reviewer = (reviewer or "").strip() or None
        if reviewer and len(reviewer) > 200:
            raise ATSError("Reviewer identifier must be at most 200 characters.")
        with self.db.transaction() as session:
            draft = self._editable(session, configuration_id, expected_revision)
            vacancy = self._vacancy(session, draft.vacancy_id)
            issues = self.issues(draft, vacancy.description)
            if issues:
                raise ATSError(" ".join(issues))
            previous = session.scalar(
                select(CriteriaConfiguration).where(
                    CriteriaConfiguration.vacancy_id == draft.vacancy_id,
                    CriteriaConfiguration.status == "APPROVED",
                )
            )
            if previous:
                previous.status = "SUPERSEDED"
                session.flush()
            draft.status = "APPROVED"
            draft.approved_at, draft.approved_by = now(), reviewer
            session.flush()
            return draft.version
