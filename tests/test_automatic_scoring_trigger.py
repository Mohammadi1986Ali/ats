"""Automatic scoring must not stall behind an unconfigured job."""

from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateScoreSnapshot,
    DocumentPipelineStage,
)
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.legacy_jd_activation import recover_legacy_jd_weights
from job_hire_ai.services.pipeline_service import PipelineService
from tests.test_job_matching import approve_criteria, make_data


def _stage(session, doc_id, name):
    return session.scalar(select(DocumentPipelineStage).where(
        DocumentPipelineStage.document_id == doc_id,
        DocumentPipelineStage.stage == name,
    ))


def test_unconfigured_first_job_does_not_block_second_job_scoring(ats, vacancy, pdf):
    unconfigured_id, _ = make_data(ats, vacancy, pdf)
    ready_job = ats.vacancies.create(VacancyInput(
        title="Backend Engineer", description="Java programming required",
    ))
    eligible_id, _ = make_data(ats, ready_job, pdf)
    approve_criteria(ats, ready_job, names=("Java",))
    ats.job_matching.match_document(eligible_id)
    with ats.db.transaction() as session:
        _stage(session, unconfigured_id, "MATCHING").status = "COMPLETED"
        _stage(session, eligible_id, "MATCHING").status = "COMPLETED"
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir, scoring_enabled=True,
    )
    assert pipeline.process_scoring_next()
    with ats.db.transaction() as session:
        assert _stage(session, unconfigured_id, "SCORING").status == "WAITING"
        assert _stage(session, eligible_id, "SCORING").status == "COMPLETED"
        assert session.scalar(select(CandidateScoreSnapshot.id).where(
            CandidateScoreSnapshot.document_id == eligible_id
        )) is not None


def test_job_created_after_worker_started_can_activate_and_score(ats, pdf):
    job = ats.vacancies.create(VacancyInput(
        title="Java Backend", description="Required Java experience",
        weighting_mode="DEFAULT",
    ))
    doc_id, _ = make_data(ats, job, pdf)
    with ats.db.transaction() as session:
        _stage(session, doc_id, "EVIDENCE_VALIDATION").status = "COMPLETED"
        _stage(session, doc_id, "MATCHING").status = "COMPLETED"
    assert not ats.job_matching.ready(job.id)
    assert recover_legacy_jd_weights(ats.db)[job.id] == (
        "Activated default JD criteria"
    )
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        matching_enabled=True, scoring_enabled=True,
    )
    assert pipeline.process_matching_next()
    with ats.db.transaction() as session:
        assert _stage(session, doc_id, "MATCHING").status == "PENDING"
    assert pipeline.process_matching_next()
    assert pipeline.process_scoring_next()
    with ats.db.transaction() as session:
        assert _stage(session, doc_id, "SCORING").status == "COMPLETED"
        assert session.scalar(select(CandidateScoreSnapshot.id).where(
            CandidateScoreSnapshot.document_id == doc_id
        )) is not None
