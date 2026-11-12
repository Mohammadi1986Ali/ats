from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RequirementType(StrEnum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    UNCLEAR = "UNCLEAR"


class Category(StrEnum):
    TECHNICAL_SKILLS = "Technical skills"
    EXPERIENCE = "Professional experience"
    EDUCATION = "Education"
    CERTIFICATIONS = "Certifications"
    DOMAIN = "Domain knowledge"
    PROJECTS = "Projects"
    LANGUAGE = "Language requirements"
    AVAILABILITY = "Availability"
    LOCATION = "Location"
    OTHER = "Other job-specific requirements"


class ExtractedCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    name: str = Field(min_length=1, max_length=200)
    category: Category
    description: str = Field(min_length=1, max_length=3000)
    requirement_type: RequirementType
    source_excerpt: str = Field(min_length=1, max_length=3000)
    extraction_notes: str = Field(max_length=2000)
    uncertainty: str = Field(max_length=2000)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    criteria: list[ExtractedCriterion] = Field(max_length=50)


class CriterionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    criterion_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    category: Category
    description: str = Field(min_length=1, max_length=3000)
    requirement_type: RequirementType
    source_excerpt: str = Field(default="", max_length=3000)
    extraction_notes: str = Field(default="", max_length=2000)
    uncertainty: str = Field(default="", max_length=2000)
    weight: int | None = Field(default=None, strict=True, ge=0, le=100)
    display_order: int = Field(ge=1, le=50, strict=True)
    evidence_acknowledged: bool = False
    acknowledgement_note: str = Field(default="", max_length=2000)


def verify_evidence(excerpt: str, description: str) -> str:
    return "VERIFIED" if excerpt.strip() and excerpt in description else "UNVERIFIED"
