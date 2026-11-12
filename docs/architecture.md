# Architecture

Streamlit (`app.py`, `ui.py`) calls services, which use repositories/ORM in central
database transactions. The storage layer only accepts generated keys. Document validation
is separate from orchestration. Pydantic validates required vacancy inputs and settings.
All operational state lives in SQLite and private local files; session state holds transient
UI edits/notices, batch results and short-lived Ollama availability checks. No operational
result caching hides database updates; criteria persist only on explicit draft save/approval.

The app requires Alembic's current schema revision. Each migration describes its own
schema, independent of future ORM changes. SQLite foreign keys are enabled on every
connection. A unique `(vacancy_id, checksum)` constraint backs service duplicate checks.
A composite foreign key ensures a document's vacancy matches its application's vacancy.

Upload transaction boundary: validate -> check active vacancy/duplicate -> create or
reuse candidate/application -> save unique file -> insert document -> commit. Exceptions
roll back all new rows and delete the new file. Batches use one transaction per file.
No existing file is touched during this rollback. An explicit candidate is reused only
on operator instruction. Documents for the same selected candidate/vacancy share the
existing Uploaded application.

Deletion is a retryable state transition. First persist Removed, then delete every
file (missing files count as deleted), then remove document metadata. A filesystem
failure leaves metadata for retry and blocks downloading that application's documents.
Removed applications and candidates are intentionally retained for history. No
automatic deletion happens on vacancy archiving.

SQLite and the filesystem have no shared transaction. Clean exception recovery is
tested; abrupt power loss can leave orphan files. Take consistent backups with the app
stopped. The local prototype assumes one operator; an authenticated multi-user service,
transactional blob store, crash reconciliation and retention/erasure tooling would need
separate design before production. Log only operation type or application IDs; no
filenames, database statement parameters, personal data or CV content in application logs.

Step 2 adds a local Ollama provider and JobAnalysisService beside existing ATS services.
It reads only Vacancy.description, records an analysis run, and saves a draft via the
same database transaction infrastructure. The schema migration is additive and leaves
the four Step 1 tables unchanged. CriteriaService owns evidence checks, integer weights,
optimistic draft revisions and atomic approval/supersession. Historical configurations
and child criteria are protected by database triggers as well as service rules.

UI editing is distinct from saving and from approval. Job-description SHA-256 fingerprints
and snapshots identify outdated configurations dynamically. Dashboard readiness metrics
are calculated from persisted configurations; model status is a short-lived connectivity
check, not a fictional operational statistic. Approved criteria never trigger screening.

Step 3 reuses the same provider's structured transport for CV qualification extraction.
TextExtractor stores bounded source segments and location maps; structure-aware chunks
are analyzed sequentially, merged and verified before a versioned profile is committed.
CVAnalysisService owns persistent batch/job/run state, caching, retry/resume and progress.
An OS lock in the data directory prevents simultaneous CV workers for the same dataset.
CandidateProfileService applies append-only HR corrections without modifying AI snapshots.
Source/profile/evidence/review rows cascade-delete with document removal. The profile
schema contains qualification sections, not identity/protected-attribute or scoring fields.

Future phases can reference profile/configuration versions in separate matching/assessment
services. No candidate-job matching, weighted candidate scores, rankings or hiring decisions
are implemented through Step 3. Raw model output and CV text never enter ordinary logs.
