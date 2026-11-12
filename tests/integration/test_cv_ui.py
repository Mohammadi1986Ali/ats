import importlib
import json

import pytest
from streamlit.testing.v1 import AppTest

from job_hire_ai.ai.provider import ModelStatus
from tests.cv_fixtures import mock_provider, pdf_bytes


@pytest.fixture(autouse=True)
def preload_and_mock(monkeypatch):
    importlib.import_module("job_hire_ai.cv_ui")
    fake = mock_provider()
    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.check", lambda self: fake.check()
    )
    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.analyze_cv",
        lambda self, segments: fake.analyze_cv(segments),
    )


def page(ats):
    script = f"""
from pathlib import Path
from job_hire_ai.runtime import Runtime
from job_hire_ai.settings import Settings
from job_hire_ai.cv_ui import cv_analysis_page
ats = Runtime(Settings(data_dir=Path({str(ats.storage.directory.parent)!r})))
cv_analysis_page(ats)
"""
    return AppTest.from_string(script, default_timeout=30).run()


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_cv_empty_states(ats, vacancy):
    app = page(ats)
    assert not app.exception
    assert any("No uploaded CVs" in info.value for info in app.info)


def test_cv_ui_select_process_correct_validate_reanalyze(ats, vacancy):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf_bytes("Java developer"))])
    document_id = ats.applications.list()[0].documents[0].id
    app = page(ats)
    app.multiselect[0].set_value([document_id]).run()
    button(app, "Analyze Selected").click().run()
    assert not app.exception
    profile = ats.profiles.list()[0]
    app.text_input(key=f"field_{profile.id}").input("technical_skills.0.name")
    app.text_area(key=f"value_{profile.id}").input(json.dumps("Java (corrected)"))
    app.text_area(key=f"review_note_{profile.id}").input("Confirmed with original CV")
    button(app, "Save Correction").click().run()
    assert not app.exception
    corrected = ats.profiles.get(profile.id)
    assert len(corrected.reviews) == 1
    assert corrected.original_extraction["technical_skills"][0]["name"] == "Java"
    app.text_input(key=f"validate_note_{profile.id}").input("Reviewed every claim").run()
    next(b for b in app.checkbox if b.key.startswith("validate_ack_")).check().run()
    assert app.text_input(key=f"validate_note_{profile.id}").value == "Reviewed every claim"
    assert next(b for b in app.checkbox if b.key.startswith("validate_ack_")).value
    button(app, "Save Validated Profile").click().run()
    assert not app.exception
    assert ats.profiles.status(ats.profiles.get(profile.id)) == "COMPLETED"
    next(b for b in app.checkbox if b.key == f"recheck_{profile.id}").check().run()
    button(app, "Re-analyze").click().run()
    assert not app.exception
    assert len(ats.profiles.list()) == 2


def test_cv_ui_pending_and_failed_retry(ats, vacancy, monkeypatch):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf_bytes("Java developer"))])
    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.check",
        lambda self: ModelStatus(False, False, "llama3.2:1b", "Ollama disconnected (test)"),
    )
    app = page(ats)
    button(app, "Analyze All Pending").click().run()
    assert not app.exception
    batch = ats.cv_analysis.batches(vacancy.id)[0]
    assert ats.cv_analysis.summary(batch.id)["Failed"] == 1
    fake = mock_provider()
    monkeypatch.setattr(
        "job_hire_ai.ai.ollama_provider.OllamaProvider.check", lambda self: fake.check()
    )
    button(app, "Retry Failed").click().run()
    assert not app.exception
    assert ats.cv_analysis.summary(batch.id)["Completed"] == 1
