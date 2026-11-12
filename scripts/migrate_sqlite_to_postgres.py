"""Explicit, non-destructive SQLite -> PostgreSQL copy for Job Hire AI.

Runs only with --apply. Does not update .env, delete SQLite, or copy CV bytes.
Stop Streamlit and all workers before using this tool.
"""

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

from job_hire_ai.database.models import Base, CandidateDocument
from job_hire_ai.settings import ROOT
from job_hire_ai.storage import LocalStorage


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup(source: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise RuntimeError("Backup path already exists; refusing to overwrite it")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(backup) as dest:
            src.backup(dest)


def _verify_local_uploads(source_engine, directory: Path) -> int:
    """Fail closed if a stored CV is absent or differs from the DB checksum."""
    storage = LocalStorage(directory / "uploads", max_bytes=2**31)
    checked = 0
    with source_engine.connect() as conn:
        for key, checksum in conn.execute(
            select(CandidateDocument.storage_key, CandidateDocument.checksum)
        ):
            cv = storage.path(key)
            if not cv.is_file() or _sha256(cv) != checksum:
                raise RuntimeError("An existing CV is missing or corrupted; aborting import")
            checked += 1
    return checked


def _schema_is_ready(connection, head: str) -> None:
    if not inspect(connection).has_table("alembic_version"):
        raise RuntimeError("Database is not migrated; run Alembic upgrade head first")
    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if revision != head:
        raise RuntimeError(f"Database revision {revision} differs from required {head}")


def migrate(source: Path, target_url: str, data_dir: Path, *, apply: bool = False) -> dict:
    if not source.is_file():
        raise RuntimeError("SQLite source file not found")
    if make_url(target_url).get_backend_name() != "postgresql":
        raise RuntimeError("Destination must be a PostgreSQL URL (never SQLite)")
    if make_url(target_url).host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Only local PostgreSQL destinations are supported in Phase 3.1")

    expected_head = ScriptDirectory.from_config(
        Config(str(ROOT / "alembic.ini"))
    ).get_current_head()
    source_url = "sqlite:///" + source.resolve().as_posix()
    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url, pool_pre_ping=True)
    try:
        with source_engine.connect() as src, target_engine.connect() as target:
            _schema_is_ready(src, expected_head)
            _schema_is_ready(target, expected_head)
            extension = target.execute(
                text("SELECT extname FROM pg_extension WHERE extname='vector'")
            ).scalar()
            if extension is None:
                raise RuntimeError("Target PostgreSQL has not enabled pgvector")
            counts = {}
            for table in Base.metadata.sorted_tables:
                src_count = src.scalar(select(func.count()).select_from(table))
                dest_count = target.scalar(select(func.count()).select_from(table))
                if dest_count:
                    raise RuntimeError(
                        "Destination is not empty; refusing to overwrite any records"
                    )
                counts[table.name] = src_count
            checked = _verify_local_uploads(source_engine, data_dir)

        report = {"tables": counts, "documents_verified": checked, "applied": False}
        if not apply:
            return report
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = data_dir / "backups" / f"ats-before-postgres-{stamp}.sqlite3"
        _backup(source, backup)
        # Retain a SHA-256 of the backup; the source is NEVER removed or edited.
        report["sqlite_backup"] = str(backup)
        report["sqlite_backup_sha256"] = _sha256(backup)

        # One transaction for all rows. A failure rolls back the PostgreSQL destination.
        with source_engine.connect() as src, target_engine.begin() as dst:
            _schema_is_ready(dst, expected_head)
            for table in Base.metadata.sorted_tables:
                if dst.scalar(select(func.count()).select_from(table)):
                    raise RuntimeError("Destination changed since preflight; refusing to continue")
            # Approval-state triggers are enabled again before committing.
            for table_name in ("job_criteria", "criteria_configurations"):
                dst.execute(text(f"ALTER TABLE {table_name} DISABLE TRIGGER USER"))
            for table in Base.metadata.sorted_tables:
                for chunk in src.execute(select(table)).partitions(200):
                    dst.execute(table.insert(), [dict(row._mapping) for row in chunk])
            for table in Base.metadata.sorted_tables:
                if "id" not in table.c or not table.c.id.primary_key:
                    continue
                maximum = dst.scalar(select(func.max(table.c.id)))
                if not isinstance(maximum, int):
                    continue
                sequence = dst.execute(
                    text("SELECT pg_get_serial_sequence(:relation, 'id')"),
                    {"relation": f"public.{table.name}"},
                ).scalar()
                if sequence:
                    dst.execute(
                        text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
                        {"sequence": sequence, "value": maximum},
                    )
            for table_name in ("job_criteria", "criteria_configurations"):
                dst.execute(text(f"ALTER TABLE {table_name} ENABLE TRIGGER USER"))
            for table in Base.metadata.sorted_tables:
                actual = dst.scalar(select(func.count()).select_from(table))
                if actual != counts[table.name]:
                    raise RuntimeError("Copy count mismatch; transaction will roll back")
        report["applied"] = True
        return report
    finally:
        source_engine.dispose()
        target_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "ats.sqlite3")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--apply", action="store_true", help="Actually copy, with backup")
    args = parser.parse_args()
    report = migrate(args.source, args.target_url, args.data_dir, apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
