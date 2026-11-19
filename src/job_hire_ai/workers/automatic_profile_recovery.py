"""One-time local recovery of pre-fix CV extraction failures on worker startup.

Recovery is deliberately versioned and bounded: restarting the app repeatedly
must never trigger repeated expensive model inference. No uploads, embeddings,
saved profiles, or other pipeline stages are changed.
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import aliased

from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    DocumentPipelineStage,
    JobApplication,
)
from job_hire_ai.retry_profiles import requeue_profiles

logger = logging.getLogger(__name__)

RECOVERY_VERSION = "timeout-to-source-only-v3"
RECOVERABLE_ERRORS = (
    "could not produce complete valid structured output",
    "output was truncated after bounded retries",
    "returned invalid structured output",
    "model missing.",
    "ollama disconnected.",
    "ollama timed out or disconnected.",
)


def recover_legacy_profiles_once(db, data_dir: Path, *, fallback_enabled: bool) -> dict:
    """Requeue previous JSON and model-unavailable failures once per release.

    The caller must hold the single-process pipeline worker lock. A marker is
    created only after the database transaction succeeds. A subsequent start
    sees it and will not requeue newly failed attempts again.
    """
    if not fallback_enabled:
        return {}
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / f".{RECOVERY_VERSION}.done"
    if marker.exists():
        return {}

    with db.transaction() as session:
        embedding = aliased(DocumentPipelineStage)
        failed = list(
            session.execute(
                select(DocumentPipelineStage.document_id, DocumentPipelineStage.error_summary)
                .join(
                    CandidateDocument,
                    DocumentPipelineStage.document_id == CandidateDocument.id,
                )
                .join(
                    JobApplication,
                    CandidateDocument.application_id == JobApplication.id,
                )
                .join(embedding, embedding.document_id == DocumentPipelineStage.document_id)
                .where(
                    DocumentPipelineStage.stage == "PROFILE_EXTRACTION",
                    DocumentPipelineStage.status == "FAILED",
                    embedding.stage == "EMBEDDING",
                    embedding.status == "COMPLETED",
                    JobApplication.status == "Uploaded",
                )
            )
        )
    ids = [
        document_id
        for document_id, error in failed
        if error and any(fragment in error.lower() for fragment in RECOVERABLE_ERRORS)
    ]
    results = requeue_profiles(db, ids, allow_exhausted=True) if ids else {}
    # Persist the one-time decision even if no failures exist. An identical
    # crash/restart must not repeatedly requeue a model that cannot parse CVs.
    marker.write_text(
        f"{RECOVERY_VERSION}\n"
        f"Requeued: {','.join(str(i) for i, result in results.items() if result == 'Requeued')}\n",
        encoding="utf-8",
    )
    logger.info(
        "Automatic one-time CV profile recovery: %s eligible, %s requeued",
        len(ids),
        sum(value == "Requeued" for value in results.values()),
    )
    return results


FAST_PROFILE_RECOVERY_VERSION = "structured-source-profile-v2"


def upgrade_old_fast_profiles_once(db, data_dir: Path) -> dict[int, str]:
    """Upgrade old literal-only profiles once, preserving review history and originals.

    Called while holding the exclusive worker lock, before normal processing.
    Never reruns a reviewed profile and never touches CV/embeddings.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / f".{FAST_PROFILE_RECOVERY_VERSION}.done"
    if marker.exists():
        return {}
    outcomes = {}
    with db.transaction() as session:
        documents = session.scalars(
            select(CandidateDocument)
            .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
            .where(JobApplication.status == "Uploaded")
        ).all()
        for document in documents:
            current = session.scalar(
                select(CandidateProfile)
                .where(CandidateProfile.document_id == document.id)
                .order_by(CandidateProfile.id.desc()).limit(1)
            )
            if current is None:
                continue
            # Avoid replacing already reviewed/corrected profiles.
            if current.reviews:
                continue
            if not any(
                "PARTIAL DETERMINISTIC EXTRACTION (FAST 1B MODE)" in w
                for w in (current.warnings or [])
            ):
                continue
            stages = {
                stage.stage: stage
                for stage in session.scalars(
                    select(DocumentPipelineStage).where(
                        DocumentPipelineStage.document_id == document.id
                    )
                )
            }
            extraction = stages.get("PROFILE_EXTRACTION")
            embedding = stages.get("EMBEDDING")
            if (
                extraction is None or embedding is None
                or extraction.status != "COMPLETED"
                or embedding.status != "COMPLETED"
            ):
                continue
            extraction.status = "PENDING"
            extraction.error_summary = None
            extraction.completed_at = None
            # Later snapshots remain in DB for audit but the pipeline must
            # generate fresh validation/matching for the new profile.
            for name in ("EVIDENCE_VALIDATION", "MATCHING", "SCORING"):
                stage = stages.get(name)
                if stage is not None and stage.status != "RUNNING":
                    stage.status = "WAITING"
                    stage.error_summary = None
                    stage.completed_at = None
            outcomes[document.id] = "Requeued partial profile"
    marker.write_text(
        FAST_PROFILE_RECOVERY_VERSION + "\\n"
        + "Requeued: " + ",".join(str(i) for i in outcomes) + "\\n",
        encoding="utf-8",
    )
    logger.info("Structured CV profile upgrade: %d requeued", len(outcomes))
    return outcomes
