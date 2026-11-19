"""JD-time weighting choices: no invented criteria or HR approval attribution."""

import json
from unittest.mock import Mock

import pytest

from job_hire_ai.ai.job_analysis import JobAnalysisService
from job_hire_ai.ai.provider import AnalysisOutput
from job_hire_ai.ai.schemas import Extraction
from job_hire_ai.errors import ATSError
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.criteria_service import default_weights, parse_manual_weights
from job_hire_ai.settings import ROOT


def test_default_weight_allocation_is_deterministic():
    class Requirement:
        def __init__(self, kind, order):
            self.requirement_type = kind
            self.display_order = order

    rows = [Requirement("REQUIRED", 1), Requirement("PREFERRED", 2),
            Requirement("UNCLEAR", 3)]
    assert default_weights(rows) == [60, 20, 20]
    assert default_weights([Requirement("REQUIRED", i) for i in range(1, 4)]) == [
        34, 33, 33
    ]


def test_manual_rules_reject_missing_duplicate_and_incorrect_total():
    assert parse_manual_weights("Java = 60\nKafka = 40") == {
        "java": 60, "kafka": 40
    }
    for input_value in ("", "Java = 90", "Java = 40\njava = 60", "Java: 100"):
        with pytest.raises(ATSError):
            parse_manual_weights(input_value)


@pytest.fixture
def jd_draft(ats):
    example = json.loads(
        (ROOT / "tests/fixtures/job_descriptions.json").read_text()
    )[0]
    vacancy = ats.vacancies.create(VacancyInput(
        title="JD-time system weighting",
        description=example["description"],
        weighting_mode="DEFAULT",
    ))
    provider = Mock()
    provider.analyze.return_value = AnalysisOutput(
        Extraction.model_validate({"criteria": example["expected"]}),
        "llama3.2:1b", "fake-test-digest", 5,
    )
    ats.job_analysis = JobAnalysisService(ats.db, provider, ats.ollama_settings)
    return vacancy, ats.job_analysis.analyze(vacancy.id)


def test_system_default_applies_model_extracted_criteria_not_fake_hr(jd_draft, ats):
    vacancy, draft_id = jd_draft
    version = ats.criteria.activate_entered_weights(draft_id, mode="DEFAULT")
    assert version == 1
    result = ats.criteria.get(draft_id)
    assert result.status == "APPROVED"
    assert result.approved_by == "SYSTEM_DEFAULT_NOT_HR"
    assert sum(row.weight for row in result.criteria) == 100
    assert ats.job_matching.ready(vacancy.id)
    assert all(row.original_extraction for row in result.criteria)


def test_manual_weighting_selected_on_jd_form(jd_draft, ats):
    _, draft_id = jd_draft
    criteria = ats.criteria.get(draft_id).criteria
    names = [row.name for row in criteria]
    rules = "\n".join(f"{name} = 25" for name in names)
    ats.criteria.activate_entered_weights(draft_id, mode="MANUAL", rules=rules)
    config = ats.criteria.get(draft_id)
    assert config.approved_by == "USER_ENTERED_JD_WEIGHTS"
    assert [r.weight for r in config.criteria] == [25] * len(names)


def test_incomplete_manual_names_leave_editable_draft(jd_draft, ats):
    _, draft_id = jd_draft
    with pytest.raises(ATSError, match="exactly match"):
        ats.criteria.activate_entered_weights(
            draft_id, mode="MANUAL", rules="Not in description = 100"
        )
    assert ats.criteria.get(draft_id).status == "DRAFT"


def test_unverified_extraction_cannot_be_auto_activated(jd_draft, ats):
    _, draft_id = jd_draft
    with ats.db.transaction() as session:
        from sqlalchemy import select

        from job_hire_ai.database.models import JobCriterion
        row = session.scalar(select(JobCriterion).where(
            JobCriterion.configuration_id == draft_id
        ))
        row.source_excerpt = "This source quote was invented by the model"
        row.evidence_status = "UNVERIFIED"
    with pytest.raises(ATSError, match="not verified"):
        ats.criteria.activate_entered_weights(draft_id, mode="DEFAULT")
    assert ats.criteria.get(draft_id).status == "DRAFT"
