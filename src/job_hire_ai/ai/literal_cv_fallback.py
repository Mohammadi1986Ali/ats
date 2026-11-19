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
    r"^(EXPERIENCE|WORK EXPERIENCE|EMPLOYMENT|EMPLOYMENT HISTORY|"
    r"PROFESSIONAL EXPERIENCE|EDUCATION|QUALIFICATIONS|SKILLS|TECHNICAL SKILLS|"
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


def _extract_clear_records(segments: list[dict], values: dict) -> None:
    section = None
    lines = _source_lines(segments)
    # Preserve source boundaries and avoid guessed records from prose.
    section_lines = []
    for line, ref in lines + [("SUMMARY", "")]:
        match = SECTION_PATTERN.match(line)
        is_heading = bool(match and (
            line.upper() == match.group(1).upper()
            or re.fullmatch(re.escape(match.group(1)) + r"[\s:—–-]*", line, re.IGNORECASE)
        ))
        if is_heading:
            if section == "experience":
                for i in range(len(section_lines) - 2):
                    employer, role, date = section_lines[i:i + 3]
                    dates = DATE_RANGE.fullmatch(date[0])
                    if (
                        dates and ROLE.search(role[0])
                        and len(employer[0]) <= 100 and len(role[0]) <= 120
                        and not employer[0].startswith(("•", "-", "*"))
                        and not role[0].startswith(("•", "-", "*"))
                    ):
                        start = dates.group("start")
                        end = dates.group("end")
                        values["employment_history"].append({
                            "employer": employer[0],
                            "job_title": role[0],
                            "start_date": start,
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
                            "uncertainty": (
                                "Explicit section lines; role details and responsibilities need review."
                            ),
                        })
                        if len(values["employment_history"]) >= 100:
                            break
            elif section == "education":
                for i, (text, source) in enumerate(section_lines):
                    degree = DEGREE.fullmatch(text)
                    if not degree:
                        continue
                    institution = None
                    evidence = [{"source_excerpt": text, "source_reference": source}]
                    if i + 1 < len(section_lines):
                        following, following_ref = section_lines[i + 1]
                        if (
                            re.search(r"\b(university|college|institute|school|polytechnic)\b",
                                      following, re.IGNORECASE)
                            and not DEGREE.match(following)
                        ):
                            institution = following
                            evidence.append({
                                "source_excerpt": following,
                                "source_reference": following_ref,
                            })
                    values["education"].append({
                        "institution": institution,
                        "degree": degree.group("degree"),
                        "field_of_study": degree.group("field").strip() or None,
                        "start_date": None,
                        "graduation_date": None,
                        "evidence": evidence,
                        "claim_status": "UNCERTAIN",
                        "uncertainty": (
                            "Literal education entry; missing dates/institutions are unknown."
                        ),
                    })
                    if len(values["education"]) >= 50:
                        break
            section_lines = []
            heading = match.group(1).upper()
            section = (
                "experience" if heading in {
                    "EXPERIENCE", "WORK EXPERIENCE", "EMPLOYMENT",
                    "EMPLOYMENT HISTORY", "PROFESSIONAL EXPERIENCE",
                }
                else "education" if heading in {"EDUCATION", "QUALIFICATIONS"}
                else None
            )
        elif section:
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
