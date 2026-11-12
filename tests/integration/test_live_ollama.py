import os

import pytest

from job_hire_ai.ai.ollama_provider import OllamaProvider
from job_hire_ai.ai.schemas import verify_evidence
from job_hire_ai.ai.settings import OllamaSettings


@pytest.mark.live_ollama
@pytest.mark.skipif(os.getenv("ATS_TEST_LIVE_OLLAMA") != "1", reason="Opt-in local inference test")
def test_live_llama_structured_analysis():
    provider = OllamaProvider(OllamaSettings())
    status = provider.check()
    if not status.available:
        pytest.skip(status.message)
    description = "Java experience is required. Kafka experience is preferred."
    result = provider.analyze(description)
    assert result.model == provider.settings.model
    assert result.digest
    assert result.extraction.criteria
    assert all(
        verify_evidence(c.source_excerpt, description) == "VERIFIED"
        for c in result.extraction.criteria
    )
    assert any(
        "java" in c.name.lower() and c.requirement_type == "REQUIRED"
        for c in result.extraction.criteria
    )
    assert any(
        "kafka" in c.name.lower() and c.requirement_type == "PREFERRED"
        for c in result.extraction.criteria
    )


@pytest.mark.live_ollama
@pytest.mark.skipif(os.getenv("ATS_TEST_LIVE_OLLAMA") != "1", reason="Opt-in local inference test")
def test_live_cv_pipeline(ats, vacancy):
    from tests.cv_fixtures import pdf_bytes

    settings = OllamaSettings(timeout_seconds=300, retries=0)
    provider = OllamaProvider(settings)
    status = provider.check()
    if not status.available:
        pytest.skip(status.message)
    ats.cv_analysis.settings, ats.cv_analysis.provider = settings, provider
    text = (
        "EXPERIENCE\nFictional Labs: Java Developer, 2020 to Present.\n"
        "Developed Java services.\nEDUCATION\nFictional University: MSc Computer Science, 2019."
    )
    assert ats.documents.upload_batch(vacancy.id, [("live-synthetic.pdf", pdf_bytes(text))])[
        0
    ].success
    document_id = ats.applications.list()[0].documents[0].id
    batch = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch)
    jobs = ats.cv_analysis.jobs(batch)
    assert jobs[0].status in {"COMPLETED", "NEEDS_REVIEW"}, jobs[0].error_summary
    profile = ats.profiles.list()[0]
    print(
        "Live section counts:",
        {
            key: len(profile.original_extraction[key])
            for key in (
                "employment_history",
                "technical_skills",
                "education",
                "certifications",
                "projects",
                "language_skills",
            )
        },
    )
    print("Run usage:", profile.analysis_run.inference_options.get("metrics"))
    assert profile.original_extraction["employment_history"]
    assert profile.original_extraction["education"]
    assert any(
        "java" in skill["name"].lower() for skill in profile.original_extraction["technical_skills"]
    )
    assert profile.evidence
    print(
        f"Live CV profile: {profile.analysis_status}; {profile.analysis_run.duration_ms} ms; "
        f"{len(profile.evidence)} evidence references; "
        f"model digest {profile.analysis_run.model_digest}"
    )
    metrics = profile.analysis_run.inference_options["metrics"]
    assert metrics["model_calls"] >= 1
    assert metrics["input_tokens"] > 0 and metrics["output_tokens"] > 0
    print(f"Measured inference usage: {metrics}")
