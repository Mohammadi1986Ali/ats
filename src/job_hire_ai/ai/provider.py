from dataclasses import asdict, dataclass, field
from typing import Protocol

from pydantic import BaseModel

from job_hire_ai.errors import ATSError


class ProviderError(ATSError):
    """Sanitized error which never includes a prompt or model response."""

    def __init__(self, message, metrics=None):
        super().__init__(message)
        self.metrics = metrics


class TruncatedResponse(ProviderError):
    """Incomplete output that must not be parsed or persisted."""


class InvalidStructuredResponse(ProviderError):
    """A complete but schema-invalid response; never accept or patch its JSON."""


@dataclass
class InferenceMetrics:
    model_calls: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    duration_ms: int = 0
    truncated_responses: int = 0
    timeouts: int = 0

    def add(self, other):
        for name in self.__dataclass_fields__:
            first, second = getattr(self, name), getattr(other, name)
            setattr(self, name, None if first is None or second is None else first + second)

    def serialize(self):
        return asdict(self)


@dataclass(frozen=True)
class ModelStatus:
    connected: bool
    available: bool
    model: str
    message: str
    digest: str | None = None


@dataclass(frozen=True)
class AnalysisOutput:
    extraction: BaseModel
    model: str
    digest: str | None
    duration_ms: int
    metrics: InferenceMetrics = field(default_factory=InferenceMetrics)


class JobAnalysisProvider(Protocol):
    def check(self) -> ModelStatus: ...

    def analyze(self, description: str) -> AnalysisOutput: ...
