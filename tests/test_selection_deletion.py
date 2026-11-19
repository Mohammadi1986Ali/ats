"""Selective dashboard deletion must preserve unrelated jobs and CVs."""

import pytest
from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateDocument, CandidateScoreSnapshot, DocumentPipelineStage,
    EmbeddingChunk, JobApplication, Vacancy,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.schemas import VacancyInput


def test_delete_only_one_cv_from_same_job(ats, vacancy):
    from tests.cv_fixtures import pdf_bytes

    assert ats.documents.upload_batch(
        vacancy.id,
        [("first.pdf", pdf_bytes("Java senior engineer"))],
    )[0].success
    assert ats.documents.upload_batch(
        vacancy.id,
        [("second.pdf", pdf_bytes("Python backend engineer"))],
    )[0].success
    with ats.db.transaction() as session:
        docs = list(session.scalars(select(CandidateDocument).order_by(CandidateDocument.id)))
        first, second = docs
        first_id, second_id = first.id, second.id
        first_key, second_key = first.storage_key, second.storage_key
    assert ats.deletion.delete_documents([first_id], vacancy.id) == 1
    with ats.db.transaction() as session:
        assert session.get(CandidateDocument, first_id) is None
        assert session.get(CandidateDocument, second_id) is not None
        assert session.scalar(select(Vacancy.id)) == vacancy.id
        assert session.scalar(select(DocumentPipelineStage.id).where(
            DocumentPipelineStage.document_id == first_id
        )) is None
    assert not ats.storage.exists(first_key)
    assert ats.storage.exists(second_key)
    assert ats.deletion.delete_documents([second_id], vacancy.id) == 1
    assert ats.vacancies.list()[0].id == vacancy.id


def test_delete_only_selected_job_and_keep_other_job(ats, vacancy, pdf):
    second = ats.vacancies.create(
        VacancyInput(title="Other Job", description="Go and PostgreSQL")
    )
    assert ats.documents.upload_batch(vacancy.id, [("one.pdf", pdf)])[0].success
    from tests.cv_fixtures import pdf_bytes

    assert ats.documents.upload_batch(second.id, [
        ("two.pdf", pdf_bytes("Golang services"))
    ])[0].success
    with ats.db.transaction() as session:
        chosen_key = session.scalar(select(CandidateDocument.storage_key).where(
            CandidateDocument.vacancy_id == vacancy.id
        ))
        kept_key = session.scalar(select(CandidateDocument.storage_key).where(
            CandidateDocument.vacancy_id == second.id
        ))
    assert ats.deletion.delete_vacancies([vacancy.id]) == 1
    with ats.db.transaction() as session:
        assert session.get(Vacancy, vacancy.id) is None
        assert session.get(Vacancy, second.id) is not None
        assert session.scalar(select(CandidateDocument.id).where(
            CandidateDocument.vacancy_id == vacancy.id
        )) is None
        assert session.scalar(select(JobApplication.id).where(
            JobApplication.vacancy_id == second.id
        )) is not None
        assert session.scalar(select(EmbeddingChunk.id).where(
            EmbeddingChunk.vacancy_id == vacancy.id
        )) is None
        assert session.scalar(select(CandidateScoreSnapshot.id).where(
            CandidateScoreSnapshot.vacancy_id == vacancy.id
        )) is None
    assert not ats.storage.exists(chosen_key)
    assert ats.storage.exists(kept_key)


def test_rejects_unselected_or_busy_cv(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        doc = session.scalar(select(CandidateDocument))
        doc_id = doc.id
    with pytest.raises(ATSError):
        ats.deletion.delete_documents([doc_id], vacancy.id + 123)
    with ats.db.transaction() as session:
        stage = session.scalar(select(DocumentPipelineStage).where(
            DocumentPipelineStage.document_id == doc_id
        ))
        stage.status = "RUNNING"
    with pytest.raises(ATSError, match="processing"):
        ats.deletion.delete_documents([doc_id], vacancy.id)
    with ats.db.transaction() as session:
        assert session.get(CandidateDocument, doc_id) is not None
