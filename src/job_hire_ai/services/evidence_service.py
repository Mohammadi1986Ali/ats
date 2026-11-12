import re
from dataclasses import asdict, dataclass
from uuid import uuid4

from job_hire_ai.ai.cv_schemas import EvidenceQuote
from job_hire_ai.text_extraction import ExtractedText, normalize_text


@dataclass
class VerifiedEvidence:
    evidence_id: str
    document_id: int
    source_excerpt: str
    source_reference: str | None
    page_number: int | None
    character_start: int | None
    character_end: int | None
    verification_status: str

    def serialize(self):
        return asdict(self)


def _fold(text: str) -> tuple[str, list[int]]:
    folded, positions = [], []
    for match in re.finditer(r"\S+", text):
        if folded:
            folded.append(" ")
            positions.append(match.start() - 1)
        folded.extend(match.group())
        positions.extend(range(match.start(), match.end()))
    return "".join(folded), positions


def verify_quote(quote: EvidenceQuote, source: ExtractedText, document_id: int) -> VerifiedEvidence:
    needle, _ = _fold(normalize_text(quote.source_excerpt))
    matches = []
    for segment in source.segments:
        if quote.source_reference and quote.source_reference != segment.reference:
            continue
        haystack, positions = _fold(segment.text)
        offset = 0
        while needle and (found := haystack.find(needle, offset)) >= 0:
            matches.append((segment, positions[found], positions[found + len(needle) - 1] + 1))
            offset = found + 1
    status = "VERIFIED" if len(matches) == 1 else "AMBIGUOUS" if matches else "UNVERIFIED"
    segment, start, end = matches[0] if len(matches) == 1 else (None, None, None)
    return VerifiedEvidence(
        uuid4().hex,
        document_id,
        quote.source_excerpt,
        segment.reference if segment else quote.source_reference,
        segment.page_number if segment else None,
        segment.character_start + start if segment else None,
        segment.character_start + end if segment else None,
        status,
    )


def claim_issues(claim) -> list[str]:
    """Conservative literal checks; these are flags, never semantic proof."""
    issues = []
    evidence = " ".join(quote.source_excerpt for quote in claim.evidence)
    source_numbers = set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)?%?(?!\w)", evidence))
    content = claim.model_dump(mode="json")
    for field in ("evidence", "claim_status", "uncertainty"):
        content.pop(field, None)
    values = str(content)
    if set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)?%?(?!\w)", values)) - source_numbers:
        issues.append("Numeric/date claim is not literally supported by its quoted evidence.")
    if hasattr(claim, "original_wording"):
        wording = " ".join(normalize_text(claim.original_wording).split())
        if wording not in " ".join(normalize_text(evidence).split()):
            issues.append("Skill original wording is absent from the cited evidence.")
    if hasattr(claim, "proficiency") and claim.proficiency:
        for level in re.findall(r"\b[A-C][12]\b", claim.proficiency):
            if level not in evidence:
                issues.append("CEFR level is not stated in the quoted evidence.")
    return issues
