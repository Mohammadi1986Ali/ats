"""Regression: v3 recovers explicit CV chronology, then refreshes pipeline."""

from sqlalchemy import select

from job_hire_ai.ai.literal_cv_fallback import extract_literal_skills
from job_hire_ai.database.models import CandidateDocument, DocumentPipelineStage
from job_hire_ai.services.evidence_service import verify_quote
from job_hire_ai.text_extraction import ExtractedText, TextSegment
from job_hire_ai.workers.automatic_profile_recovery import (
    FAST_PROFILE_RECOVERY_VERSION,
    upgrade_old_fast_profiles_once,
)


def test_employment_formats_are_source_backed_not_inferred():
    source = [
        {"reference": "pdf:p1:block1", "text": "PROFESSIONAL EXPERIENCE"},
        {"reference": "pdf:p1:block2", "text": (
            "Senior Software Engineer\nblu Bank\nJan 2020 – Present"
        )},
        {"reference": "pdf:p1:block3", "text": "Backend Developer | MEBank | Feb 2016 - Dec 2019"},
        {"reference": "pdf:p2:block1", "text": "ACADEMIC BACKGROUND"},
        {"reference": "pdf:p2:block2", "text": (
            "Master of Science: Data Driven Business\n"
            "The Hague University of Applied Sciences"
        )},
    ]
    result = extract_literal_skills(source)
    assert [(r.employer, r.job_title) for r in result.employment_history] == [
        ("blu Bank", "Senior Software Engineer"),
        ("MEBank", "Backend Developer"),
    ]
    assert result.employment_history[0].currently_employed
    assert result.education[0].field_of_study == "Data Driven Business"
    assert all(r.claim_status == "UNCERTAIN" for r in result.employment_history)
    extracted = ExtractedText([
        TextSegment(s["reference"], s["text"], s["text"], 1, "paragraph")
        for s in source
    ], [])
    for record in result.employment_history + result.education:
        for evidence in record.evidence:
            assert verify_quote(evidence, extracted, 1).verification_status == "VERIFIED"


def test_no_fabricated_employer_from_prose():
    result = extract_literal_skills([
        {"reference": "pdf:p1:block1", "text": "EXPERIENCE\nBuilt Java services for banks."},
        {"reference": "pdf:p2:block1", "text": "EDUCATION\nInterested in cloud engineering."},
    ])
    assert not result.employment_history
    assert not result.education


def test_previous_partial_profile_requeues_all_dependent_stages(ats, vacancy, pdf, tmp_path):
    assert ats.documents.upload_batch(vacancy.id, [("old.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        doc_id = session.scalar(select(CandidateDocument.id))
        rows = {
            row.stage: row for row in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == doc_id
                )
            )
        }
        for name in (
            "EMBEDDING", "PROFILE_EXTRACTION", "EVIDENCE_VALIDATION",
            "MATCHING", "SCORING",
        ):
            rows[name].status = "COMPLETED"
    from job_hire_ai.database.models import (
        CandidateProfile,
        CVAnalysisBatch,
        CVAnalysisJob,
        CVAnalysisRun,
        DocumentTextExtraction,
    )
    with ats.db.transaction() as session:
        extraction = session.scalar(select(DocumentTextExtraction))
        document = session.get(CandidateDocument, doc_id)
        batch = CVAnalysisBatch(vacancy_id=vacancy.id)
        session.add(batch)
        session.flush()
        job = CVAnalysisJob(batch_id=batch.id, document_id=doc_id, status="NEEDS_REVIEW")
        session.add(job)
        session.flush()
        run = CVAnalysisRun(
            job_id=job.id, document_id=doc_id, document_checksum=document.checksum,
            model_name="test", prompt_version="old", schema_version="candidate-profile-v1",
            config_hash="old", inference_options={}, status="COMPLETED",
        )
        session.add(run)
        session.flush()
        session.add(CandidateProfile(
            candidate_id=document.application.candidate_id, document_id=doc_id,
            text_extraction_id=extraction.id, analysis_run_id=run.id,
            extraction_version=1, config_hash="old", analysis_status="NEEDS_REVIEW",
            original_extraction={}, warnings=[
                "PARTIAL DETERMINISTIC EXTRACTION (FAST 1B MODE): old"
            ],
        ))
    path = tmp_path / "recovery"
    assert upgrade_old_fast_profiles_once(ats.db, path) == {
        doc_id: "Requeued partial profile"
    }
    with ats.db.transaction() as session:
        rows = {
            row.stage: row for row in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == doc_id
                )
            )
        }
        assert rows["PROFILE_EXTRACTION"].status == "PENDING"
        assert rows["EMBEDDING"].status == "COMPLETED"
        assert all(rows[n].status == "WAITING" for n in (
            "EVIDENCE_VALIDATION", "MATCHING", "SCORING"
        ))
    assert (path / f".{FAST_PROFILE_RECOVERY_VERSION}.done").exists()
    assert upgrade_old_fast_profiles_once(ats.db, path) == {}
