import logging

from job_hire_ai.database.models import JobApplication
from job_hire_ai.database.repositories import Repository
from job_hire_ai.errors import ATSError

logger = logging.getLogger(__name__)


class ApplicationService:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage

    def list(self, vacancy_id=None):
        with self.db.transaction() as session:
            return Repository.applications(session, vacancy_id)

    def remove(self, application_id: int):
        # Durable two-stage deletion: retain metadata until ALL bytes have been deleted.
        # A failed delete is visible as Removed + pending files and can be retried safely.
        with self.db.transaction() as session:
            application = session.get(JobApplication, application_id)
            if application is None:
                raise ATSError("Application no longer exists.")
            application.status = "Removed"
            keys = [document.storage_key for document in application.documents]
        try:
            for key in keys:
                self.storage.delete(key)
        except OSError as exc:
            logger.error("Application file deletion failed; retry required (id=%s)", application_id)
            raise ATSError("Removal pending: a file could not be deleted. Retry removal.") from exc
        with self.db.transaction() as session:
            application = session.get(JobApplication, application_id)
            application.documents.clear()
