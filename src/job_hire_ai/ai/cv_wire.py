"""Small model-facing records adapted into the unchanged stored profile schema."""

import json
import re
from typing import Annotated, Literal

from pydantic import Field, create_model, model_validator

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction, StrictModel

WIRE_VERSION = "cv-wire-v1"
ShortText = Annotated[str, Field(min_length=1, max_length=180)]


class Quote(StrictModel):
    ref: str = Field(min_length=1, max_length=200)
    quote: ShortText


class Record(StrictModel):
    evidence: list[Quote] = Field(min_length=1, max_length=3)
    note: ShortText | None = None


class Job(Record):
    kind: Literal["job"]
    employer: ShortText | None = None
    job_title: ShortText | None = None
    start_date: ShortText | None = None
    end_date: ShortText | None = None
    current: bool | None = None
    employment_type: ShortText | None = None
    responsibilities: list[ShortText] = Field(default_factory=list, max_length=30)
    technologies: list[ShortText] = Field(default_factory=list, max_length=40)


class Skill(Record):
    kind: Literal["skill"]
    original_wording: ShortText
    name: ShortText | None = None


class Education(Record):
    kind: Literal["education"]
    institution: ShortText | None = None
    degree: ShortText | None = None
    field_of_study: ShortText | None = None
    start_date: ShortText | None = None
    graduation_date: ShortText | None = None


class Certification(Record):
    kind: Literal["cert"]
    name: ShortText
    issuer: ShortText | None = None
    date: ShortText | None = None


class Project(Record):
    kind: Literal["project"]
    project_name: ShortText | None = None
    description: ShortText | None = None
    technologies: list[ShortText] = Field(default_factory=list, max_length=40)
    responsibilities: list[ShortText] = Field(default_factory=list, max_length=30)
    outcomes: list[ShortText] = Field(default_factory=list, max_length=30)


class Language(Record):
    kind: Literal["language"]
    language: ShortText
    proficiency: ShortText | None = None


class Information(Record):
    kind: Literal["domain", "availability", "location", "other"]
    information: ShortText


CompactRecord = Annotated[
    Job | Skill | Education | Certification | Project | Language | Information,
    Field(discriminator="kind"),
]


class CompactCV(StrictModel):
    records: list[CompactRecord] = Field(max_length=40)
    warnings: list[ShortText] = Field(default_factory=list, max_length=8)
    more: bool = False

    @model_validator(mode="after")
    def factual_records(self):
        for record in self.records:
            content = record.model_dump(exclude={"kind", "evidence", "note"}, exclude_none=True)
            if not any(value is not None and value != [] for value in content.values()):
                raise ValueError("A qualification record must contain a documented fact.")
        return self

    def profile(self, references: dict[str, str]) -> CVExtraction:
        result = {section: [] for section in PROFILE_SECTIONS}
        for record in self.records:
            common = {
                "evidence": [
                    {
                        "source_excerpt": quote.quote,
                        "source_reference": references.get(quote.ref, f"unknown:{quote.ref}"),
                    }
                    for quote in record.evidence
                ],
                "claim_status": "UNCERTAIN" if record.note else "SUPPORTED",
                "uncertainty": record.note or "",
            }
            value = record.model_dump(exclude={"kind", "evidence", "note"})
            if isinstance(record, Job):
                section = "employment_history"
                value["currently_employed"] = value.pop("current")
            elif isinstance(record, Skill):
                section = "technical_skills"
                value["name"] = value["name"] or value["original_wording"]
                value["category"] = None
            elif isinstance(record, Education):
                section = "education"
            elif isinstance(record, Certification):
                section = "certifications"
            elif isinstance(record, Project):
                section = "projects"
            elif isinstance(record, Language):
                section = "language_skills"
            else:
                section = {
                    "domain": "relevant_domain_experience",
                    "availability": "availability",
                    "location": "location_information",
                    "other": "additional_job_relevant_information",
                }[record.kind]
            result[section].append({**common, **value})
        known = {s["original_wording"].casefold() for s in result["technical_skills"]}
        for row in result["employment_history"] + result["projects"]:
            for technology in row["technologies"]:
                if technology.casefold() in {
                    (row.get("employer") or "").casefold(),
                    (row.get("job_title") or "").casefold(),
                }:
                    continue
                cited = [
                    quote
                    for quote in row["evidence"]
                    if re.search(
                        r"(?<!\w)" + re.escape(technology) + r"(?!\w)", quote["source_excerpt"]
                    )
                ]
                if cited and technology.casefold() not in known:
                    result["technical_skills"].append(
                        {
                            "name": technology,
                            "original_wording": technology,
                            "category": None,
                            "evidence": cited,
                            "claim_status": row["claim_status"],
                            "uncertainty": row["uncertainty"],
                        }
                    )
                    known.add(technology.casefold())
        return CVExtraction.model_validate_json(
            json.dumps(
                {
                    **result,
                    "extraction_warnings": self.warnings,
                }
            )
        )


def section_schema(segments: list[dict]):
    heading = next((s["section"] for s in segments if s.get("section")), None) or next(
        (s["text"].strip().rstrip(":").casefold() for s in segments if s.get("kind") == "heading"),
        "",
    )
    target = {
        "experience": Job,
        "employment": Job,
        "employment history": Job,
        "work experience": Job,
        "professional experience": Job,
        "skills": Skill,
        "technical skills": Skill,
        "education": Education,
        "certifications": Certification,
        "certificates": Certification,
        "projects": Project,
        "languages": Language,
        "language skills": Language,
    }.get(heading)
    if target is None:
        return CompactCV, None
    label = target.__name__
    if target is Job:
        target = create_model(
            "StatedJob",
            __base__=Job,
            **{
                key: (ShortText | None, ...)
                for key in ("employer", "job_title", "start_date", "end_date")
            },
        )
    elif target is Education:
        target = create_model(
            "StatedEducation",
            __base__=Education,
            **{
                key: (ShortText | None, ...)
                for key in ("institution", "degree", "field_of_study", "graduation_date")
            },
        )
    return create_model(
        f"CV{target.__name__}Response",
        __base__=CompactCV,
        records=(list[target], Field(max_length=40)),
    ), label
