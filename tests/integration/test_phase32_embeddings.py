"""Offline Phase 3.2 tests: synthetic text, no real Ollama and no private CVs."""

from sqlalchemy import text

from job_hire_ai.services.embedding_service import (
    DIMENSIONS,
    EmbeddingService,
    chunk_cv,
    chunk_jd,
)
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.text_extraction import ExtractedText, TextSegment


class FakeEmbedder:
    model = "nomic-embed-text"

    def __init__(self, broken=False):
        self.calls = 0
        self.broken = broken

    def embed(self, values):
        self.calls += 1
        if self.broken:
            raise RuntimeError("model is unavailable")
        return [[0.125] * DIMENSIONS for _ in values]


def test_chunking_preserves_source_locations_and_all_text():
    text_value = "Java concurrency and payments. " * 120
    extracted = ExtractedText(
        [TextSegment("pdf:p2:block3", text_value, text_value, 2, "paragraph")], []
    )
    pieces = chunk_cv(extracted, max_chars=250)
    assert len(pieces) > 1
    assert all(c.reference.startswith("pdf:p2:block3#part") for c in pieces)
    assert "".join("".join(c.content.split()) for c in pieces) == "".join(
        text_value.split()
    )
    assert chunk_jd("Build Java APIs.\n\nUse PostgreSQL.")[0].reference == "jd:paragraph1#part0"


def test_sqlite_embedding_index_is_idempotent_and_versioned(ats, vacancy):
    fake = FakeEmbedder()
    service = EmbeddingService(ats.db, fake)
    description = vacancy.description
    pieces = chunk_jd(description)
    assert service.index(
        source_type="JD", vacancy_id=vacancy.id, document_id=None,
        source=description, chunks=pieces,
    ) == len(pieces)
    assert service.current(
        source_type="JD", vacancy_id=vacancy.id, document_id=None, source=description,
    )
    assert service.index(
        source_type="JD", vacancy_id=vacancy.id, document_id=None,
        source=description, chunks=pieces,
    ) == len(pieces)
    assert fake.calls == 1
    changed = description + " Also Kubernetes."
    service.index(
        source_type="JD", vacancy_id=vacancy.id, document_id=None,
        source=changed, chunks=chunk_jd(changed),
    )
    with ats.db.transaction() as session:
        rows = session.execute(text("SELECT reference, chunk_text FROM embedding_chunks")).all()
    assert len(rows) == len(chunk_jd(changed))
    assert all(r[0].startswith("jd:paragraph") for r in rows)


def test_pipeline_auto_embed_and_restart_with_fake_local_model(ats, vacancy, pdf):
    result = ats.documents.upload_batch(vacancy.id, [("synthetic.pdf", pdf)])[0]
    assert result.success
    fake = FakeEmbedder()
    service = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        embeddings_enabled=True, embedder=fake,
    )
    assert service.process_next()  # text
    assert service.process_next()  # CV embedding
    assert service.process_next()  # JD embedding
    assert not service.process_next()
    _, rows = service.overview(vacancy.id)
    assert rows[0]["TEXT_EXTRACTION"] == "COMPLETED"
    assert rows[0]["EMBEDDING"] == "COMPLETED"
    with ats.db.transaction() as session:
        assert session.scalar(text("SELECT COUNT(*) FROM embedding_chunks")) >= 2
    reopened = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        embeddings_enabled=True, embedder=fake,
    )
    assert not reopened.process_next()


def test_embedding_failure_is_isolated_and_text_survives(ats, vacancy, pdf):
    assert ats.documents.upload_batch(vacancy.id, [("synthetic.pdf", pdf)])[0].success
    service = PipelineService(
        ats.db, ats.documents, ats.pipeline.data_dir,
        embeddings_enabled=True, embedder=FakeEmbedder(broken=True),
    )
    assert service.process_next()
    assert service.process_next()
    _, rows = service.overview(vacancy.id)
    assert rows[0]["TEXT_EXTRACTION"] == "COMPLETED"
    assert rows[0]["EMBEDDING"] == "FAILED"
    assert service.process_next()  # failed CV does not block JD
    assert not service.process_next()  # no tight retry loop
