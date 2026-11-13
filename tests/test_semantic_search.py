"""Offline retrieval tests: real source chunks, no fictional AI responses."""

import pytest
from sqlalchemy import select

from job_hire_ai.database.models import CandidateDocument, JobApplication
from job_hire_ai.schemas import VacancyInput
from job_hire_ai.services.embedding_service import Chunk, DIMENSIONS, EmbeddingService
from job_hire_ai.services.semantic_search_service import SemanticSearchService


class StubEmbedder:
    model = "nomic-embed-text"

    def embed(self, texts):
        def vector(value):
            low = value.lower()
            if "java" in low or "concurren" in low:
                return [1.0, 0.0] + [0.0] * (DIMENSIONS - 2)
            if "python" in low or "machine learning" in low:
                return [0.0, 1.0] + [0.0] * (DIMENSIONS - 2)
            return [0.5, 0.5] + [0.0] * (DIMENSIONS - 2)
        return [vector(t) for t in texts]


def test_semantic_retrieval_is_scoped_and_source_grounded(ats, vacancy, pdf):
    another = ats.vacancies.create(
        VacancyInput(title="Second vacancy", description="Python data pipelines.")
    )
    assert ats.documents.upload_batch(vacancy.id, [("first.pdf", pdf)])[0].success
    assert ats.documents.upload_batch(another.id, [("other.pdf", pdf)])[0].success
    with ats.db.transaction() as session:
        first = session.scalar(select(CandidateDocument).where(
            CandidateDocument.vacancy_id == vacancy.id
        ))
        second = session.scalar(select(CandidateDocument).where(
            CandidateDocument.vacancy_id == another.id
        ))
        first_id, second_id = first.id, second.id
    embedder = StubEmbedder()
    index = EmbeddingService(ats.db, embedder)
    index.index(
        source_type="CV", vacancy_id=vacancy.id, document_id=first_id,
        source="first CV", chunks=[
            Chunk("pdf:page1#part0", "Hands-on Java concurrency."),
            Chunk("pdf:page2#part0", "Python machine learning models."),
        ],
    )
    index.index(
        source_type="CV", vacancy_id=another.id, document_id=second_id,
        source="second CV", chunks=[Chunk("pdf:page1#part0", "Other job Java.")],
    )
    search = SemanticSearchService(ats.db, embedder)
    hits = search.search("Java concurrency", vacancy_id=vacancy.id)
    assert [h.document_id for h in hits] == [first_id, first_id]
    assert hits[0].reference == "pdf:page1#part0"
    assert hits[0].text == "Hands-on Java concurrency."
    assert hits[0].similarity == pytest.approx(1.0)
    assert all(h.vacancy_id == vacancy.id for h in hits)

    limited = search.search("Java", vacancy_id=vacancy.id, document_id=first_id, limit=1)
    assert len(limited) == 1
    assert limited[0].document_id == first_id

    with ats.db.transaction() as session:
        application = session.get(JobApplication, first.application_id)
        application.status = "Removed"
    assert search.search("Java", vacancy_id=vacancy.id) == []
    assert len(search.search("Java", vacancy_id=another.id)) == 1


def test_job_description_retrieval_and_input_validation(ats, vacancy):
    embedder = StubEmbedder()
    EmbeddingService(ats.db, embedder).index(
        source_type="JD", vacancy_id=vacancy.id, document_id=None,
        source=vacancy.description, chunks=[Chunk("jd:paragraph1#part0", vacancy.description)],
    )
    search = SemanticSearchService(ats.db, embedder)
    hits = search.search("Java", vacancy_id=vacancy.id, source_type="JD")
    assert len(hits) == 1
    assert hits[0].document_id is None
    assert hits[0].reference == "jd:paragraph1#part0"
    assert hits[0].text == vacancy.description
    assert search.search("Java", vacancy_id=vacancy.id) == []
    with pytest.raises(ValueError):
        search.search("", vacancy_id=vacancy.id)
    with pytest.raises(ValueError):
        search.search("Java", vacancy_id=vacancy.id, source_type="JD", document_id=1)
    with pytest.raises(ValueError):
        search.search("Java", vacancy_id=vacancy.id, limit=51)
