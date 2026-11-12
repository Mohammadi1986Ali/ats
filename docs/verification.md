# Verification record

Executed on 10 November 2026 on Windows with Python 3.12.10, using the committed
`uv.lock` and the local uv executable bootstrapped in `.tools/bin/uv.exe`.
No real applicant CVs or production applicant records were used.

| Command/check | Result |
| --- | --- |
| `uv sync --locked` | Passed; 54 packages resolved/checked |
| `uv run alembic upgrade head` | Passed; initial schema applied |
| `uv run alembic current` | `5e9be2fa29c7 (head)` |
| `uv run alembic check` | No new upgrade operations detected |
| `uv run pytest -q` | **47 passed in 54.71 seconds** |
| `uv run ruff check .` | All checks passed |
| Streamlit launch on 127.0.0.1:8501 | Passed |
| HTTP `/` and `/_stcore/health` | Both returned 200; health body `ok` |
| Default database metrics after verification | All five metrics zero; no seeded records |

Service tests cover a full batch of 100 distinct synthetic PDFs, mixed valid/invalid
uploads, duplicate content under different names, candidate reuse within/across vacancies,
storage-key collision preservation, database rollback cleanup, existing-file preservation,
deletion failure/retry, checksum tampering, foreign keys, dashboard filtering and reopening
the database/storage through a new runtime instance.

Streamlit AppTest exercises empty states, vacancy creation/editing/archive/reactivation,
PDF page and DOCX text previews, download-button availability, confirmed removal,
uninitialized-database guidance and navigation through the actual application entry point.
Cold imports are preloaded outside AppTest's instrumented script thread and a 30-second
UI timeout accommodates Windows startup overhead. Final suite has no failing/skipped tests.

Not verified through a real browser: file-picker/drag-and-drop behavior, browser download
completion, and visual appearance across browser/window sizes. Upload processing and
download bytes are covered at the service boundary. The real HTTP server startup is
verified separately. Crash/power-loss recovery and simultaneous multi-user writers are
not guaranteed; see the documented storage/deletion policy and prototype limitations.

## Step 2 verification — 11 November 2026

Executed on Windows with Python 3.12.10, Ollama Python client 0.6.3 and the updated
`uv.lock`. The local Ollama service is installed and reachable, but `llama3.1:8b` is not
in its model inventory. No model was downloaded and no substitute was used for inference.

| Check | Result |
| --- | --- |
| `uv sync` | Updated environment and lockfile for Ollama/httpx; project version 0.2.0 |
| `uv run alembic upgrade head` | Applied additive revision `0e8846979a09` |
| `uv run alembic check` | No new upgrade operations detected |
| `uv run pytest -q` | **96 passed, 1 skipped in 14.84 seconds** |
| `uv run ruff check .` | All checks passed |
| Explicit opt-in `pytest -m live_ollama -rs` | Skipped: model missing; run `ollama pull llama3.1:8b` |
| Existing production records versus pre-migration backup | All four Step 1 tables' rows unchanged |
| Existing private CV files versus pre-migration checksums | Unchanged |
| Real configuration AppTest smoke check | Dashboard renders nine metrics; Job Management reports missing Llama accurately |
| Existing server `/_stcore/health` | HTTP 200, `ok` |

Before applying the migration, a local SQLite backup and private file checksum manifest
were saved under the ignored `data/backups/` directory. The existing database contained one
vacancy and no candidate/application/document rows. All pre-existing row values were
compared to the backup after migration. A separate migration test populates a temporary
Step 1 database with a synthetic CV and verifies that its bytes and associations survive
upgrade, downgrade to Step 1 and upgrade again. No fictional production data was inserted.

All Step 1 regression tests remain in the normal suite. Step 2 tests use deterministic
mocked providers and synthetic Java/Data Engineer/DevOps descriptions. They cover real
Ollama client request construction, CPU options, strict JSON schemas, classifications,
evidence checks, unsupported model excerpts, unavailable models, timeouts/retries, prompt
separation, draft creation/editing, integer weights, approval, database immutability,
version history, description changes/races, persistence and dashboard readiness metrics.

Streamlit tests exercise the criteria table through its edit state: add rows, edit fields,
delete rows, reorder, save, observe the running total and approve. Other UI tests cover
offline manual configuration, mocked analysis-button behavior, approval gating, existing
vacancy/CV workflows, navigation and dashboard links to unfinished jobs. LLM availability
is mocked in the normal suite; no running Ollama/model is required.

Live Llama inference, extraction quality, prompt-injection robustness against an actual
model, CPU latency and memory consumption remain **unverified because the required model
is absent**. Browser visual appearance, drag-and-drop and completed browser downloads
are not covered by AppTest. Hardware-dependent acceptance can be run after installing
the configured model using the documented opt-in test. No candidate AI extraction,
candidate matching, candidate scoring or Step 3 features were implemented.

## Step 3 verification — 11 November 2026

Executed with Python 3.12.10 on Windows, project version 0.3.0 and the existing local
Ollama service. This time `llama3.1:8b` was installed and available; no model download
was performed by this implementation or the application.

| Check | Result |
| --- | --- |
| `uv sync --offline` | Environment/lock updated to 0.3.0 from cached dependencies |
| `uv run alembic upgrade head` | Applied additive revision `f72e820b4b1c` |
| `uv run alembic check` | No new upgrade operations detected |
| `uv run pytest -q` | **133 passed, 2 opt-in live tests skipped in 18.64 seconds** |
| `uv run ruff check .` | All checks passed |
| Explicit `test_live_cv_pipeline` with ATS_TEST_LIVE_OLLAMA=1 | **1 passed in 155.32 seconds** |
| Real CV worker duration | **152,853 ms**, synthetic PDF only |
| Live result | Persisted COMPLETED profile; employment, Java skills and education present; four evidence references |
| Existing production data after migration | All seven pre-existing tables' rows and one stored CV checksum unchanged |
| Real configuration read-only smoke check | Dashboard and CV Analysis rendered without inference or mutations |
| Existing Streamlit `/_stcore/health` | HTTP 200, `ok` |

Live model: `llama3.1:8b`; digest:
`46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`.
CPU-only provider options (`num_gpu=0`) were used. The optional test allowed 300 seconds
and disabled retries for this sample. Timing is one observation, not a workstation-wide
performance/quality guarantee. The sample was created in a migrated temporary test database;
no applicant's existing CV was submitted to inference and no production test records were seeded.

Normal tests exercise PDF/DOCX structure/text extraction, invalid/scanned documents,
normalization, whitespace-tolerant evidence offsets, ambiguous/unsupported quotes, schema
validation, missing information, partial employment dates, overlapping role preservation,
skills, education, projects, certifications, language proficiency, injection separation,
protected-field guards, invented numeric/CEFR flags, long-document chunking and exact deduplication.
They also cover cache reuse/digest invalidation, profile versions and restart persistence,
independent processing of ten distinct synthetic CVs, failed-document retry, interrupted-job
resume, worker locking, source/profile/evidence update immutability, HR correction/validation
history, cascading deletion, dashboard metrics and all previous Step 1/2 regressions.

Streamlit tests verify CV selection, one-click processing, all-pending analysis, offline
failure/retry, correction, HR validation, re-analysis and extraction history. Existing page
tests remain; the dashboard now has fifteen real metrics. The existing migration regression
test upgrades a populated Step 1 database through all migrations, downgrades and upgrades
again while verifying original document bytes.

Not fully verified: ten-CV **live** batch performance (ten-CV execution is mocked in the
normal suite), long/complex real-model extraction quality, every PDF/DOCX visual layout,
real-browser drag-and-drop/download completion and resistance to all prompt-injection or
semantic hallucination attacks. The interrupted document restarts from its first chunk;
earlier completed profiles are preserved. No candidate-job matching, scores, ranking,
ratings, automated rejection or hiring decisions were added.

## Configuration update: Llama 3.2 3B

Project defaults, the local ignored `.env` and configuration examples now use
`llama3.2:3b`, temperature 0, context 4096, output 1024 and `OLLAMA_TIMEOUT=300`.
The previous `OLLAMA_TIMEOUT_SECONDS` alias and explicit Python `timeout_seconds`
arguments remain supported. Existing records and files were not modified.

The smaller context required compact CV schema instructions and a 2800-token prompt
reserve, producing a nonzero 544-character chunk budget under these defaults. The
complete structured-output schema is still enforced. Prompt version is now
`cv-qualifications-v2`, so previous extractions are not silently reused across the change.

Verification: **136 normal tests passed, 2 optional live tests skipped** in 41.94 seconds;
Ruff and formatting checks passed. A separate actual synthetic-CV test with
`llama3.2:3b` **passed in 61.93 seconds**, with worker duration **59,230 ms**, three
evidence references and a NEEDS_REVIEW profile. No requirement to pass HR review was
relaxed. Model digest:
`a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72`.
No real applicant CVs were used in this live check. Restart Streamlit after changing
environment settings to replace its cached runtime resources.

## Configuration update: Llama 3.2 1B

The local `.env`, defaults, examples and current setup documentation now use
`llama3.2:1b`, context 4096, output 512 and temperature 0. The existing 300-second
timeout is retained. With the unchanged context reserve, CV chunks have a 1568-character
budget and job descriptions a 3168-character budget. Existing records and profiles
are preserved. The configured model is installed locally; no download was performed.

Verification: **136 tests passed, 2 optional live tests skipped in 140.96 seconds**;
Ruff and formatting checks passed. Live 1B inference was not executed for this
configuration update; prior 3B timings above remain historical measurements.
Restart Streamlit to apply these settings to its cached runtime.
