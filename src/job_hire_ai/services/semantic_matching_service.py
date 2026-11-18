"""Automatic JD-to-CV semantic *retrieval*, using saved embeddings only.

Cosine similarity links passages for human inspection. It is not evidence of
a qualification, a calibrated probability, a suitability score or a hiring decision.
No generative model calls, candidate rankings, or HR-criteria approval required.
"""

import json
import math
from dataclasses import dataclass

from sqlalchemy import select, text

from job_hire_ai.database.models import (
    CandidateDocument,
    DocumentTextExtraction,
    JobApplication,
    SemanticMatchResult,
    Vacancy,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.embedding_service import CHUNKER_VERSION, DIMENSIONS, source_hash

SEMANTIC_VERSION = "cosine-retrieval-v1"
MAX_JD_CHUNKS = 40
MAX_CV_CHUNKS = 120
TOP_MATCHES_PER_JD = 2


@dataclass(frozen=True)
class StoredChunk:
    reference: str
    text: str
    vector: tuple[float, ...]


def _vector(raw) -> tuple[float, ...]:
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        raise ATSError("Stored embedding cannot be decoded.") from exc
    if (
        not isinstance(values, list) or len(values) != DIMENSIONS
        or any(type(value) not in (float, int) or not math.isfinite(value)
               for value in values)
    ):
        raise ATSError("Stored embedding has invalid dimensions or values.")
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude <= 1e-12:
        raise ATSError("Stored embedding has zero length.")
    return tuple(value / magnitude for value in values)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))


def _chunks(session, *, kind: str, vacancy_id: int, document_id: int | None,
            fingerprint: str, model: str, limit: int) -> list[StoredChunk]:
    # PostgreSQL's CAST(vector AS TEXT) yields a JSON-compatible vector string;
    # SQLite holds the same JSON list as text. No re-embedding is performed.
    rows = session.execute(
        text("""
            SELECT reference, chunk_text, CAST(embedding AS TEXT) AS vector
            FROM embedding_chunks
            WHERE source_type=:kind AND vacancy_id=:vacancy
              AND ((CAST(:doc AS INTEGER) IS NULL AND document_id IS NULL)
                   OR document_id=:doc)
              AND source_hash=:fingerprint AND model=:model
              AND chunker_version=:chunker
            ORDER BY chunk_index
            LIMIT :limit
        """),
        {
            "kind": kind, "vacancy": vacancy_id, "doc": document_id,
            "fingerprint": fingerprint, "model": model,
            "chunker": CHUNKER_VERSION, "limit": limit + 1,
        },
    ).all()
    if len(rows) > limit:
        raise ATSError("Too many embedding chunks for bounded semantic comparison.")
    return [
        StoredChunk(reference=ref, text=chunk_text, vector=_vector(raw))
        for ref, chunk_text, raw in rows
    ]


class SemanticMatchingService:
    """Persist bounded, immutable passage comparisons scoped to the correct job."""

    def __init__(self, db, embedding_model: str = "nomic-embed-text"):
        self.db = db
        self.embedding_model = embedding_model

    def _identity(self, session, document_id: int):
        document = session.get(CandidateDocument, document_id)
        if document is None:
            raise ATSError("Candidate CV does not exist.")
        application = session.get(JobApplication, document.application_id)
        if application is None or application.status != "Uploaded":
            raise ATSError("Removed candidate applications cannot be compared.")
        vacancy = session.get(Vacancy, document.vacancy_id)
        if vacancy is None or not vacancy.description.strip():
            raise ATSError("Current Job Description is empty.")
        source = session.scalar(
            select(DocumentTextExtraction)
            .where(
                DocumentTextExtraction.document_id == document_id,
                DocumentTextExtraction.checksum == document.checksum,
            )
            .order_by(DocumentTextExtraction.id.desc()).limit(1)
        )
        if source is None:
            raise ATSError("Current CV source text has not been extracted.")
        return document, vacancy, source_hash(vacancy.description), source_hash(
            source.checksum + ":" + source.extractor_version
        )

    def ready(self, vacancy_id: int) -> bool:
        """Only query readiness; never invoke embedding or a language model."""
        with self.db.transaction() as session:
            vacancy = session.get(Vacancy, vacancy_id)
            if vacancy is None or not vacancy.description.strip():
                return False
            return bool(session.execute(text("""
                SELECT 1 FROM embedding_chunks
                WHERE source_type='JD' AND vacancy_id=:vacancy
                  AND document_id IS NULL AND source_hash=:hash
                  AND model=:model AND chunker_version=:version LIMIT 1
            """), {
                "vacancy": vacancy_id, "hash": source_hash(vacancy.description),
                "model": self.embedding_model, "version": CHUNKER_VERSION,
            }).first())

    def match_document(self, document_id: int) -> SemanticMatchResult:
        with self.db.transaction() as session:
            document, vacancy, jd_hash, cv_hash = self._identity(session, document_id)
            existing = session.scalar(
                select(SemanticMatchResult).where(
                    SemanticMatchResult.document_id == document_id,
                    SemanticMatchResult.jd_source_hash == jd_hash,
                    SemanticMatchResult.cv_source_hash == cv_hash,
                    SemanticMatchResult.embedding_model == self.embedding_model,
                    SemanticMatchResult.method_version == SEMANTIC_VERSION,
                )
            )
            if existing:
                return existing
            jd = _chunks(
                session, kind="JD", vacancy_id=vacancy.id, document_id=None,
                fingerprint=jd_hash, model=self.embedding_model, limit=MAX_JD_CHUNKS,
            )
            cv = _chunks(
                session, kind="CV", vacancy_id=vacancy.id, document_id=document.id,
                fingerprint=cv_hash, model=self.embedding_model, limit=MAX_CV_CHUNKS,
            )
            if not jd or not cv:
                raise ATSError(
                    "Current JD/CV embeddings are missing; wait for embedding to finish."
                )
            matches = []
            for jd_chunk in jd:
                most_relevant = sorted(
                    cv, key=lambda item: _cosine(jd_chunk.vector, item.vector),
                    reverse=True,
                )[:TOP_MATCHES_PER_JD]
                matches.append({
                    "jd_reference": jd_chunk.reference,
                    "jd_excerpt": jd_chunk.text,
                    "cv_passages": [
                        {
                            "source_reference": item.reference,
                            "source_excerpt": item.text,
                            "cosine_similarity": round(
                                _cosine(jd_chunk.vector, item.vector), 6
                            ),
                        }
                        for item in most_relevant
                    ],
                    "interpretation": "SEMANTIC_RELEVANCE_ONLY",
                })
            result = SemanticMatchResult(
                document_id=document_id, vacancy_id=vacancy.id,
                jd_source_hash=jd_hash, cv_source_hash=cv_hash,
                embedding_model=self.embedding_model,
                method_version=SEMANTIC_VERSION, comparisons=matches,
            )
            session.add(result)
            session.flush()
            return result

    def current_result(self, document_id: int) -> SemanticMatchResult | None:
        with self.db.transaction() as session:
            try:
                _, _, jd_hash, cv_hash = self._identity(session, document_id)
            except ATSError:
                return None
            return session.scalar(
                select(SemanticMatchResult)
                .where(
                    SemanticMatchResult.document_id == document_id,
                    SemanticMatchResult.jd_source_hash == jd_hash,
                    SemanticMatchResult.cv_source_hash == cv_hash,
                    SemanticMatchResult.embedding_model == self.embedding_model,
                    SemanticMatchResult.method_version == SEMANTIC_VERSION,
                )
                .order_by(SemanticMatchResult.id.desc())
            )

    def list_for_vacancy(self, vacancy_id: int) -> list[SemanticMatchResult]:
        with self.db.transaction() as session:
            vacancy = session.get(Vacancy, vacancy_id)
            if vacancy is None:
                return []
            return list(session.scalars(
                select(SemanticMatchResult)
                .join(CandidateDocument,
                      CandidateDocument.id == SemanticMatchResult.document_id)
                .join(JobApplication, JobApplication.id == CandidateDocument.application_id)
                .where(
                    SemanticMatchResult.vacancy_id == vacancy_id,
                    SemanticMatchResult.jd_source_hash == source_hash(vacancy.description),
                    SemanticMatchResult.embedding_model == self.embedding_model,
                    SemanticMatchResult.method_version == SEMANTIC_VERSION,
                    JobApplication.status == "Uploaded",
                )
                .order_by(SemanticMatchResult.document_id, SemanticMatchResult.id.desc())
            ))
