import io

import pymupdf
import pytest
from alembic import command
from alembic.config import Config
from docx import Document

from job_hire_ai.runtime import Runtime
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.settings import ROOT, Settings


@pytest.fixture
def ats(tmp_path):
    runtime = Runtime(Settings(data_dir=tmp_path))
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = runtime.db.engine.url.render_as_string()
    command.upgrade(config, "head")
    yield runtime
    runtime.db.engine.dispose()


@pytest.fixture
def vacancy(ats):
    return ats.vacancies.create(
        VacancyInput(title="Senior Java Developer", description="Build and maintain Java services.")
    )


@pytest.fixture
def pdf():
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Synthetic CV: Java and SQL experience.")
        return document.tobytes()


@pytest.fixture
def docx():
    document = Document()
    document.add_paragraph("Synthetic applicant document for automated testing.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
