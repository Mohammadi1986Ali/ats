import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from job_hire_ai.database.models import Candidate, CandidateDocument, JobApplication, Vacancy
from job_hire_ai.database.repositories import Repository
from job_hire_ai.documents import FileValidator
from job_hire_ai.errors import ATSError
from job_hire_ai.schemas import UploadResult

logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage
        self.validator = FileValidator(storage)

    def upload_batch(self, vacancy_id, files, candidate_id=None):
        if not 1 <= len(files) <= 100:
            raise ATSError("Select between 1 and 100 CVs per batch.")
        results = []
        for filename, data in files:
            try:
                application_id = self._upload(vacancy_id, filename, data, candidate_id)
                results.append(
                    UploadResult(
                        filename=filename,
                        success=True,
                        message="Uploaded",
                        application_id=application_id,
                    )
                )
            except ATSError as exc:
                results.append(UploadResult(filename=filename, success=False, message=str(exc)))
            except Exception:
                logger.error("Upload operation failed; no CV content logged")
                results.append(
                    UploadResult(
                        filename=filename,
                        success=False,
                        message="Upload failed. Check local database and storage, then retry.",
                    )
                )
        return results

    def _upload(self, vacancy_id, filename, data, candidate_id):
        extension, text_status = self.validator.validate(filename, data)
        checksum = self.storage.checksum(data)
        key = None
        try:
            with self.db.transaction() as session:
                vacancy = session.get(Vacancy, vacancy_id)
                if vacancy is None or vacancy.status != "Active":
                    raise ATSError("Select an active vacancy to upload CVs.")
                if Repository.duplicate(session, vacancy_id, checksum):
                    raise ATSError(
                        "Duplicate CV: identical content already exists in this vacancy."
                    )
                if candidate_id is None:
                    candidate = Candidate()
                    session.add(candidate)
                    session.flush()
                else:
                    candidate = session.get(Candidate, candidate_id)
                    if candidate is None:
                        raise ATSError("Selected candidate no longer exists.")
                application = session.scalar(
                    select(JobApplication).where(
                        JobApplication.candidate_id == candidate.id,
                        JobApplication.vacancy_id == vacancy_id,
                        JobApplication.status == "Uploaded",
                    )
                )
                if application is None:
                    application = JobApplication(candidate_id=candidate.id, vacancy_id=vacancy_id)
                    session.add(application)
                    session.flush()
                key = self.storage.save(data, extension)
                session.add(
                    CandidateDocument(
                        application_id=application.id,
                        vacancy_id=vacancy_id,
                        original_filename=filename,
                        storage_key=key,
                        file_type=extension,
                        file_size=len(data),
                        checksum=checksum,
                        text_extraction_status=text_status,
                    )
                )
                session.flush()
                application_id = application.id
            return application_id
        except Exception as exc:
            if key:
                try:
                    self.storage.delete(key)
                except OSError:
                    logger.error("Rollback file cleanup failed; local storage audit required")
            if isinstance(exc, IntegrityError):
                raise ATSError(
                    "Upload conflict: duplicate CV or record changed. Refresh and retry."
                ) from exc
            raise

    def download(self, document_id):
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None or document.application.status != "Uploaded":
                raise ATSError("Document is no longer available.")
            data = self.storage.retrieve(document.storage_key)
            if self.storage.checksum(data) != document.checksum:
                raise ATSError("Stored file integrity check failed. Restore from local backup.")
            return document.original_filename, data, document.file_type
