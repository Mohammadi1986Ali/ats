"""Local non-generative embedding and versioned storage for job descriptions and CVs."""

import hashlib
import json
import math
import re
from dataclasses import dataclass

import httpx
from sqlalchemy import text

from job_hire_ai.errors import ATSError
from job_hire_ai.text_extraction import ExtractedText, TextSegment

CHUNKER_VERSION = "source-chunks-v1"
DIMENSIONS = 768  # nomic-embed-text; keep schema and model dimensions aligned
MAX_CHARS = 1300


@dataclass(frozen=True)
class Chunk:
    reference: str
    content: str


def chunk_cv(extracted: ExtractedText, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Split by source blocks and word boundaries, retaining original references."""
    if max_chars < 128:
        raise ValueError("Chunk size must be at least 128 characters")
    chunks = []
    for segment in extracted.segments:
        remaining = segment.text.strip()
        part = 0
        while remaining:
            if len(remaining) <= max_chars:
                cut = len(remaining)
            else:
                cut = max(
                    remaining.rfind("\n", 0, max_chars),
                    remaining.rfind(". ", 0, max_chars),
                    remaining.rfind(" ", 0, max_chars),
                )
                if cut < max_chars // 3:
                    cut = max_chars
                elif remaining[cut:cut + 2] == ". ":
                    cut += 1
            piece, remaining = remaining[:cut].strip(), remaining[cut:].lstrip()
            if piece:
                chunks.append(Chunk(f"{segment.reference}#part{part}", piece))
                part += 1
    if len(chunks) > 500:
        raise ATSError("Text requires more than 500 embedding chunks; review document length.")
    return chunks


def chunk_jd(description: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Stable paragraph references for the vacancy description."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", description) if p.strip()]
    if not parts and description.strip():
        parts = [description.strip()]
    source = ExtractedText(
        [
            TextSegment(f"jd:paragraph{i}", part, part, None, "paragraph")
            for i, part in enumerate(parts, 1)
        ],
        [],
    )
    return chunk_cv(source, max_chars)


def source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LocalEmbedder:
    """Only Ollama /api/embed. No generative chat model or external API."""

    def __init__(self, base_url="http://127.0.0.1:11434", model="nomic-embed-text", timeout=90):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > 8:
            raise ValueError("Embed in bounded batches of at most eight chunks")
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model, "input": texts, "truncate": False},
                )
                response.raise_for_status()
                vectors = response.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ATSError("Local embedding model unavailable or rejected the input.") from exc
        if len(vectors) != len(texts):
            raise ATSError("Embedding response count differs from input chunk count.")
        for vector in vectors:
            if len(vector) != DIMENSIONS or any(
                not isinstance(n, (int, float)) or not math.isfinite(n) for n in vector
            ):
                raise ATSError("Embedding model returned invalid dimensions or values.")
        return vectors


class EmbeddingService:
    """Idempotent, transactional indexing. Failed model calls save no partial vectors."""

    def __init__(self, db, embedder):
        self.db = db
        self.embedder = embedder

    def current(
        self, *, source_type: str, vacancy_id: int, document_id: int | None, source: str
    ) -> bool:
        with self.db.transaction() as session:
            return bool(
                session.execute(
                    text("""
                        SELECT 1 FROM embedding_chunks
                        WHERE source_type=:kind AND vacancy_id=:vacancy
                          AND ((CAST(:doc AS INTEGER) IS NULL AND document_id IS NULL)
                              OR document_id=:doc)
                          AND source_hash=:hash AND model=:model
                          AND chunker_version=:version LIMIT 1
                    """),
                    {
                        "kind": source_type, "vacancy": vacancy_id, "doc": document_id,
                        "hash": source_hash(source), "model": self.embedder.model,
                        "version": CHUNKER_VERSION,
                    },
                ).first()
            )

    def index(
        self, *, source_type: str, vacancy_id: int, document_id: int | None,
        source: str, chunks: list[Chunk]
    ) -> int:
        if source_type not in {"JD", "CV"} or (source_type == "CV") != (document_id is not None):
            raise ValueError("Invalid embedding source identity.")
        if not chunks:
            raise ATSError("No readable text to embed.")
        fingerprint = source_hash(source)
        keys = {
            "kind": source_type, "vacancy": vacancy_id, "doc": document_id,
            "hash": fingerprint, "model": self.embedder.model, "version": CHUNKER_VERSION,
        }
        with self.db.transaction() as session:
            existing = session.scalar(
                text("""
                    SELECT COUNT(*) FROM embedding_chunks
                    WHERE source_type=:kind AND vacancy_id=:vacancy
                      AND ((CAST(:doc AS INTEGER) IS NULL AND document_id IS NULL)
                              OR document_id=:doc)
                      AND source_hash=:hash AND model=:model AND chunker_version=:version
                """),
                keys,
            )
            if existing == len(chunks):
                return existing
        vectors = []
        for start in range(0, len(chunks), 8):
            vectors.extend(
                self.embedder.embed([c.content for c in chunks[start:start + 8]])
            )
        with self.db.transaction() as session:
            session.execute(
                text("""
                    DELETE FROM embedding_chunks
                    WHERE source_type=:kind AND vacancy_id=:vacancy
                      AND ((CAST(:doc AS INTEGER) IS NULL AND document_id IS NULL)
                              OR document_id=:doc)
                """),
                keys,
            )
            postgres = session.bind.dialect.name == "postgresql"
            insertion = text("""
                INSERT INTO embedding_chunks
                  (source_type, document_id, vacancy_id, source_hash, model,
                   chunker_version, chunk_index, reference, chunk_text, embedding)
                VALUES (:kind, :doc, :vacancy, :hash, :model, :version,
                        :idx, :ref, :content, CAST(:vector AS vector))
            """ if postgres else """
                INSERT INTO embedding_chunks
                  (source_type, document_id, vacancy_id, source_hash, model,
                   chunker_version, chunk_index, reference, chunk_text, embedding)
                VALUES (:kind, :doc, :vacancy, :hash, :model, :version,
                        :idx, :ref, :content, :vector)
            """)
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                session.execute(
                    insertion,
                    {
                        **keys, "idx": index, "ref": chunk.reference,
                        "content": chunk.content, "vector": json.dumps(vector),
                    },
                )
        return len(chunks)
