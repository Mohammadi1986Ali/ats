"""Phase 3.1 contract tests. No database service or CV data required."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from job_hire_ai.database.models import CriteriaConfiguration, JobCriterion
from job_hire_ai.database.session import Database
from job_hire_ai.settings import ROOT, Settings


def test_sqlite_is_default_and_can_be_overridden(tmp_path):
    fallback = Settings(_env_file=None, data_dir=tmp_path)
    assert fallback.database_url.startswith("sqlite:///")
    overridden = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url_override="postgresql+psycopg://ats:example@localhost:5433/job_hire_ai",
    )
    assert overridden.database_url.startswith("postgresql+psycopg://")


def test_sqlite_engine_keeps_foreign_keys_enabled():
    db = Database("sqlite:///:memory:")
    try:
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
    finally:
        db.engine.dispose()


def test_rejects_unexpected_dialect():
    with pytest.raises(ValueError, match="Only SQLite and PostgreSQL"):
        Database("mysql://root@localhost/test")


def test_postgres_schema_compiles_without_sqlite_only_functions():
    dialect = postgresql.dialect()
    ddl = str(CreateTable(JobCriterion.__table__).compile(dialect=dialect))
    assert "typeof(" not in ddl
    assert "weight BETWEEN 0 AND 100" in ddl
    indexes = {index.name: index for index in CriteriaConfiguration.__table__.indexes}
    ddl = str(CreateIndex(indexes["uq_current_approved"]).compile(dialect=dialect))
    assert "WHERE status = 'APPROVED'" in ddl


def test_sqlite_alembic_head_is_non_destructive(tmp_path):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = "sqlite:///" + str(tmp_path / "ats.sqlite3")
    command.upgrade(config, "head")
    db = Database(config.attributes["database_url"])
    try:
        with db.engine.connect() as conn:
            assert inspect(conn).has_table("document_pipeline_stages")
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
                "c9f8a7d4b132"
            )
    finally:
        db.engine.dispose()
