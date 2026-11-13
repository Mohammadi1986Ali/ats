"""Deterministic audit of the *stored* profile against the original extracted CV.

This is not human approval. A successful audit may still require human review.
No LLM is invoked and no candidate is scored or ranked.
"""

from dataclasses import dataclass

from sqlalchemy import select

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction
from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    DocumentTextExtraction,
    EvidenceReference,
    JobApplication,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.evidence_service import claim_issues, verify_quote
from job_hire_ai.text_extraction import ExtractedText


@dataclass(frozen=True)
class EvidenceAudit:
    profile_id: int
    claims_checked: int
    quotes_checked: int
    issues: tuple[str, ...]
    review_required: bool

    @property
    def needs_review(self) -> bool:
        return self.review_required


class EvidenceValidationService:
    """Re-check persisted quote provenance; never auto-approve a candidate."""

    def __init__(self, db):
        self.db = db

    def validate_document(self, document_id: int) -> EvidenceAudit:
        with self.db.transaction() as session:
            document = session.get(CandidateDocument, document_id)
            if document is None:
                raise ATSError("CV was removed before evidence validation.")
            application = session.get(JobApplication, document.application_id)
            if application is None or application.status != "Uploaded":
                raise ATSError("Application is no longer available for evidence validation.")

            profile = session.scalar(
                select(CandidateProfile)
                .where(CandidateProfile.document_id == document_id)
                .order_by(CandidateProfile.id.desc())
                .limit(1)
            )
            if profile is None:
                raise ATSError("No persisted candidate profile for evidence validation.")
            extracted = session.get(DocumentTextExtraction, profile.text_extraction_id)
            if (
                extracted is None
                or extracted.document_id != document_id
                or extracted.checksum != document.checksum
            ):
                raise ATSError("Profile source does not match the current uploaded CV.")
            try:
                source = ExtractedText.deserialize(extracted.source)
                payload = CVExtraction.model_validate(profile.original_extraction)
            except (ValueError, KeyError, TypeError) as exc:
                raise ATSError(
                    "Persisted profile or source is invalid; review extraction."
                ) from exc

            existing = list(
                session.scalars(
                    select(EvidenceReference).where(EvidenceReference.profile_id == profile.id)
                )
            )
            by_path = {}
            for row in existing:
                if row.document_id != document_id:
                    raise ATSError("Evidence belongs to the wrong CV.")
                by_path.setdefault(row.field_path, []).append(row)

            issues = []
            claim_count = quote_count = 0
            for section in PROFILE_SECTIONS:
                for index, claim in enumerate(getattr(payload, section)):
                    claim_count += 1
                    path = f"{section}.{index}"
                    issues.extend(f"{path}: {issue}" for issue in claim_issues(claim))
                    if claim.claim_status == "UNCERTAIN":
                        issues.append(f"{path}: claim marked uncertain by extraction.")
                    saved = by_path.pop(path, [])
                    if len(saved) != len(claim.evidence):
                        raise ATSError("Evidence rows are incomplete or inconsistent.")
                    remaining = list(saved)
                    for quote in claim.evidence:
                        quote_count += 1
                        candidates = [
                            item for item in remaining
                            if item.source_excerpt == quote.source_excerpt
                            and (
                                quote.source_reference is None
                                or item.source_reference == quote.source_reference
                            )
                        ]
                        if not candidates:
                            raise ATSError("Evidence quote does not match the stored profile.")
                        row = candidates[0]
                        remaining.remove(row)
                        checked = verify_quote(quote, source, document_id)
                        row.source_reference = checked.source_reference
                        row.verification_status = checked.verification_status
                        row.page_number = checked.page_number
                        row.character_start = checked.character_start
                        row.character_end = checked.character_end
                        if checked.verification_status != "VERIFIED":
                            issues.append(
                                f"{path}: {checked.verification_status.lower()} source evidence."
                            )
            if by_path:
                raise ATSError("Unexpected evidence rows exist for this profile.")

            # Preserve extraction warnings and all reviewer corrections. This
            # automated audit never creates a human validation review event.
            prefix = "Evidence audit: "
            warnings = [
                warning for warning in profile.warnings
                if not warning.startswith(prefix)
            ]
            warnings.extend(prefix + issue for issue in issues)
            profile.warnings = list(dict.fromkeys(warnings))
            if profile.warnings:
                profile.analysis_status = "NEEDS_REVIEW"
            return EvidenceAudit(
                profile.id, claim_count, quote_count, tuple(issues),
                profile.analysis_status == "NEEDS_REVIEW",
            )
