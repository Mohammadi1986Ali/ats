"""Explicit local ATS data reset: remove CVs, jobs/JDs, and their derived records.

Stop Start ATS.cmd first. This command never deletes code, .env, models, or
database schema. No automatic startup reset; requires an explicit confirmation.
"""

import argparse
import re
from pathlib import Path

from sqlalchemy import delete, text

from job_hire_ai.database.models import Base, CandidateDocument
from job_hire_ai.database.session import Database
from job_hire_ai.errors import ATSError
from job_hire_ai.settings import Settings
from job_hire_ai.storage import LocalStorage
from job_hire_ai.workers.local_lock import worker_lock

CONFIRMATION = "DELETE ALL ATS DATA"
_KEY = re.compile(r"^[a-f0-9]{32}\\.(?:pdf|docx)$")


def reset_ats_data(db, storage: LocalStorage, data_dir: Path) -> dict:
    """Clear ONLY this application's tables in a transaction, then CV files.

    Safeguard: caller must hold both worker locks; run only with ATS stopped.
    PostgreSQL uses the schema's own table list, not all database tables.
    """
    with db.transaction() as session:
        counts = {
            "resumes": session.query(CandidateDocument).count(),
        }
        from job_hire_ai.database.models import Vacancy
        counts["jobs"] = session.query(Vacancy).count()
        tables = list(Base.metadata.sorted_tables)
        if db.engine.dialect.name == "postgresql":
            preparer = db.engine.dialect.identifier_preparer
            names = ", ".join(preparer.format_table(table) for table in tables)
            session.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        else:
            for table in reversed(tables):
                session.execute(delete(table))

    # Only files with ATS-generated storage keys, within its dedicated upload folder.
    failed = []
    removed = 0
    for file in storage.directory.iterdir():
        if not _KEY.fullmatch(file.name):
            continue
        try:
            storage.delete(file.name)
            removed += 1
        except OSError:
            failed.append(file.name)

    # These markers apply to the erased job/CV database state.
    for marker in data_dir.glob(".*.done"):
        if marker.is_file() and not marker.is_symlink():
            marker.unlink(missing_ok=True)
    counts["files_deleted"] = removed
    counts["file_cleanup_failed"] = len(failed)
    if failed:
        raise ATSError(
            f"Database cleared, but {len(failed)} managed CV file(s) could not be removed. "
            "Close applications holding those files and run the reset again."
        )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete ALL local ATS resumes and JDs")
    parser.add_argument("--confirm", required=True, help=f"Must equal: {CONFIRMATION}")
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        parser.error("No deletion performed. Confirmation phrase does not match.")

    settings = Settings()
    db = Database(settings.database_url)
    storage = LocalStorage(settings.directory / "uploads", settings.max_file_mb * 1024**2)
    try:
        with worker_lock(settings.directory / "pipeline-text-worker.lock"):
            with worker_lock(settings.directory / "cv-worker.lock"):
                result = reset_ats_data(db, storage, settings.directory)
        print(
            "ATS data deleted: "
            f"{result['jobs']} jobs/JDs, {result['resumes']} resumes, "
            f"{result['files_deleted']} stored CV files. "
            "Code, database schema, .env and local Ollama models preserved."
        )
        return 0
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
