from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from job_hire_ai.settings import ROOT


class OllamaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OLLAMA_", env_file=ROOT / ".env", extra="ignore", populate_by_name=True
    )
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:0.5b"
    temperature: float = Field(default=0, ge=0, le=1)
    num_ctx: int = Field(default=8192, ge=4096, le=32768)
    num_predict: int = Field(default=4096, ge=256, le=8192)
    num_thread: int = Field(default=2, ge=1, le=32)
    timeout_seconds: float = Field(
        default=120,
        ge=1,
        le=600,
        validation_alias=AliasChoices("OLLAMA_TIMEOUT", "OLLAMA_TIMEOUT_SECONDS"),
    )
    retries: int = Field(default=0, ge=0, le=2)
    max_description_chars: int = Field(default=12000, ge=100, le=50000)
    cv_truncation_retries: int = Field(default=4, ge=0, le=4)
    cv_max_model_calls: int = Field(default=32, ge=1, le=64)
    cv_chunk_chars: int = Field(default=1200, ge=300, le=6000)
    cv_max_output_tokens: int = Field(default=3072, ge=256, le=8192)
    cv_literal_fallback_enabled: bool = True
    cv_fast_mode_1b: bool = False

    @field_validator("base_url")
    @classmethod
    def local_only(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError(
                "Ollama must use a local HTTP loopback URL without credentials or paths."
            )
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def local_model(cls, value):
        if not value.strip() or "cloud" in value.lower() or "/" in value or " " in value:
            raise ValueError("Use a locally installed model tag, such as qwen2.5:0.5b.")
        return value

    def options(self):
        return {
            "temperature": self.temperature,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "num_thread": self.num_thread,
            "num_gpu": 0,
            "seed": 42,
        }
