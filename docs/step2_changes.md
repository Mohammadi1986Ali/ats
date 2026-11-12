# Step 2 file changes

Added:

- `src/job_hire_ai/ai/__init__.py`
- `src/job_hire_ai/ai/provider.py` — typed provider contract and safe model status/results
- `src/job_hire_ai/ai/ollama_provider.py` — local client, inventory checks, structured inference
- `src/job_hire_ai/ai/settings.py` — adjustable CPU options, endpoint validation and timeouts
- `src/job_hire_ai/ai/schemas.py` — strict extraction and HR input schemas, source verification
- `src/job_hire_ai/ai/prompts.py` — versioned prompt and separated untrusted input
- `src/job_hire_ai/ai/job_analysis.py` — audited analysis workflow and description/draft race checks
- `src/job_hire_ai/ai_ui.py` — criteria editor, draft/approval actions and history
- `src/job_hire_ai/services/criteria_service.py` — versions, provenance, weights and approval
- `migrations/versions/0e8846979a09_add_versioned_hr_criteria_and_local_job_.py`
- `tests/fixtures/job_descriptions.json` — three fictional jobs with expected mock extractions
- `tests/unit/test_ollama_provider.py`
- `tests/integration/test_criteria.py`
- `tests/integration/test_step2_migration.py`
- `tests/integration/test_live_ollama.py`
- `docs/job_analysis.md`
- `docs/criteria_configuration.md`
- `docs/ai_integration.md`
- `docs/step2_changes.md`

Modified:

- `pyproject.toml`, `uv.lock` — client dependencies, version and optional live-test marker
- `.env.example` — local model configuration
- `app.py` — navigation key and version-aware cached runtime
- `src/job_hire_ai/__init__.py` — current package description
- `src/job_hire_ai/runtime.py` — compose new services/provider
- `src/job_hire_ai/database/models.py` — three additive entities and relationships
- `src/job_hire_ai/services/dashboard_service.py` — persisted readiness metrics
- `src/job_hire_ai/ui.py` — AI section, readiness metrics and unfinished-job navigation
- `tests/integration/test_ui.py` — original regression coverage plus Step 2 UI interactions
- `README.md`, `docs/architecture.md`, `docs/data_model.md`, `docs/user_guide.md`,
  `docs/verification.md` — current operation, schema, usage and actual validation results

The original Step 1 migration, vacancy/candidate/application/document services, document
validation and file storage code were preserved. Runtime backup files are ignored and
contain private local data; they are not source artifacts or demonstration records.
