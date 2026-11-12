import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from job_hire_ai.ai.job_analysis import JobAnalysisService
from job_hire_ai.ai.provider import AnalysisOutput, ProviderError
from job_hire_ai.ai.schemas import Extraction
from job_hire_ai.database.models import CriteriaConfiguration, JobCriterion
from job_hire_ai.errors import ATSError
from job_hire_ai.runtime import Runtime
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.criteria_service import criterion_input, description_hash
from job_hire_ai.settings import ROOT, Settings

EXAMPLES = json.loads((ROOT / "tests/fixtures/job_descriptions.json").read_text())


@pytest.fixture
def analyzed(ats):
    example = EXAMPLES[0]
    vacancy = ats.vacancies.create(
        VacancyInput(title=example["title"], description=example["description"])
    )
    extraction = Extraction.model_validate_json(json.dumps({"criteria": example["expected"]}))
    provider = Mock()
    provider.analyze.return_value = AnalysisOutput(extraction, "llama3.2:1b", "test-digest", 3)
    ats.job_analysis = JobAnalysisService(ats.db, provider, ats.ollama_settings)
    draft_id = ats.job_analysis.analyze(vacancy.id)
    return vacancy, draft_id, provider


def rows(ats, draft_id):
    return [
        criterion_input(row).model_dump(mode="json") for row in ats.criteria.get(draft_id).criteria
    ]


def weighted(ats, draft_id):
    values = rows(ats, draft_id)
    for value in values:
        value["weight"] = 25
    ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
    return ats.criteria.get(draft_id)


def test_analysis_creates_unapproved_grounded_draft(ats, analyzed):
    vacancy, draft_id, provider = analyzed
    provider.analyze.assert_called_once_with(vacancy.description)
    draft = ats.criteria.get(draft_id)
    assert draft.status == "DRAFT" and draft.approved_at is None and draft.version == 1
    assert all(c.weight is None and c.created_by == "AI" for c in draft.criteria)
    assert all(c.evidence_status == "VERIFIED" for c in draft.criteria)
    run = ats.job_analysis.runs(vacancy.id)[0]
    assert run.status == "SUCCEEDED" and run.model_digest == "test-digest"
    assert run.prompt_version == "job-requirements-v1" and run.duration_ms >= 0


def test_edit_add_remove_reorder_provenance(ats, analyzed):
    _, draft_id, _ = analyzed
    values = rows(ats, draft_id)
    values.pop(2)
    values[0]["description"] = "HR clarified Java experience requirement"
    values[0]["display_order"], values[1]["display_order"] = 2, 1
    values.append(
        {
            "name": "Manual qualification",
            "category": "Other job-specific requirements",
            "description": "HR-entered requirement for review",
            "requirement_type": "UNCLEAR",
            "source_excerpt": "",
            "weight": 0,
            "display_order": 3,
        }
    )
    # The previous last criterion occupied order 4; the new manual row uses 3.
    ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
    edited = ats.criteria.get(draft_id)
    assert len(edited.criteria) == 4
    assert edited.criteria[0].name == "Spring Boot"
    assert edited.criteria[1].created_by == "AI_MODIFIED"
    assert edited.criteria[2].created_by == "MANUAL"
    assert edited.criteria[2].evidence_status == "UNVERIFIED"
    assert edited.criteria[1].original_extraction["description"] != edited.criteria[1].description


@pytest.mark.parametrize("weight", [-1, 101, 0.5, 25.0, "25", True])
def test_invalid_weights_rejected(ats, analyzed, weight):
    _, draft_id, _ = analyzed
    values = rows(ats, draft_id)
    values[0]["weight"] = weight
    with pytest.raises(ValidationError):
        ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)


def test_approval_requires_weights_and_criteria(ats, analyzed):
    _, draft_id, _ = analyzed
    with pytest.raises(ATSError, match="weight"):
        ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)
    ats.criteria.save_draft(draft_id, [], ats.criteria.get(draft_id).revision)
    with pytest.raises(ATSError, match="at least one"):
        ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)


def test_precise_weight_total_and_zero_policy(ats, analyzed):
    _, draft_id, _ = analyzed
    values = rows(ats, draft_id)
    for value, weight in zip(values, [0, 33, 33, 33], strict=True):
        value["weight"] = weight
    ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
    with pytest.raises(ATSError, match="100%"):
        ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)
    values[1]["weight"] = 34
    ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
    ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision, "test-hr")
    assert ats.criteria.get(draft_id).criteria[0].weight == 0


def test_unverified_evidence_needs_acknowledgement_and_note(ats, analyzed):
    _, draft_id, _ = analyzed
    values = rows(ats, draft_id)
    for value in values:
        value["weight"] = 25
    values[0]["source_excerpt"] = "Kubernetes is required"  # unsupported/invented quote
    ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
    assert ats.criteria.get(draft_id).criteria[0].evidence_status == "UNVERIFIED"
    with pytest.raises(ATSError, match="source excerpt"):
        ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)
    values[0]["evidence_acknowledged"] = True
    with pytest.raises(ATSError):
        ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
        ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)
    values[0]["acknowledgement_note"] = (
        "HR acknowledges this excerpt is unsupported; review exception."
    )
    ats.criteria.save_draft(draft_id, values, ats.criteria.get(draft_id).revision)
    ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)
    assert ats.criteria.get(draft_id).criteria[0].evidence_status == "UNVERIFIED"


def test_approval_immutable_and_new_version_history(ats, analyzed):
    vacancy, draft_id, _ = analyzed
    approved = weighted(ats, draft_id)
    ats.criteria.approve(draft_id, approved.revision, "hr-reviewer")
    old_rows = rows(ats, draft_id)
    with pytest.raises(ATSError, match="immutable"):
        ats.criteria.save_draft(draft_id, old_rows, ats.criteria.get(draft_id).revision)
    new_id = ats.criteria.create_draft(vacancy.id)
    assert ats.criteria.get(new_id).version == 2
    assert ats.criteria.get(draft_id).status == "APPROVED"
    ats.criteria.approve(new_id, ats.criteria.get(new_id).revision, "second-reviewer")
    assert ats.criteria.get(draft_id).status == "SUPERSEDED"
    assert rows(ats, draft_id) == old_rows
    assert ats.criteria.get(draft_id).approved_by == "hr-reviewer"
    assert len(ats.criteria.history(vacancy.id)) == 2


def test_database_blocks_historical_mutation(ats, analyzed):
    _, draft_id, _ = analyzed
    draft = weighted(ats, draft_id)
    ats.criteria.approve(draft_id, draft.revision)
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        row = session.scalar(select(JobCriterion).where(JobCriterion.configuration_id == draft_id))
        row.weight = 50
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        session.execute(
            text("DELETE FROM job_criteria WHERE configuration_id=:id"), {"id": draft_id}
        )
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        session.get(CriteriaConfiguration, draft_id).job_description_hash = "x" * 64


def test_description_changes_block_approval_and_require_reanalysis(ats, analyzed):
    vacancy, draft_id, _ = analyzed
    draft = weighted(ats, draft_id)
    ats.vacancies.edit(
        vacancy.id,
        VacancyInput(
            title=vacancy.title, description=vacancy.description + " English is required."
        ),
    )
    with pytest.raises(ATSError, match="description changed"):
        ats.criteria.approve(draft_id, draft.revision)
    ats.job_analysis.analyze(vacancy.id, replace_draft=True)
    updated = ats.criteria.get(draft_id)
    assert updated.job_description_hash != draft.job_description_hash
    assert updated.job_description_hash == description_hash(ats.vacancies.list()[0].description)
    assert all(c.weight is None for c in updated.criteria)


def test_dashboard_new_metrics_and_outdated_approval(ats, analyzed):
    vacancy, draft_id, _ = analyzed
    assert ats.dashboard.snapshot()["criteria_metrics"] == {
        "Total vacancies": 1,
        "Approved AI criteria": 0,
        "Awaiting HR approval": 1,
        "Outdated criteria": 0,
    }
    draft = weighted(ats, draft_id)
    ats.criteria.approve(draft_id, draft.revision)
    assert ats.dashboard.snapshot()["criteria_metrics"]["Approved AI criteria"] == 1
    ats.vacancies.edit(
        vacancy.id,
        VacancyInput(title=vacancy.title, description=vacancy.description + " Extra requirement."),
    )
    metrics = ats.dashboard.snapshot(vacancy.id)["criteria_metrics"]
    assert metrics["Approved AI criteria"] == 0 and metrics["Outdated criteria"] == 1
    assert ats.criteria.get(draft_id).status == "APPROVED"


def test_failure_keeps_existing_draft_and_records_sanitized_run(ats, analyzed):
    vacancy, draft_id, provider = analyzed
    saved_rows = rows(ats, draft_id)
    provider.analyze.side_effect = RuntimeError("private source text not to log")
    with pytest.raises(ProviderError):
        ats.job_analysis.analyze(vacancy.id, replace_draft=True)
    assert rows(ats, draft_id) == saved_rows
    run = ats.job_analysis.runs(vacancy.id)[0]
    assert run.status == "FAILED" and "private source" not in run.error_summary


def test_description_change_during_inference(ats, analyzed):
    vacancy, draft_id, provider = analyzed
    original = provider.analyze.return_value

    def concurrent_edit(_):
        ats.vacancies.edit(vacancy.id, VacancyInput(title="Edited", description="New description"))
        return original

    provider.analyze.side_effect = concurrent_edit
    with pytest.raises(ProviderError, match="changed during analysis"):
        ats.job_analysis.analyze(vacancy.id, replace_draft=True)
    assert ats.criteria.get(draft_id).job_description_snapshot == vacancy.description


def test_draft_revision_conflict(ats, analyzed):
    _, draft_id, _ = analyzed
    draft = ats.criteria.get(draft_id)
    weighted(ats, draft_id)
    with pytest.raises(ATSError, match="another session"):
        ats.criteria.save_draft(draft_id, rows(ats, draft_id), draft.revision)


def test_configuration_survives_restart(ats, analyzed):
    vacancy, draft_id, _ = analyzed
    draft = weighted(ats, draft_id)
    ats.criteria.approve(draft_id, draft.revision, "reviewer")
    other = Runtime(Settings(data_dir=ats.storage.directory.parent))
    try:
        restored = other.criteria.get(draft_id)
        assert restored.status == "APPROVED" and restored.approved_by == "reviewer"
        assert len(restored.criteria) == 4
        assert other.criteria.history(vacancy.id)[0].version == 1
    finally:
        other.db.engine.dispose()


def test_unsupported_llm_excerpt_is_flagged_not_silently_verified(ats, analyzed):
    vacancy, draft_id, provider = analyzed
    payload = {
        "criteria": [
            {**EXAMPLES[0]["expected"][0], "source_excerpt": "AWS certification is mandatory"}
        ]
    }
    extraction = Extraction.model_validate_json(json.dumps(payload))
    provider.analyze.return_value = AnalysisOutput(extraction, "llama3.2:1b", "test-digest", 1)
    ats.job_analysis.analyze(vacancy.id, replace_draft=True)
    saved = ats.criteria.get(draft_id)
    assert saved.criteria[0].created_by == "AI"
    assert saved.criteria[0].evidence_status == "UNVERIFIED"
    assert not saved.criteria[0].evidence_acknowledged
    values = rows(ats, draft_id)
    values[0]["weight"] = 100
    ats.criteria.save_draft(draft_id, values, saved.revision)
    with pytest.raises(ATSError, match="source excerpt"):
        ats.criteria.approve(draft_id, ats.criteria.get(draft_id).revision)
