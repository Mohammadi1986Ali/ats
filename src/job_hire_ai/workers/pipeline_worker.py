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
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.settings import Settings
from job_hire_ai.storage import LocalStorage
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
    analyzer = None
    if settings.profile_extraction_enabled:
        if not settings.embeddings_enabled:
            raise ATSError("Automatic profile extraction requires ATS_EMBEDDINGS_ENABLED=true.")
        model_settings = OllamaSettings()
        analyzer = CVAnalysisService(
            db, documents, OllamaProvider(model_settings), model_settings,
            settings.directory / "cv-worker.lock",
        )
    service = PipelineService(
        db, documents, settings.directory,
        embeddings_enabled=settings.embeddings_enabled,
        embedding_model=settings.embedding_model,
        embedding_base_url=settings.embedding_base_url,
        profile_extraction_enabled=settings.profile_extraction_enabled,
        profile_analyzer=analyzer,
        evidence_validation_enabled=settings.evidence_validation_enabled,
    )
    try:
        with worker_lock(settings.directory / "pipeline-text-worker.lock"):
            service.recover_interrupted()
            logging.info("Automatic CV text worker started")
            while True:
                if not service.process_next():
                    time.sleep(2)
    except ATSError as exc:
        # Another healthy worker owns the lock; extra startup instances simply exit.
        logging.info("CV text worker not started: %s", exc)
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
