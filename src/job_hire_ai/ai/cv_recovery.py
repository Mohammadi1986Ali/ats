"""Bounded source splitting; never repair or accept a truncated JSON prefix."""

from dataclasses import dataclass


@dataclass
class CallBudget:
    remaining: int

    def take(self):
        if self.remaining <= 0:
            from job_hire_ai.ai.provider import ProviderError

            raise ProviderError("CV model-call limit reached. No incomplete profile was saved.")
        self.remaining -= 1


def split_segments(segments: list[dict]) -> list[list[dict]]:
    """Split near a section/paragraph boundary, or sentence/word for one large paragraph."""
    if len(segments) > 1:
        total = sum(len(s["text"]) for s in segments)
        candidates = list(range(1, len(segments)))
        headings = [i for i in candidates if segments[i].get("kind") == "heading"]
        index = min(
            headings or candidates,
            key=lambda i: abs(sum(len(s["text"]) for s in segments[:i]) - total / 2),
        )
        return [segments[:index], segments[index:]]
    if not segments:
        return []
    text = segments[0]["text"]
    if len(text) < 80:
        return []  # Retry compactly rather than splitting away meaning from a tiny source.
    import re

    positions = [m.end() for m in re.finditer(r"\n|(?<=[.!?])\s+", text)]
    positions = [i for i in positions if 0 < i < len(text)]
    if not positions:
        positions = [m.end() for m in re.finditer(r"\s+", text) if m.end() < len(text)]
    if not positions:
        return []
    cut = min(positions, key=lambda i: abs(i - len(text) / 2))
    return [[{**segments[0], "text": part.strip()}] for part in (text[:cut], text[cut:])]
