from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class Vacancy(Timestamps, Base):
    __tablename__ = "vacancies"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0"),
        CheckConstraint("length(trim(description)) > 0"),
        CheckConstraint("status IN ('Active', 'Archived')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str]
    status: Mapped[str] = mapped_column(String(20), default="Active", index=True)
    applications: Mapped[list["JobApplication"]] = relationship(back_populates="vacancy")
    criteria_configurations: Mapped[list["CriteriaConfiguration"]] = relationship(
        back_populates="vacancy"
    )
    analysis_runs: Mapped[list["JobAnalysisRun"]] = relationship(back_populates="vacancy")


class Candidate(Timestamps, Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    applications: Mapped[list["JobApplication"]] = relationship(back_populates="candidate")

    @property
    def reference(self) -> str:
        return f"C-{self.id:06d}"


class JobApplication(Timestamps, Base):
    __tablename__ = "job_applications"
    __table_args__ = (
        UniqueConstraint("id", "vacancy_id"),
        CheckConstraint("status IN ('Uploaded', 'Removed')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="Uploaded", index=True)
    candidate: Mapped[Candidate] = relationship(back_populates="applications")
    vacancy: Mapped[Vacancy] = relationship(back_populates="applications")
    documents: Mapped[list["CandidateDocument"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class CandidateDocument(Base):
    __tablename__ = "candidate_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "vacancy_id"], ["job_applications.id", "job_applications.vacancy_id"]
        ),
        UniqueConstraint("vacancy_id", "checksum", name="uq_vacancy_checksum"),
        CheckConstraint("file_size > 0"),
        CheckConstraint("file_type IN ('pdf', 'docx')"),
        CheckConstraint("text_extraction_status IN ('Available', 'Unavailable')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(index=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(64), unique=True)
    file_type: Mapped[str] = mapped_column(String(8))
    file_size: Mapped[int]
    checksum: Mapped[str] = mapped_column(String(64))
    upload_timestamp: Mapped[datetime] = mapped_column(default=now, index=True)
    text_extraction_status: Mapped[str] = mapped_column(String(20))
    application: Mapped[JobApplication] = relationship(back_populates="documents")


class JobAnalysisRun(Base):
    __tablename__ = "job_analysis_runs"
    __table_args__ = (CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(200))
    model_digest: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_description_hash: Mapped[str] = mapped_column(String(64))
    inference_options: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(default=now)
    completed_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    error_summary: Mapped[str | None] = mapped_column(String(500))
    vacancy: Mapped[Vacancy] = relationship(back_populates="analysis_runs")
    configurations: Mapped[list["CriteriaConfiguration"]] = relationship(
        back_populates="analysis_run"
    )


class CriteriaConfiguration(Base):
    __tablename__ = "criteria_configurations"
    __table_args__ = (
        UniqueConstraint("vacancy_id", "version"),
        CheckConstraint("version > 0"),
        CheckConstraint("status IN ('DRAFT', 'APPROVED', 'SUPERSEDED')"),
        CheckConstraint("status = 'DRAFT' OR approved_at IS NOT NULL"),
        Index(
            "uq_current_approved",
            "vacancy_id",
            unique=True,
            sqlite_where=text("status = 'APPROVED'"),
            postgresql_where=text("status = 'APPROVED'"),
        ),
        Index(
            "uq_current_draft", "vacancy_id", unique=True,
            sqlite_where=text("status = 'DRAFT'"),
            postgresql_where=text("status = 'DRAFT'"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), index=True)
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    job_description_hash: Mapped[str] = mapped_column(String(64))
    job_description_snapshot: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=now)
    approved_at: Mapped[datetime | None]
    approved_by: Mapped[str | None] = mapped_column(String(200))
    analysis_run_id: Mapped[int | None] = mapped_column(ForeignKey("job_analysis_runs.id"))
    revision: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": revision}
    vacancy: Mapped[Vacancy] = relationship(back_populates="criteria_configurations")
    analysis_run: Mapped[JobAnalysisRun | None] = relationship(back_populates="configurations")
    criteria: Mapped[list["JobCriterion"]] = relationship(
        back_populates="configuration",
        cascade="all, delete-orphan",
        order_by="JobCriterion.display_order",
    )


class JobCriterion(Base):
    __tablename__ = "job_criteria"
    __table_args__ = (
        UniqueConstraint("configuration_id", "criterion_key"),
        CheckConstraint("length(trim(name)) > 0 AND length(trim(description)) > 0"),
        CheckConstraint("requirement_type IN ('REQUIRED', 'PREFERRED', 'UNCLEAR')"),
        CheckConstraint("evidence_status IN ('VERIFIED', 'UNVERIFIED')"),
        CheckConstraint("created_by IN ('AI', 'AI_MODIFIED', 'MANUAL')"),
        CheckConstraint(
            "weight IS NULL OR weight BETWEEN 0 AND 100"
        ),
        CheckConstraint("display_order BETWEEN 1 AND 50"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("criteria_configurations.id"), index=True
    )
    criterion_key: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100))
    description: Mapped[str]
    requirement_type: Mapped[str] = mapped_column(String(20))
    source_excerpt: Mapped[str]
    extraction_notes: Mapped[str]
    uncertainty: Mapped[str]
    evidence_status: Mapped[str] = mapped_column(String(20))
    evidence_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledgement_note: Mapped[str]
    weight: Mapped[int | None]
    display_order: Mapped[int]
    created_by: Mapped[str] = mapped_column(String(20))
    original_extraction: Mapped[dict | None] = mapped_column(JSON)
    configuration: Mapped[CriteriaConfiguration] = relationship(back_populates="criteria")


class DocumentTextExtraction(Base):
    __tablename__ = "document_text_extractions"
    __table_args__ = (UniqueConstraint("document_id", "checksum", "extractor_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), index=True
    )
    checksum: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(100))
    source: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=now)


class CVAnalysisBatch(Base):
    __tablename__ = "cv_analysis_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    jobs: Mapped[list["CVAnalysisJob"]] = relationship(back_populates="batch")


class CVAnalysisJob(Base):
    __tablename__ = "cv_analysis_jobs"
    __table_args__ = (
        UniqueConstraint("batch_id", "document_id"),
        CheckConstraint(
            "status IN ('PENDING', 'EXTRACTING', 'ANALYZING', 'VALIDATING', "
            "'COMPLETED', 'FAILED', 'NEEDS_REVIEW')"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("cv_analysis_batches.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    force_reanalysis: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    worker_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    error_summary: Mapped[str | None] = mapped_column(String(500))
    reused_profile: Mapped[bool] = mapped_column(default=False)
    batch: Mapped[CVAnalysisBatch] = relationship(back_populates="jobs")
    runs: Mapped[list["CVAnalysisRun"]] = relationship(
        back_populates="job", order_by="CVAnalysisRun.id"
    )


class CVAnalysisRun(Base):
    __tablename__ = "cv_analysis_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("cv_analysis_jobs.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="SET NULL"), index=True
    )
    document_checksum: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(200))
    model_digest: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[str] = mapped_column(String(100))
    config_hash: Mapped[str] = mapped_column(String(64))
    inference_options: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="EXTRACTING")
    started_at: Mapped[datetime] = mapped_column(default=now)
    completed_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    chunk_count: Mapped[int] = mapped_column(default=0)
    chunks_completed: Mapped[int] = mapped_column(default=0)
    error_summary: Mapped[str | None] = mapped_column(String(500))
    job: Mapped[CVAnalysisJob] = relationship(back_populates="runs")
    profile: Mapped["CandidateProfile | None"] = relationship(back_populates="analysis_run")


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"
    __table_args__ = (
        UniqueConstraint("document_id", "extraction_version"),
        CheckConstraint("extraction_version > 0"),
        CheckConstraint("analysis_status IN ('COMPLETED', 'NEEDS_REVIEW')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), index=True
    )
    text_extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_text_extractions.id", ondelete="CASCADE")
    )
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("cv_analysis_runs.id"), unique=True)
    extraction_version: Mapped[int]
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    analysis_status: Mapped[str] = mapped_column(String(20))
    original_extraction: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=now)
    candidate: Mapped[Candidate] = relationship()
    document: Mapped[CandidateDocument] = relationship()
    analysis_run: Mapped[CVAnalysisRun] = relationship(back_populates="profile")
    evidence: Mapped[list["EvidenceReference"]] = relationship(
        back_populates="profile", passive_deletes=True
    )
    reviews: Mapped[list["ProfileReviewEvent"]] = relationship(
        back_populates="profile", passive_deletes=True, order_by="ProfileReviewEvent.id"
    )


class EvidenceReference(Base):
    __tablename__ = "evidence_references"
    __table_args__ = (
        CheckConstraint("verification_status IN ('VERIFIED', 'UNVERIFIED', 'AMBIGUOUS')"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(200))
    source_excerpt: Mapped[str]
    source_reference: Mapped[str | None] = mapped_column(String(200))
    page_number: Mapped[int | None]
    character_start: Mapped[int | None]
    character_end: Mapped[int | None]
    verification_status: Mapped[str] = mapped_column(String(20))
    profile: Mapped[CandidateProfile] = relationship(back_populates="evidence")


class ProfileReviewEvent(Base):
    __tablename__ = "profile_review_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(200))
    original_value: Mapped[dict | list | str | None] = mapped_column(JSON)
    corrected_value: Mapped[dict | list | str | None] = mapped_column(JSON)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=now)
    profile: Mapped[CandidateProfile] = relationship(back_populates="reviews")



class JobMatchResult(Base):
    """Versioned, non-scoring criterion-evidence matches for HR inspection."""

    __tablename__ = "job_match_results"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "configuration_id", "profile_id",
            name="uq_job_match_snapshot",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), index=True
    )
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("criteria_configurations.id"), index=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), index=True
    )
    criteria_results: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=now)



class DocumentPipelineStage(Base):
    """Durable, application-specific stage state; one stage per uploaded document."""

    __tablename__ = "document_pipeline_stages"
    __table_args__ = (
        UniqueConstraint("document_id", "stage", name="uq_document_pipeline_stage"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'WAITING')",
            name="ck_document_pipeline_stage_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    error_summary: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class Vector768Type(UserDefinedType):
    """PostgreSQL pgvector storage; SQLite uses text in offline tests."""

    cache_ok = True

    def get_col_spec(self, **_):
        return "vector(768)"


class EmbeddingChunk(Base):
    """Source-addressable embeddings; not a hiring decision or score."""

    __tablename__ = "embedding_chunks"
    __table_args__ = (
        UniqueConstraint(
            "source_type", "vacancy_id", "document_id", "source_hash",
            "model", "chunker_version", "chunk_index", name="uq_embedding_chunk",
        ),
        CheckConstraint(
            "(source_type = 'CV' AND document_id IS NOT NULL) OR "
            "(source_type = 'JD' AND document_id IS NULL)",
            name="ck_embedding_source",
        ),
    )
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    source_type: Mapped[str] = mapped_column(String(8))
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), index=True
    )
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    source_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    chunker_version: Mapped[str] = mapped_column(String(40))
    chunk_index: Mapped[int]
    reference: Mapped[str] = mapped_column(String(200))
    chunk_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[str] = mapped_column(Vector768Type().with_variant(Text(), "sqlite"))
