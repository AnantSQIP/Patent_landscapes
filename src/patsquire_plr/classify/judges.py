"""Model judges for relevance and segments (ADR 0011), and the checks on their answers.

A judge must quote its evidence **verbatim** from the patent text. An answer whose evidence
is not in the text is not used; the reason is recorded. Confidence is a word (low, medium,
high) rather than a number, because models do not produce calibrated numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.landscape.taxonomy import SegmentSpec

Confidence = Literal["low", "medium", "high"]
CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
# A quote must carry meaning: fragments such as "ing" or "the" occur in almost any text.
MIN_EVIDENCE_CHARS = 15


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RelevanceJudgement(_Strict):
    relevant: bool
    confidence: Confidence
    evidence: str
    reason: str


class SegmentAssignment(_Strict):
    segment_id: str
    evidence: str


class SegmentJudgement(_Strict):
    assignments: list[SegmentAssignment]


RELEVANCE_PROMPT = PromptTemplate(
    id="relevance.judge",
    version="1",
    system=(
        "You are a patent analyst screening patents for a patent landscape. Decide whether "
        "the patent belongs to the landscape's topic, using only the patent text given. "
        "A patent belongs if its invention is about the topic or applies it in an essential "
        "way; a passing mention does not count. Quote the evidence exactly as it appears "
        "in the patent text (a short phrase copied character for character). Reply only "
        "with JSON matching the schema."
    ),
    user=(
        "Landscape topic: {topic}\n"
        "Scope: {scope}\n"
        "Segments of the landscape:\n{segments}\n\n"
        "Patent text:\n{text}\n\n"
        "Is this patent within the landscape? Give relevant (true/false), confidence "
        "(low, medium or high), evidence (an exact quote from the patent text) and a "
        "one-sentence reason."
    ),
)

SEGMENT_PROMPT = PromptTemplate(
    id="segments.classify",
    version="2",
    system=(
        "You are a patent analyst assigning a patent to the segments of a landscape. A "
        "patent may belong to several segments or to none. Decide from the PATENT TEXT "
        "only. For each segment you assign, the evidence must be a phrase copied word for "
        "word from the PATENT TEXT (never from the segment list, never your own words). "
        "Use only the segment ids given. Reply only with JSON matching the schema."
    ),
    user=(
        "SEGMENT LIST (id: name - definition; includes; excludes):\n{segments}\n\n"
        "PATENT TEXT (quote evidence only from here):\n<<<\n{text}\n>>>\n\n"
        "List the segments this patent clearly belongs to. For each, copy a phrase of "
        "about 5 to 15 words from the PATENT TEXT as evidence. Return an empty list if none "
        "fits."
    ),
)


def segment_lines(segments: Sequence[SegmentSpec], *, detailed: bool) -> str:
    lines = []
    for s in segments:
        if detailed:
            lines.append(
                f"- {s.id}: {s.name} - {s.definition}; includes: {'; '.join(s.includes)}; "
                f"excludes: {'; '.join(s.excludes)}"
            )
        else:
            lines.append(f"- {s.name}: {s.definition}")
    return "\n".join(lines)


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


def evidence_problem(evidence: str, text: str, prompt_material: str = "") -> str | None:
    """Why the quoted evidence cannot be accepted, or None if it is in the text.
    ``prompt_material`` is other text the model was shown (e.g. the segment list); a quote
    from there is named as such, since it proves nothing about the patent."""
    quote = _normal(evidence).strip(" .\"'")
    if len(quote) < MIN_EVIDENCE_CHARS:
        return f"evidence quote is shorter than {MIN_EVIDENCE_CHARS} characters"
    if quote in _normal(text):
        return None
    if prompt_material and quote in _normal(prompt_material):
        return f"evidence quotes the segment list, not the patent: {evidence[:60]!r}"
    return f"evidence is not in the patent text: {evidence[:80]!r}"


def confident_enough(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_ORDER[confidence] >= CONFIDENCE_ORDER[minimum]


def unknown_segments(judgement: SegmentJudgement, segment_ids: set[str]) -> list[str]:
    return sorted({a.segment_id for a in judgement.assignments} - segment_ids)
