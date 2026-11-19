"""Conservative CV fallback when a local model cannot emit valid structured JSON.

This is NOT an LLM profile or a complete CV analysis. It extracts explicit
technology mentions and clearly formatted employment/education records from
source text, with original segment references. Always requires manual review.
"""

import json
import re

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction

# These are *literal tokens*, not inferred synonyms or model-generated skills.
KNOWN_TERMS = (
    "Spring Boot", "Spring Framework", "Spring Security", "Spring MVC",
    "Spring Data JPA", "Microservices", "PostgreSQL", "JavaScript", "TypeScript",
    "Kubernetes", "Terraform", "Hibernate", "RabbitMQ", "MongoDB", "Apache Kafka",
    "Kafka", "Redis", "Oracle", "MySQL", "SQLite", "Docker", "Python", "Java",
    "C++", "C#", "Go", "Golang", "Rust", "Scala", "Kotlin", "Linux",
    "AWS", "Azure", "GCP", "Git", "Jenkins", "GitHub Actions", "CI/CD",
    "REST", "GraphQL", "gRPC", "SQL", "NoSQL", "Flink", "Spark",
    "Pandas", "NumPy", "PyTorch", "TensorFlow", "CUDA", "React",
    "Angular", "Vue.js", "Node.js", "FastAPI", "Django", "JUnit",
    "Mockito", "Testcontainers", "JMeter", "JProfiler", "Netty",
    "FIX", "SWIFT",
)

FAST_MODE_WARNING = (
    "PARTIAL DETERMINISTIC EXTRACTION (FAST 1B MODE): the 1B model was not used "
    "for structured CV analysis to avoid long CPU stalls. Literal skills and "
    "clearly formatted employment/education records were extracted; results are partial. "
    "Human review of the original CV is required."
)

FALLBACK_WARNING = (
    "PARTIAL DETERMINISTIC EXTRACTION: the local language model could not "
    "produce valid complete JSON. Only literal skills and clearly formatted "
    "employment/education records were extracted; other qualifications may be missing. "
    "Human review of the original CV is required before relying on this profile."
)



# A conservative parser for CV sections with clear role/date and degree patterns.
# Never infer employment dates, jobs, institutions, or competencies.
SECTION_PATTERN = re.compile(
    r"^(PROFESSIONAL EXPERIENCE|WORK EXPERIENCE|EMPLOYMENT HISTORY|EXPERIENCE|EMPLOYMENT|"
    r"EDUCATION|ACADEMIC BACKGROUND|QUALIFICATIONS|TECHNICAL SKILLS|SKILLS|"
    r"LANGUAGES|CERTIFICATIONS|PROJECTS|SUMMARY)\b",
    re.IGNORECASE,
)
DATE_RANGE = re.compile(
    r"^(?P<start>(?:[A-Za-z]{3,9}\s+)?(?:19|20)\d{2})\s*"
    r"[-–—]\s*"
    r"(?P<end>(?:[A-Za-z]{3,9}\s+)?(?:19|20)\d{2}|Present|Current|Now)"
    r"(?:\s*\((?P<type>[^)]{1,60})\))?$",
    re.IGNORECASE,
)
DEGREE = re.compile(
    r"^(?P<degree>Bachelor(?:'s)?(?:\s+of\s+\w+)?|"
    r"Master(?:'s)?(?:\s+of\s+\w+)?|"
    r"Ph\.?D\.?|Doctor(?:ate)?|BSc|MSc|MBA|BA|MA|BS|MS)"
    r"\s*(?::|in\s+|of\s+)?\s*(?P<field>.*)$",
    re.IGNORECASE,
)
ROLE = re.compile(
    r"\b(engineer|developer|architect|analyst|manager|consultant|"
    r"designer|scientist|administrator|specialist|director|lead|intern|"
    r"researcher|officer|technician)\b",
    re.IGNORECASE,
)


def _source_lines(segments: list[dict]) -> list[tuple[str, str]]:
    result = []
    for segment in segments:
        ref, text = segment.get("reference"), segment.get("text")
        if isinstance(ref, str) and ref and isinstance(text, str):
            for line in text.splitlines():
                line = line.strip()
                if line:
                    result.append((line, ref))
    return result


def _employment_entry(lines: list[tuple[str, str]], offset: int):
    """Find literal employer, title and dates in common adjacent CV layouts."""
    near = lines[offset:offset + 3]
    # Employer / role / date, role / employer / date, or date / role / employer.
    candidates = []
    if len(near) == 3:
        candidates.extend([
            (near[0], near[1], near[2]),
            (near[1], near[0], near[2]),
            (near[2], near[1], near[0]),
        ])
    for employer, role, date in candidates:
        dates = DATE_RANGE.fullmatch(date[0])
        if (
            dates and ROLE.search(role[0]) and not ROLE.search(employer[0])
            and len(employer[0]) <= 100 and len(role[0]) <= 120
            and not employer[0].startswith(("•", "-", "*"))
        ):
            return employer, role, date, dates
    # Many CVs format a row as "Senior Engineer | Company | Jan 2020 - Present".
    if not near:
        return None
    line, ref = near[0]
    parts = [part.strip() for part in re.split(r"\s+[|•]\s+|\s+@\s+", line)]
    if len(parts) == 3:
        for role_idx, employer_idx in ((0, 1), (1, 0)):
            dates = DATE_RANGE.fullmatch(parts[2])
            if dates and ROLE.search(parts[role_idx]) and not ROLE.search(parts[employer_idx]):
                return (
                    (parts[employer_idx], ref), (parts[role_idx], ref),
                    (parts[2], ref), dates,
                )
    return None


def _extract_clear_records(segments: list[dict], values: dict) -> None:
    section = None
    lines = _source_lines(segments)
    section_lines = []

    def finish_section():
        if section == "experience":
            seen = set()
            for i in range(len(section_lines)):
                item = _employment_entry(section_lines, i)
                if item is None:
                    continue
                employer, role, date, dates = item
                key = (employer[0].casefold(), role[0].casefold(), date[0])
                if key in seen:
                    continue
                seen.add(key)
                end = dates.group("end")
                values["employment_history"].append({
                    "employer": employer[0],
                    "job_title": role[0],
                    "start_date": dates.group("start"),
                    "end_date": end,
                    "currently_employed": end.casefold() in {"present", "current", "now"},
                    "employment_type": dates.group("type"),
                    "responsibilities": [],
                    "technologies": [],
                    "evidence": [
                        {"source_excerpt": text, "source_reference": source}
                        for text, source in (employer, role, date)
                    ],
                    "claim_status": "UNCERTAIN",
                    "uncertainty": "Literal job entry; duties and duration need human review.",
                })
                if len(values["employment_history"]) >= 100:
                    break
        elif section == "education":
            for i, (line, ref) in enumerate(section_lines):
                degree = DEGREE.fullmatch(line)
                if degree is None:
                    continue
                institution = None
                evidence = [{"source_excerpt": line, "source_reference": ref}]
                if i + 1 < len(section_lines):
                    candidate, candidate_ref = section_lines[i + 1]
                    if (
                        re.search(
                            r"\b(university|college|institute|school|polytechnic)\b",
                            candidate, re.IGNORECASE,
                        )
                        and not DEGREE.match(candidate)
                    ):
                        institution = candidate
                        evidence.append({
                            "source_excerpt": candidate, "source_reference": candidate_ref,
                        })
                values["education"].append({
                    "institution": institution,
                    "degree": degree.group("degree"),
                    "field_of_study": degree.group("field").strip() or None,
                    "start_date": None,
                    "graduation_date": None,
                    "evidence": evidence,
                    "claim_status": "UNCERTAIN",
                    "uncertainty": "Literal degree entry; dates and institution require review.",
                })
                if len(values["education"]) >= 50:
                    break

    for line, ref in lines + [("SUMMARY", "")]:
        match = SECTION_PATTERN.match(line)
        heading = bool(match and re.fullmatch(
            re.escape(match.group(1)) + r"[\s:—–-]*",
            line, re.IGNORECASE,
        ))
        if heading:
            finish_section()
            section_lines = []
            value = match.group(1).upper()
            section = (
                "experience" if value in {
                    "EXPERIENCE", "WORK EXPERIENCE", "EMPLOYMENT",
                    "EMPLOYMENT HISTORY", "PROFESSIONAL EXPERIENCE",
                }
                else "education" if value in {
                    "EDUCATION", "ACADEMIC BACKGROUND", "QUALIFICATIONS",
                }
                else None
            )
        elif section is not None:
            section_lines.append((line, ref))


def extract_literal_skills(
    segments: list[dict], *, warning: str = FALLBACK_WARNING
) -> CVExtraction:
    """Extract a *partial* explicitly labelled profile with genuine source quotes.

    No LLM output is consumed. Each quote is exactly the matched substring from
    the source segment, retaining the original reference. Do not fabricate
    experience dates, degrees, employers, proficiency or undocumented skills.
    """
    skills = {}
    terms = sorted(KNOWN_TERMS, key=len, reverse=True)
    for segment in segments:
        text = segment.get("text", "")
        ref = segment.get("reference")
        if not isinstance(text, str) or not isinstance(ref, str) or not ref:
            continue
        spans = []
        for term in terms:
            for match in re.finditer(
                r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE
            ):
                start, end = match.span()
                # Avoid overlapping claims, e.g. "Java" inside another term.
                if any(start < prior_end and end > prior_start for prior_start, prior_end in spans):
                    continue
                spans.append((start, end))
                original = match.group()
                key = term.casefold()
                if key not in skills:
                    skills[key] = {
                        "name": term,
                        "original_wording": original,
                        "category": None,
                        "evidence": [{
                            "source_excerpt": original,
                            "source_reference": ref,
                        }],
                        "claim_status": "UNCERTAIN",
                        "uncertainty": (
                            "Literal technology mention only; role, experience, "
                            "proficiency and context require human review."
                        ),
                    }
                else:
                    quote = {"source_excerpt": original, "source_reference": ref}
                    if (
                        quote not in skills[key]["evidence"]
                        and len(skills[key]["evidence"]) < 20
                    ):
                        skills[key]["evidence"].append(quote)
                break
    values = {section: [] for section in PROFILE_SECTIONS}
    values["technical_skills"] = list(skills.values())[:200]
    _extract_clear_records(segments, values)
    values["extraction_warnings"] = [warning]
    if not skills:
        values["extraction_warnings"].append(
            "No technologies from the limited literal vocabulary were found. "
            "This does not mean the candidate has no qualifications."
        )
    return CVExtraction.model_validate_json(json.dumps(values))
