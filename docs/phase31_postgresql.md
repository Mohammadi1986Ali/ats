# Phase 3.1 — PostgreSQL + pgvector preparation

## Scope and safety

SQLite remains the **default backend**. Existing `data/ats.sqlite3` and
`data/uploads/` are untouched. The PostgreSQL extension is enabled only when the
connected database dialect is PostgreSQL. No embeddings/RAG/vector tables are
created until Phase 3.2–3.4.

The old SQLite schema contains SQLite-only triggers and a `typeof` check. The
migration now installs PostgreSQL-native equivalent approval/immutability
triggers and partial indexes. These protections must be integration-tested on a
real PostgreSQL instance before any live data switch. Do not use a production ATS
database as a first test.

## Start isolated PostgreSQL locally (Windows)

With Docker Desktop installed and running, from the project directory:

```powershell
$env:ATS_POSTGRES_PASSWORD = "<choose-your-own-strong-local-password>"
docker compose -f compose.postgres.yml up -d
uv sync
uv pip install --python .venv -r requirements-postgres.txt
```

Configure a **fresh** PostgreSQL database through the optional `ATS_DATABASE_URL_OVERRIDE`
environment variable (do not commit secrets). Since the password may contain URL
special characters, URL-encode it for SQLAlchemy. For example, with a simple test
password, the local URL format is:

`postgresql+psycopg://ats:<URL-ENCODED-PASSWORD>@localhost:5433/job_hire_ai`

Because the Phase 3.1 driver is installed separately from the existing
`uv.lock`-managed SQLite environment, run commands with `uv run --no-sync`
while using PostgreSQL. Running `uv sync` again may remove the optional driver;
reinstall it with `uv pip install --python .venv -r requirements-postgres.txt`.
This temporary installation split will be replaced by locked dependencies
in a follow-up before PostgreSQL becomes the default.

Keep the ATS application and background workers **stopped** during the database
migration and data copy. Run `uv run --no-sync alembic upgrade head` with the target URL set.
This creates PostgreSQL ATS tables and activates pgvector. Do **not** point the
existing ATS application at PostgreSQL while it is still empty.

## SQLite -> PostgreSQL data copy

Use the import utility in preview mode first:

```powershell
uv run --no-sync python scripts/migrate_sqlite_to_postgres.py --target-url "$env:ATS_DATABASE_URL_OVERRIDE"
```

It verifies that both schemas have the latest Alembic revision, the destination
has pgvector and **zero existing ATS records**, and the local CV files match
stored SHA-256 digests. It prints counts, not CV text. It doesn't change either
DB in preview mode.

After a successful preview and a backup of the whole `data/` directory, explicitly
apply:

```powershell
uv run --no-sync python scripts/migrate_sqlite_to_postgres.py --target-url "$env:ATS_DATABASE_URL_OVERRIDE" --apply
```

`--apply` takes an additional timestamped SQLite backup, copies all ATS tables
(including history and pipeline state) inside **one PostgreSQL transaction**,
restores integer sequences, re-enables database integrity triggers and checks row
counts. It does **not** delete or modify the source SQLite database. Existing CV
files remain under the same `ATS_DATA_DIR/data/uploads` storage; they are not
embedded inside PostgreSQL.

**Do not migrate while the ATS or its workers are running.** On failure, leave
SQLite active and examine the error. The import refuses a nonempty destination;
it is not a merge/sync tool. If the Postgres transaction fails, its data changes
are rolled back; inspect the target before retrying.

Only after a verified import and local functional tests, set the PostgreSQL
connection URL in the environment for the ATS process and restart the app/worker.
To roll back safely, stop processes and remove the database override; this uses
the preserved SQLite data, but writes made subsequently in PostgreSQL will **not**
automatically appear in SQLite. A read-only cutover is recommended.

## Verification

```powershell
uv run --no-sync pytest -q tests/unit/test_postgres_preparation.py
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

A live PostgreSQL integration test requires an available local Docker daemon or
PostgreSQL service. CI SQLite tests by themselves do not establish that a live
PostgreSQL migration has succeeded.
