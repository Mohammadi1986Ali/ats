"""Automatic JD/CV semantic retrieval from real stored vectors, without LLM calls."""

from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateDocument,
    DocumentPipelineStage,
    SemanticMatchResult,
)
from job_hire_ai.services.embedding_service import (
    DIMENSIONS,
    EmbeddingService,
    chunk_jd,
    source_hash,
)
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.services.semantic_matching_service import SemanticMatchingService


class FakeEmbedder:
    model = "nomic-embed-text"

    def embed(self, texts):
        vectors = []
        for content in texts:
            vec = [0.0] * DIMENSIONS
            if "Java" in content:
                vec[0] = 1.0
            elif "Python" in content:
                vec[1] = 1.0
            else:
                vec[2] = 1.0
            vectors.append(vec)
        return vectors


def index_test_sources(ats, vacancy, pdf, *, cv_word="Java"):
    from job_hire_ai.database.models import DocumentTextExtraction
    from job_hire_ai.text_extraction import ExtractedText, chunk_document
    from job_hire_ai.services.embedding_service import chunk_cv

    assert ats.documents.upload_batch(vacancy.id, [("candidate.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        doc = session.scalar(
            select(CandidateDocument).where(CandidateDocument.vacancy_id == vacancy.id)
            .order_by(CandidateDocument.id.desc())
        )
        saved = session.scalar(
            select(DocumentTextExtraction)
            .where(DocumentTextExtraction.document_id == doc.id)
        )
        extracted = ExtractedText.deserialize(saved.source)
        fingerprint = saved.checksum + ":" + saved.extractor_version
        document_id = doc.id
        stages = {
            stage.stage: stage
            for stage in session.scalars(
                select(DocumentPipelineStage)
                .where(DocumentPipelineStage.document_id == document_id)
            )
        }
        stages["EMBEDDING"].status = "COMPLETED"
        stages["PROFILE_EXTRACTION"].status = "COMPLETED"
        stages["EVIDENCE_VALIDATION"].status = "COMPLETED"

    embed = EmbeddingService(ats.db, FakeEmbedder())
    embed.index(
        source_type="JD", vacancy_id=vacancy.id, document_id=None,
        source=vacancy.description, chunks=chunk_jd(vacancy.description),
    )
    embed.index(
        source_type="CV", vacancy_id=vacancy.id, document_id=document_id,
        source=fingerprint, chunks=chunk_cv(extracted),
    )
    return document_id


def test_matching_works_without_approved_criteria(ats, vacancy, pdf):
    document_id = index_test_sources(ats, vacancy, pdf)
    semantic = SemanticMatchingService(ats.db)
    assert semantic.ready(vacancy.id)
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        matching_enabled=True, semantic_matcher=semantic,
    )
    assert pipeline.process_matching_next()
    with ats.db.transaction() as session:
        matching = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "MATCHING",
            )
        )
        assert matching.status == "COMPLETED"
    result = semantic.current_result(document_id)
    assert result is not None
    assert len(result.comparisons) == 1
    assert result.comparisons[0]["jd_excerpt"] == vacancy.description
    assert "Java" in result.comparisons[0]["cv_passages"][0]["source_excerpt"]
    assert "source_reference" in result.comparisons[0]["cv_passages"][0]
    assert "cosine_similarity" in result.comparisons[0]["cv_passages"][0]
    assert "candidate_score" not in result.comparisons[0]
    assert semantic.match_document(document_id).id == result.id
    assert not pipeline.process_matching_next()


def test_semantic_matching_fails_closed_without_saved_vectors(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("no-vectors.pdf", pdf)])[0].success
    assert ats.pipeline.process_next()
    with ats.db.transaction() as session:
        doc = session.scalar(select(CandidateDocument))
        doc_id = doc.id
        stages = {
            row.stage: row for row in session.scalars(
                select(DocumentPipelineStage).where(
                    DocumentPipelineStage.document_id == doc_id
                )
            )
        }
        stages["EVIDENCE_VALIDATION"].status = "COMPLETED"
    semantic = SemanticMatchingService(ats.db)
    assert not semantic.ready(vacancy.id)
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        matching_enabled=True, semantic_matcher=semantic,
    )
    assert not pipeline.process_matching_next()
    assert semantic.current_result(doc_id) is None


def test_changing_jd_invalidates_semantic_snapshot(ats, vacancy, pdf):
    document_id = index_test_sources(ats, vacancy, pdf)
    semantic = SemanticMatchingService(ats.db)
    first = semantic.match_document(document_id)
    with ats.db.transaction() as session:
        from job_hire_ai.database.models import Vacancy

        session.get(Vacancy, vacancy.id).description = "Python services"
    assert semantic.current_result(document_id) is None
    # A newer JD must have its *own* embeddings before the worker can re-match.
    assert not semantic.ready(vacancy.id)
    EmbeddingService(ats.db, FakeEmbedder()).index(
        source_type="JD", vacancy_id=vacancy.id, document_id=None,
        source="Python services", chunks=chunk_jd("Python services"),
    )
    second = semantic.match_document(document_id)
    assert second.id != first.id
    assert second.jd_source_hash == source_hash("Python services")
    assert semantic.current_result(document_id).id == second.id


def test_semantic_results_are_scoped_to_correct_vacancy(ats, vacancy, pdf):
    document_id = index_test_sources(ats, vacancy, pdf)
    semantic = SemanticMatchingService(ats.db)
    first = semantic.match_document(document_id)
    with ats.db.transaction() as session:
        assert session.scalar(select(SemanticMatchResult.id)) == first.id
    assert [row.id for row in semantic.list_for_vacancy(vacancy.id)] == [first.id]


def test_semantic_only_does_not_create_hr_score(ats, vacancy, pdf):
    document_id = index_test_sources(ats, vacancy, pdf)
    pipeline = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        matching_enabled=True, scoring_enabled=True,
    )
    assert pipeline.process_matching_next()
    assert not pipeline.process_scoring_next()
    with ats.db.transaction() as session:
        from job_hire_ai.database.models import CandidateScoreSnapshot

        assert session.scalar(select(CandidateScoreSnapshot.id)) is None
        score = session.scalar(
            select(DocumentPipelineStage).where(
                DocumentPipelineStage.document_id == document_id,
                DocumentPipelineStage.stage == "SCORING",
            )
        )
        assert score.status == "WAITING"
