# Phase 3.2 — Local CV / Job Description embeddings

## What this phase does

The ATS keeps its existing upload and Python-only text extraction. With
`ATS_EMBEDDINGS_ENABLED=true`, the independent pipeline worker then:

1. Selects CVs whose persisted TEXT_EXTRACTION stage is COMPLETED.
2. Splits the saved text into bounded chunks, preserving PDF page/block or DOCX
   paragraph/table reference and a stable part number.
3. Calls local Ollama `/api/embed` (not a generative chat prompt), in batches of 8.
4. Saves the source text, reference, model, source hash, chunker version, index and
   full 768-dimensional vector atomically.
5. Marks each CV's EMBEDDING stage COMPLETED or FAILED independently.
6. Indexes current job descriptions automatically and re-indexes them when edited.

No hiring decision, score, ranking, RAG search, or LLM-generated profile is
produced by this phase. Semantic retrieval is a later phase.

## Safe setup

The configuration is **opt-in**: all existing SQLite users continue running
without Ollama embedding work after updating and migrating. The default
`ATS_EMBEDDINGS_ENABLED=false` is intentional.

If using PostgreSQL, finish Phase 3.1: PostgreSQL must be running with
pgvector enabled, migrations applied and any desired SQLite data copied
*before* switching the application database. Never transfer data while an
ATS worker or Streamlit is running.

After pulling the latest GitHub branch, stop the ATS and its workers, and run
the latest Alembic migration (`uv run alembic upgrade head`) with your intended
database URL. Both SQLite and PostgreSQL schemas receive the new stage;
PostgreSQL stores vectors natively as `vector(768)`; SQLite stores vectors as
text for offline development tests.

Install and start Ollama locally, and pull the embedding model:

```powershell
ollama pull nomic-embed-text
```

Set the following environment variables (or equivalent `.env` entries):

```dotenv
ATS_EMBEDDINGS_ENABLED=true
ATS_EMBEDDING_MODEL=nomic-embed-text
ATS_EMBEDDING_BASE_URL=http://127.0.0.1:11434
```

Then restart the ATS and the independent worker. Embedding is performed by
the background pipeline worker, not by the Streamlit request. The existing
dashboard displays the additional EMBEDDING stage.

**Privacy:** Chunk text is written to your selected ATS database in addition
to original CV files. Keep PostgreSQL credentials secret, restrict access to
the database and Ollama port, and do not point the model URL at an untrusted
server. CV vectors are linked to their document by an ON DELETE CASCADE
foreign key. Reindexing a JD replaces its previous chunks in one transaction.

**Model compatibility:** The PostgreSQL column and validation are fixed at
768 dimensions. A different embedding model is not interchangeable without
a schema migration and explicit reindex. Existing embeddings are reused only
when source hash, embedding model and chunker version match.

**Limitations:** No OCR is provided; scanned PDFs are blocked at text
extraction. A failed CV embedding stays FAILED instead of silently retrying
forever. The worker's restart recovery will only retry interrupted RUNNING
jobs up to three attempts. Manual retry controls are future work. JD failures
are logged and skipped until worker restart or the description changes.

## Verification

```powershell
uv run ruff check .
uv run pytest -q -m "not live_ollama"
```

The new regression tests use deterministic synthetic vectors and need no
Ollama. GitHub Actions also runs an isolated PostgreSQL instance with pgvector
and checks that real 768-dimensional vector rows can be inserted and read.
A passing test suite does not prove your local Ollama model is installed.

The pipeline and database can be checked directly:

```sql
SELECT stage, status, count(*) FROM document_pipeline_stages
GROUP BY stage, status ORDER BY stage, status;

SELECT source_type, model, count(*) FROM embedding_chunks
GROUP BY source_type, model ORDER BY source_type;
```
