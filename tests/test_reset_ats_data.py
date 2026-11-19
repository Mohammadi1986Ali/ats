"""Reset preserves schema/settings and removes all jobs, CVs and source files."""

import pytest
from sqlalchemy import inspect, select

from job_hire_ai.database.models import Candidate, CandidateDocument, Vacancy
from job_hire_ai.reset_ats_data import reset_ats_data
from job_hire_ai.schemas import VacancyInput


def test_reset_clears_jobs_resumes_candidates_and_files(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("resume.pdf", pdf)])[0].success
    extra = ats.vacancies.create(VacancyInput(
        title="Second role", description="Kafka and Java",
    ))
    assert extra.id != vacancy.id
    with ats.db.transaction() as session:
        key = session.scalar(select(CandidateDocument.storage_key))
        assert ats.storage.exists(key)
    preserved = ats.storage.directory / "personal-not-ats.txt"
    preserved.write_text("Keep this", encoding="utf-8")
    marker = ats.pipeline.data_dir / ".structured-source-profile-v3.done"
    marker.write_text("done", encoding="utf-8")
    result = reset_ats_data(ats.db, ats.storage, ats.pipeline.data_dir)
    assert result["jobs"] == 2
    assert result["resumes"] == 1
    assert result["files_deleted"] == 1
    assert result["file_cleanup_failed"] == 0
    assert not ats.storage.exists(key)
    assert preserved.read_text(encoding="utf-8") == "Keep this"
    assert not marker.exists()
    with ats.db.transaction() as session:
        assert not session.scalars(select(Candidate)).all()
        assert not session.scalars(select(CandidateDocument)).all()
        assert not session.scalars(select(Vacancy)).all()
        assert inspect(session.bind).has_table("vacancies")
    assert ats.vacancies.create(VacancyInput(
        title="New role after reset", description="Backend services",
    )).id > 0


def test_reset_is_idempotent_and_does_not_delete_unmanaged_files(ats):
    path = ats.storage.directory / "user_uploaded_notes.pdf"
    path.write_bytes(b"keep")
    for _ in range(2):
        result = reset_ats_data(ats.db, ats.storage, ats.pipeline.data_dir)
        assert result["jobs"] == 0
        assert result["resumes"] == 0
    assert path.read_bytes() == b"keep"
