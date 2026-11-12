# Step 2: job-description analysis

Job Management retains its vacancy controls and adds AI Job Requirements beneath them.
Analysis sends only the selected vacancy's description to the local Ollama endpoint.
Candidate records, applications and uploaded CVs never enter this workflow.

The provider checks connectivity and the configured model tag first. The UI caches
availability for 15 seconds and offers an explicit refresh. Analysis always checks again.
With the model missing, run `ollama pull llama3.2:1b`; with the server unavailable,
start the Ollama desktop app or run `ollama serve`. The app never pulls models.
Unrelated vacancy/CV operations continue to work while inference is offline.

The versioned system prompt `job-requirements-v1` specifies grounded job qualifications,
required/preferred/unclear distinctions, exact supporting quotes, and uncertainty notes.
It forbids invented qualifications, weights, candidate evaluation and irrelevant personal
attributes. JSON document input is sent in a separate user message and explicitly treated
as untrusted data. Embedded instructions must be ignored. Prompt separation reduces
injection risk but does not guarantee immunity; HR must inspect every result.

Pydantic's strict `Extraction` schema requires an array of at most 50 criteria, each with
name, category, description, requirement_type, source_excerpt, extraction_notes and
uncertainty. Fields cannot be omitted; notes/uncertainty can be explicitly empty strings.
Category and requirement type are enums. Unknown fields, including model-assigned
weights, invalidate the output. Empty extraction arrays are allowed but cannot be approved.
The JSON schema is supplied both through Ollama's structured-output `format` parameter
and in the system message. Invalid JSON, missing fields, unexpected fields, invalid enum
values and truncated responses are rejected without overwriting any existing draft.

Source verification checks that each nonblank excerpt is an exact, contiguous, case-sensitive
substring of the version's stored description snapshot. No fuzzy matching silently repairs
quotes. A valid schema with a nonmatching excerpt is saved as UNVERIFIED for HR correction,
not presented as grounded evidence. Exact quotes do not establish that the interpretation,
priority or numeric threshold is correct. For example, 'Senior' does not establish five
years, and quoting a sentence mentioning Kafka does not make a preferred skill mandatory.
HR must remove unsupported or inappropriate criteria even when the quote matches.

Analysis creates an unapproved draft, with every weight null. Each run stores model tag,
available model digest, prompt version, input SHA-256, inference options, start/completion
UTC timestamps, wall-clock duration, status and sanitized failure summary. Raw model output
and full prompts are not logged. Completed configurations retain the description snapshot
and original per-criterion extraction for audit. The UI shows the ten most recent runs;
all run rows persist in SQLite. A hard process interruption can leave a RUNNING audit row;
a new analysis can be started, and no approval is inferred from that incomplete run.

Analysis checks the description fingerprint again after inference. If the description or
draft revision changed during inference, saving is rejected, preserving the current draft.
Re-analysis never overwrites approved history. An explicit replacement checkbox permits
replacing a DRAFT only, and weights are reset to unassigned in that replacement.

Fictional Java, Data Engineer and DevOps descriptions with expected mock outputs live in
`tests/fixtures/job_descriptions.json`. They are neither production seed data nor provider
fallbacks. Their classifications/evidence are tested without any model service.
