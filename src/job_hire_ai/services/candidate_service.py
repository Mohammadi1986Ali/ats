from job_hire_ai.database.models import Candidate
from job_hire_ai.database.repositories import Repository


class CandidateService:
    def __init__(self, db):
        self.db = db

    def list(self):
        with self.db.transaction() as session:
            return Repository.candidates(session)

    def create(self):
        with self.db.transaction() as session:
            candidate = Candidate()
            session.add(candidate)
            session.flush()
            return candidate
