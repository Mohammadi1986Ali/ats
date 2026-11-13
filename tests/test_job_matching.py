"""Phase 3.6: deterministic, evidence-grounded matching; no AI-generated verdicts."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, EvidenceQuote
from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    CandidateScoreSnapshot,
    CriteriaConfiguration,
    CVAnalysisBatch,
    CVAnalysisJob,
    CVAnalysisRun,
    DocumentPipelineStage,
    DocumentTextExtraction,
    EvidenceReference,
    JobApplication,
    JobCriterion,
    JobMatchResult,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.candidate_scoring_service import CandidateScoringService
from job_hire_ai.services.criteria_service import description_hash
from job_hire_ai.services.evidence_service import verify_quote
from job_hire_ai.services.job_matching_service import JobMatchingService
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.text_extraction import ExtractedText


def make_data(ats, vacancy, pdf, *, supported=True):
    assert ats.documents.upload_batch(vacancy.id, [("matching.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        document = session.scalar(
            select(CandidateDocument).where(CandidateDocument.vacancy_id == vacancy.id)
        )
        original = session.scalar(
            select(DocumentTextExtraction).where(
                DocumentTextExtraction.document_id == document.id
            )
        )
        source = ExtractedText.deserialize(original.source)
        quote = EvidenceQuote(
            source_excerpt="Java and SQL experience.",
            source_reference=source.segments[0].reference,
        )
        payload = {section: [] for section in PROFILE_SECTIONS}
        payload["technical_skills"] = [{
            "name": "Java", "original_wording": "Java", "category": "programming",
            "evidence": [quote.model_dump(mode="json")],
            "claim_status": "SUPPORTED" if supported else "UNCERTAIN",
            "uncertainty": "" if supported else "Unclear from document",
        }]
        payload["extraction_warnings"] = []
        batch = CVAnalysisBatch(vacancy_id=vacancy.id)
        session.add(batch)
        session.flush()
        job = CVAnalysisJob(
            batch_id=batch.id, document_id=document.id, status="COMPLETED"
        )
        session.add(job)
        session.flush()
        run = CVAnalysisRun(
            job_id=job.id, document_id=document.id,
            document_checksum=document.checksum,
            model_name="test-only", prompt_version="test-only",
            schema_version="candidate-profile-v1", config_hash="test-only",
            inference_options={}, status="COMPLETED",
        )
        session.add(run)
        session.flush()
        profile = CandidateProfile(
            candidate_id=document.application.candidate_id, document_id=document.id,
            text_extraction_id=original.id, analysis_run_id=run.id,
            extraction_version=1, config_hash="test-only",
            analysis_status="NEEDS_REVIEW" if not supported else "COMPLETED",
            original_extraction=payload, warnings=[],
        )
        session.add(profile)
        session.flush()
        evidence = verify_quote(quote, source, document.id).serialize()
        evidence["id"] = evidence.pop("evidence_id")
        session.add(EvidenceReference(
            profile_id=profile.id, field_path="technical_skills.0", **evidence
        ))
        return document.id, profile.id


def approve_criteria(ats, vacancy, *, names=("Java", "Python")):
    with ats.db.transaction() as session:
        configuration = CriteriaConfiguration(
            vacancy_id=vacancy.id, version=1, status="DRAFT",
            job_description_hash=description_hash(vacancy.description),
            job_description_snapshot=vacancy.description,
            revision=1,
        )
        session.add(configuration)
        session.flush()
        for number, name in enumerate(names, 1):
            session.add(JobCriterion(
                configuration_id=configuration.id,
                criterion_key=f"criterion-{number}", name=name,
                category="skill", description=f"Experience in {name}",
                requirement_type="REQUIRED" if number == 1 else "PREFERRED",
                source_excerpt=vacancy.description,
                extraction_notes="", uncertainty="",
                evidence_status="VERIFIED", evidence_acknowledged=False,
                acknowledgement_note="", weight=100 if len(names) == 1 else 50,
                display_order=number, created_by="MANUAL",
            ))
        session.flush()
        # Approve only after saving criteria, honoring DB immutability triggers.
        configuration.approved_at = datetime.now(UTC)
        configuration.status = "APPROVED"
        return configuration.id


def test_matching_returns_real_quote_without_ranking(ats, vacancy, pdf):
    document_id, profile_id = make_data(ats, vacancy, pdf)
    configuration_id = approve_criteria(ats, vacancy)
    matching = JobMatchingService(ats.db)
    result = matching.match_document(document_id)
    assert result.profile_id == profile_id
    assert result.configuration_id == configuration_id
    assert len(result.criteria_results) == 2
    first, second = result.criteria_results
    assert first["status"] == "EVIDENCED"
    assert first["evidence"][0]["source_excerpt"] == "Java and SQL experience."
    assert first["evidence"][0]["document_id"] == document_id
    assert second["status"] == "NOT_EVIDENCED"
    assert second["evidence"] == []
    assert all("score" not in row and "rank" not in row for row in result.criteria_results)
    assert matching.match_document(document_id).id == result.id
    assert len(matching.list_for_vacancy(vacancy.id)) == 1


def test_uncertain_claim_must_not_become_evidence(ats, vacancy, pdf):
    document_id, _ = make_data(ats, vacancy, pdf, supported=False)
    approve_criteria(ats, vacancy, names=("Java",))
    result = JobMatchingService(ats.db).match_document(document_id)
    assert result.criteria_results[0]["status"] == "NOT_EVIDENCED"


def test_unapproved_job_is_not_matched(ats, vacancy, pdf):
    document_id, _ = make_data(ats, vacancy, pdf)
    matcher = JobMatchingService(ats.db)
    assert not matcher.ready(vacancy.id)
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "EVIDENCE_VALIDATION",
            )
        )
        stage.status = "COMPLETED"
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        matching_enabled=True, matcher=matcher,
    )
    assert pipeline.process_matching_next() is False
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "MATCHING",
            )
        )
        assert stage.status == "WAITING"
    approve_criteria(ats, vacancy, names=("Java",))
    assert pipeline.process_matching_next()
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "MATCHING",
            )
        )
        assert stage.status == "COMPLETED"
        assert session.scalar(select(JobMatchResult).where(
            JobMatchResult.document_id == document_id
        )) is not None


def test_removed_application_has_no_matching_result(ats, vacancy, pdf):
    document_id, _ = make_data(ats, vacancy, pdf)
    approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        document = session.get(CandidateDocument, document_id)
        session.get(JobApplication, document.application_id).status = "Removed"
    matching = JobMatchingService(ats.db)
    assert matching.list_for_vacancy(vacancy.id) == []
    with pytest.raises(ATSError, match="Removed applications"):
        matching.match_document(document_id)


def test_scoring_is_weighted_evidence_coverage_not_suitability(ats, vacancy, pdf):
    document_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java", "Python"))
    match = JobMatchingService(ats.db).match_document(document_id)
    score = CandidateScoringService(ats.db).score_document(document_id)
    assert score.match_result_id == match.id
    assert score.profile_id == profile_id
    assert score.configuration_id == config_id
    assert (score.documented_weight, score.unverified_weight, score.review_weight) == (
        50, 50, 0
    )
    java, python = score.criterion_breakdown
    assert java["documented_points"] == 50
    assert java["evidence"][0]["source_excerpt"] == "Java and SQL experience."
    assert python["documented_points"] == 0
    assert python["evidence_status"] == "NOT_EVIDENCED"
    assert CandidateScoringService(ats.db).score_document(document_id).id == score.id
    assert len(CandidateScoringService(ats.db).list_for_vacancy(vacancy.id)) == 1


def test_scoring_refuses_missing_or_corrupt_matching_snapshot(ats, vacancy, pdf):
    document_id, _ = make_data(ats, vacancy, pdf)
    approve_criteria(ats, vacancy, names=("Java",))
    scoring = CandidateScoringService(ats.db)
    with pytest.raises(ATSError, match="Matching"):
        scoring.score_document(document_id)
    match = JobMatchingService(ats.db).match_document(document_id)
    with ats.db.transaction() as session:
        persisted = session.get(JobMatchResult, match.id)
        persisted.criteria_results = [{
            **persisted.criteria_results[0],
            "status": "EVIDENCED",
            "evidence": [],
        }]
    with pytest.raises(ATSError, match="lacks source references"):
        scoring.score_document(document_id)
    with ats.db.transaction() as session:
        assert session.scalar(
            select(CandidateScoreSnapshot).where(
                CandidateScoreSnapshot.document_id == document_id
            )
        ) is None


def test_scoring_pipeline_requires_completed_matching(ats, vacancy, pdf):
    document_id, _ = make_data(ats, vacancy, pdf)
    approve_criteria(ats, vacancy, names=("Java",))
    JobMatchingService(ats.db).match_document(document_id)
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir, scoring_enabled=True,
    )
    assert not pipeline.process_scoring_next()
    with ats.db.transaction() as session:
        matching = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "MATCHING",
            )
        )
        matching.status = "COMPLETED"
    assert pipeline.process_scoring_next()
    with ats.db.transaction() as session:
        stage = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "SCORING",
            )
        )
        assert stage.status == "COMPLETED"
        assert stage.attempts == 1
        assert session.scalar(select(CandidateScoreSnapshot.id)) is not None
    assert not pipeline.process_scoring_next()


def test_uncertain_claim_is_not_documented_weight(ats, vacancy, pdf):
    document_id, _ = make_data(ats, vacancy, pdf, supported=False)
    approve_criteria(ats, vacancy, names=("Java",))
    JobMatchingService(ats.db).match_document(document_id)
    score = CandidateScoringService(ats.db).score_document(document_id)
    assert score.documented_weight == 0
    assert score.unverified_weight == 100
    assert score.review_weight == 0
