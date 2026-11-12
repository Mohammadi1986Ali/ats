from alembic import command
from alembic.config import Config

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
        result = ats.documents.upload_batch(vacancy.id, [("existing.pdf", pdf)])[0]
        assert result.success
        document = ats.applications.list()[0].documents[0]
        command.upgrade(config, "head")
        command.check(config)
        assert ats.documents.download(document.id)[1] == pdf
        assert ats.vacancies.list()[0].id == vacancy.id
        assert ats.criteria.history(vacancy.id) == []
        command.downgrade(config, "5e9be2fa29c7")
        assert ats.documents.download(document.id)[1] == pdf
        assert len(ats.applications.list()) == 1
        command.upgrade(config, "head")
        assert ats.documents.download(document.id)[1] == pdf
    finally:
        ats.db.engine.dispose()
