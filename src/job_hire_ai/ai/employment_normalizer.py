"""Conservative, source-grounded normalization of extracted CV job records.

A job is an employer + role, not a responsibility, CV bullet, or isolated
technology. Never manufacture an employer, title or dates when repairing the
output of a small local model.
"""
import copy
import re

from job_hire_ai.ai.cv_schemas import CVExtraction
from job_hire_ai.text_extraction import normalize_text

NORMALIZATION_VERSION = "employment-records-v2"
PLACEHOLDERS = {"", "unknown", "n/a", "none", "null", "not provided", "not specified"}


def _normalized(value):
    return " ".join(normalize_text(value or "").casefold().split())


def _is_title(value):
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= 100:
        return False
    if _normalized(value) in PLACEHOLDERS:
        return False
    if any(char in value for char in "\n•"):
        return False
    if len(value.split()) > 12 or value.strip().endswith((".", ":", ";")):
        return False
    return True


def _is_employer(value):
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= 100:
        return False
    if _normalized(value) in PLACEHOLDERS:
        return False
    if any(char in value for char in "\n•"):
        return False
    if len(value.split()) > 12 or value.strip().endswith((".", ":", ";")):
        return False
    return True


def _mentioned_in_source(value, source_segments):
    token = _normalized(value)
    if not token:
        return False
    # Avoid matching short partial words such as "in" inside "banking".
    pattern = re.compile(r"(?<!\w)" + re.escape(token) + r"(?!\w)")
    return any(pattern.search(_normalized(segment.text)) for segment in source_segments)


def normalize_employment(extraction: CVExtraction, source):
    """Keep only source-supported employer/role pairs; merge duplicated roles.

    A role may be mentioned across adjacent CV blocks. Do not require all
    dates to be present, but retain separate entries with conflicting dates,
    as those could represent distinct periods of employment.
    """
    payload = extraction.model_dump(mode="json")
    original = payload["employment_history"]
    kept = []
    by_identity = {}
    dropped = 0
    merged = 0
    for row in original:
        employer = row.get("employer")
        title = row.get("job_title")
        if (
            not _is_employer(employer) or not _is_title(title)
            or not _mentioned_in_source(employer, source.segments)
            or not _mentioned_in_source(title, source.segments)
            or _normalized(employer) == _normalized(title)
        ):
            dropped += 1
            continue
        key = (_normalized(employer), _normalized(title))
        old_index = next(
            (index for index in by_identity.get(key, []) if all(
                not (kept[index].get(field) and row.get(field)
                     and _normalized(kept[index][field]) != _normalized(row[field]))
                for field in ("start_date", "end_date")
            )), None,
        )
        if old_index is None:
            kept.append(copy.deepcopy(row))
            by_identity.setdefault(key, []).append(len(kept) - 1)
            continue
        saved = kept[old_index]
        merged += 1
        for field in ("start_date", "end_date", "employment_type", "currently_employed"):
            if saved.get(field) is None and row.get(field) is not None:
                saved[field] = row[field]
        for field in ("technologies", "responsibilities", "evidence"):
            for value in row.get(field) or []:
                if value not in saved[field]:
                    saved[field].append(value)
        if row.get("claim_status") == "UNCERTAIN":
            saved["claim_status"] = "UNCERTAIN"
        saved["uncertainty"] = "; ".join(dict.fromkeys(
            text for text in (saved.get("uncertainty"), row.get("uncertainty")) if text
        ))[:2000]
    payload["employment_history"] = kept
    if dropped or merged:
        payload["extraction_warnings"].append(
            f"Employment record normalization: combined {merged} duplicate "
            f"role entries and excluded {dropped} incomplete/unsupported records. "
            "Inspect the original CV for missing or ambiguous work history."
        )
    return CVExtraction.model_validate(payload)
