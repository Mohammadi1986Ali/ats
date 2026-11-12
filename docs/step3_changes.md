# Step 3 added and modified files

Added source:

- `src/job_hire_ai/ai/cv_schemas.py` — strict qualification records and correction inputs
- `src/job_hire_ai/ai/cv_prompts.py` — versioned, untrusted-input CV prompt
- `src/job_hire_ai/ai/extraction_merger.py` — exact-claim deduplication and quote preservation
- `src/job_hire_ai/text_extraction.py` — PDF/DOCX source extraction, normalization and chunking
- `src/job_hire_ai/services/evidence_service.py` — literal verification, offsets and claim flags
- `src/job_hire_ai/services/cv_analysis_service.py` — persistent batches, cache, stages and profiles
- `src/job_hire_ai/services/candidate_profile_service.py` — profile/history access and HR overlays
- `src/job_hire_ai/workers/__init__.py`
- `src/job_hire_ai/workers/local_lock.py` — sequential worker OS guard
- `src/job_hire_ai/cv_ui.py` — CV Analysis page and all processing/review controls
- `migrations/versions/f72e820b4b1c_add_source_text_cv_analysis_batches_.py`

Added tests and documentation:

- `tests/__init__.py`, `tests/cv_fixtures.py`
- `tests/unit/test_cv_extraction.py`
- `tests/integration/test_cv_pipeline.py`
- `tests/integration/test_cv_ui.py`
- `docs/cv_analysis.md`
- `docs/evidence_traceability.md`
- `docs/candidate_profile_schema.md`
- `docs/step3_changes.md`

Modified:

- `pyproject.toml`, `uv.lock` — project version 0.3.0; existing dependencies reused
- `src/job_hire_ai/__init__.py` — current package description
- `src/job_hire_ai/ai/provider.py`, `ai/ollama_provider.py` — reuse structured transport for both tasks
- `src/job_hire_ai/database/models.py` — seven additive source/analysis/profile/evidence/review tables
- `src/job_hire_ai/runtime.py` — compose the new services with existing storage/provider
- `src/job_hire_ai/ui.py` — additional persisted CV dashboard metrics
- `app.py` — fourth navigation page and safe domain-error handling
- `tests/integration/test_ui.py` — updated total dashboard metric count
- `tests/integration/test_live_ollama.py` — opt-in real synthetic-CV pipeline test
- `README.md`, `docs/architecture.md`, `docs/data_model.md`, `docs/user_guide.md`,
  `docs/ai_integration.md`, `docs/verification.md` — current behavior and executed checks

Original migrations and Step 1 upload/storage/deletion services were preserved. New
foreign-key cascade policies extend existing application removal to extracted CV data.
Production pre-migration backup files remain private/ignored under `data/backups/`.
