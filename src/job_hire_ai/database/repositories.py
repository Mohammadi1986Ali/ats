from sqlalchemy import select
from sqlalchemy.orm import selectinload

from job_hire_ai.database.models import Candidate, CandidateDocument, JobApplication, Vacancy


class Repository:
    @staticmethod
    def vacancies(session, status=None):
        query = select(Vacancy).order_by(Vacancy.created_at.desc(), Vacancy.id.desc())
        if status:
            query = query.where(Vacancy.status == status)
        return list(session.scalars(query))

    @staticmethod
    def candidates(session):
        return list(session.scalars(select(Candidate).order_by(Candidate.id)))

    @staticmethod
    def applications(session, vacancy_id=None):
        query = (
            select(JobApplication)
            .options(
                selectinload(JobApplication.candidate),
                selectinload(JobApplication.documents),
                selectinload(JobApplication.vacancy),
            )
            .order_by(JobApplication.created_at.desc(), JobApplication.id.desc())
        )
        if vacancy_id is not None:
            query = query.where(JobApplication.vacancy_id == vacancy_id)
        return list(session.scalars(query))

    @staticmethod
    def duplicate(session, vacancy_id, checksum):
        return (
            session.scalar(
                select(CandidateDocument.id).where(
                    CandidateDocument.vacancy_id == vacancy_id,
                    CandidateDocument.checksum == checksum,
                )
            )
            is not None
        )
