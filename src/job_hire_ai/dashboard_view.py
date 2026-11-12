"""Read-only dashboard projections. Never synthesize hiring scores or pipeline progress."""

from collections import Counter
from dataclasses import dataclass
from typing import Any

STAGES = (
    "TEXT_EXTRACTION",
    "EMBEDDING",
    "PROFILE_EXTRACTION",
    "EVIDENCE_VALIDATION",
    "MATCHING",
    "SCORING",
    "HR_REVIEW",
)
LABELS = {
    "TEXT_EXTRACTION": "Text extraction",
    "EMBEDDING": "Text embedding",
    "PROFILE_EXTRACTION": "Profile extraction",
    "EVIDENCE_VALIDATION": "Evidence validation",
    "MATCHING": "Job matching",
    "SCORING": "Scoring",
    "HR_REVIEW": "HR review",
}
FUTURE_STAGES = set(STAGES[2:])
STATUS_TEXT = {
    "PENDING": "Queued",
    "RUNNING": "Running",
    "COMPLETED": "Completed",
    "FAILED": "Failed",
    "WAITING": "Not active yet",
}
STAGE_ICONS = {
    "PENDING": "◷",
    "RUNNING": "●",
    "COMPLETED": "✓",
    "FAILED": "✕",
    "WAITING": "—",
}


@dataclass(frozen=True)
class JobPulse:
    vacancy_id: int
    job: str
    job_status: str
    uploaded: int
    queued: int
    running: int
    completed: int
    failed: int

    @property
    def progress(self) -> int:
        return round(100 * self.completed / self.uploaded) if self.uploaded else 0

    @property
    def in_progress(self) -> int:
        return self.queued + self.running

    @property
    def state(self) -> str:
        if not self.uploaded:
            return "No CVs"
        if self.running:
            return "Processing"
        if self.queued:
            return "Queued"
        if self.failed:
            return "Needs attention"
        return "Text extracted"


def job_pulses(vacancies: list[Any], items: list[dict]) -> list[JobPulse]:
    """Keep jobs with zero uploads and archived jobs visible."""
    counts: dict[int, Counter] = {}
    for row in items:
        vid = row["Job ID"]
        counts.setdefault(vid, Counter())[row.get("TEXT_EXTRACTION", "UNKNOWN")] += 1
    result = []
    for vacancy in vacancies:
        c = counts.get(vacancy.id, Counter())
        result.append(
            JobPulse(
                vacancy_id=vacancy.id,
                job=vacancy.title,
                job_status=vacancy.status,
                uploaded=sum(c.values()),
                queued=c["PENDING"],
                running=c["RUNNING"],
                completed=c["COMPLETED"],
                failed=c["FAILED"],
            )
        )
    return result


def aggregate(pulses: list[JobPulse]) -> dict[str, int]:
    return {
        "Jobs": len(pulses),
        "Uploaded CVs": sum(p.uploaded for p in pulses),
        "Queued / Running": sum(p.in_progress for p in pulses),
        "Text complete": sum(p.completed for p in pulses),
        "Failed": sum(p.failed for p in pulses),
    }


def stage_breakdown(items: list[dict]) -> list[dict]:
    """Counts use each document once per stage; waiting stages are explicitly inactive."""
    rows = []
    for stage in STAGES:
        statuses = Counter(row.get(stage, "WAITING") for row in items)
        rows.append(
            {
                "Stage": LABELS[stage],
                "Completed": statuses["COMPLETED"],
                "Running": statuses["RUNNING"],
                "Queued": statuses["PENDING"],
                "Failed": statuses["FAILED"],
                "Waiting": statuses["WAITING"],
                "Availability": (
                    "Not implemented" if stage in FUTURE_STAGES
                    else "Not enabled" if stage == "EMBEDDING" and
                    statuses["WAITING"] == len(items) else "Active"
                ),
            }
        )
    return rows


def current_stage(row: dict) -> tuple[str, str]:
    """Prefer failed/running/pending stage, otherwise first waiting stage."""
    for status in ("FAILED", "RUNNING", "PENDING"):
        for stage in STAGES:
            if row.get(stage) == status:
                return LABELS[stage], STATUS_TEXT[status]
    for stage in STAGES:
        if row.get(stage) == "WAITING":
            return LABELS[stage], "Not active yet"
    return "Complete", "Completed"


def document_rows(items: list[dict], query: str = "", status: str = "All") -> list[dict]:
    output = []
    q = query.strip().casefold()
    for row in items:
        stage, state = current_stage(row)
        if status != "All" and state != status:
            continue
        haystack = f"{row.get('Filename', '')} {row.get('CV', '')} {row.get('Application', '')}"
        if q and q not in haystack.casefold():
            continue
        result = {
            "CV ID": row["CV"],
            "Application": row["Application"],
            "Filename": row["Filename"],
            "Current stage": stage,
            "Status": state,
            "Text": STAGE_ICONS.get(row.get("TEXT_EXTRACTION"), "?"),
            "Embedding": STAGE_ICONS.get(row.get("EMBEDDING"), "?"),
            "Profile": STAGE_ICONS.get(row.get("PROFILE_EXTRACTION"), "?"),
            "Evidence": STAGE_ICONS.get(row.get("EVIDENCE_VALIDATION"), "?"),
            "Matching": STAGE_ICONS.get(row.get("MATCHING"), "?"),
            "Scoring": STAGE_ICONS.get(row.get("SCORING"), "?"),
            "HR review": STAGE_ICONS.get(row.get("HR_REVIEW"), "?"),
            "Text duration (s)": (
                round(row["TEXT_EXTRACTION_duration_ms"] / 1000, 2)
                if row.get("TEXT_EXTRACTION_duration_ms") is not None
                else None
            ),
            "Error": row.get("TEXT_EXTRACTION_error") or row.get("EMBEDDING_error") or "",
        }
        output.append(result)
    return sorted(output, key=lambda r: (r["Status"] != "Failed", r["CV ID"]))


def failure_rows(items: list[dict]) -> list[dict]:
    return [
        {
            "CV ID": row["CV"],
            "Job": row["Job"],
            "Application": row["Application"],
            "Filename": row["Filename"],
            "Stage": LABELS[stage],
            "Error": row.get(f"{stage}_error") or "No diagnostic available",
        }
        for row in items
        for stage in STAGES
        if row.get(stage) == "FAILED"
    ]
