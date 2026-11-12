import json

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction


def merge_extractions(extractions: list[CVExtraction]) -> CVExtraction:
    """Deduplicate identical claims, combining their original quoted evidence only."""
    merged = {section: [] for section in PROFILE_SECTIONS}
    warnings = []
    for extraction in extractions:
        payload = extraction.model_dump(mode="json")
        warnings.extend(payload["extraction_warnings"])
        for section in PROFILE_SECTIONS:
            existing = {
                json.dumps({k: v for k, v in row.items() if k != "evidence"}, sort_keys=True): row
                for row in merged[section]
            }
            for row in payload[section]:
                key = json.dumps({k: v for k, v in row.items() if k != "evidence"}, sort_keys=True)
                if key in existing:
                    for quote in row["evidence"]:
                        if quote not in existing[key]["evidence"]:
                            existing[key]["evidence"].append(quote)
                else:
                    merged[section].append(row)
                    existing[key] = row
    return CVExtraction.model_validate_json(
        json.dumps({**merged, "extraction_warnings": list(dict.fromkeys(warnings))})
    )
