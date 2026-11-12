import copy
import json

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction, ReviewInput
from job_hire_ai.database.models import (
    CandidateDocument,
    CandidateProfile,
    DocumentTextExtraction,
    JobApplication,
    ProfileReviewEvent,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.text_extraction import ExtractedText


def apply_value(payload: dict, path: str, value: object) -> object:
    parts = path.split(".")
    if len(parts) == 1 and parts[0] in PROFILE_SECTIONS:
        original = copy.deepcopy(payload[parts[0]])
        payload[parts[0]] = value
        return original
    if len(parts) != 3 or parts[0] not in PROFILE_SECTIONS or not parts[1].isdigit():
        raise ATSError("Choose a record field such as technical_skills.0.name.")
    if parts[2] in {"evidence", "claim_status", "uncertainty"}:
        raise ATSError("Original evidence and extraction uncertainty cannot be overwritten.")
    try:
        row = payload[parts[0]][int(parts[1])]
        if parts[2] not in row:
            raise KeyError(parts[2])
        original = copy.deepcopy(row[parts[2]])
        row[parts[2]] = value
    except (IndexError, KeyError, TypeError):
        raise ATSError("Unknown profile field. Refresh the profile before correcting it.") from None
    return original


class CandidateProfileService:
    def __init__(self, db):
        self.db = db

    def list(self, vacancy_id: int | None = None, document_id: int | None = None):
        with self.db.transaction() as session:
            query = (
                select(CandidateProfile)
                .join(CandidateDocument)
                .join(JobApplication, CandidateDocument.application_id == JobApplication.id)
                .where(JobApplication.status == "Uploaded")
                .options(
                    selectinload(CandidateProfile.candidate),
                    selectinload(CandidateProfile.document),
                    selectinload(CandidateProfile.analysis_run),
                    selectinload(CandidateProfile.evidence),
                    selectinload(CandidateProfile.reviews),
                )
                .order_by(CandidateProfile.extraction_version.desc(), CandidateProfile.id.desc())
            )
            if vacancy_id is not None:
                query = query.where(JobApplication.vacancy_id == vacancy_id)
            if document_id is not None:
                query = query.where(CandidateProfile.document_id == document_id)
            return list(session.scalars(query))

    def get(self, profile_id: int):
        with self.db.transaction() as session:
            profile = session.scalar(
                select(CandidateProfile)
                .where(CandidateProfile.id == profile_id)
                .options(
                    selectinload(CandidateProfile.candidate),
                    selectinload(CandidateProfile.document),
                    selectinload(CandidateProfile.analysis_run),
                    selectinload(CandidateProfile.evidence),
                    selectinload(CandidateProfile.reviews),
                )
            )
            if profile is None or profile.document.application.status != "Uploaded":
                raise ATSError("Profile is no longer available for this application.")
            return profile

    @staticmethod
    def effective(profile: CandidateProfile) -> dict:
        payload = copy.deepcopy(profile.original_extraction)
        for event in profile.reviews:
            if event.field_path != "__validation__":
                apply_value(payload, event.field_path, event.corrected_value)
        return payload

    @staticmethod
    def status(profile: CandidateProfile) -> str:
        if profile.reviews:
            return (
                "COMPLETED"
                if profile.reviews[-1].field_path == "__validation__"
                else "NEEDS_REVIEW"
            )
        return profile.analysis_status

    def source(self, profile_id: int) -> ExtractedText:
        profile = self.get(profile_id)
        with self.db.transaction() as session:
            row = session.get(DocumentTextExtraction, profile.text_extraction_id)
            if row is None:
                raise ATSError("Source text is no longer available. Refresh the application.")
            return ExtractedText.deserialize(row.source)

    def correct(self, profile_id: int, correction: ReviewInput, expected_review_count: int):
        profile = self.get(profile_id)
        if len(profile.reviews) != expected_review_count:
            raise ATSError("Profile review changed. Refresh before saving your correction.")
        current = self.effective(profile)
        original = apply_value(current, correction.field_path, correction.corrected_value)
        # Validate the entire corrected profile without changing its immutable AI payload.
        CVExtraction.model_validate_json(json.dumps(current))
        with self.db.transaction() as session:
            row = session.get(CandidateProfile, profile_id)
            if row is None or len(row.reviews) != expected_review_count:
                raise ATSError("Profile review changed. Refresh and retry.")
            session.add(
                ProfileReviewEvent(
                    profile_id=profile_id,
                    field_path=correction.field_path,
                    original_value=original,
                    corrected_value=correction.corrected_value,
                    reviewer=correction.reviewer,
                    note=correction.note,
                )
            )

    def validate(
        self,
        profile_id: int,
        reviewer: str | None,
        note: str,
        acknowledge_issues: bool,
        expected_review_count: int,
    ):
        profile = self.get(profile_id)
        if not note.strip() or len(note) > 2000 or (reviewer and len(reviewer) > 200):
            raise ATSError(
                "Enter a review note (1–2000 characters) and reviewer up to 200 characters."
            )
        unresolved = (
            profile.warnings
            or profile.reviews
            or any(e.verification_status != "VERIFIED" for e in profile.evidence)
        )
        if unresolved and not acknowledge_issues:
            raise ATSError(
                "Explicitly acknowledge the warnings and evidence issues before validation."
            )
        with self.db.transaction() as session:
            row = session.get(CandidateProfile, profile_id)
            if row is None or len(row.reviews) != expected_review_count:
                raise ATSError("Profile review changed. Refresh and retry.")
            session.add(
                ProfileReviewEvent(
                    profile_id=profile_id,
                    field_path="__validation__",
                    original_value=None,
                    corrected_value={"validated": True, "issues_acknowledged": acknowledge_issues},
                    reviewer=reviewer or None,
                    note=note.strip(),
                )
            )
