"""Optional live PostgreSQL test, enabled in GitHub Actions with a local service."""

import io
import os

import pytest
from alembic import command
from alembic.config import Config
from docx import Document
from sqlalchemy import func, select, text

from job_hire_ai.database.models import CandidateDocument, DocumentTextExtraction, Vacancy
from job_hire_ai.database.session import Database
from job_hire_ai.runtime import Runtime
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.embedding_service import EmbeddingService, chunk_jd
from job_hire_ai.settings import ROOT, Settings
from scripts.migrate_sqlite_to_postgres import migrate


def test_sqlite_data_copies_to_fresh_postgres_without_deleting_source(tmp_path):
    target_url = os.getenv("ATS_TEST_POSTGRES_URL")
    if not target_url:
        pytest.skip("Live PostgreSQL URL not configured")
    source = tmp_path / "ats.sqlite3"
    source_url = "sqlite:///" + source.as_posix()
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url_override=source_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = source_url
    command.upgrade(config, "head")
    ats = Runtime(settings)
    try:
        vacancy = ats.vacancies.create(
            VacancyInput(title="Synthetic Java vacancy", description="Java and SQL")
        )
        cv = Document()
        cv.add_paragraph("Synthetic candidate with Java, SQL, and Spring Boot experience.")
        output = io.BytesIO()
        cv.save(output)
        uploaded = ats.documents.upload_batch(vacancy.id, [("sample.docx", output.getvalue())])
        assert uploaded[0].success
        assert ats.pipeline.process_next()
        assert not ats.pipeline.process_next()
    finally:
        ats.db.engine.dispose()

    preview = migrate(source, target_url, tmp_path)
    assert preview["applied"] is False
    assert preview["documents_verified"] == 1
    report = migrate(source, target_url, tmp_path, apply=True)
    assert report["applied"] is True
    assert report["sqlite_backup_sha256"]
    assert source.is_file()
    with Database(target_url).engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Vacancy)) == 1
        assert connection.scalar(select(func.count()).select_from(CandidateDocument)) == 1
        assert connection.scalar(select(func.count()).select_from(DocumentTextExtraction)) == 1
        extension = connection.scalar(
            text("SELECT extname FROM pg_extension WHERE extname='vector'")
        )
        assert extension == "vector"
    # Phase 3.2: exercise actual pgvector(768) writes and reads on PostgreSQL.
    class FakeEmbedder:
        model = "nomic-embed-text"

        def embed(self, texts):
            return [[0.25] * 768 for _ in texts]

    postgres = Database(target_url)
    try:
        indexer = EmbeddingService(postgres, FakeEmbedder())
        indexer.index(
            source_type="JD", vacancy_id=1, document_id=None,
            source="Java and SQL", chunks=chunk_jd("Java and SQL"),
        )
        with postgres.engine.connect() as connection:
            dimensions = connection.scalar(
                text("SELECT vector_dims(embedding) FROM embedding_chunks LIMIT 1")
            )
            assert dimensions == 768
    finally:
        postgres.engine.dispose()
    # Idempotency/safety: never import into a populated target.
    with pytest.raises(RuntimeError, match="not empty"):
        migrate(source, target_url, tmp_path, apply=True)
