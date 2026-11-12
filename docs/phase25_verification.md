# Phase 2.5 — end-to-end verification and stabilization

Phase 2.5 tests the **existing** automatic text pipeline and Phase 2 dashboard,
without changing the selected LLM, adding RAG, or uploading actual CVs.

## Automated checks

The CI workflow `.github/workflows/ats-ci.yml` runs linting and all non-live
pytest tests on Python 3.12. No Ollama server is required. In particular,
`tests/integration/test_phase25_stabilization.py` exercises:

- 100 synthetic CVs uploaded to one vacancy and 35 to another
- automatic creation of 135 independent six-stage records
- background-worker-equivalent processing via the service queue
- persistent extracted text and no repeated extraction after reopening SQLite
- dashboard counts that reconcile with the persisted per-job records
- strict job isolation and failure isolation
- interrupted work being safely recovered without duplicate text records
- unimplemented AI/matching/scoring/HR stages remaining `WAITING`

These tests prove the service layer using synthetic documents; they are **not** a
real-browser test, Windows detached-process test, or a test of local Ollama.

## Real Windows acceptance check

After getting the updated branch, run the existing migration and Streamlit entry
point described in the main README. Use lawfully obtained test CVs, or synthetic
files, and upload 100 for one vacancy and 35 for another. Confirm:

1. The Job Dashboard shows 100 and 35 independently.
2. Text extraction starts without clicking `Analyze` for individual CVs.
3. The Pipeline Monitor shows the actual per-document stages and errors.
4. Closing the browser does not stop the separate Python worker.
5. Reopening the app preserves the results, including after restarting the worker.
6. A malformed or missing document is marked `FAILED` while other jobs proceed.
7. The five later AI/matching/scoring/HR stages still read `WAITING`; Phase 2.5
   does **not** implement them.

Check `data/pipeline-worker.log` for background-worker failures. The log should
not include CV text. This repository is a local, single-operator prototype;
no production authentication, OCR or automatic hiring decision is provided.
