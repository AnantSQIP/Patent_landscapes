"""Similarity scores and prototypes (ADR 0011).

Scores are cosine similarities of embedding vectors, rounded half-even to 4 decimals and
held as ``Decimal``. Every threshold comparison is therefore exact and reproducible, and
the same embeddings always give the same decisions.

Prototypes describe what "on topic" means, in texts the embedding model can compare:
* the topic and its notes (``topic``);
* each segment's name, definition, includes and keywords (``segment:<id>``);
* the text of each publication a person confirmed as relevant (``known:<publication>``).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from patsquire_plr.errors import PlrError
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.taxonomy import SegmentSpec

FOUR_PLACES = Decimal("0.0001")


def cosine(a: Sequence[float], b: Sequence[float]) -> Decimal:
    if len(a) != len(b):
        raise PlrError(f"vectors of different sizes: {len(a)} and {len(b)}")
    norm = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b))
    if norm == 0:
        raise PlrError("cannot compare a zero vector")
    value = math.fsum(x * y for x, y in zip(a, b, strict=True)) / norm
    return Decimal(repr(value)).quantize(FOUR_PLACES, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Prototype:
    name: str  # "topic", "segment:<id>" or "known:<publication>"
    text: str


def shared_terms(segments: Sequence[SegmentSpec]) -> set[str]:
    """Terms every segment lists (case-insensitive), e.g. the topic's own keywords. They say
    nothing about which segment a family belongs to."""
    sets = [{t.casefold() for t in s.terms()} for s in segments]
    return set.intersection(*sets) if len(sets) > 1 else set()


def segment_prototype_text(segment: SegmentSpec, shared: AbstractSet[str] = frozenset()) -> str:
    """A segment in words the embedding can compare, without the terms all segments share."""
    terms = "; ".join(t for t in segment.terms() if t.casefold() not in shared)
    includes = "; ".join(segment.includes)
    return f"{segment.name}: {segment.definition} Includes: {includes}. Keywords: {terms}."


def relevance_prototypes(
    scope: Scope, segments: Sequence[SegmentSpec], known_texts: dict[str, str]
) -> list[Prototype]:
    topic = f"{scope.topic}. {scope.notes}" if scope.notes else scope.topic
    return [
        Prototype("topic", topic),
        *(Prototype(f"segment:{s.id}", segment_prototype_text(s)) for s in segments),
        *(Prototype(f"known:{p}", t) for p, t in sorted(known_texts.items())),
    ]


def in_sample(seed: int, key: str, rate: Decimal) -> bool:
    """A fixed pseudo-random choice: the same seed, key and rate always give the same
    answer, and about ``rate`` of all keys are chosen."""
    digest = hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
    return Decimal(int(digest[:12], 16)) / Decimal(16**12) < rate
