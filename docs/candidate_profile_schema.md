# Candidate profile schema and persistence

Schema version: `candidate-profile-v1`. Prompt version: `cv-qualifications-v2`.
The Pydantic v2 extraction schema is passed to the existing Ollama structured-output
transport and the response is validated again server-side. Extra/missing fields,
wrong types and malformed JSON fail the document; no fallback profile is generated.
Every root section is required, but absent information uses empty arrays/nulls.

| Section | Record fields in addition to evidence/classification |
| --- | --- |
| employment_history | employer, job_title, start_date, end_date, currently_employed, employment_type, responsibilities, technologies |
| technical_skills | name, original_wording, category |
| education | institution, degree, field_of_study, start_date, graduation_date |
| certifications | name, issuer, date |
| projects | project_name, description, technologies, responsibilities, outcomes |
| language_skills | language, proficiency as stated |
| relevant_domain_experience | information |
| availability | information |
| location_information | job-relevant information only |
| additional_job_relevant_information | information |
| extraction_warnings | explicit list of warning strings |

Each record contains `evidence` (quote/source-reference pairs), `claim_status`
(SUPPORTED/UNCERTAIN), and an uncertainty string. Date fields are strings/null, not
inferred calendar values. Responsibilities/technologies/outcomes are explicit lists.
Skills preserve source wording even when a standard synonymous skill name is used.
Education institutions do not imply degrees; language statements do not imply CEFR.
The app calculates neither derived experience duration nor candidate-job fit.

The normalized SQLite schema adds seven tables without altering the existing seven:

| Table | Responsibility |
| --- | --- |
| document_text_extractions | Per-document/checksum/extractor source-segment cache |
| cv_analysis_batches | Persistent vacancy-scoped analysis batches |
| cv_analysis_jobs | Document selection, stages, ownership, attempts and outcome |
| cv_analysis_runs | Per-attempt model/digest/options/versions/checksum, timing, chunks and errors |
| candidate_profiles | Candidate/document/source/run links, extraction version and immutable original payload |
| evidence_references | Per-profile claim paths, exact quotes, locations, offsets and verification |
| profile_review_events | Append-only original/corrected values, reviewer, note and timestamp |

Profile collections are stored as strictly validated JSON rather than separate relational
employment/skill/education/project tables: this phase needs an immutable structured source
snapshot, not cross-candidate matching queries. Evidence, audit and review rows are relational
for traceability. This avoids duplicating original values in parallel representations and
can be extended with query projections in later phases.

Every profile references an existing candidate and its own source document. Unique
`(document_id, extraction_version)` and unique analysis-run links protect versions. Source
cache keys and batch/document pairs are unique. Foreign keys/indexes connect the graph.
Document deletion cascades source/profiles/evidence/reviews and nulls job/run document IDs.
Database triggers reject updates to source caches, original profiles, evidence and review
events; cascade deletion remains allowed for privacy. The UI has no historical overwrite
operation. Corrections are stored separately and validated against the same full schema.

Profile creation, evidence rows and completed job/run state commit in one transaction.
Text caches and stage transitions commit separately to retain source/progress on failure.
Failures create no partial profile and do not touch other CVs. A resumed active document
gets a new attempt; earlier completed profiles remain available. Re-analysis success
allocates the next profile version; failed attempts remain only in run history.

The profile view joins candidate reference and original filename from Step 1 instead of
asking the LLM for identity. HR corrections never update Candidate.display_name/email
and never merge people. Reviewer identifiers are optional and unauthenticated in this
single-operator local prototype.
