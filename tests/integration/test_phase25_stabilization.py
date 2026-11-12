"""Regression checks for automatic per-vacancy CV processing and persistence.

Uses synthetic DOCX data; no Ollama, PII or network dependency.
"""

import io
from collections import Counter

from docx import Document
from sqlalchemy import func, select

from job_hire_ai.dashboard_view import aggregate, job_pulses, stage_breakdown
from job_hire_ai.database.models import (
    CandidateDocument,
    DocumentPipelineStage,
    DocumentTextExtraction,
)
from job_hire_ai.database.session import Database
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.document_service import DocumentService
from job_hire_ai.services.pipeline_service import PIPELINE_STAGES, PipelineService


def _cv(index: int, job: str) -> tuple[str, bytes]:
    doc = Document()
    doc.add_paragraph(f"Synthetic candidate {index} for {job}. Java, SQL, Python.")
    content = io.BytesIO()
    doc.save(content)
    return f"synthetic_{job}_{index}.docx", content.getvalue()


def test_100_plus_35_cvs_process_without_manual_analysis(ats, vacancy):
    """135 uploads, two independent jobs, no AI call and honest dashboard metrics."""
    second = ats.vacancies.create(VacancyInput(title="Data Analyst", description="SQL"))
    first_files = [_cv(i, "java") for i in range(100)]
    second_files = [_cv(i, "analytics") for i in range(35)]
    assert all(r.success for r in ats.documents.upload_batch(vacancy.id, first_files))
    assert all(r.success for r in ats.documents.upload_batch(second.id, second_files))

    summaries, rows = ats.pipeline.overview()
    assert {x["Job ID"]: x["Uploaded"] for x in summaries} == {
        vacancy.id: 100,
        second.id: 35,
    }
    assert len(rows) == 135
    assert all(x["TEXT_EXTRACTION"] == "PENDING" for x in rows)
    assert all(x[stage] == "WAITING" for x in rows for stage in PIPELINE_STAGES[1:])
    with ats.db.transaction() as session:
        stage_count = session.scalar(select(func.count(DocumentPipelineStage.id)))
        assert stage_count == 135 * len(PIPELINE_STAGES)

    # Simulates an independent worker processing the durable queue.
    processed = 0
    while ats.pipeline.process_next():
        processed += 1
        assert processed <= 135
    assert processed == 135
    assert ats.pipeline.process_next() is False
    with ats.db.transaction() as session:
        assert session.scalar(select(func.count(DocumentTextExtraction.id))) == 135
    summaries, rows = ats.pipeline.overview()
    assert {x["Job ID"]: x["Text complete"] for x in summaries} == {
        vacancy.id: 100,
        second.id: 35,
    }
    assert all(x["TEXT_EXTRACTION"] == "COMPLETED" for x in rows)

    pulses = job_pulses(ats.vacancies.list(), rows)
    assert aggregate(pulses)["Uploaded CVs"] == 135
    assert aggregate(pulses)["Text complete"] == 135
    assert aggregate(pulses)["Failed"] == 0
    assert all(x["Waiting"] == 135 for x in stage_breakdown(rows)[1:])

    # Simulate Streamlit/worker restart using a fresh connection to the same DB.
    reopened_db = Database(ats.db.engine.url.render_as_string())
    try:
        reopened = PipelineService(
            reopened_db, DocumentService(reopened_db, ats.storage), ats.pipeline.data_dir
        )
        _, persisted = reopened.overview()
        assert len(persisted) == 135
        assert Counter(x["TEXT_EXTRACTION"] for x in persisted) == {"COMPLETED": 135}
        assert reopened.process_next() is False
    finally:
        reopened_db.engine.dispose()


def test_failure_isolated_to_document_and_other_jobs_continue(ats, vacancy):
    second = ats.vacancies.create(VacancyInput(title="Data Analyst", description="SQL"))
    assert ats.documents.upload_batch(vacancy.id, [_cv(1, "java")])[0].success
    assert ats.documents.upload_batch(second.id, [_cv(2, "analytics")])[0].success
    _, items = ats.pipeline.overview(vacancy.id)
    with ats.db.transaction() as session:
        document = session.get(CandidateDocument, items[0]["CV"])
        ats.storage.delete(document.storage_key)
    assert ats.pipeline.process_next() is True
    assert ats.pipeline.process_next() is True
    first, _ = ats.pipeline.overview(vacancy.id)
    other, _ = ats.pipeline.overview(second.id)
    assert first[0]["Failed"] == 1
    assert other[0]["Text complete"] == 1


def test_worker_restarts_do_not_lose_pending_or_duplicate_saved_text(ats, vacancy):
    assert all(r.success for r in ats.documents.upload_batch(
        vacancy.id, [_cv(1, "java"), _cv(2, "java")]
    ))
    with ats.db.transaction() as session:
        stage = session.scalar(select(DocumentPipelineStage).where(
            DocumentPipelineStage.stage == "TEXT_EXTRACTION"
        ).order_by(DocumentPipelineStage.id))
        stage.status = "RUNNING"
        stage.attempts = 1
    ats.pipeline.recover_interrupted()
    while ats.pipeline.process_next():
        pass
    ats.pipeline.recover_interrupted()
    assert ats.pipeline.process_next() is False
    with ats.db.transaction() as session:
        assert session.scalar(select(func.count(DocumentTextExtraction.id))) == 2
        statuses = Counter(session.scalars(select(DocumentPipelineStage.status).where(
            DocumentPipelineStage.stage == "TEXT_EXTRACTION"
        )))
        assert statuses == {"COMPLETED": 2}
