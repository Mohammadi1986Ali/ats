import time

from sqlalchemy import select

from job_hire_ai.ai.prompts import PROMPT_VERSION
from job_hire_ai.ai.provider import ProviderError
from job_hire_ai.ai.schemas import CriterionInput
from job_hire_ai.database.models import CriteriaConfiguration, JobAnalysisRun, now
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import CriteriaService, description_hash


class JobAnalysisService:
    def __init__(self, db, provider, settings):
        self.db, self.provider, self.settings = db, provider, settings

    def runs(self, vacancy_id):
        with self.db.transaction() as session:
            return list(
                session.scalars(
                    select(JobAnalysisRun)
                    .where(JobAnalysisRun.vacancy_id == vacancy_id)
                    .order_by(JobAnalysisRun.id.desc())
                    .limit(10)
                )
            )

    def analyze(self, vacancy_id, replace_draft=False):
        with self.db.transaction() as session:
            vacancy = CriteriaService._vacancy(session, vacancy_id)
            description = vacancy.description
            existing = session.scalar(
                select(CriteriaConfiguration).where(
                    CriteriaConfiguration.vacancy_id == vacancy_id,
                    CriteriaConfiguration.status == "DRAFT",
                )
            )
            if existing and not replace_draft:
                raise ATSError(
                    "A draft exists. Explicitly confirm replacement before re-analyzing."
                )
            existing_id = existing.id if existing else None
            existing_revision = existing.revision if existing else None
            run = JobAnalysisRun(
                vacancy_id=vacancy_id,
                model_name=self.settings.model,
                prompt_version=PROMPT_VERSION,
                input_description_hash=description_hash(description),
                inference_options=self.settings.options(),
            )
            session.add(run)
            session.flush()
            run_id = run.id
        started = time.perf_counter()
        try:
            output = self.provider.analyze(description)
            with self.db.transaction() as session:
                vacancy = CriteriaService._vacancy(session, vacancy_id)
                if description_hash(vacancy.description) != description_hash(description):
                    raise ATSError("Job description changed during analysis. Analyze it again.")
                if existing_id:
                    draft = CriteriaService._editable(session, existing_id, existing_revision)
                    draft.criteria.clear()
                    draft.revision += 1
                else:
                    draft = CriteriaService._new_draft(session, vacancy)
                draft.job_description_hash = description_hash(description)
                draft.job_description_snapshot = description
                draft.analysis_run_id = run_id
                for index, extracted in enumerate(output.extraction.criteria, 1):
                    original = extracted.model_dump(mode="json")
                    value = CriterionInput(**original, display_order=index)
                    draft.criteria.append(
                        CriteriaService._row(value, description, created_by="AI", original=original)
                    )
                run = session.get(JobAnalysisRun, run_id)
                run.model_digest, run.model_name = output.digest, output.model
                run.status, run.completed_at = "SUCCEEDED", now()
                run.duration_ms = int((time.perf_counter() - started) * 1000)
                session.flush()
                return draft.id
        except Exception as exc:
            summary = (
                str(exc)
                if isinstance(exc, ATSError)
                else (
                    "Analysis could not be saved. Check the local database and service, then retry."
                )
            )
            with self.db.transaction() as session:
                run = session.get(JobAnalysisRun, run_id)
                run.status, run.completed_at = "FAILED", now()
                run.duration_ms = int((time.perf_counter() - started) * 1000)
                run.error_summary = summary[:500]
            raise ProviderError(summary) from None
