# Step 3: CV analysis workflow

This step extracts documented qualifications, not suitability, match scores, rankings,
Strong/Medium/Weak labels or hiring decisions. Job criteria and weights are not inputs
to CV analysis. A candidate retains their Step 1 identity and is never merged with
another candidate based on name, filename, skill or model output.

## Start and use

Ollama must be running locally with the configured model. From PowerShell:

```powershell
ollama list
ollama pull llama3.2:1b   # only if missing; never downloaded by the application
ollama serve             # only if the desktop app/server is not already running
```

In another terminal from the project directory:

```powershell
uv sync
uv run alembic upgrade head
uv run streamlit run app.py
```

1. Create/select a vacancy and upload PDF/DOCX CVs in Candidate Management.
2. Open **CV Analysis** and select the vacancy. The table shows actual CVs, latest
   processing status, latest extraction version and failure details.
3. Select CVs and click **Analyze Selected**, or click **Analyze All Pending**.
   Both persist a batch before starting sequential processing immediately. At most
   100 documents are processed per batch; all-pending queues the first 100 if more remain.
4. Watch current document/chunk, progress counts and the elapsed-time loading indicator.
   Each completed CV/profile commits immediately. A failed CV does not discard others.
5. Select a candidate/document/extraction version in the profile section. Inspect
   employment, skills, education, projects, certifications, languages and other sections.
   Empty sections show NOT_DOCUMENTED, which is not a confirmed lack of qualification.
6. Open **Source evidence viewer** for excerpt verification, PDF page references,
   DOCX paragraph/table references and the normalized source text at each location.
7. In **HR corrections and validation**, enter a field path such as
   `technical_skills.0.name`, a JSON value such as `"Java"`, a reviewer if available,
   and a review note. **Save Correction** appends an audit event; it never rewrites AI output.
   For an omitted record, a whole section such as `education` can be replaced with a
   complete schema-valid JSON array. The displayed schema/original extraction provides
   the required shape. This advanced editor requires JSON for lists/nulls/structured records.
8. Review corrected claims against the CV, enter a validation note and explicitly
   acknowledge warnings/evidence issues. **Save Validated Profile** records HR validation
   separately. It does not make unverified evidence verified or establish legal suitability.
9. To generate a fresh extraction, select the re-analysis confirmation and click
   **Re-analyze**. A new successful extraction version is created; prior versions remain.

## Persistent batches, retry and resume

Batch/job rows are in SQLite, not just session state. Worker stages are PENDING,
EXTRACTING, ANALYZING, VALIDATING, COMPLETED, FAILED and NEEDS_REVIEW. The page's batch
history lists attempts, chunk counts, duration, reused extraction and safe failure messages.
The same configured local model is used sequentially; no per-CV Ollama process is launched.
An OS file lock in the data directory prevents concurrent CV workers for that dataset.
Tokens/leases identify the active job; profile saving checks ownership and application state.

Ordinary Streamlit reruns do not restart a batch. If the app/navigation/process interrupts
processing, click **Run / Resume Batch**. Once the OS lock is available, active incomplete
jobs are marked interrupted and requeued; completed profiles stay intact. The interrupted
CV starts a fresh attempt from its first chunk; partial chunk results are not checkpointed.
**Retry Failed** requeues surviving failed documents and starts processing them. Deleted
documents remain failed with a clear message; they cannot be retried without a new upload.
This is a synchronous local worker, not a background scheduler. Navigating away can
interrupt active work; resume explicitly. Review operations and basic ATS pages do not
require a model to be online.

## Cache and extraction versions

Before inference, SHA-256 is checked against the original stored file. Text extractions
are cached per document/checksum/extractor version. Profile reuse is scoped to the same
document and fingerprint of model tag/digest, inference options, prompt/schema version,
extractor and chunker. Other document records always get their own analysis, even if
bytes match across vacancies. The configured model must be available to verify its digest.

A matching valid structured extraction is reused by default, including existing review
flags/corrections; reuse does not mean HR approved it. Failed analyses create no profile.
Explicit re-analysis bypasses reuse and allocates a new per-document extraction version
only on successful persistence. Original payload/evidence/source/review-event rows cannot
be updated: database triggers enforce immutability. HR-corrected values are overlays from
append-only events, with original value, corrected value, reviewer, timestamp and note.
Another correction invalidates the current HR validation until reviewed again.

## Long CVs and CPU settings

Extraction retains PDF blocks/pages and detectable table rows, plus DOCX body paragraphs,
headings and table rows. Normalization preserves dates, numbers, technical terms and
section boundaries; it never summarizes source text. DOCX non-body content (images,
text boxes, nested tables, headers/footers, embedded objects) is not extracted and produces
an explicit warning. Empty PDF pages produce OCR/review warnings; image-only CVs fail with
an OCR-required message. There is no OCR in this step.

Long text is packed by structural units into sequential chunks. Oversized paragraphs
split on lines, sentences and then word boundaries, retaining the original location ID.
An unbroken span that cannot fit is rejected, not silently sliced. No content is truncated.
The chunk budget is `min(10000, 2 * (NUM_CTX - NUM_PREDICT - 2800))`, including unit metadata;
the reserve covers compact CV schema instructions (2592 characters per chunk by default).
The full response schema remains enforced through Ollama's structured-output format.
This is a conservative character
heuristic, not the model tokenizer. Limits are 250,000 normalized characters, 200 PDF pages,
100 chunks and 100 documents per batch; exceeding them produces an explicit failure.
Exact duplicate structured claims merge with their distinct source quotes. Semantically
similar/different roles are not automatically merged. Chunk counts and completed chunks
are stored on each run; warnings record recognized omissions and unresolved source issues.

The existing OLLAMA settings apply (CPU `num_gpu=0`, temperature 0, seed 42, 8 threads,
8192 context and 4096 output tokens by default). Temperature zero does not guarantee
identical output. CPU extraction can take minutes per chunk. The default
`OLLAMA_TIMEOUT` is 600 seconds per attempt; consider `OLLAMA_RETRIES=0`
to avoid a second long request on timeouts; restart Streamlit after changing `.env`.
Truncated JSON is rejected with an actionable output-token error, never fabricated or
silently repaired. A changed model digest during a document causes failure/retry.

## Dashboard and privacy

Six additional metrics show total uploaded CVs, analyzed CVs, pending analysis, latest
failed CVs, latest profiles requiring review and average successful processing duration.
Per-vacancy status counts are also shown. Failed CVs can have a previous valid profile;
these metrics therefore need not sum to total CVs. Duration averages exclude cache reuse
and failed attempts. The original Step 1/2 metrics retain their definitions.

Removing an application deletes its CVs and cascades source text, profiles, evidence and
review events. Diagnostic batch/run metadata remains with document links cleared. Local
backups are separate copies requiring operator retention/deletion. The prototype has no
production authentication/authorization or database encryption; use a trusted workstation
and never expose it publicly. No CV text/raw model output is logged or sent to external
APIs. Structured extraction omits candidate names, contacts and protected attributes.
Prompt injection and semantic hallucination are mitigated, not eliminated; HR must review.

Run `uv run pytest` and `uv run ruff check .`. Normal tests mock the LLM. To test a real
synthetic-CV pipeline without using applicant data:

```powershell
$env:ATS_TEST_LIVE_OLLAMA = "1"
uv run pytest tests/integration/test_live_ollama.py::test_live_cv_pipeline -q -s
Remove-Item Env:ATS_TEST_LIVE_OLLAMA
```
