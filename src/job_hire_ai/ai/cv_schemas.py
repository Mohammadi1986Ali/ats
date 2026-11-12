"""Qualification extraction schemas. No identity, protected traits or suitability fields."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "candidate-profile-v1"


class ClaimStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNCERTAIN = "UNCERTAIN"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class EvidenceQuote(StrictModel):
    source_excerpt: str = Field(min_length=1, max_length=5000)
    source_reference: str | None = Field(description="Exact segment ID supplied with the CV text")


class SupportedRecord(StrictModel):
    evidence: list[EvidenceQuote] = Field(min_length=1, max_length=800)
    claim_status: ClaimStatus
    uncertainty: str = Field(max_length=2000)


class EmploymentRecord(SupportedRecord):
    employer: str | None
    job_title: str | None
    start_date: str | None = Field(
        description="Date exactly as stated, never invented months/years"
    )
    end_date: str | None
    currently_employed: bool | None
    employment_type: str | None
    responsibilities: list[str] = Field(max_length=30)
    technologies: list[str] = Field(max_length=40)


class SkillRecord(SupportedRecord):
    name: str = Field(min_length=1, max_length=200)
    original_wording: str = Field(min_length=1, max_length=1000)
    category: str | None


class EducationRecord(SupportedRecord):
    institution: str | None
    degree: str | None
    field_of_study: str | None
    start_date: str | None
    graduation_date: str | None


class ProjectRecord(SupportedRecord):
    project_name: str | None
    description: str | None
    technologies: list[str] = Field(max_length=40)
    responsibilities: list[str] = Field(max_length=30)
    outcomes: list[str] = Field(max_length=30)


class CertificationRecord(SupportedRecord):
    name: str = Field(min_length=1, max_length=300)
    issuer: str | None
    date: str | None


class LanguageRecord(SupportedRecord):
    language: str = Field(min_length=1, max_length=200)
    proficiency: str | None = Field(description="As stated; do not infer CEFR levels")


class InformationRecord(SupportedRecord):
    information: str = Field(min_length=1, max_length=3000)

    @field_validator("information")
    @classmethod
    def reject_personal_attribute_labels(cls, value):
        if re.search(
            r"(?i)\b(age|date of birth|gender|ethnicity|religion|marital status|"
            r"sexual orientation|disability|nationality|candidate name|email|phone)\s*[:=]",
            value,
        ):
            raise ValueError("Personal/protected attribute labels are not qualification fields.")
        return value


class CVExtraction(StrictModel):
    employment_history: list[EmploymentRecord] = Field(max_length=100)
    technical_skills: list[SkillRecord] = Field(max_length=200)
    education: list[EducationRecord] = Field(max_length=50)
    certifications: list[CertificationRecord] = Field(max_length=100)
    projects: list[ProjectRecord] = Field(max_length=100)
    language_skills: list[LanguageRecord] = Field(max_length=50)
    relevant_domain_experience: list[InformationRecord] = Field(max_length=100)
    availability: list[InformationRecord] = Field(max_length=30)
    location_information: list[InformationRecord] = Field(max_length=30)
    additional_job_relevant_information: list[InformationRecord] = Field(max_length=100)
    extraction_warnings: list[str] = Field(max_length=100)


PROFILE_SECTIONS = tuple(k for k in CVExtraction.model_fields if k != "extraction_warnings")


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    field_path: str = Field(min_length=1, max_length=200)
    corrected_value: object
    reviewer: str | None = Field(default=None, max_length=200)
    note: str = Field(min_length=1, max_length=2000)
