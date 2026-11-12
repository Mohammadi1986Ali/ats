# HR criteria configuration and versioning

Configurations belong to exactly one vacancy. A partial unique index allows one DRAFT
and one current APPROVED configuration per vacancy. `(vacancy_id, version)` is unique.
Version numbers are allocated when a draft is first created, starting at 1. Re-analysis
of an explicitly replaceable draft keeps its version; replacing an approved configuration
requires a distinct new draft/version. Only drafts can be edited.

The editor supports adding/removing rows, editing category/name/description/type/evidence,
uncertainty notes and weights. Set unique display_order integers (1–50) to reorder; saving
sorts by that order. Up to 50 criteria are supported. A hidden stable criterion reference
connects saved edits to their provenance. New rows are MANUAL. AI rows retain their original
extraction payload; editing their substantive content marks them AI_MODIFIED. Changing a
weight/order alone does not change extraction provenance: all weights are HR decisions.
Removing a draft criterion does not remove it from any prior approved version.

Weights use nullable integer percentage points. Null means unassigned. Each weight must
be an integer from 0 to 100, and the total must be exactly 100 for approval. Zero-weight
criteria are explicitly supported: they remain visible but have no configured importance.
Decimals, booleans, negative values and numeric strings are rejected by the service schema.
The UI uses Decimal for the running total and normalizes whole-valued dataframe numbers
to integers. These weights are stored priorities only; no candidate score is calculated.
There is no universal or automatically normalized set of weights.

Save Draft persists the table without approving it. Approval requires nonempty criteria,
required fields, all weights assigned and totaling 100, a matching current description,
and every evidence problem either corrected or acknowledged with a nonblank review note.
An acknowledgement leaves evidence UNVERIFIED and remains visible in historical versions.
When an existing excerpt is edited its previous acknowledgement is reset: save the correction,
then review and acknowledge it again if necessary. Source evidence is always recomputed
server-side, never trusted from the browser. Manual additions are subject to the same policy.

Unsaved table edits disable approval. An explicit checkbox confirms HR review of the saved
configuration. An optional reviewer identifier is retained but is **not authenticated** in
this local prototype. HR remains responsible for relevant, appropriate selection criteria;
model generation or an exact quote establishes neither lack of bias nor legal compliance.

Approval atomically marks any previous APPROVED version SUPERSEDED and promotes the saved
draft to APPROVED, recording approval timestamp/reviewer. SQLite triggers block updates,
inserts or deletions of criteria under historical configurations and block changes to
approved configuration content. The only allowed historical configuration update is
APPROVED -> SUPERSEDED with its content unchanged. Database checks also guard approval
totals, null weights, unresolved evidence and source snapshots. Service checks provide
user-facing errors. Optimistic revisions reject edits/approval based on a stale draft.
SQLite constraints also prevent two current drafts or current approvals per vacancy.

Changing the description does not mutate or supersede the approved configuration.
Its SHA-256 no longer matches; Job Management and Dashboard flag it Outdated and exclude
it from readiness counts. A stale draft cannot be approved. Re-analyze to obtain a fresh
draft, or create a new manual draft from an existing approval and review every carried-over
criterion against the current description. Carried-over acknowledgements are cleared when
the description changed. Only a new explicit approval supersedes the old approved version.

Dashboard's four additional metrics are vacancy-scoped: total vacancies; current matching
approvals (AI-assisted or manual); vacancies with a draft; vacancies with an outdated
current approval. Draft and outdated counts can overlap. Vacancies without a current
approval, with a draft, or with an outdated approval appear in unfinished configuration
navigation. All five Step 1 metrics remain and keep their previous definitions.
