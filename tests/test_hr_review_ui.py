"""Phase 4.1 HR review: read-only projections and existing ATS integration."""
from types import SimpleNamespace

from job_hire_ai.hr_review_ui import criterion_details, review_summary
from tests.test_job_matching import approve_criteria, make_data


def test_review_summary_does_not_invent_score_or_decision():
    docs = [{
        "CV": 8, "Application": 3, "Filename": "candidate.pdf",
        "SCORING": "COMPLETED",
    }]
    result = review_summary(docs, {}, {}, 75, 45)
    assert result[0]["Evidence score"] is None
    assert result[0]["Classification"] == "Not scored"
    assert result[0]["Llama status"] == "Not evaluated"
    assert not any("Decision" in key for key in result[0])


def test_review_summary_keeps_failed_llama_distinct_from_rule_score():
    docs = [{
        "CV": 8, "Application": 3, "Filename": "candidate.pdf",
        "SCORING": "COMPLETED",
    }]
    outcome = SimpleNamespace(status="FAILED", score=None, review_weight=None)
    rule = SimpleNamespace(documented_weight=42, review_weight=58)
    row = review_summary(docs, {8: outcome}, {8: rule}, 75, 45)[0]
    assert row["Evidence score"] == 42
    assert row["Classification"] == "Needs review"
    assert row["Scoring source"] == "Literal evidence"
    assert row["Llama status"] == "FAILED"


def test_review_summary_uses_completed_llama_score():
    docs = [{
        "CV": 9, "Application": 4, "Filename": "applicant.docx",
        "SCORING": "COMPLETED",
    }]
    outcome = SimpleNamespace(status="COMPLETED", score=86, review_weight=14)
    row = review_summary(docs, {9: outcome}, {}, 75, 45)[0]
    assert row["Evidence score"] == 86
    assert row["Classification"] == "Good"
    assert row["Scoring source"] == "Llama + evidence"


def test_criterion_details_keep_citations_and_missing_evidence_visible():
    rows = criterion_details([
        {
            "criterion": "Java", "weight": 15, "status": "EVIDENCED",
            "reason": "Direct quotation",
            "evidence": [{
                "source_reference": "pdf:p1:block5",
                "page_number": 1, "source_excerpt": "Java",
            }],
        },
        {
            "name": "Redis", "status": "REVIEW_REQUIRED", "evidence": [],
        },
    ])
    assert rows[0]["Criterion"] == "Java"
    assert rows[0]["Verified excerpt"] == "Java"
    assert rows[0]["Source"] == "pdf:p1:block5"
    assert rows[1]["Criterion"] == "Redis"
    assert rows[1]["Verified excerpt"] is None


def test_review_reads_real_uploaded_document_without_writing(ats, vacancy, pdf):
    doc_id, _ = make_data(ats, vacancy, pdf)
    approve_criteria(ats, vacancy, names=("Java",))
    _, docs = ats.pipeline.overview(vacancy.id)
    assert any(row["CV"] == doc_id for row in docs)
    filename, original, kind = ats.documents.download(doc_id)
    assert filename == "matching.pdf"
    assert original == pdf
    assert kind == "pdf"
    profile = ats.profiles.list(document_id=doc_id)
    assert profile and profile[0].document_id == doc_id
