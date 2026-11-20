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
    LlamaScoringService,
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
            extraction=LlamaAssessment.model_validate({"findings": findings})
        )
    return mock


def test_llama_evidence_score_persisted_with_source_and_labels(ats, vacancy, pdf):
    doc_id, profile_id = make_data(ats, vacancy, pdf)
    config_id = approve_criteria(ats, vacancy, names=("Java", "Python"))
    with ats.db.transaction() as session:
        rows = list(session.scalars(select(JobCriterion).where(
            JobCriterion.configuration_id == config_id
        ).order_by(JobCriterion.display_order)))
        source_id = session.scalar(select(EvidenceReference.id).where(
            EvidenceReference.profile_id == profile_id
        ))
    llama = LlamaScoringService(ats.db, provider([
        {"criterion_id": rows[0].id, "status": "EVIDENCED",
         "evidence_ids": [source_id], "reason": "Java is directly mentioned."},
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
        source_id = session.scalar(select(EvidenceReference.id))
    llama = LlamaScoringService(ats.db, provider([
        {"criterion_id": criterion_id, "status": "EVIDENCED",
         "evidence_ids": [source_id], "reason": "Direct source mention."},
    ]))
    assert llama.pending_document() == doc_id
    llama.evaluate(doc_id)
    assert llama.pending_document() is None
    with ats.db.transaction() as session:
        assert session.scalar(select(LlamaScoreResult.id)) is not None
        assert session.scalar(select(CandidateScoreSnapshot.id)) is None
