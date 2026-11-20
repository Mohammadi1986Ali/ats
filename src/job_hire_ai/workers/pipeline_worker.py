"""Independent local text processing worker, started by the Streamlit app."""

import argparse
import logging
import time
from pathlib import Path

from job_hire_ai.ai.ollama_provider import OllamaProvider
from job_hire_ai.ai.settings import OllamaSettings
from job_hire_ai.database.session import Database
from job_hire_ai.errors import ATSError
from job_hire_ai.services.cv_analysis_service import CVAnalysisService
from job_hire_ai.services.document_service import DocumentService
from job_hire_ai.services.legacy_jd_activation import recover_legacy_jd_weights
from job_hire_ai.services.llama_scoring_service import LlamaScoringService
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.settings import Settings
from job_hire_ai.storage import LocalStorage
from job_hire_ai.workers.automatic_profile_recovery import (
    recover_legacy_profiles_once,
    upgrade_old_fast_profiles_once,
)
from job_hire_ai.workers.local_lock import worker_lock


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings(data_dir=args.data_dir)
    db = Database(settings.database_url)
    storage = LocalStorage(settings.directory / "uploads", settings.max_file_mb * 1024**2)
    documents = DocumentService(db, storage)
    if settings.evidence_validation_enabled and not settings.profile_extraction_enabled:
        raise ATSError(
            "Automatic evidence validation requires ATS_PROFILE_EXTRACTION_ENABLED=true."
        )
    if settings.matching_enabled and not settings.evidence_validation_enabled:
        raise ATSError(
            "Automatic job matching requires ATS_EVIDENCE_VALIDATION_ENABLED=true."
        )
    if settings.scoring_enabled and not settings.matching_enabled:
        raise ATSError("Automatic scoring requires ATS_MATCHING_ENABLED=true.")
    analyzer = None
    if settings.profile_extraction_enabled:
        if not settings.embeddings_enabled:
            raise ATSError("Automatic profile extraction requires ATS_EMBEDDINGS_ENABLED=true.")
        model_settings = OllamaSettings()
        if (
            model_settings.model in {"llama3.2:1b", "qwen2.5:0.5b"}
            and settings.automatic_profile_fast_mode
        ):
            model_settings = model_settings.model_copy(update={"cv_fast_mode_1b": True})
        analyzer = CVAnalysisService(
            db, documents, OllamaProvider(model_settings), model_settings,
            settings.directory / "cv-worker.lock",
        )
    llama = (
        LlamaScoringService(
            db, OllamaProvider(OllamaSettings(model=settings.llama_scoring_model))
        ) if settings.llama_scoring_enabled and settings.scoring_enabled else None
    )
    service = PipelineService(
        db, documents, settings.directory,
        embeddings_enabled=settings.embeddings_enabled,
        embedding_model=settings.embedding_model,
        embedding_base_url=settings.embedding_base_url,
        profile_extraction_enabled=settings.profile_extraction_enabled,
        profile_analyzer=analyzer,
        evidence_validation_enabled=settings.evidence_validation_enabled,
        matching_enabled=settings.matching_enabled,
        scoring_enabled=settings.scoring_enabled,
    )
    try:
        with worker_lock(settings.directory / "pipeline-text-worker.lock"):
            service.recover_interrupted()
            if llama is not None:
                interrupted = llama.recover_interrupted()
                if interrupted:
                    logging.warning(
                        "Recovered %s interrupted Llama scoring attempts; retry in dashboard",
                        interrupted,
                    )
            if analyzer is not None:
                if (
                    analyzer.settings.cv_fast_mode_1b
                    and analyzer.settings.cv_literal_fallback_enabled
                ):
                    upgrade_old_fast_profiles_once(db, settings.directory)
                recover_legacy_profiles_once(
                    db, settings.directory,
                    fallback_enabled=analyzer.settings.cv_literal_fallback_enabled,
                )
            if settings.matching_enabled and settings.scoring_enabled:
                recovered = recover_legacy_jd_weights(db)
                logging.info("Legacy JD weighting recovery: %s", recovered)
            logging.info("Automatic CV text worker started")
            last_jd_check = time.monotonic()
            while True:
                if service.process_next():
                    continue
                # Llama assesses only CVs with completed deterministic scoring.
                # Failed model attempts are persisted, never looped indefinitely.
                if llama is not None:
                    document_id = llama.pending_document()
                    if document_id is not None:
                        try:
                            llama.evaluate(document_id)
                        except ATSError as exc:
                            logging.warning("Llama scoring CV %s: %s", document_id, exc)
                        continue
                # Jobs may be created while the background worker is alive.
                # Recover eligible default-weight JDs without requiring restart.
                if settings.matching_enabled and settings.scoring_enabled:
                    current = time.monotonic()
                    if current - last_jd_check >= 30:
                        last_jd_check = current
                        try:
                            outcomes = recover_legacy_jd_weights(db)
                            if outcomes:
                                logging.info("Automatic JD criteria recovery: %s", outcomes)
                        except Exception:
                            logging.exception("JD criteria recovery failed; worker continues")
                time.sleep(2)
    except ATSError as exc:
        # Another healthy worker owns the lock; extra startup instances simply exit.
        logging.info("CV text worker not started: %s", exc)
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
