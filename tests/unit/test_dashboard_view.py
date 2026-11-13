"""Phase 2 dashboard projections must be deterministic and job-isolated."""
from types import SimpleNamespace

from job_hire_ai.dashboard_view import (
    aggregate,
    current_stage,
    document_rows,
    failure_rows,
    job_pulses,
    stage_breakdown,
)


def _job(id, title, status="Active"):
    return SimpleNamespace(id=id, title=title, status=status)


def _cv(job, n, status="PENDING", error=None):
    return {
        "Job ID": job,
        "Job": "Java" if job == 1 else "Data",
        "Application": n + 100,
        "CV": n,
        "Filename": f"candidate_{n}.pdf",
        "TEXT_EXTRACTION": status,
        "TEXT_EXTRACTION_duration_ms": 1200 if status == "COMPLETED" else None,
        "TEXT_EXTRACTION_attempts": 1 if status != "PENDING" else 0,
        "TEXT_EXTRACTION_error": error,
        "EMBEDDING": "WAITING",
        "PROFILE_EXTRACTION": "WAITING",
        "EVIDENCE_VALIDATION": "WAITING",
        "MATCHING": "WAITING",
        "SCORING": "WAITING",
        "HR_REVIEW": "WAITING",
    }


def test_job_first_counts_include_zero_upload_and_archived_jobs():
    jobs = [_job(1, "Java"), _job(2, "Data"), _job(3, "Old", "Archived")]
    cvs = [_cv(1, 1, "COMPLETED"), _cv(1, 2, "RUNNING"),
           _cv(1, 3, "FAILED", "Unreadable"), _cv(2, 4)]
    pulses = job_pulses(jobs, cvs)
    assert [(p.job, p.uploaded, p.progress) for p in pulses] == [
        ("Java", 3, 33), ("Data", 1, 0), ("Old", 0, 0)
    ]
    assert aggregate(pulses) == {
        "Jobs": 3, "Uploaded CVs": 4, "Queued / Running": 2,
        "Text complete": 1, "Failed": 1
    }
    assert pulses[2].state == "No CVs"


def test_stage_breakdown_marks_future_stages_inactive():
    cvs = [_cv(1, 1, "COMPLETED"), _cv(1, 2, "FAILED", "Missing file")]
    stages = stage_breakdown(cvs)
    assert stages[0]["Completed"] == 1
    assert stages[0]["Failed"] == 1
    assert stages[0]["Availability"] == "Active"
    assert stages[1]["Waiting"] == 2
    assert stages[1]["Availability"] == "Not enabled"
    assert all(
        row["Waiting"] == 2 and row["Availability"] == "Waiting for prior stage"
        for row in stages[2:6]
    )
    assert all(
        row["Waiting"] == 2 and row["Availability"] == "Not implemented"
        for row in stages[5:]
    )


def test_document_filter_and_failure_log_show_real_stage():
    cvs = [_cv(1, 1, "COMPLETED"), _cv(1, 2, "FAILED", "Corrupt PDF")]
    assert current_stage(cvs[0]) == ("Text embedding", "Not active yet")
    rows = document_rows(cvs, "candidate_2", "Failed")
    assert len(rows) == 1 and rows[0]["CV ID"] == 2
    assert document_rows(cvs, "candidate_1", "Failed") == []
    assert document_rows(cvs, "candidate_1", "All")[0]["Text duration (s)"] == 1.2
    assert failure_rows(cvs)[0]["Error"] == "Corrupt PDF"


def test_job_scoped_data_never_mix():
    cvs = [_cv(1, 1), _cv(2, 2, "COMPLETED")]
    first = [r for r in cvs if r["Job ID"] == 1]
    second = [r for r in cvs if r["Job ID"] == 2]
    assert job_pulses([_job(1, "Java")], first)[0].uploaded == 1
    assert job_pulses([_job(2, "Data")], second)[0].completed == 1
