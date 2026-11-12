from pydantic import BaseModel, ConfigDict, Field


class VacancyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=100000)
    department: str | None = Field(default=None, max_length=200)


class UploadResult(BaseModel):
    filename: str
    success: bool
    message: str
    application_id: int | None = None
