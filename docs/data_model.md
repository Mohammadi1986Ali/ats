# Data model

| Table | Purpose | Key relationships and constraints |
| --- | --- | --- |
| vacancies | Title, optional department, description, Active/Archived status, timestamps | Nonblank title/description; indexed status |
| candidates | Optional display name/email, timestamps | Generated C-000001 display reference from primary key; unknown details null |
| job_applications | Uploaded/Removed association and timestamps | Required candidate/vacancy foreign keys; indexed foreign keys and status |
| candidate_documents | Original filename, UUID storage key, type, byte size, SHA-256, upload time, text availability | Required application/vacancy composite foreign key; unique storage key and vacancy/checksum; positive size |
| job_analysis_runs | Local model/digest, prompt version, input fingerprint, inference options, timestamps, duration, status and safe error | Required vacancy FK; no CV or raw-response fields |
| criteria_configurations | Vacancy-specific draft/approved/superseded versions, description snapshot/hash, approval/reviewer, optimistic revision | Unique vacancy/version; partial unique indexes for one current draft and approval; optional analysis-run FK |
| job_criteria | Requirement fields, evidence, integer weight/order, acknowledgement, provenance and original extraction | Required configuration FK; unique configuration/criterion key; bounded weights and enums; immutable historical rows |
| document_text_extractions | Original/normalized source segments and locations | Unique document/checksum/extractor; document cascade deletion |
| cv_analysis_batches | Vacancy-scoped persistent batch | Required vacancy FK |
| cv_analysis_jobs | Selected document, status, attempts, worker token and outcome | Batch/document uniqueness; document link nulls on deletion |
| cv_analysis_runs | Per-attempt model/configuration, timing, chunks and safe errors | Required job FK; document link nulls on deletion |
| candidate_profiles | Immutable structured qualification JSON and extraction version | Candidate/document/source/run FKs; unique document/version and run |
| evidence_references | Claim-path quotations, locations, offsets and verification | Profile/document FKs; cascade deletion; immutable updates |
| profile_review_events | Separate original/corrected values, reviewer, note, timestamp | Profile FK; append-only corrections/validation; cascade deletion |

Every entity has an integer primary key. A vacancy and a candidate each have many
applications; every application has exactly one of each and may contain multiple CV
documents. Uploading without an explicit candidate creates one candidate/application
per valid file. Selecting an existing candidate reuses their active application for the
selected vacancy. Candidate names and email are never inferred from file names or text.

Timestamps are generated in UTC; SQLite stores naive UTC values. IDs and timestamps are
not editable through the UI. Vacancy status changes update its timestamp. Removed
application history remains referentially valid; documents disappear only after successful
file cleanup. Candidate records retained after removal count toward total candidates.
No hard-delete vacancy/candidate action is offered. Foreign keys prevent orphan rows.

Dashboard scope: global candidate count includes all candidate rows. Vacancy-filtered
candidate count is the distinct candidates with any historical application in that vacancy.
Current Uploaded applications and their CVs contribute to operational totals, charts,
and recent uploads. Removed applications are accessible through Candidate Management.

The Step 2 migration adds three tables, indexes and immutability/approval triggers.
It does not rebuild, rename or remove the Step 1 tables. SQLAlchemy relationships connect
vacancies to configurations/runs, configurations to criteria and optional analysis runs,
and criteria to their configuration. Version history preserves description snapshots and
all approved criteria/weights. Draft rows may be replaced; approved rows cannot be edited.
See [criteria configuration](criteria_configuration.md) for transition and evidence policies.

Step 3 adds seven more tables and update-immutability triggers, leaving all previous tables
intact. Qualification collections use a Pydantic-validated JSON snapshot; audit/evidence/
review records are relational. See [candidate profile schema](candidate_profile_schema.md).
