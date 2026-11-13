import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from job_hire_ai.ai.cv_wire import CompactCV, section_schema
from job_hire_ai.ai.ollama_provider import OllamaProvider
from job_hire_ai.ai.provider import ProviderError
from job_hire_ai.ai.settings import OllamaSettings
from job_hire_ai.text_extraction import ExtractedText, TextSegment, chunk_document


def record(kind="skill", quote="Java", **fields):
    return {"kind": kind, **fields, "evidence": [{"ref": "s1", "quote": quote}]}


def payload(word="Java"):
    return {"records": [record(original_wording=word, quote=word)]}


def response(data=None, reason="stop", input_tokens=100, output_tokens=40, done=True):
    return SimpleNamespace(
        done_reason=reason,
        done=done,
        prompt_eval_count=input_tokens,
        eval_count=output_tokens,
        message=SimpleNamespace(content=json.dumps(data if data is not None else payload())),
    )


def provider(responses, **options):
    client = Mock()
    client.list.return_value = SimpleNamespace(
        models=[SimpleNamespace(model="llama3.2:1b", digest="test-digest")]
    )
    client.chat.side_effect = responses
    return OllamaProvider(
        OllamaSettings(_env_file=None, model="llama3.2:1b", **options),
        Mock(return_value=client)
    ), client


def source(text="Java", ref="pdf:p1:block1", kind="paragraph"):
    return {"reference": ref, "text": text, "kind": kind}


def test_compact_success_uses_schema_and_preserves_original_reference():
    llm, client = provider([response()])
    result = llm.analyze_cv([source()])
    skill = result.extraction.technical_skills[0]
    assert skill.name == skill.original_wording == "Java"
    assert skill.evidence[0].source_reference == "pdf:p1:block1"
    assert client.chat.call_args.kwargs["format"] == CompactCV.model_json_schema()
    assert result.metrics.model_calls == 1
    assert result.metrics.input_tokens == 100 and result.metrics.output_tokens == 40
    assert result.metrics.truncated_responses == result.metrics.timeouts == 0
    assert result.metrics.duration_ms >= 0


def test_omitted_sections_and_optional_fields_are_unknown_not_invented():
    result = CompactCV.model_validate_json('{"records":[]}').profile({})
    assert result.employment_history == result.technical_skills == result.education == []
    assert result.certifications == result.projects == result.language_skills == []
    wire = {"records": [record("job", quote="Engineer", job_title="Engineer")]}
    job = (
        CompactCV.model_validate_json(json.dumps(wire)).profile({"s1": "p1"}).employment_history[0]
    )
    assert job.employer is job.start_date is job.end_date is job.currently_employed is None


def test_all_six_essential_sections_survive_transport_conversion():
    wire = {
        "records": [
            record("job", employer="Fictional Labs", job_title="Developer", start_date="2020"),
            record(original_wording="Java"),
            record("education", institution="Fictional University", degree="MSc"),
            record("cert", name="Fictional certificate"),
            record("project", project_name="API", outcomes=["15% lower latency"]),
            record("language", language="English", proficiency="professional"),
        ]
    }
    result = CompactCV.model_validate_json(json.dumps(wire)).profile({"s1": "p1"})
    assert all(
        len(getattr(result, field)) == 1
        for field in (
            "employment_history",
            "technical_skills",
            "education",
            "certifications",
            "projects",
            "language_skills",
        )
    )
    assert result.employment_history[0].start_date == "2020"
    assert result.projects[0].outcomes == ["15% lower latency"]


@pytest.mark.parametrize(
    "wire",
    [
        {"records": [{"kind": "skill", "original_wording": "Java"}]},
        {"records": [record(original_wording="Java", quote="x" * 181)]},
        {"records": [], "score": 100},
        {"records": [record("job")]},
    ],
)
def test_invalid_compact_data_never_passes_validation(wire):
    with pytest.raises(ValidationError):
        CompactCV.model_validate_json(json.dumps(wire))


def test_incomplete_json_is_not_repaired_or_saved():
    invalid = response()
    invalid.message.content = '{"records":['
    llm, client = provider([invalid], cv_truncation_retries=0)
    with pytest.raises(ProviderError, match="valid structured output") as error:
        llm.analyze_cv([source()])
    assert client.chat.call_count == error.value.metrics.model_calls == 1


def test_truncated_json_is_retried_and_usage_includes_discarded_output():
    truncated = response(reason="length", output_tokens=4096)
    truncated.message.content = '{"records":['
    llm, client = provider([truncated, response()], cv_truncation_retries=1)
    result = llm.analyze_cv([source()])
    assert result.extraction.technical_skills[0].name == "Java"
    assert result.metrics.model_calls == client.chat.call_count == 2
    assert result.metrics.truncated_responses == 1
    assert result.metrics.input_tokens == 200 and result.metrics.output_tokens == 4136


def test_truncated_larger_source_splits_at_meaningful_boundary():
    llm, client = provider([response(reason="length"), response(), response(payload("Python"))])
    result = llm.analyze_cv([source("Java", "p1"), source("Python", "p2", "heading")])
    assert [skill.name for skill in result.extraction.technical_skills] == ["Java", "Python"]
    assert result.metrics.model_calls == 3
    assert (
        len(
            json.loads(client.chat.call_args_list[1].kwargs["messages"][1]["content"])[
                "cv_segments"
            ]
        )
        == 1
    )


def test_truncation_retries_are_bounded_and_exhaustion_fails():
    llm, client = provider([response(reason="length")] * 10, cv_truncation_retries=2)
    with pytest.raises(ProviderError, match="bounded retries") as error:
        llm.analyze_cv([source()])
    assert client.chat.call_count == error.value.metrics.model_calls == 3
    assert error.value.metrics.truncated_responses == 3


def test_global_call_cap_also_bounds_transient_and_truncation_retries():
    llm, client = provider(
        [response(reason="length")] * 10, cv_truncation_retries=4, cv_max_model_calls=2
    )
    with pytest.raises(ProviderError, match="model-call limit") as error:
        llm.analyze_cv([source()])
    assert client.chat.call_count == error.value.metrics.model_calls == 2


def test_declared_overflow_is_recovered_not_silently_accepted():
    llm, client = provider([response({**payload(), "more": True}), response()])
    result = llm.analyze_cv([source()])
    assert result.metrics.truncated_responses == 1 and client.chat.call_count == 2


def test_timeouts_are_bounded_and_unreported_tokens_remain_unknown():
    llm, client = provider([httpx.ReadTimeout("private content")] * 10, retries=1)
    with pytest.raises(ProviderError, match="timed out") as error:
        llm.analyze_cv([source()])
    assert client.chat.call_count == error.value.metrics.model_calls == 2
    assert error.value.metrics.timeouts == 2
    assert error.value.metrics.input_tokens is error.value.metrics.output_tokens is None
    assert "private content" not in str(error.value)


def test_false_completion_flag_is_treated_as_truncation():
    llm, client = provider([response(done=False), response()])
    assert llm.analyze_cv([source()]).metrics.truncated_responses == 1


def test_short_quotes_and_optional_fields_reduce_output_without_losing_skills():
    names = [f"Tool{i}" for i in range(20)]
    wire = CompactCV.model_validate_json(
        json.dumps({"records": [record(original_wording=name, quote=name) for name in names]})
    )
    stored = wire.profile({"s1": "p1"})
    compact = json.dumps(
        wire.model_dump(exclude_defaults=True, exclude_none=True), separators=(",", ":")
    )
    expanded = json.dumps(stored.model_dump(mode="json"), separators=(",", ":"))
    assert len(compact) < len(expanded) / 2
    assert [skill.name for skill in stored.technical_skills] == names


def test_only_literally_quoted_job_technologies_project_into_skills():
    wire = CompactCV.model_validate_json(
        json.dumps(
            {
                "records": [
                    record(
                        "job",
                        job_title="Developer",
                        technologies=["Java", "AWS"],
                        quote="Built Java services",
                    )
                ]
            }
        )
    )
    stored = wire.profile({"s1": "p1"})
    assert [skill.name for skill in stored.technical_skills] == ["Java"]


def test_section_grammar_and_unknown_dates_remain_explicit():
    schema, label = section_schema(
        [{**source("EXPERIENCE", kind="heading"), "section": "experience"}]
    )
    assert label == "Job"
    value = {
        "records": [
            record("job", job_title="Developer", employer=None, start_date=None, end_date=None)
        ]
    }
    stored = schema.model_validate_json(json.dumps(value)).profile({"s1": "p1"})
    assert stored.employment_history[0].start_date is None
    assert section_schema([source("QUALIFICATIONS", kind="heading")])[0] is CompactCV


def test_long_document_chunks_keep_headings_with_their_following_section():
    texts = ["EXPERIENCE", "Java role. " * 50, "EDUCATION", "MSc details. " * 50]
    segments = [
        TextSegment(f"p{i}", text, text, None, "heading" if i % 2 == 0 else "paragraph")
        for i, text in enumerate(texts)
    ]
    chunks = chunk_document(ExtractedText(segments, []), 2400)
    assert len(chunks) == 2
    assert [chunk[0]["text"] for chunk in chunks] == ["EXPERIENCE", "EDUCATION"]
    assert (
        " ".join(unit["text"] for chunk in chunks for unit in chunk).split()
        == " ".join(texts).split()
    )


def test_invalid_complete_json_recovers_from_smaller_source_sections():
    invalid = response({"records": [{"kind": "skill", "original_wording": "Java"}]})
    llm, client = provider([
        invalid, response(payload("Java")), response(payload("Python")),
    ], cv_truncation_retries=2)
    result = llm.analyze_cv([
        source("Java", "p1"), source("Python", "p2", kind="heading"),
    ])
    assert [row.name for row in result.extraction.technical_skills] == [
        "Java", "Python",
    ]
    assert client.chat.call_count == result.metrics.model_calls == 3


def test_cv_generation_reserves_full_configured_json_budget():
    llm, client = provider([response()], cv_max_output_tokens=3072)
    llm.analyze_cv([source("Java")])
    assert client.chat.call_args.kwargs["options"]["num_predict"] == 3072
