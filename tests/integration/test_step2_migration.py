from alembic import command
from alembic.config import Config

from job_hire_ai.database.models import Candidate, CandidateDocument, JobApplication
from job_hire_ai.runtime import Runtime
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.settings import ROOT, Settings


def test_step1_data_and_cv_survive_upgrade_and_downgrade(tmp_path, pdf):
    settings = Settings(data_dir=tmp_path)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "5e9be2fa29c7")
    ats = Runtime(settings)
    try:
        vacancy = ats.vacancies.create(VacancyInput(title="Existing", description="Existing role"))
        # Seed genuine pre-pipeline records while the older schema is active.
        # The modern upload service must only run after the stage-table migration.
        extension, text_status = ats.documents.validator.validate("existing.pdf", pdf)
        storage_key = ats.storage.save(pdf, extension)
        with ats.db.transaction() as session:
            candidate = Candidate()
            session.add(candidate)
            session.flush()
            application = JobApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
            session.add(application)
            session.flush()
            document = CandidateDocument(
                application_id=application.id,
                vacancy_id=vacancy.id,
                original_filename="existing.pdf",
                storage_key=storage_key,
                file_type=extension,
                file_size=len(pdf),
                checksum=ats.storage.checksum(pdf),
                text_extraction_status=text_status,
            )
            session.add(document)
            session.flush()
            document_id = document.id
        command.upgrade(config, "head")
        command.check(config)
        assert ats.documents.download(document_id)[1] == pdf
        assert ats.vacancies.list()[0].id == vacancy.id
        assert ats.criteria.history(vacancy.id) == []
        command.downgrade(config, "5e9be2fa29c7")
        assert ats.documents.download(document_id)[1] == pdf
        assert len(ats.applications.list()) == 1
        command.upgrade(config, "head")
        assert ats.documents.download(document_id)[1] == pdf
    finally:
        ats.db.engine.dispose()
