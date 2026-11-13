"""Source-grounded semantic retrieval; no LLM completion or hiring decisions.

PostgreSQL uses pgvector cosine distance. SQLite offers a deterministic
Python fallback for development tests; vectors are never sent externally.
"""

import json
import math
from dataclasses import dataclass

from sqlalchemy import text

from job_hire_ai.errors import ATSError
from job_hire_ai.services.embedding_service import CHUNKER_VERSION, DIMENSIONS


@dataclass(frozen=True)
class SearchHit:
    document_id: int | None
    vacancy_id: int
    source_type: str
    reference: str
    text: str
    similarity: float
    chunk_index: int


def _validate_vector(vector: list[float]) -> None:
    if len(vector) != DIMENSIONS or any(
        not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n)
        for n in vector
    ):
        raise ATSError("Search embedding has incorrect dimensions or non-finite values.")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class SemanticSearchService:
    """Retrieve indexed passages for one vacancy without crossing job boundaries.

    Removed applications are not searchable. Each hit retains the exact stored
    source passage/reference; retrieval never infers a skill or candidate score.
    """

    def __init__(self, db, embedder):
        self.db = db
        self.embedder = embedder

    def search(
        self, query: str, *, vacancy_id: int, source_type: str = "CV",
        document_id: int | None = None, limit: int = 5,
    ) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("Search query must not be blank.")
        if vacancy_id < 1 or document_id is not None and document_id < 1:
            raise ValueError("Identifiers must be positive.")
        if source_type not in {"CV", "JD"}:
            raise ValueError("source_type must be CV or JD.")
        if source_type == "JD" and document_id is not None:
            raise ValueError("A JD search cannot specify document_id.")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50.")
        query_vector = self.embedder.embed([query.strip()])[0]
        _validate_vector(query_vector)

        parameters = {
            "vacancy": vacancy_id, "kind": source_type,
            "document": document_id, "model": self.embedder.model,
            "version": CHUNKER_VERSION, "limit": limit,
        }
        # Restrict to active applications, and use only chunks of the currently
        # persisted vacancy text / uploaded document revision.
        join_clause = """
            JOIN vacancies AS v ON v.id = e.vacancy_id
            LEFT JOIN candidate_documents AS d ON d.id = e.document_id
            LEFT JOIN job_applications AS a ON a.id = d.application_id
        """
        filter_clause = """
            e.vacancy_id = :vacancy AND e.source_type = :kind
            AND e.model = :model AND e.chunker_version = :version
            AND (:document IS NULL OR e.document_id = :document)
            AND (
                (e.source_type = 'JD' AND e.document_id IS NULL)
                OR (e.source_type = 'CV' AND d.vacancy_id = e.vacancy_id
                    AND a.vacancy_id = e.vacancy_id AND a.status = 'Uploaded')
            )
        """
        with self.db.transaction() as session:
            postgres = session.bind.dialect.name == "postgresql"
            if postgres:
                # Explicit cast avoids relying on driver registration of pgvector.
                # The distance expression is parameterized and computed by Postgres.
                rows = session.execute(
                    text(f"""
                        SELECT e.document_id, e.vacancy_id, e.source_type,
                               e.reference, e.chunk_text, e.chunk_index,
                               (1 - (e.embedding <=> CAST(:vector AS vector))) AS score
                        FROM embedding_chunks AS e
                        {join_clause}
                        WHERE {filter_clause}
                        ORDER BY e.embedding <=> CAST(:vector AS vector), e.id
                        LIMIT :limit
                    """),
                    {**parameters, "vector": json.dumps(query_vector)},
                ).mappings().all()
                return [
                    SearchHit(
                        document_id=row["document_id"],
                        vacancy_id=row["vacancy_id"],
                        source_type=row["source_type"],
                        reference=row["reference"],
                        text=row["chunk_text"],
                        similarity=max(-1.0, min(1.0, float(row["score"]))),
                        chunk_index=row["chunk_index"],
                    )
                    for row in rows
                ]
            rows = session.execute(
                text(f"""
                    SELECT e.document_id, e.vacancy_id, e.source_type,
                           e.reference, e.chunk_text, e.chunk_index, e.embedding
                    FROM embedding_chunks AS e
                    {join_clause}
                    WHERE {filter_clause}
                """),
                parameters,
            ).mappings().all()

        hits = []
        for row in rows:
            try:
                vector = json.loads(row["embedding"])
                _validate_vector(vector)
            except (ValueError, TypeError) as exc:
                raise ATSError("Corrupt stored embedding; reindex the document.") from exc
            hits.append(
                SearchHit(
                    document_id=row["document_id"],
                    vacancy_id=row["vacancy_id"],
                    source_type=row["source_type"],
                    reference=row["reference"],
                    text=row["chunk_text"],
                    similarity=_cosine(query_vector, vector),
                    chunk_index=row["chunk_index"],
                )
            )
        hits.sort(key=lambda hit: (-hit.similarity, hit.document_id or 0, hit.chunk_index))
        return hits[:limit]
