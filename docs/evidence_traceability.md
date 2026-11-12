# CV evidence, provenance and limitations

Source extraction is deterministic and separate from model inference. The private SQLite
source cache stores original and normalized text per segment. PDF source locations identify
page/block or page/table/row; DOCX locations identify body paragraph or table/row. Paragraph
and table order is preserved. PDF reading order and table layout depend on document structure
and PyMuPDF heuristics; they are not guaranteed to match a human's visual reading order.

Every extracted record must include at least one nonblank excerpt and source reference
(a missing reference may be explicitly null). Server-side evidence rows receive generated
IDs and the actual document ID. Verification compares NFKC-normalized text while collapsing
whitespace, without case-insensitive, fuzzy or semantic matching. A matching unique excerpt
is VERIFIED. Multiple matches without sufficient location specificity are AMBIGUOUS.
Missing quotes or wrong supplied locations are UNVERIFIED; the application never invents
replacement evidence. Verified offsets address the concatenation of normalized segments
separated by two newlines, not original PDF bytes or DOCX XML. Page/location and excerpt
remain available even when offsets are unknown.

All factual values in a model record should be supported by its quote(s), as required by
the versioned prompt. Exact quote verification proves text location only. Additional
conservative checks flag numeric/date claims absent from the quoted evidence, skill
original wording absent from its quotes, and CEFR levels not explicitly quoted. These
checks do not establish every paraphrase, employer, responsibility or skill synonym is
semantically justified. An invented claim attached to a real quote can still evade them.
NEEDS_REVIEW is used for warnings, uncertain claims or evidence issues. COMPLETED means
the extraction pipeline completed without detected issues, not that HR approved every claim.

SUPPORTED and UNCERTAIN are the model's preserved claim classifications. Empty sections
are displayed as NOT_DOCUMENTED, never as a failed qualification. Partial employment dates
remain strings as stated, with null for missing data. No employment duration, total years,
career-gap penalty, score or suitability judgment is calculated; overlaps cannot be
double-counted because this step calculates no experience duration.

Three layers remain distinct:

1. Original/normalized source segments, which never change in place.
2. The validated original AI profile and immutable evidence-verification rows.
3. Append-only HR correction and validation events, applied as an effective profile view.

Original evidence always refers to the original extraction. A changed value is visibly
HR corrected; it does not inherit proof from the original quote automatically. Whole-section
corrections can add omitted records and their manually supplied quotes, but those overlays
are HR-authored, not newly model-verified evidence. The original remains available for
comparison. HR must inspect source locations and explicitly acknowledge issues when
validating. Validation leaves source verification flags unchanged and records an audit event;
a later correction requires renewed validation.

The system prompt separates CV JSON data from instructions and rejects embedded requests
to score, rank, recommend or invent content. The schema has no candidate identity, contact,
age, gender, ethnicity, religion, marital status, sexual orientation, disability, nationality,
rating or scoring fields. Obvious personal/protected labels in generic information records
are rejected as well. These guards cannot prove freedom from injection, protected-trait
proxies or irrelevant free-text claims; HR must remove inappropriate information. Names
and photos are not qualification evidence. Source CVs/raw extracted text may still contain
personal information and remain private to authorized workstation users.

Inference uses the existing loopback-only Ollama client with proxies/redirects disabled.
No external inference API or model auto-download is introduced. Original CV files remain
UUID-named in private storage. Uploaded checksums are checked before analysis. CV text,
raw model output and validation exception payloads are not written to ordinary logs.
Only sanitized operational errors are shown/stored. The model digest, prompt/schema
versions, options, checksums, timings and chunk counts are retained for reproducibility.

Application removal cascades all source/profile/evidence/review data for its documents;
diagnostic audit records retain metadata with document IDs set null. This is not complete
erasure of backups, disk free space or the original anonymous candidate/application history.
Use OS permissions, disk encryption and an operator-managed retention policy. This local
prototype is not suitable for public deployment, legal compliance claims or autonomous
recruitment decisions.
