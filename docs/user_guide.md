# User guide

Run `uv sync`, `uv run alembic upgrade head` and `uv run streamlit run app.py` from
the project directory. See the README for installation and local privacy limitations.

**Job Management:** create a job with a title and description. Select it in the table's
selector to view details, edit fields, archive or reactivate. Status filters include
historical vacancies. Archiving preserves every candidate, application and CV.

**Candidate Management:** select an active job, choose up to 100 PDFs/DOCXs, review the
validation preview and submit. An invalid file does not prevent valid files being stored.
Rejections explain duplicate content, invalid documents, unsupported filenames/types or
size limits. Existing applicant association requires explicitly selecting the candidate's
reference. Default uploads create anonymous candidates; no identity information is invented.
Scanned PDFs remain downloadable but show that text extraction is unavailable.

Use search and status filters to find applications. Select an application for document
metadata, a PDF page or DOCX text preview, and the original download. Open the downloaded
CV in your local viewer for full formatting. To remove
its files, select the confirmation checkbox and click Remove application. This is permanent;
a Removed history record and candidate reference remain. If cleanup fails, filter Removed
and click Retry file deletion after resolving local file permissions. Archived jobs still
allow historical downloads and removal, but require reactivation for new uploads.

**Dashboard:** choose All vacancies or a specific vacancy. Metrics and tables reflect real
stored data, including zero values for an empty database. Candidate counts retain history;
application/CV counts reflect current uploads. Restarting preserves all records and files.

**AI Job Requirements (Step 2):** in Job Management select a vacancy and scroll to the new
section. With local Llama installed, analyze the description, review the editable criteria,
set weights totaling 100%, save the draft and explicitly approve it. Approved versions are
immutable; create a new draft for changes. See [job analysis](job_analysis.md) and
[criteria configuration](criteria_configuration.md) for evidence, weighting and history.
These operations do not perform CV AI extraction or candidate screening.

**CV Analysis (Step 3):** choose a vacancy and select its uploaded CVs. Analyze Selected
or Analyze All Pending starts a persistent sequential batch. Inspect extracted profiles,
source evidence, warnings and prior versions. Use HR corrections/validation to store
auditable changes separately from the AI output. Re-analyze creates a new version;
Retry Failed and Run / Resume Batch recover incomplete work without discarding successful
profiles. See [CV analysis](cv_analysis.md). No matching, ranking or scoring is performed.
