import json
from types import SimpleNamespace
from unittest.mock import Mock

from job_hire_ai.ai.ollama_provider import OllamaProvider
from tests.cv_fixtures import pdf_bytes


def test_usage_persists_on_success_and_truncation_failure_without_overwriting_profile(ats, vacancy):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf_bytes("Java experience."))])
    document_id = ats.applications.list()[0].documents[0].id
    client = Mock()
    client.list.return_value = SimpleNamespace(
        models=[SimpleNamespace(model=ats.ollama_settings.model, digest="metrics-test-digest")]
    )
    client.chat.return_value = SimpleNamespace(
        done_reason="stop",
        done=True,
        prompt_eval_count=123,
        eval_count=45,
        message=SimpleNamespace(
            content=json.dumps(
                {
                    "records": [
                        {
                            "kind": "skill",
                            "original_wording": "Java",
                            "evidence": [{"ref": "s1", "quote": "Java"}],
                        }
                    ],
                }
            )
        ),
    )
    ats.cv_analysis.provider = OllamaProvider(ats.ollama_settings, Mock(return_value=client))
    batch = ats.cv_analysis.create_batch(vacancy.id, [document_id])
    ats.cv_analysis.process_batch(batch)
    profile = ats.profiles.list()[0]
    usage = profile.analysis_run.inference_options["metrics"]
    assert (
        usage["model_calls"] == 1 and usage["input_tokens"] == 123 and usage["output_tokens"] == 45
    )
    original = profile.original_extraction
    ats.cv_analysis.provider.settings = ats.ollama_settings.model_copy(
        update={"cv_truncation_retries": 1}
    )
    # This test covers the fail-closed, no-fallback configuration. The
    # source-grounded partial fallback has its own separate integration tests.
    ats.cv_analysis.settings = ats.cv_analysis.settings.model_copy(
        update={"cv_literal_fallback_enabled": False}
    )
    client.chat.return_value.done_reason = "length"
    client.chat.return_value.message.content = '{"records":['
    failed = ats.cv_analysis.create_batch(vacancy.id, [document_id], force=True)
    ats.cv_analysis.process_batch(failed)
    job = ats.cv_analysis.jobs(failed)[0]
    usage = job.runs[0].inference_options["metrics"]
    assert job.status == "FAILED"
    assert usage["model_calls"] == usage["truncated_responses"] == 2
    assert usage["input_tokens"] == 246 and usage["output_tokens"] == 90
    assert (
        len(ats.profiles.list()) == 1
        and ats.profiles.get(profile.id).original_extraction == original
    )
