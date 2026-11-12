"""Phase 1: upload queues every CV, independent text worker processes it without Ollama."""

from sqlalchemy import func, select

from job_hire_ai.database.models import DocumentPipelineStage, DocumentTextExtraction
from job_hire_ai.services.pipeline_service import PIPELINE_STAGES


def test_upload_enqueues_text_and_future_stages(ats, vacancy, pdf):
    result = ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    assert len(result) == 1 and result[0].success
    with ats.db.transaction() as session:
        stages = list(
            session.scalars(select(DocumentPipelineStage).order_by(DocumentPipelineStage.id))
        )
    assert [stage.stage for stage in stages] == list(PIPELINE_STAGES)
    assert stages[0].status == "PENDING"
    assert all(stage.status == "WAITING" for stage in stages[1:])

    assert ats.pipeline.process_next() is True
    assert ats.pipeline.process_next() is False
    with ats.db.transaction() as session:
        text = session.scalar(select(DocumentTextExtraction))
        stages = list(
            session.scalars(select(DocumentPipelineStage).order_by(DocumentPipelineStage.id))
        )
    assert text is not None
    assert "Java" in str(text.source)
    assert stages[0].status == "COMPLETED"
    assert stages[0].duration_ms is not None
    assert all(stage.status == "WAITING" for stage in stages[1:])


def test_multiple_jobs_remain_isolated(ats, vacancy, pdf, docx):
    from job_hire_ai.schemas import VacancyInput

    second = ats.vacancies.create(VacancyInput(title="Data Analyst", description="Analyze data"))
    assert ats.documents.upload_batch(vacancy.id, [("java.pdf", pdf)])[0].success
    assert ats.documents.upload_batch(second.id, [("analyst.docx", docx)])[0].success
    while ats.pipeline.process_next():
        pass
    first_summary, first_docs = ats.pipeline.overview(vacancy.id)
    second_summary, second_docs = ats.pipeline.overview(second.id)
    assert first_summary[0]["Uploaded"] == 1
    assert second_summary[0]["Uploaded"] == 1
    assert first_docs[0]["Job ID"] != second_docs[0]["Job ID"]
    assert all(row["TEXT_EXTRACTION"] == "COMPLETED" for row in first_docs + second_docs)


def test_bad_source_is_failed_without_destroying_other_cv(ats, vacancy, pdf):
    successful = ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0]
    assert successful.success
    with ats.db.transaction() as session:
        stage = session.scalar(select(DocumentPipelineStage))
        stage_id = stage.id
    # Simulate the uploaded file going missing after it was saved.
    with ats.db.transaction() as session:
        from job_hire_ai.database.models import CandidateDocument

        document = session.get(CandidateDocument, stage.document_id)
        ats.storage.delete(document.storage_key)
    assert ats.pipeline.process_next() is True
    with ats.db.transaction() as session:
        stage = session.get(DocumentPipelineStage, stage_id)
        assert stage.status == "FAILED"
        assert stage.error_summary
        assert session.scalar(select(func.count(DocumentTextExtraction.id))) == 0


def test_worker_recovers_interrupted_and_does_not_reprocess(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        stage = session.scalar(select(DocumentPipelineStage))
        stage.status = "RUNNING"
        stage.attempts = 1
    ats.pipeline.recover_interrupted()
    assert ats.pipeline.process_next()
    ats.pipeline.recover_interrupted()
    assert not ats.pipeline.process_next()
    with ats.db.transaction() as session:
        assert session.scalar(select(func.count(DocumentTextExtraction.id))) == 1


def test_upload_100_cvs_creates_100_independent_queued_jobs(ats, vacancy):
    import io

    from docx import Document

    files = []
    for index in range(100):
        cv = Document()
        cv.add_paragraph(f"Synthetic resume number {index}: Python and SQL.")
        buffer = io.BytesIO()
        cv.save(buffer)
        files.append((f"candidate_{index}.docx", buffer.getvalue()))
    results = ats.documents.upload_batch(vacancy.id, files)
    assert all(result.success for result in results)
    summary, items = ats.pipeline.overview(vacancy.id)
    assert summary[0]["Uploaded"] == 100
    assert summary[0]["Queued"] == 100
    assert len(items) == 100
    with ats.db.transaction() as session:
        assert session.scalar(select(func.count(DocumentPipelineStage.id))) == (
            100 * len(PIPELINE_STAGES)
        )
    while ats.pipeline.process_next():
        pass
    summary, _ = ats.pipeline.overview(vacancy.id)
    assert summary[0]["Text complete"] == 100
    with ats.db.transaction() as session:
        assert session.scalar(select(func.count(DocumentTextExtraction.id))) == 100
