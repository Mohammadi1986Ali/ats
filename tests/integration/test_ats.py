from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from job_hire_ai.database.models import Candidate, CandidateDocument, JobApplication
from job_hire_ai.errors import ATSError
from job_hire_ai.runtime import Runtime
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.settings import Settings


def test_vacancy_lifecycle(ats, vacancy):
    ats.vacancies.edit(
        vacancy.id,
        VacancyInput(
            title="Updated role", description="Updated description", department="Engineering"
        ),
    )
    edited = ats.vacancies.list()[0]
    assert edited.title == "Updated role"
    assert edited.department == "Engineering"
    assert edited.updated_at >= edited.created_at
    ats.vacancies.set_status(vacancy.id, "Archived")
    assert not ats.vacancies.list("Active")
    assert len(ats.vacancies.list("Archived")) == 1
    ats.vacancies.set_status(vacancy.id, "Active")
    assert len(ats.vacancies.list("Active")) == 1


def test_candidate_creation(ats):
    candidate = ats.candidates.create()
    assert candidate.reference == "C-000001"
    assert candidate.display_name is candidate.email is None


def test_mixed_uploads_and_download(ats, vacancy, pdf, docx):
    results = ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf), ("cv.docx", docx), ("broken.pdf", b"bad")]
    )
    assert [r.success for r in results] == [True, True, False]
    assert len(ats.candidates.list()) == 2
    applications = ats.applications.list(vacancy.id)
    assert len(applications) == 2
    for application in applications:
        document = application.documents[0]
        name, data, kind = ats.documents.download(document.id)
        assert data == (pdf if kind == "pdf" else docx)
        assert name == f"cv.{kind}"
        assert document.file_size == len(data)
        assert document.checksum == ats.storage.checksum(data)


def test_duplicate_detection_and_cross_vacancy(ats, vacancy, pdf):
    results = ats.documents.upload_batch(vacancy.id, [("one.pdf", pdf), ("renamed.pdf", pdf)])
    assert results[0].success
    assert not results[1].success and "Duplicate" in results[1].message
    assert len(ats.applications.list()) == 1
    other = ats.vacancies.create(VacancyInput(title="Other", description="Another role"))
    candidate_id = ats.candidates.list()[0].id
    assert ats.documents.upload_batch(other.id, [("cv.pdf", pdf)], candidate_id)[0].success
    assert len(ats.candidates.list()) == 1
    assert len(ats.applications.list()) == 2


def test_explicit_candidate_multiple_documents(ats, vacancy, pdf, docx):
    candidate = ats.candidates.create()
    results = ats.documents.upload_batch(
        vacancy.id, [("cv.pdf", pdf), ("cv.docx", docx)], candidate.id
    )
    assert all(r.success for r in results)
    assert len(ats.candidates.list()) == 1
    assert len(ats.applications.list()) == 1
    assert len(ats.applications.list()[0].documents) == 2


def test_batch_limits(ats, vacancy, pdf):
    with pytest.raises(ATSError):
        ats.documents.upload_batch(vacancy.id, [])
    with pytest.raises(ATSError):
        ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)] * 101)
    results = ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)] * 100)
    assert len(results) == 100
    assert sum(r.success for r in results) == 1


def test_archived_history_and_blocked_upload(ats, vacancy, pdf):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    ats.vacancies.set_status(vacancy.id, "Archived")
    assert len(ats.applications.list(vacancy.id)) == 1
    result = ats.documents.upload_batch(vacancy.id, [("other.pdf", pdf)])[0]
    assert not result.success and "active vacancy" in result.message


def test_remove_deletes_files_retains_history(ats, vacancy, pdf):
    result = ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0]
    document = ats.applications.list()[0].documents[0]
    ats.applications.remove(result.application_id)
    assert not ats.storage.exists(document.storage_key)
    application = ats.applications.list()[0]
    assert application.status == "Removed" and application.documents == []
    assert len(ats.candidates.list()) == 1
    with pytest.raises(ATSError):
        ats.documents.download(document.id)
    assert ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0].success


def test_delete_failure_is_retryable(ats, vacancy, pdf, monkeypatch):
    result = ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0]
    original = ats.storage.delete

    def fail(_):
        raise PermissionError("synthetic failure")

    monkeypatch.setattr(ats.storage, "delete", fail)
    with pytest.raises(ATSError, match="pending"):
        ats.applications.remove(result.application_id)
    application = ats.applications.list()[0]
    assert application.status == "Removed" and len(application.documents) == 1
    with pytest.raises(ATSError):
        ats.documents.download(application.documents[0].id)
    monkeypatch.setattr(ats.storage, "delete", original)
    ats.applications.remove(result.application_id)
    assert ats.applications.list()[0].documents == []


def test_upload_commit_failure_cleans_new_file(ats, vacancy, pdf, monkeypatch):
    original = ats.db.transaction

    @contextmanager
    def broken_transaction():
        with original() as session:
            yield session
            raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(ats.db, "transaction", broken_transaction)
    result = ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])[0]
    assert not result.success
    assert list(ats.storage.directory.iterdir()) == []
    monkeypatch.setattr(ats.db, "transaction", original)
    assert ats.candidates.list() == [] and ats.applications.list() == []


def test_persistence(ats, vacancy, pdf):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    settings = Settings(data_dir=ats.storage.directory.parent)
    ats.db.engine.dispose()
    restarted = Runtime(settings)
    try:
        assert restarted.vacancies.list()[0].title == "Senior Java Developer"
        document = restarted.applications.list()[0].documents[0]
        assert restarted.documents.download(document.id)[1] == pdf
    finally:
        restarted.db.engine.dispose()


def test_foreign_keys_enforced(ats):
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        session.add(JobApplication(candidate_id=999, vacancy_id=999))


def test_referenced_candidate_cannot_be_deleted(ats, vacancy, pdf):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        session.delete(session.get(Candidate, 1))


def test_dashboard_real_counts(ats, vacancy, pdf, docx):
    assert ats.dashboard.snapshot()["metrics"] == {
        "Active vacancies": 1,
        "Archived vacancies": 0,
        "Candidate records": 0,
        "Job applications": 0,
        "Uploaded CVs": 0,
    }
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf), ("cv.docx", docx)])
    metrics = ats.dashboard.snapshot()["metrics"]
    assert (
        metrics["Candidate records"] == metrics["Uploaded CVs"] == metrics["Job applications"] == 2
    )
    assert ats.dashboard.snapshot()["counts"][0]["Applications"] == 2
    ats.vacancies.set_status(vacancy.id, "Archived")
    assert ats.dashboard.snapshot()["metrics"]["Archived vacancies"] == 1
    ats.applications.remove(ats.applications.list()[0].id)
    assert ats.dashboard.snapshot()["metrics"]["Uploaded CVs"] == 1


def test_empty_dashboard(ats):
    assert all(value == 0 for value in ats.dashboard.snapshot()["metrics"].values())


def test_filtered_dashboard(ats, vacancy, pdf):
    other = ats.vacancies.create(VacancyInput(title="Other", description="Another role"))
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    assert ats.dashboard.snapshot(other.id)["metrics"]["Candidate records"] == 0
    assert ats.dashboard.snapshot(vacancy.id)["metrics"]["Uploaded CVs"] == 1


def test_checksum_constraint_in_database(ats, vacancy, pdf):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    with pytest.raises(IntegrityError), ats.db.transaction() as session:
        original = session.scalar(select(CandidateDocument))
        session.add(
            CandidateDocument(
                application_id=original.application_id,
                vacancy_id=vacancy.id,
                original_filename="copy.pdf",
                storage_key="b" * 32 + ".pdf",
                file_type="pdf",
                file_size=len(pdf),
                checksum=original.checksum,
                text_extraction_status="Available",
            )
        )


def test_download_detects_tampering(ats, vacancy, pdf):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    document = ats.applications.list()[0].documents[0]
    ats.storage.path(document.storage_key).write_bytes(b"tampered")
    with pytest.raises(ATSError, match="integrity"):
        ats.documents.download(document.id)


def test_full_hundred_unique_documents(ats, vacancy):
    import pymupdf

    files = []
    for index in range(100):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), f"Synthetic CV {index}")
            files.append((f"synthetic-{index}.pdf", document.tobytes()))
    results = ats.documents.upload_batch(vacancy.id, files)
    assert len(results) == 100 and all(r.success for r in results)
    assert len(ats.candidates.list()) == len(ats.applications.list()) == 100
    assert ats.dashboard.snapshot()["metrics"]["Uploaded CVs"] == 100


def test_failed_upload_preserves_existing_files(ats, vacancy, pdf, docx, monkeypatch):
    ats.documents.upload_batch(vacancy.id, [("cv.pdf", pdf)])
    old_document = ats.applications.list()[0].documents[0]

    def fail_save(*_):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(ats.storage, "save", fail_save)
    result = ats.documents.upload_batch(vacancy.id, [("cv.docx", docx)])[0]
    assert not result.success
    assert len(ats.candidates.list()) == len(ats.applications.list()) == 1
    assert ats.documents.download(old_document.id)[1] == pdf
