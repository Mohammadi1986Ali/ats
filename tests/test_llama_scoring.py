"""End-to-end Llama scoring tests; no Ollama service needed."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from job_hire_ai.ai.provider import ProviderError
from job_hire_ai.database.models import (
    CandidateScoreSnapshot,
    DocumentPipelineStage,
    EvidenceReference,
    JobCriterion,
    LlamaScoreResult,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.llama_scoring_service import (
    LlamaAssessment,
    LlamaCriterionFinding,
    LlamaScoringService,
    _relevant_evidence,
    score_band,
)
from tests.test_job_matching import approve_criteria, make_data


def provider(findings=None, fail=None):
    mock = Mock()
    mock.settings.model = "llama3.2:1b"
    if fail:
        mock.generate_structured.side_effect = ProviderError(fail)
    else:
        mock.generate_structured.return_value = SimpleNamespace(
            extraction=LlamaAssessment.model_validate({"findings": findings or []})
        )
    return mock


def test_llama_evidence_score_persisted_with_source_and_labels(ats, vacancy, pdf):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java", "Python"))
    with ats.db.transaction() as session:
        rows = list(session.scalars(select(JobCriterion).where(
            JobCriterion.configuration_id == config_id
        ).order_by(JobCriterion.display_order)))
        assert session.scalar(select(EvidenceReference.id).where(
            EvidenceReference.profile_id == profile_id
        ))
    llama = LlamaScoringService(ats.db, provider([
        {"criterion_id": rows[0].id, "status": "EVIDENCED",
         "evidence_ids": ["e1"], "reason": "Java is directly mentioned."},
        {"criterion_id": rows[1].id, "status": "NOT_EVIDENCED",
         "evidence_ids": [], "reason": "No verified Python evidence provided."},
    ]))
    result = llama.evaluate(doc_id)
    assert result.status == "COMPLETED"
    assert result.score == 50
    assert result.review_weight == 0
    assert result.breakdown[0]["evidence"][0]["source_excerpt"] == (
        "Java and SQL experience."
    )
    assert llama.current(doc_id).id == result.id
    assert score_band(result.score, 75, 45) == "Medium"
    assert score_band(result.score, 60, 55) == "Weak"
    assert score_band(result.score, 50, 45) == "Good"
    assert ats.vacancies.list()[0].good_threshold == 75
    ats.vacancies.set_score_bands(vacancy.id, 60, 40)
    assert ats.vacancies.list()[0].good_threshold == 60
    with pytest.raises(ATSError):
        ats.vacancies.set_score_bands(vacancy.id, 30, 60)


def test_llama_rejects_fabricated_sources_without_fake_score(ats, vacancy, pdf):
    doc_id, _ = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        criterion_id = session.scalar(select(JobCriterion.id).where(
            JobCriterion.configuration_id == config_id
        ))
    llama = LlamaScoringService(ats.db, provider([
        {"criterion_id": criterion_id, "status": "EVIDENCED",
         "evidence_ids": ["invented-source"], "reason": "Made up."},
    ]))
    with pytest.raises(ATSError, match="Llama scoring failed"):
        llama.evaluate(doc_id)
    stored = llama.current(doc_id)
    assert stored.status == "FAILED"
    assert stored.score is None
    assert "invented" in stored.error_summary
    assert llama.retry(doc_id) is None
    assert llama.current(doc_id) is None


def test_llama_failure_visible_no_fallback_score(ats, vacancy, pdf):
    doc_id, _ = make_data(ats, vacancy, pdf)
    approve_criteria(ats, vacancy, names=("Java",))
    llama = LlamaScoringService(ats.db, provider(fail="Model unavailable"))
    with pytest.raises(ATSError):
        llama.evaluate(doc_id)
    result = llama.current(doc_id)
    assert result.status == "FAILED"
    assert result.score is None
    assert "Model unavailable" in result.error_summary


def test_background_queue_uses_completed_pipeline_and_skips_finished(ats, vacancy, pdf):
    doc_id, _ = make_data(ats, vacancy, pdf)
    approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        session.scalar(select(DocumentPipelineStage).where(
            DocumentPipelineStage.document_id == doc_id,
            DocumentPipelineStage.stage == "SCORING",
        )).status = "COMPLETED"
    with ats.db.transaction() as session:
        criterion_id = session.scalar(select(JobCriterion.id))
        assert session.scalar(select(EvidenceReference.id))
    llama = LlamaScoringService(ats.db, provider([
        {"criterion_id": criterion_id, "status": "EVIDENCED",
         "evidence_ids": ["e1"], "reason": "Direct source mention."},
    ]))
    assert llama.pending_document() == doc_id
    llama.evaluate(doc_id)
    assert llama.pending_document() is None
    with ats.db.transaction() as session:
        assert session.scalar(select(LlamaScoreResult.id)) is not None
        assert session.scalar(select(CandidateScoreSnapshot.id)) is None


def test_interrupted_llama_score_is_recoverable(ats, vacancy, pdf):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        session.add(LlamaScoreResult(
            document_id=doc_id, profile_id=profile_id,
            configuration_id=config_id, model_name="llama3.2:1b",
            status="RUNNING", score=None, review_weight=None,
            breakdown=None, error_summary=None,
        ))
    llama = LlamaScoringService(ats.db, provider(fail="Connection unavailable"))
    assert llama.recover_interrupted() == 1
    stored = llama.current(doc_id)
    assert stored.status == "FAILED"
    assert "interrupted" in stored.error_summary.lower()
    llama.retry(doc_id)
    assert llama.current(doc_id) is None


def test_large_verified_cv_no_longer_rejected_by_fixed_limit(ats, vacancy, pdf):
    """Regression: 46 verified excerpts must not trigger an immediate failed score."""
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        first = session.scalar(select(EvidenceReference).where(
            EvidenceReference.profile_id == profile_id
        ))
        # Evidence references are real DB records, not fabricated model results.
        for index in range(46):
            session.add(EvidenceReference(
                profile_id=profile_id,
                document_id=doc_id,
                id=f"large-cv-{index}",
                field_path=f"skills.{index + 1}",
                source_excerpt=f"Unrelated technology number {index}",
                source_reference=first.source_reference,
                page_number=first.page_number,
                character_start=first.character_start,
                character_end=first.character_end,
                verification_status="VERIFIED",
            ))
    with ats.db.transaction() as session:
        criterion_id = session.scalar(select(JobCriterion.id).where(
            JobCriterion.configuration_id == config_id
        ))
    fake = provider([
        {"criterion_id": criterion_id, "status": "EVIDENCED",
         "evidence_ids": ["e1"], "reason": "Verified Java excerpt."},
    ])
    llama = LlamaScoringService(ats.db, fake)
    result = llama.evaluate(doc_id)
    assert result.status == "COMPLETED"
    assert result.score == 100
    assert fake.generate_structured.call_count == 1
    request = fake.generate_structured.call_args.args[0][1]["content"]
    assert len(request) < 6000


def test_recover_prior_context_failures_without_retrying_other_errors(ats, vacancy, pdf):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        session.add(LlamaScoreResult(
            document_id=doc_id, profile_id=profile_id,
            configuration_id=config_id, model_name="llama3.2:1b",
            status="FAILED", score=None, review_weight=None, breakdown=None,
            error_summary="Llama context limit: at most 25 JD criteria",
        ))
    llama = LlamaScoringService(ats.db, provider(fail="Ollama unavailable"))
    assert llama.recover_outdated_failures() == 1
    assert llama.current(doc_id) is None
    assert llama.recover_outdated_failures() == 0


def test_relevance_selection_does_not_ignore_uninspected_excerpts():
    evidence = [
        SimpleNamespace(id=n, source_excerpt=f"Other subject {n}")
        for n in range(60)
    ]
    evidence[45] = SimpleNamespace(id=45, source_excerpt="Extensive Java experience")
    chosen, omitted = _relevant_evidence(
        SimpleNamespace(name="Java"), evidence, limit=12
    )
    assert len(chosen) == 12
    assert omitted is True
    assert 45 in [row.id for row in chosen]


def test_small_llama_falls_back_to_one_criterion_on_incomplete_batch(
    ats, vacancy, pdf
):
    doc_id, _ = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java", "Python"))
    with ats.db.transaction() as session:
        criteria = list(session.scalars(select(JobCriterion).where(
            JobCriterion.configuration_id == config_id
        ).order_by(JobCriterion.display_order)))
    model = provider()
    model.generate_structured.side_effect = [
        SimpleNamespace(extraction=LlamaAssessment(findings=[])),
        SimpleNamespace(extraction=LlamaCriterionFinding(
            criterion_id=criteria[0].id, status="EVIDENCED",
            evidence_ids=["e1"], reason="Directly supported Java evidence.",
        )),
        SimpleNamespace(extraction=LlamaCriterionFinding(
            criterion_id=criteria[1].id, status="REVIEW_REQUIRED",
            evidence_ids=[], reason="No sufficient verified Python evidence.",
        )),
    ]
    result = LlamaScoringService(ats.db, model).evaluate(doc_id)
    assert result.status == "COMPLETED"
    assert result.score == 50
    assert result.review_weight == 50
    assert model.generate_structured.call_count == 3
    assert model.generate_structured.call_args.args[1] is LlamaCriterionFinding


def test_small_llama_timeout_falls_back_to_short_individual_prompts(
    ats, vacancy, pdf
):
    doc_id, _ = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        criterion_id = session.scalar(select(JobCriterion.id).where(
            JobCriterion.configuration_id == config_id
        ))
    model = provider()
    model.generate_structured.side_effect = [
        ProviderError("Ollama timed out or disconnected."),
        SimpleNamespace(extraction=LlamaCriterionFinding(
            criterion_id=criterion_id, status="EVIDENCED",
            evidence_ids=["e1"], reason="Source states Java.",
        )),
    ]
    result = LlamaScoringService(ats.db, model).evaluate(doc_id)
    assert result.status == "COMPLETED"
    assert result.score == 100
    assert model.generate_structured.call_count == 2


def test_recover_old_timeout_and_incomplete_scoring_failures(
    ats, vacancy, pdf
):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java",))
    with ats.db.transaction() as session:
        session.add(LlamaScoreResult(
            document_id=doc_id, profile_id=profile_id,
            configuration_id=config_id, model_name="llama3.2:1b",
            status="FAILED", score=None, review_weight=None,
            breakdown=None, error_summary="Llama returned incomplete or duplicate JD criteria.",
        ))
    llama = LlamaScoringService(ats.db, provider(fail="unavailable"))
    assert llama.recover_outdated_failures() == 1
    assert llama.current(doc_id) is None
