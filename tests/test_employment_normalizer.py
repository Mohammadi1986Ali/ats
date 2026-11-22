"""Employment normalization must not turn CV bullets into fake jobs."""
from types import SimpleNamespace

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction
from job_hire_ai.ai.employment_normalizer import normalize_employment


def source(*texts):
    return SimpleNamespace(segments=[
        SimpleNamespace(text=text) for text in texts
    ])


def job(employer, title, *, start=None, end=None, tasks=None, evidence=None):
    return {
        "employer": employer, "job_title": title,
        "start_date": start, "end_date": end, "currently_employed": None,
        "employment_type": None,
        "responsibilities": tasks or [],
        "technologies": [], "evidence": evidence or [{
            "source_excerpt": title, "source_reference": "pdf:p1:block1",
        }],
        "claim_status": "SUPPORTED", "uncertainty": "",
    }


def profile(jobs):
    data = {section: [] for section in PROFILE_SECTIONS}
    data["employment_history"] = jobs
    data["extraction_warnings"] = []
    return CVExtraction.model_validate(data)


def test_twenty_duplicate_role_mentions_are_not_twenty_jobs():
    original = source(
        "EXPERIENCE\nblu Bank\nSenior Software Engineer\nFeb 2025 – Jan 2026",
        "Developed payment services and maintained high availability.",
    )
    jobs = [
        job("blu Bank", "Senior Software Engineer",
            start="Feb 2025", end="Jan 2026", tasks=[f"Task {i}"])
        for i in range(20)
    ]
    fixed = normalize_employment(profile(jobs), original)
    assert len(fixed.employment_history) == 1
    assert len(fixed.employment_history[0].responsibilities) == 20
    assert "combined 19 duplicate" in fixed.extraction_warnings[0]


def test_distinct_employers_and_roles_remain_separate():
    original = source(
        "blu Bank\nSenior Software Engineer\n2025–2026",
        "Specific Group\nSenior Backend Engineer\n2023–2025",
        "Middle East Bank\nSenior Java Developer\n2020–2023",
    )
    jobs = [
        job("blu Bank", "Senior Software Engineer", start="2025"),
        job("Specific Group", "Senior Backend Engineer", start="2023"),
        job("Middle East Bank", "Senior Java Developer", start="2020"),
    ]
    fixed = normalize_employment(profile(jobs), original)
    assert [(r.employer, r.job_title) for r in fixed.employment_history] == [
        ("blu Bank", "Senior Software Engineer"),
        ("Specific Group", "Senior Backend Engineer"),
        ("Middle East Bank", "Senior Java Developer"),
    ]


def test_invented_employer_or_title_does_not_create_work_history():
    original = source(
        "Java and PostgreSQL. Developed payment services.",
        "EXPERIENCE\nblu Bank\nSenior Software Engineer",
    )
    jobs = [
        job("blu Bank", "Senior Software Engineer"),
        job("invented company", "Senior Engineer"),
        job("blu Bank", "Designed and implemented database improvements."),
        job(None, "Senior Software Engineer"),
        job("blu Bank", None),
    ]
    fixed = normalize_employment(profile(jobs), original)
    assert len(fixed.employment_history) == 1
    assert "excluded 4" in fixed.extraction_warnings[0]


def test_distinct_date_ranges_of_same_role_not_silently_merged():
    original = source(
        "blu Bank\nSenior Engineer\n2018 – 2019\nSenior Engineer\n2024 – 2025",
    )
    fixed = normalize_employment(profile([
        job("blu Bank", "Senior Engineer", start="2018", end="2019"),
        job("blu Bank", "Senior Engineer", start="2024", end="2025"),
    ]), original)
    assert len(fixed.employment_history) == 2


def test_partial_job_rows_do_not_hallucinate_employer_or_dates():
    original = source("Senior Backend Engineer. Built reliable systems.")
    fixed = normalize_employment(
        profile([job(None, "Senior Backend Engineer", tasks=["Built reliable systems"])]),
        original,
    )
    assert fixed.employment_history == []
    assert "original CV" in fixed.extraction_warnings[0]
