"""Conservative CV fallback when a local model cannot emit valid structured JSON.

This is NOT an LLM profile or a complete CV analysis. It only recognizes a
small explicit technology vocabulary in the actual extracted CV text, with
literal quotations and original segment IDs. Always requires manual review.
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

FALLBACK_WARNING = (
    "PARTIAL DETERMINISTIC EXTRACTION: the local language model could not "
    "produce valid complete JSON. Only literally observed technologies were "
    "extracted; employment, education and other qualifications may be missing. "
    "Human review of the original CV is required before relying on this profile."
)


def extract_literal_skills(segments: list[dict]) -> CVExtraction:
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
                    if quote not in skills[key]["evidence"]:
                        skills[key]["evidence"].append(quote)
                break
    values = {section: [] for section in PROFILE_SECTIONS}
    values["technical_skills"] = list(skills.values())[:200]
    values["extraction_warnings"] = [FALLBACK_WARNING]
    if not skills:
        values["extraction_warnings"].append(
            "No technologies from the limited literal vocabulary were found. "
            "This does not mean the candidate has no qualifications."
        )
    return CVExtraction.model_validate_json(json.dumps(values))
