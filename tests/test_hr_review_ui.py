"""Phase 4.1 HR review: read-only projections and existing ATS integration."""
from types import SimpleNamespace

from job_hire_ai.hr_review_ui import (
    criterion_details, evidence_choice_label, evidence_groups,
    evidence_record_label, grouped_evidence_label, review_summary,
)
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


def test_unverified_extraction_is_not_presented_as_verified_cv_evidence():
    evidence = SimpleNamespace(
        field_path="education.0", source_excerpt="exact short source text",
        page_number=None, verification_status="UNVERIFIED",
    )
    label = evidence_choice_label(evidence)
    assert "Unverified" in label
    assert "page unknown" in label
    assert "exact short source text" in label


def test_verified_evidence_label_is_human_readable():
    evidence = SimpleNamespace(
        field_path="technical_skills.1", source_excerpt="Java and PostgreSQL",
        page_number=2, verification_status="VERIFIED",
    )
    label = evidence_choice_label(evidence)
    assert "Verified CV quote" in label
    assert "Technical Skills" in label
    assert "page 2" in label


def test_duplicate_quotations_are_grouped_but_underlying_evidence_is_preserved():
    first = SimpleNamespace(
        id="e1", field_path="technical_skills.0",
        source_excerpt="Java SE/EE technologies", source_reference="pdf:p2:block8",
        page_number=2, verification_status="VERIFIED",
    )
    second = SimpleNamespace(
        id="e2", field_path="technical_skills.1",
        source_excerpt=" Java SE/EE   technologies ",
        source_reference="pdf:p2:block8", page_number=2,
        verification_status="VERIFIED",
    )
    separate = SimpleNamespace(
        id="e3", field_path="technical_skills.2",
        source_excerpt="Java SE/EE technologies",
        source_reference="pdf:p3:block7", page_number=3,
        verification_status="VERIFIED",
    )
    payload = {"technical_skills": [
        {"name": "Java"}, {"name": "Spring Boot"}, {"name": "JVM"},
    ]}
    groups = evidence_groups([first, second, separate], payload)
    assert len(groups) == 2
    duplicates = next(group for group in groups if len(group[0]) == 2)
    assert {e.id for e in duplicates[0]} == {"e1", "e2"}
    assert "Java" in duplicates[1][0]
    assert "Spring Boot" in duplicates[1][1]
    assert "linked qualifications" in grouped_evidence_label(duplicates)


def test_identical_unverified_quote_does_not_merge_with_verified_quote():
    verified = SimpleNamespace(
        id="e1", field_path="education.0", source_excerpt="MSc",
        source_reference="pdf:p1:block2", page_number=1,
        verification_status="VERIFIED",
    )
    unverified = SimpleNamespace(
        id="e2", field_path="education.1", source_excerpt="MSc",
        source_reference="pdf:p1:block2", page_number=1,
        verification_status="UNVERIFIED",
    )
    groups = evidence_groups([verified, unverified], {
        "education": [{"degree": "MSc"}, {"degree": "MSc"}],
    })
    assert len(groups) == 2
    assert "Unverified" in grouped_evidence_label(groups[1])
    assert evidence_record_label(verified, {"education": [{"degree": "MSc"}]}) == (
        "Education: MSc"
    )


def test_hr_evidence_picker_uses_selectable_tables_not_long_dropdowns():
    from inspect import getsource

    from job_hire_ai.hr_review_ui import candidate_review

    source = getsource(candidate_review)
    picker = source.split('groups = evidence_groups(matching, payload)', 1)[1]
    picker = picker.split('with st.expander("Advanced: original structured profile', 1)[0]
    assert 'selection_mode="single-row"' in picker
    assert '"Select a unique source quotation"' not in picker
    assert '"Qualification to review"' not in picker
    assert 'selected_evidence = None' in picker
