"""One-time recovery for previously uploaded CVs with unweighted JDs."""

from sqlalchemy import select

from job_hire_ai.database.models import DocumentPipelineStage
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.legacy_jd_activation import (
    literal_jd_criteria,
    recover_legacy_jd_weights,
)


def test_source_only_fallback_does_not_invent_requirements():
    assert literal_jd_criteria("Friendly team and competitive culture.") == []
    result = literal_jd_criteria("Must have Java and Spring Boot. Python is preferred.")
    assert {row.name for row in result} == {"Java", "Spring Boot", "Python"}
    assert all(row.source_excerpt in "Must have Java and Spring Boot. Python is preferred."
               for row in result)
    assert all(row.requirement_type == "REQUIRED" for row in result[:2])
    assert result[2].requirement_type == "UNCLEAR"


def test_legacy_job_with_uploaded_cv_gets_default_criteria(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("existing.pdf", pdf)])[0].success
    result = recover_legacy_jd_weights(ats.db)
    assert result[vacancy.id] == "Activated default JD criteria"
    assert ats.job_matching.ready(vacancy.id)
    configuration = ats.criteria.history(vacancy.id)[0]
    assert configuration.approved_by == "SYSTEM_DEFAULT_NOT_HR"
    assert [row.name for row in configuration.criteria] == ["Java"]
    assert [row.weight for row in configuration.criteria] == [100]
    assert recover_legacy_jd_weights(ats.db) == {}
    assert ats.vacancies.list()[0].weighting_mode == "DEFAULT"


def test_existing_semantic_match_is_requeued_after_default_activation(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("old.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.stage == "MATCHING"
            )
        )
        stage.status = "COMPLETED"
    assert recover_legacy_jd_weights(ats.db)[vacancy.id] == (
        "Activated default JD criteria"
    )
    assert ats.job_matching.requeue_stale()
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.stage == "MATCHING"
            )
        )
        assert stage.status == "PENDING"


def test_explicit_manual_weighting_is_not_overwritten(ats, pdf):
    vacancy = ats.vacancies.create(VacancyInput(
        title="Manual", description="Java and Kafka required",
        weighting_mode="MANUAL", weighting_rules="Java = 60\\nKafka = 40",
    ))
    assert ats.documents.upload_batch(vacancy.id, [("manual.pdf", pdf)])[0].success
    assert recover_legacy_jd_weights(ats.db) == {}
    assert ats.criteria.history(vacancy.id) == []


def test_no_explicit_jd_skills_cannot_auto_activate(ats, pdf):
    vacancy = ats.vacancies.create(VacancyInput(
        title="General role", description="Enjoy a supportive team environment.",
    ))
    assert ats.documents.upload_batch(vacancy.id, [("general.pdf", pdf)])[0].success
    assert recover_legacy_jd_weights(ats.db)[vacancy.id] == (
        "No explicit supported JD skills found"
    )
    assert not ats.job_matching.ready(vacancy.id)
