# Job Hire AI

An AI-Assisted Applicant Tracking System, a Master's Personal Project
for the Master Data Driven Business programme at The Hague University of Applied Sciences.
Step 1 provides the basic ATS. Step 2 adds local Llama job-description analysis and
HR-configurable, versioned criteria. Step 3 adds source-aware CV extraction, persistent
sequential analysis batches, versioned candidate profiles and auditable HR corrections.
There is no candidate matching, candidate scoring, ranking, automated rejection or OCR.
Existing vacancy, application, CV and job-criteria workflows are preserved.

## One-click Windows launch (PostgreSQL + local embeddings)

After updating `feature/ats-pipeline`, start Docker Desktop and Ollama, then
**double-click `Start ATS.cmd`**. On the first run only, enter the password
you already chose when creating the local PostgreSQL container. The launcher
saves configuration in the local Git-ignored `.env` file, starts the database,
installs the optional database driver if needed, runs additive Alembic
migrations and launches Streamlit. On later runs, double-click the same file.
No PowerShell environment variables or manual worker command are required.

The launcher never deletes SQLite or PostgreSQL data. If PostgreSQL fails to
connect, it stops with an error instead of silently falling back to SQLite.
If Docker Desktop or Ollama isn't running, start the respective local service.
Do not commit or share `.env`. The initial launch has not been verified on
every Windows configuration; report any error displayed in the launcher window.

## Windows installation

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Open PowerShell in this project directory:

```powershell
uv sync
Copy-Item .env.example .env   # optional; defaults work without this file
uv run alembic upgrade head
uv run streamlit run app.py
```

Open `http://127.0.0.1:8501`. Streamlit binds to loopback by default in this project.
Keep this working directory when running commands. The default database and uploads
are in `data/ats.sqlite3` and `data/uploads/`, rooted at the project directory.
They survive restarts. No records are seeded. Schema creation is performed by Alembic,
never by application startup. Startup checks the migration revision and shows a setup
message if it needs updating.

`ATS_DATA_DIR` and `ATS_MAX_FILE_MB` can be set in `.env` or environment variables.
Use an absolute data directory when moving deployments. The default document limit is
10 MiB. If increasing it, also raise `server.maxUploadSize` in `.streamlit/config.toml`.
The application service always enforces the configured limit independently of the UI.

## Step 2: local job requirements analysis

Install [Ollama for Windows](https://docs.ollama.com/windows), then in PowerShell:

```powershell
ollama pull llama3.2:1b
ollama list
# Only if the background Ollama app is not already serving:
ollama serve
```

In another terminal, run `uv sync`, `uv run alembic upgrade head` and
`uv run streamlit run app.py`. Existing Step 1 tables and uploads remain intact.
The app never downloads a model automatically. Default inference uses the local
`llama3.2:1b` model, temperature 0, 8192 context, 4096 output tokens, 8 CPU threads,
and `num_gpu=0`. `.env.example` lists all adjustable `OLLAMA_` settings. Restart
Streamlit after changing settings. Only loopback HTTP endpoints and local model tags
are accepted; proxies and redirects are disabled for inference requests.

In **Job Management**, select a vacancy and scroll to **AI Job Requirements**:

1. Check the real Ollama/model status, then click **Analyze Job Description**.
2. Review required/preferred/unclear requirements, source excerpts and uncertainty.
   Unverified excerpts are flagged; no weights or approvals are assigned by Llama.
3. Add/delete rows in the editable table. Edit fields and use unique `display_order`
   numbers to reorder criteria. Assign integer weights totaling exactly 100%.
   Zero-weight criteria are allowed; a blank weight is unassigned.
4. **Save Draft** persists edits separately from approval. Correct unverified evidence,
   or explicitly acknowledge it with a review note. An acknowledgement does not verify it.
5. Review the saved draft, optionally enter a reviewer identifier, tick the approval
   confirmation, and click **Approve Configuration**. Unsaved changes block approval.
6. Use **Create New Draft** to revise approved criteria. Approving the new version
   supersedes the previous approval atomically; historical criteria remain immutable.
   Editing a job description flags its old approval as outdated without replacing it.
   Re-analyze the updated description and approve again. Replacing an existing draft
   requires an explicit checkbox; a failed re-analysis preserves that draft.

Ollama being offline does not prevent basic ATS operations or manual draft work.
Missing models, timeouts, invalid JSON and truncated output produce actionable errors,
never sample extraction results. New dashboard metrics come from the database and
link to unfinished job configurations. Approved metrics include only configurations
matching the current description; manual configurations are included in these totals.

See [job analysis](docs/job_analysis.md), [criteria configuration](docs/criteria_configuration.md)
and [AI integration](docs/ai_integration.md) for the prompt, schemas, evidence policy,
versioning and limitations. Fictional job examples live exclusively under `tests/fixtures/`.

## First vacancy and CVs

For Step 3 analysis after upload, see [CV analysis](docs/cv_analysis.md). The **CV Analysis**
page supports selected/all-pending CVs, progress, retry/resume, evidence, profiles and corrections.
See [Step 3 file changes](docs/step3_changes.md), [profile schema](docs/candidate_profile_schema.md)
and [verification results](docs/verification.md) for implementation and test details.

1. In **Job Management**, expand **Create vacancy**. Enter `Senior Java Developer`
   and a job description; department is optional. Click **Create vacancy**.
2. In **Candidate Management**, select this vacancy. Choose up to 100 PDF/DOCX files.
   Review the validation table and click **Upload batch**. Each file is independent:
   valid files persist even if others fail. Results include counts and rejection reasons.
3. By default, each accepted CV creates an anonymous candidate reference and application.
   Name and email stay null. To associate CVs with a known existing applicant, explicitly
   choose their candidate reference before uploading. Their current application to that
   vacancy is reused; a different vacancy creates a separate application.
4. Search/filter the applications table, select an application, inspect document metadata,
   and use **Preview CV** to view PDF pages or a DOCX text preview. **Download original CV**
   retains the original filename and full formatting for use in a local viewer.
5. **Dashboard** shows current database counts, recent activity and applications per vacancy.
   The vacancy filter applies to every panel. Candidate counts include retained candidate
   history; applications and CV counts include current uploads only.
6. In **Job Management**, select a vacancy to edit it, archive it or reactivate it.
   Archiving retains all applications and documents. Archived vacancies are selectable
   in Candidate Management for historical review, download and removal, but reject uploads.

## Removal and persistence policy

Select an application, tick the permanent deletion confirmation and click **Remove application**.
Removal deletes all its CV bytes and document metadata. A `Removed` application history
entry remains; Step 3 source text, profiles, evidence and review events cascade-delete with
their document. Analysis audit jobs/runs retain diagnostic metadata but their document links
are cleared. Local backups are separate copies and must be managed/deleted by the operator.
Candidate records are not purged automatically;
the UI is not a complete personal-data erasure workflow. Identical CVs can be uploaded
again after successful removal. Active duplicate content is rejected per vacancy regardless
of filename; the same file is permitted in a different vacancy.

Deletion first commits `Removed`, then deletes files, then clears document metadata.
If deletion is interrupted, select **Removed** in the application status filter and retry.
Downloads are blocked for removed applications. Metadata remains until cleanup completes.
This also supports retry after interruption between individual file deletions.

Uploads validate contents before writing, use UUID storage keys, and roll back their database
transaction plus newly created file on failure. SQLite and the filesystem cannot participate
in one atomic transaction: sudden power loss/process termination can leave an unreferenced
file between save and commit. Back up database and uploads together while the app is stopped.
If rollback file cleanup fails due to OS permissions, a minimal error is logged and local
storage inspection is required. Existing files are never overwritten by an upload.

## Verification

```powershell
uv run pytest
uv run ruff check .
uv run alembic check
```

Tests migrate isolated temporary SQLite databases and generate synthetic PDF/DOCX bytes.
They cover document validation, candidate/application associations, persistence, foreign
keys, duplicates, independent batches, dashboard filtering/counts, file retrieval/deletion,
rollback cleanup, deletion retry and Streamlit page interactions.

## Structure

```text
app.py                         Streamlit entry point and sidebar navigation
pyproject.toml / uv.lock        Dependencies and reproducible environment
alembic.ini / migrations/       Versioned database schema
.streamlit/config.toml          Light theme, private local binding, telemetry off
src/job_hire_ai/
  settings.py                  Pydantic environment configuration
  schemas.py                   Validated vacancy input and upload results
  runtime.py                   Service composition
  ui.py                        Three functional Streamlit pages
  errors.py                    Safe operational errors
  database/
    models.py                  ATS entities, criteria versions and analysis runs
    session.py                 Transactions and SQLite foreign keys
    repositories.py            Shared read queries
  services/
    vacancy_service.py         Vacancy lifecycle
    candidate_service.py       Candidate records
    application_service.py     Application history and removal
    document_service.py        Upload orchestration and downloads
    dashboard_service.py       Live database metrics
    criteria_service.py        Drafts, precise weights, evidence and version approval
  ai/                          Shared Ollama transport; job/CV prompts, schemas and merge logic
  ai_ui.py                     HR criteria editor, approval and history
  cv_ui.py                     CV batch processing, profiles, evidence and HR corrections
  text_extraction.py           Structured PDF/DOCX source extraction and bounded chunks
  workers/local_lock.py         Cross-process guard for one sequential local CV worker
  storage.py                   Private filesystem operations and SHA-256
  documents.py                 PDF/DOCX structural validation
tests/
  conftest.py                  Migrated temporary DB and synthetic fixtures
  unit/                       Input/document/storage validation
  integration/                ATS workflows and UI interactions
docs/
  architecture.md
  data_model.md
  user_guide.md
data/                          Runtime files only (ignored by version control)
```

## Privacy and limitations

Local prototype for a trusted workstation: **no production authentication or multi-user
authorization; do not expose it on the public internet**. CV storage is private to the
app (no static serving) but not encrypted by this application. Configure Windows account
permissions and disk encryption as appropriate. CV source text and validated profiles are
persisted locally in SQLite for traceability; CV text is never written to application logs.
Source caches are reused across extraction versions and delete with the document.
A structurally valid PDF with no text
is accepted and marked unavailable. Password-protected and repaired PDFs are rejected.
DOCX archive expansion is bounded, corrupted archives and macros are rejected.
Structural validation is not antivirus scanning or an untrusted-document sandbox.
There is no automatic candidate identity inference, CV scoring, OCR, ranking,
candidate matching or cloud inference. Job descriptions and selected CV text chunks are
sent only to the configured local Ollama server. Names, contact details and protected
attributes are excluded from the structured qualification schema; source documents may
still contain them. See [evidence/privacy limitations](docs/evidence_traceability.md).
The prototype supports sequential use by a single HR operator; simultaneous writers and
hard process-crash recovery are outside its guarantees. See [architecture](docs/architecture.md)
and [data model](docs/data_model.md) for details.

Normal tests mock the LLM and require no running Ollama. Optional live verification:

```powershell
$env:ATS_TEST_LIVE_OLLAMA = "1"
uv run pytest -m live_ollama -rs
Remove-Item Env:ATS_TEST_LIVE_OLLAMA
```

Live tests skip with an explicit reason if the configured model is unavailable.

Executed checks and their limits are recorded in [verification](docs/verification.md).
In this workspace uv was bootstrapped locally; if `uv` is not on your PATH, use
`.\.tools\bin\uv.exe` in place of `uv`, or add that directory to your current
PowerShell session's PATH. A fresh checkout requires installing uv as described above.


## Automatic CV pipeline (Phase 1)

Run `uv run alembic upgrade head` after updating the source. Start the ATS as usual
with `uv run streamlit run app.py`. The app starts an **independent local Python
worker** automatically after verifying database migrations. CV uploads create
Application, Document, and durable per-document pipeline stages in one database
transaction. The worker polls every two seconds, extracts text without Ollama,
and stores it in the existing `document_text_extractions` table. A local OS file
lock ensures only one text worker processes the queue; a restart recovers
interrupted jobs, with bounded attempts. The Dashboard and Candidate Management
pages show live text-stage status by vacancy and CV, refreshed every four seconds.

The following downstream stages exist as explicit `WAITING` placeholders:
Profile Extraction, Evidence Validation, Matching, Scoring and HR Review. They
**are not executed by the Phase 1 worker**; no AI outputs or scores are faked.
An unreadable scanned CV fails text extraction with an OCR-needed explanation.
The worker continues when the browser tab closes. Logs are stored under the
local data directory (`data/pipeline-worker.log`). Source documents, job records,
and successful extraction results survive worker failure. The database remains
SQLite in this phase; migration does not reset existing records.


## Job-first Dashboard and Pipeline Monitor (Phase 2)

Open **Dashboard** for an overview of every job (including archived or empty jobs),
with real CV counts, queued/running/failed text extractions and completion progress.
Select a job and choose **Open this Job's CV pipeline**, or navigate directly to
**Pipeline Monitor** from the sidebar. The monitor contains six stage counts, a
filterable/searchable CV table and per-CV stage details (duration, attempts, error).
The dashboard and monitor refresh every four seconds without manually refreshing
the browser. Job-specific data is read from the existing persisted pipeline tables.

**Important:** Only text extraction runs automatically in Phase 2. The remaining
stages are displayed as *Not implemented / Not active yet*, not falsely counted as
completed. A completed text extraction does not imply completed candidate analysis
or hiring assessment. No new migration or model is required for this UI phase; the
Phase 1 migration must already be applied.

## Phase 3.3: source-grounded semantic retrieval

Indexed CV and job-description passages can now be searched locally using the
same `nomic-embed-text` model as indexing. PostgreSQL computes cosine distance
with pgvector; SQLite has a deterministic Python fallback for development.
Retrieval returns **stored original text, original source reference, document ID,
vacancy ID, and similarity**. It does not generate statements about candidates,
score them for hiring, or make HR decisions.

With PostgreSQL and Ollama already running and the job/CV embeddings completed:

```powershell
uv run --no-sync python -m job_hire_ai.search_cli --vacancy-id 1 --query "Java concurrency"
uv run --no-sync python -m job_hire_ai.search_cli --vacancy-id 1 --source JD --query "backend reliability"
uv run --no-sync python -m job_hire_ai.search_cli --vacancy-id 1 --document-id 2 --query "Kafka"
```

Use your real vacancy and document IDs; no test content is inserted automatically.
Results are job-scoped and exclude removed applications. Matching is retrieval
only; downstream profile extraction, evidence validation and hiring scores remain
separate pipeline phases. Query text goes only to the configured local Ollama
embedding endpoint.

Run offline service tests with `uv run pytest tests/test_semantic_search.py`.

## Phase 3.4: automatic, source-backed candidate profiles

The background pipeline can now process uploaded CVs in order:

`TEXT_EXTRACTION → EMBEDDING → PROFILE_EXTRACTION`

After embeddings complete, the worker invokes the existing bounded local Ollama
CV analysis implementation. It saves structured employment, technical skills,
education, certifications, projects, languages and other job-relevant fields
through the existing versioned `candidate_profiles` tables. The extractor
must return a validated schema, and source quotes are checked against the
actual extracted text; missing/ambiguous evidence is flagged as
`NEEDS_REVIEW`, never silently accepted.

For manual Streamlit launches, set `ATS_PROFILE_EXTRACTION_ENABLED=true` in
your ignored local `.env`, alongside `ATS_EMBEDDINGS_ENABLED=true`, and
restart the Streamlit server **and any previously detached pipeline worker**.
`Start ATS.cmd` enables profile extraction by default. A CPU-only local
model can take substantially longer than embedding; monitor the recorded
`PROFILE_EXTRACTION` stage and review the worker log rather than starting
another worker. On a model/validation failure the stage is marked `FAILED`,
with no fabricated profile. A repeatedly interrupted stage is bounded by the
existing retry limit.

The separate `EVIDENCE_VALIDATION`, `MATCHING`, `SCORING`, and `HR_REVIEW`
pipeline stages remain waiting; profile completion does **not** imply
human approval or candidate suitability. Existing manual CV analysis workflows
and review/correction history are preserved. This phase requires no new
Alembic schema migration.

Run offline stage-orchestration tests with:

```powershell
uv run --no-sync pytest tests/test_automatic_profiles.py
```

## Phase 3.5: deterministic evidence audit

With `ATS_EVIDENCE_VALIDATION_ENABLED=true`, the local worker continues
`TEXT_EXTRACTION → EMBEDDING → PROFILE_EXTRACTION → EVIDENCE_VALIDATION`
automatically. The standard Windows `Start ATS.cmd` launcher enables this
flag and restarts the detached worker on launch.

Evidence validation operates on the **actual persisted CV profile** and the
original extracted CV text. It re-checks each claim's quoted source,
reference, location, and literal evidence consistency, detects missing or
unexpected evidence rows, and reports audit findings in the pipeline stage
without changing the immutable versioned profile or its evidence records.
Unverified or ambiguous sources require human review and are flagged in
the pipeline-stage diagnostic; immutable extraction status is preserved.
Structural validation errors mark the pipeline stage `FAILED` with a diagnostic.
Completed evidence audits may still have a visible warning stating that human
review is required. Completion means **automated checks ran**, not that an
HR reviewer accepted the evidence. No fabricated AI responses, candidate
ranking, automatic hiring decisions, or silent approval are introduced.

The audit is idempotent, preserves original generated profile content and
manual reviewer events, and leaves `MATCHING`, `SCORING`, and `HR_REVIEW`
for later stages. It introduces no database schema changes. For Streamlit
started manually instead of through the Windows launcher, set
`ATS_EVIDENCE_VALIDATION_ENABLED=true` in the ignored local `.env` and
restart both Streamlit and the existing detached worker.

Test the deterministic behavior offline with:

```powershell
uv run --no-sync pytest tests/test_evidence_validation_pipeline.py
```

## Phase 3.6: job-to-CV evidence matching (no score)

With `ATS_MATCHING_ENABLED=true` and the previous pipeline stages enabled,
the worker advances automatically from `EVIDENCE_VALIDATION` to `MATCHING`
**only after HR approves criteria for the current job-description revision**.
Without approved/current criteria, matching stays `WAITING`; there is no
fictional match, default ranking or automatic rejection. The Windows
`Start ATS.cmd` launcher turns the matching stage on automatically.

The matching implementation is deliberately conservative. A criterion is
`EVIDENCED` only if its exact name appears in a supported extracted claim
**and** an associated, independently verified original CV quote. Otherwise
it is `NOT_EVIDENCED` (no evidence found, **not** "candidate lacks skill").
Unverified/unacknowledged job criteria are `REVIEW_REQUIRED`. It never
converts embedding cosine similarity into a factual assertion about the
candidate. This literal-only baseline may miss legitimate synonyms or
paraphrases. No suitability score, rank, hiring decision or inferred years
of experience is generated.

Every result stores the approved criteria configuration ID, CV document ID,
profile version ID, criterion key/status, and real source quote/reference.
An additive Alembic migration creates `job_match_results`, without resetting
existing data. A newer approved criteria version automatically requeues
completed matching, preserving old snapshots for audit.

To inspect already-computed results without triggering any AI work:

```powershell
uv run --no-sync python -m job_hire_ai.match_cli --vacancy-id 1
uv run --no-sync python -m job_hire_ai.match_cli --vacancy-id 1 --document-id 2
```

Test offline with `uv run --no-sync pytest tests/test_job_matching.py`.
`SCORING` and `HR_REVIEW` remain waiting; no automatic HR approvals are made.

## Phase 3.7: explainable evidence coverage (not hiring suitability)

After `MATCHING` completes, `SCORING` records an auditable **documentation
coverage** snapshot. It uses only the HR-approved criterion percentages and
the evidence states saved by Phase 3.6. The three reported buckets always
sum to 100% of approved weights:

- `documented_weight`: criteria with verified source-quoted evidence
- `unverified_weight`: criteria with no direct evidence in the CV; **unknown,
  not a judgment that the candidate lacks the skill**
- `review_weight`: criteria that require explicit evidence review

A criterion contributes its whole approved weight to exactly one bucket.
There are no ML predictions, synthetic evidence, hidden partial-credit
calculations, candidate ranking, automatic rejection, or hiring decisions.
Phase 3.6 intentionally uses conservative literal matching; this coverage
number is not a comparable measure of candidate competence or overall fit.

The additive migration creates `candidate_score_snapshots` tied to the
immutable approved criteria version, CV profile and matching result. Previous
snapshots remain available for audit. Re-matching after new criteria approval
automatically requeues the scoring stage. `HR_REVIEW` stays separate.

With the standard Windows launcher, `ATS_SCORING_ENABLED=true` is configured
automatically. For manual startup, enable that flag and all preceding stages,
then restart the background worker. No database reset is necessary.

To inspect already-persisted coverage (read-only):

```powershell
uv run --no-sync python -m job_hire_ai.score_cli --vacancy-id 1
uv run --no-sync python -m job_hire_ai.score_cli --vacancy-id 1 --document-id 2
```

Run offline tests with `uv run --no-sync pytest tests/test_job_matching.py`.

## Recovery for failed local CV profile extraction

If the Pipeline Monitor shows `PROFILE_EXTRACTION FAILED` with "output
truncated" or "invalid structured output" while local Ollama responds with
HTTP 200, the CV's source text and embeddings do **not** need to be reuploaded.
The extractor now reserves the entire configured JSON output budget, uses
smaller source sections, and attempts bounded splitting on either truncation
or schema validation errors. It does not parse partial JSON or fabricate
missing CV facts.

After pulling the latest branch and restarting `start-ats.ps1` (which
migrates unchanged legacy default CV settings, without overwriting custom
values), explicitly requeue only those failed CV IDs:

```powershell
uv run --no-sync python -m job_hire_ai.retry_profiles --document-ids 1 2 3
```

The background worker picks them up automatically. The retry command checks
the application and embedding status, only retries `FAILED` profiles, and
stops after three stage attempts. It preserves uploaded files, document
text, embeddings, CV analysis history, and PostgreSQL tables. Larger CVs
can still fail on a CPU-only 1B model; that failure remains visible rather
than creating an unsupported profile. If unsuccessful again, inspect the
stage's stored error and model metrics before changing model profiles.

## Recovery after repeated CV model JSON failure (Phase 3.4 robustness)

Tiny local models such as `llama3.2:1b` can exhaust bounded split
retries even when Ollama responds HTTP 200. After that specific failure,
the application now has an explicit, visibly incomplete fallback:

- It reads **only actual source CV segments** and recognizes literal
  technology terms from a fixed vocabulary, with original source references.
- Every fallback term is `UNCERTAIN` and the immutable profile records a
  prominent `PARTIAL DETERMINISTIC EXTRACTION` warning. It never claims to
  have extracted employment, education or unsupported qualifications.
- These uncertain claims **cannot become positive match evidence or scoring
  points**, and an HR reviewer must inspect the original CV. No model response
  is forged or silently accepted.
- Missing Ollama, timeouts and non-JSON-related errors **remain failures**.
- Set `OLLAMA_CV_LITERAL_FALLBACK_ENABLED=false` to disable this mode.

For previously failed documents (including those already at their three-stage
retry limit), after pulling the new branch and restarting ATS, use the
operator-authorized retry. This does **not** reupload CVs, reset PostgreSQL,
or re-run completed text/embedding stages:

```powershell
uv run --no-sync python -m job_hire_ai.retry_profiles --document-ids 1 2 3 --allow-exhausted
```

Expect `PROFILE_EXTRACTION COMPLETED` in Pipeline Monitor when an incomplete
reviewable fallback profile is saved. Inspect the profile's warnings; this is
**not** evidence that a full AI extraction was successful. A stronger locally
installed model is still recommended for full employment/education extraction.
