"""Explicitly requeue failed CV profiles without re-uploading or deleting data.

Usage: uv run --no-sync python -m job_hire_ai.retry_profiles --document-ids 1 2 3
"""

import argparse
import sys

from sqlalchemy import select

from job_hire_ai.database.models import (
    CandidateDocument,
    DocumentPipelineStage,
    JobApplication,
)
from job_hire_ai.database.session import Database
from job_hire_ai.settings import Settings


def requeue_profiles(
    db, document_ids: list[int], max_attempts: int = 3, allow_exhausted: bool = False
) -> dict[int, str]:
    """Only requeue failed profile stages with completed source/embedding prerequisites."""
    outcomes = {}
    with db.transaction() as session:
        for document_id in dict.fromkeys(document_ids):
            document = session.get(CandidateDocument, document_id)
            if document is None:
                outcomes[document_id] = "CV not found"
                continue
            application = session.get(JobApplication, document.application_id)
            if application is None or application.status != "Uploaded":
                outcomes[document_id] = "Application removed; no retry"
                continue
            stages = {
                stage.stage: stage
                for stage in session.scalars(
                    select(DocumentPipelineStage).where(
                        DocumentPipelineStage.document_id == document_id
                    )
                )
            }
            profile = stages.get("PROFILE_EXTRACTION")
            embedding = stages.get("EMBEDDING")
            if profile is None or embedding is None:
                outcomes[document_id] = "Pipeline stages missing"
            elif profile.status != "FAILED":
                outcomes[document_id] = f"Not failed ({profile.status}); unchanged"
            elif embedding.status != "COMPLETED":
                outcomes[document_id] = "Embedding not completed"
            elif profile.attempts >= max_attempts and not allow_exhausted:
                outcomes[document_id] = "Retry limit reached; inspect failure"
            else:
                # One explicit operator-authorized recovery cycle after code or
                # model changes. Ordinary retries still honor bounded attempts.
                if profile.attempts >= max_attempts:
                    profile.attempts = 0
                profile.status = "PENDING"
                profile.error_summary = None
                profile.completed_at = None
                outcomes[document_id] = "Requeued"
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description="Requeue failed automatic CV extraction")
    parser.add_argument("--document-ids", nargs="+", type=int, required=True)
    parser.add_argument(
        "--allow-exhausted", action="store_true",
        help="Explicitly reset failed stage attempt counts for one recovery cycle.",
    )
    args = parser.parse_args()
    if any(document_id <= 0 for document_id in args.document_ids):
        parser.error("CV document IDs must be positive.")
    db = Database(Settings().database_url)
    try:
        results = requeue_profiles(
            db, args.document_ids, allow_exhausted=args.allow_exhausted
        )
        for document_id, outcome in results.items():
            print(f"CV {document_id}: {outcome}")
        print("The existing background worker will process requeued CVs automatically.")
    finally:
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
