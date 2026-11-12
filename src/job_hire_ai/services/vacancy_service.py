from job_hire_ai.database.models import Vacancy
from job_hire_ai.database.repositories import Repository
from job_hire_ai.errors import ATSError
from job_hire_ai.schemas import VacancyInput


class VacancyService:
    def __init__(self, db):
        self.db = db

    def list(self, status=None):
        with self.db.transaction() as session:
            return Repository.vacancies(session, status)

    def create(self, values: VacancyInput):
        with self.db.transaction() as session:
            vacancy = Vacancy(**values.model_dump())
            session.add(vacancy)
            session.flush()
            return vacancy

    def edit(self, vacancy_id: int, values: VacancyInput):
        with self.db.transaction() as session:
            vacancy = session.get(Vacancy, vacancy_id)
            if vacancy is None:
                raise ATSError("Vacancy no longer exists.")
            for key, value in values.model_dump().items():
                setattr(vacancy, key, value)

    def set_status(self, vacancy_id: int, status: str):
        if status not in {"Active", "Archived"}:
            raise ATSError("Invalid vacancy status.")
        with self.db.transaction() as session:
            vacancy = session.get(Vacancy, vacancy_id)
            if vacancy is None:
                raise ATSError("Vacancy no longer exists.")
            vacancy.status = status
