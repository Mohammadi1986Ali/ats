from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATS_", env_file=ROOT / ".env", extra="ignore")
    data_dir: Path = ROOT / "data"
    max_file_mb: int = Field(default=10, ge=1, le=100)

    @property
    def directory(self) -> Path:
        return self.data_dir.resolve() if self.data_dir.is_absolute() else ROOT / self.data_dir

    @property
    def database_url(self) -> str:
        return "sqlite:///" + (self.directory / "ats.sqlite3").as_posix()
