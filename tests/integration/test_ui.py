import importlib
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from job_hire_ai.ai.provider import ModelStatus
from job_hire_ai.ai.schemas import CriterionInput
from job_hire_ai.settings import ROOT


@pytest.fixture(autouse=True)
def preload_ui_modules(monkeypatch):
    # Load native dataframe/chart libraries outside AppTest's instrumented script
    # thread. Cold imports are disproportionately slow there on Windows.
    importlib.import_module("job_hire_ai.ui")
    importlib.import_module("altair")
    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.check",
        lambda self: ModelStatus(False, False, self.settings.model, "Ollama disconnected (test)."),
    )


def page(ats, name):
    # Exercise exactly the page functions mounted by the real entry point, with
    # migrated temporary storage rather than the production data directory.
    script = f"""
import streamlit as st
from job_hire_ai.runtime import Runtime
from job_hire_ai.settings import Settings
from job_hire_ai.ui import {name}
from pathlib import Path
ats = Runtime(Settings(data_dir=Path({str(ats.storage.directory.parent)!r})))
{name}(ats)
"""
    return AppTest.from_string(script, default_timeout=30).run()


def test_empty_dashboard_ui(ats):
    app = page(ats, "dashboard")
    assert not app.exception
    assert len(app.metric) == 15
    assert all(metric.value == "0" for metric in app.metric)
    assert any("No vacancies" in info.value for info in app.info)


def test_create_and_edit_vacancy_ui(ats):
    app = page(ats, "jobs")
    app.text_input(key="new_t").input("Senior Java Developer")
    app.text_area(key="new_desc").input("Develop Java applications.")
    app.button[0].click().run()
    assert not app.exception
    vacancy = ats.vacancies.list()[0]
    assert vacancy.title == "Senior Java Developer"
    app.text_input(key=f"edit_{vacancy.id}_t").input("Lead Java Developer")
    next(button for button in app.button if button.label == "Save changes").click().run()
    assert not app.exception
    assert ats.vacancies.list()[0].title == "Lead Java Developer"
    next(button for button in app.button if button.label == "Archive vacancy").click().run()
    assert not app.exception
    assert ats.vacancies.list()[0].status == "Archived"
    next(button for button in app.button if button.label == "Reactivate vacancy").click().run()
    assert not app.exception
    assert ats.vacancies.list()[0].status == "Active"


def test_empty_candidate_page(ats):
    app = page(ats, "candidates")
    assert not app.exception
    assert "Create a vacancy" in app.info[0].value


def test_candidate_details_and_removal_ui(ats, vacancy, pdf):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    app = page(ats, "candidates")
    assert not app.exception
    assert app.get("download_button")
    next(box for box in app.checkbox if box.label == "Preview CV").check().run()
    assert not app.exception
    assert app.number_input[0].label == "Preview page"
    assert app.number_input[0].value == 1
    next(box for box in app.checkbox if "confirm permanent" in box.label).check().run()
    next(button for button in app.button if button.label == "Remove application").click().run()
    assert not app.exception
    assert ats.applications.list()[0].status == "Removed"
    assert not list(ats.storage.directory.iterdir())


def test_docx_preview_ui(ats, vacancy, docx):
    ats.documents.upload_batch(vacancy.id, [("cv.docx", docx)])
    app = page(ats, "candidates")
    next(box for box in app.checkbox if box.label == "Preview CV").check().run()
    assert not app.exception
    assert any("Synthetic applicant" in element.value for element in app.text)


def test_entrypoint_navigation(tmp_path, monkeypatch):
    # Missing schema gives setup guidance without auto-creating tables.
    monkeypatch.setenv("ATS_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert any("alembic upgrade head" in error.value for error in app.error)
    assert Path(tmp_path / "ats.sqlite3").exists()


def test_initialized_entrypoint_navigation(ats, vacancy, monkeypatch):
    import streamlit as st

    st.cache_resource.clear()
    monkeypatch.setenv("ATS_DATA_DIR", str(ats.storage.directory.parent))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "Dashboard"
    app.sidebar.radio[0].set_value("Job Management").run()
    assert not app.exception
    assert app.title[0].value == "Job Management"
    app.sidebar.radio[0].set_value("Candidate Management").run()
    assert not app.exception
    assert app.title[0].value == "Candidate Management"
    st.cache_resource.clear()


def test_offline_manual_draft_editor_and_approval(ats, vacancy):
    app = page(ats, "jobs")
    assert not app.exception
    assert any("Ollama disconnected" in warning.value for warning in app.warning)
    next(button for button in app.button if button.label == "Create New Draft").click().run()
    assert not app.exception
    draft = ats.criteria.history(vacancy.id)[0]
    value = CriterionInput(
        name="Java",
        category="Technical skills",
        description="Java services experience",
        requirement_type="REQUIRED",
        source_excerpt="Java services",
        weight=100,
        display_order=1,
    )
    ats.criteria.save_draft(draft.id, [value.model_dump()], draft.revision)
    app.run()
    assert not app.exception
    next(button for button in app.button if button.label == "Save Draft").click().run()
    assert not app.exception
    next(box for box in app.checkbox if "approve this saved" in box.label).check().run()
    next(button for button in app.button if button.label == "Approve Configuration").click().run()
    assert not app.exception
    assert ats.criteria.get(draft.id).status == "APPROVED"
    assert any("Approved criteria version" in element.value for element in app.markdown)


def test_mocked_analysis_button_creates_unweighted_draft(ats, vacancy, monkeypatch):

    from job_hire_ai.ai.provider import AnalysisOutput
    from job_hire_ai.ai.schemas import Extraction

    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.check",
        lambda self: ModelStatus(True, True, self.settings.model, "Ollama connected (test)."),
    )
    extraction = Extraction.model_validate_json(
        json.dumps(
            {
                "criteria": [
                    {
                        "name": "Java",
                        "category": "Technical skills",
                        "description": "Maintain Java services",
                        "requirement_type": "REQUIRED",
                        "source_excerpt": "Java services",
                        "extraction_notes": "",
                        "uncertainty": "",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.analyze",
        lambda self, _: AnalysisOutput(extraction, "llama3.2:1b", "test-digest", 1),
    )
    app = page(ats, "jobs")
    next(button for button in app.button if button.label == "Analyze Job Description").click().run()
    assert not app.exception
    draft = ats.criteria.history(vacancy.id)[0]
    assert draft.status == "DRAFT" and draft.criteria[0].weight is None
    assert next(b for b in app.button if b.label == "Approve Configuration").disabled


def test_criteria_table_add_edit_delete_reorder(ats, vacancy):
    draft_id = ats.criteria.create_draft(vacancy.id)
    app = page(ats, "jobs")
    assert not app.exception
    key = app.data_editor[0].key
    app.session_state[key] = {
        "edited_rows": {},
        "deleted_rows": [],
        "added_rows": [
            {
                "name": "Java",
                "category": "Technical skills",
                "description": "Java services",
                "requirement_type": "REQUIRED",
                "source_excerpt": "Java services",
                "weight": 60,
                "display_order": 2,
            },
            {
                "name": "Maintenance",
                "category": "Professional experience",
                "description": "Maintain Java services",
                "requirement_type": "REQUIRED",
                "source_excerpt": "maintain Java services",
                "weight": 40,
                "display_order": 1,
            },
        ],
    }
    app.run()
    assert not app.exception
    assert any(metric.value == "100%" for metric in app.metric)
    next(button for button in app.button if button.label == "Save Draft").click().run()
    assert not app.exception
    saved = ats.criteria.get(draft_id)
    assert [c.name for c in saved.criteria] == ["Maintenance", "Java"]
    key = app.data_editor[0].key
    app.session_state[key] = {
        "edited_rows": {0: {"name": "Java maintenance", "weight": 100}},
        "deleted_rows": [1],
        "added_rows": [],
    }
    app.run()
    assert not app.exception
    next(button for button in app.button if button.label == "Save Draft").click().run()
    assert not app.exception
    saved = ats.criteria.get(draft_id)
    assert len(saved.criteria) == 1 and saved.criteria[0].name == "Java maintenance"
    assert saved.criteria[0].weight == 100


def test_dashboard_navigates_to_unfinished_job(ats, vacancy, monkeypatch):
    import streamlit as st

    st.cache_resource.clear()
    monkeypatch.setenv("ATS_DATA_DIR", str(ats.storage.directory.parent))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    next(button for button in app.button if button.key == f"configure_{vacancy.id}").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == "Job Management"
    assert app.header[0].value == "AI Job Requirements"
    st.cache_resource.clear()
