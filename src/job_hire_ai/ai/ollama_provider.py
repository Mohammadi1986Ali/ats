import time

import httpx
import ollama
from pydantic import ValidationError

from job_hire_ai.ai.prompts import messages
from job_hire_ai.ai.provider import (
    AnalysisOutput,
    InferenceMetrics,
    InvalidStructuredResponse,
    ModelStatus,
    ProviderError,
    TruncatedResponse,
)
from job_hire_ai.ai.schemas import Extraction
from job_hire_ai.ai.settings import OllamaSettings


class OllamaProvider:
    def __init__(self, settings: OllamaSettings, client_factory=ollama.Client):
        self.settings, self.client_factory = settings, client_factory

    def _client(self, timeout):
        return self.client_factory(
            host=self.settings.base_url,
            timeout=httpx.Timeout(timeout, connect=3),
            trust_env=False,
            follow_redirects=False,
        )

    def check(self):
        try:
            response = self._client(3).list()
            model = next((m for m in response.models if m.model == self.settings.model), None)
            if model is None:
                return ModelStatus(
                    True,
                    False,
                    self.settings.model,
                    f"Model missing. Run: ollama pull {self.settings.model}",
                )
            return ModelStatus(True, True, self.settings.model, "Ollama connected", model.digest)
        except (ConnectionError, httpx.HTTPError, ollama.ResponseError):
            return ModelStatus(
                False,
                False,
                self.settings.model,
                "Ollama disconnected. Start the local Ollama app or run ollama serve, then retry.",
            )

    def analyze(self, description):
        if not description.strip():
            raise ProviderError("The job description is empty.")
        # Conservative input budget, without silently truncating the source document.
        budget = max(0, self.settings.num_ctx - self.settings.num_predict - 2000) * 2
        if len(description) > min(self.settings.max_description_chars, budget):
            raise ProviderError(
                "Job description exceeds the configured input budget. Shorten it or increase "
                "OLLAMA_NUM_CTX while reserving room for the structured response."
            )
        return self.generate_structured(messages(description), Extraction)

    def analyze_cv(self, segments):
        from job_hire_ai.ai.cv_prompts import cv_messages
        from job_hire_ai.ai.cv_recovery import CallBudget, split_segments
        from job_hire_ai.ai.cv_wire import section_schema
        from job_hire_ai.ai.extraction_merger import merge_extractions

        metrics = InferenceMetrics()
        budget = CallBudget(self.settings.cv_max_model_calls)
        recoveries = self.settings.cv_truncation_retries
        started = time.perf_counter()
        identity = None

        def extract(part, brief=False):
            nonlocal recoveries, identity
            # Short transport IDs resolve back to original persistent source locations.
            refs = {f"s{i}": segment["reference"] for i, segment in enumerate(part, 1)}
            inputs = [
                {"ref": key, "text": segment["text"]}
                for key, segment in zip(refs, part, strict=True)
            ]
            try:
                schema, section = section_schema(part)
                prompt = cv_messages(inputs, brief)
                if section:
                    prompt[0]["content"] += (
                        f"\nThis is a {section} section. Extract all its {section} records. "
                        "Keep this kind and preserve stated essential fields and technologies."
                    )
                # Do not artificially cap tokens at 512 + input characters:
                # structured JSON can be far longer than the CV source excerpt.
                output_limit = min(
                    self.settings.num_predict, self.settings.cv_max_output_tokens
                )
                response = self.generate_structured(prompt, schema, budget, output_limit)
                metrics.add(response.metrics)
                current = (response.model, response.digest)
                if identity is not None and identity != current:
                    raise ProviderError(
                        "Model changed during CV recovery. Retry with one stable model."
                    )
                identity = current
                wire = response.extraction
                if wire.more or len(wire.records) == 40:
                    metrics.truncated_responses += 1
                    raise TruncatedResponse("CV response reached its record/detail limit.")
                return wire.profile(refs)
            except (TruncatedResponse, InvalidStructuredResponse) as exc:
                if exc.metrics:
                    metrics.add(exc.metrics)
                if recoveries <= 0:
                    raise ProviderError(
                        "CV extraction could not produce complete valid structured output "
                        "within bounded source-splitting retries. No partial profile saved. "
                        "Try smaller CV sections or a more capable local evaluation model."
                    ) from None
                recoveries -= 1
                parts = split_segments(part)
                return (
                    merge_extractions([extract(value, True) for value in parts])
                    if parts
                    else extract(part, True)
                )
            except ProviderError as exc:
                if exc.metrics:
                    metrics.add(exc.metrics)
                raise

        try:
            result = extract(segments)
            return AnalysisOutput(
                result,
                identity[0],
                identity[1],
                int((time.perf_counter() - started) * 1000),
                metrics,
            )
        except ProviderError as exc:
            exc.metrics = metrics
            raise
        except ValidationError:
            raise ProviderError(
                "CV response could not be validated. No profile was saved.", metrics
            ) from None

    def generate_structured(
        self, prompt_messages, response_schema, call_budget=None, output_limit=None
    ):
        """Shared local transport, schema validation, timing and controlled retries."""
        metrics = InferenceMetrics()
        try:
            return self._generate(
                prompt_messages, response_schema, call_budget, metrics, output_limit
            )
        except ProviderError as exc:
            exc.metrics = metrics
            raise

    def _generate(self, prompt_messages, response_schema, call_budget, metrics, output_limit):
        status = self.check()
        if not status.available:
            raise ProviderError(status.message)
        started = time.perf_counter()
        for attempt in range(self.settings.retries + 1):
            if call_budget is not None:
                call_budget.take()
            metrics.model_calls += 1
            request_started = time.perf_counter()
            try:
                response = self._client(self.settings.timeout_seconds).chat(
                    model=self.settings.model,
                    messages=prompt_messages,
                    format=response_schema.model_json_schema(),
                    options={
                        **self.settings.options(),
                        "num_predict": output_limit
                        if output_limit is not None
                        else self.settings.num_predict,
                    },
                    stream=False,
                    keep_alive="5m",
                )

                def token_count(name, result=response):
                    value = getattr(result, name, None)
                    return value if type(value) is int and value >= 0 else None

                for name, field in (
                    ("prompt_eval_count", "input_tokens"),
                    ("eval_count", "output_tokens"),
                ):
                    count, previous = token_count(name), getattr(metrics, field)
                    setattr(
                        metrics,
                        field,
                        None if count is None or previous is None else previous + count,
                    )
                if (
                    response.done_reason in {"length", "max_tokens", "max_output_tokens"}
                    or getattr(response, "done", None) is False
                ):
                    metrics.truncated_responses += 1
                    raise TruncatedResponse(
                        "Model output was truncated. Increase OLLAMA_NUM_PREDICT and retry."
                    )
                if response.done_reason not in {None, "stop"}:
                    raise ProviderError(
                        "Unexpected model completion reason. No extraction was saved."
                    )
                extraction = response_schema.model_validate_json(response.message.content)
                return AnalysisOutput(
                    extraction,
                    self.settings.model,
                    status.digest,
                    int((time.perf_counter() - started) * 1000),
                    metrics,
                )
            except ValidationError:
                raise InvalidStructuredResponse(
                    "The model returned invalid structured output. No extraction was saved. "
                    "Retry with smaller CV sections or a different local model."
                ) from None
            except (httpx.TimeoutException, httpx.ConnectError, ConnectionError):
                metrics.input_tokens = metrics.output_tokens = None
                metrics.timeouts += 1
                if attempt < self.settings.retries:
                    continue
                raise ProviderError(
                    "Ollama timed out or disconnected. Check the local service; CPU inference may "
                    "need a higher OLLAMA_TIMEOUT. No generated extraction was saved."
                ) from None
            except ollama.ResponseError as exc:
                metrics.input_tokens = metrics.output_tokens = None
                if exc.status_code in {500, 502, 503, 504} and attempt < self.settings.retries:
                    continue
                if exc.status_code == 404:
                    raise ProviderError(
                        f"Model unavailable. Run: ollama pull {self.settings.model}"
                    ) from None
                raise ProviderError(
                    "Ollama rejected the request. Check local model/service health."
                ) from None
            except httpx.HTTPError:
                metrics.input_tokens = metrics.output_tokens = None
                raise ProviderError(
                    "Ollama request failed. Check the configured local service."
                ) from None
            finally:
                metrics.duration_ms += int((time.perf_counter() - request_started) * 1000)
