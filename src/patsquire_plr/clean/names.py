"""Deterministic applicant name normalisation (build prompt §7).

Two layers, both fully logged:

1. **Rules** that only remove formatting, never meaning: Unicode NFKC, case folding, "&"
   to "and", punctuation to spaces, collapsed whitespace, and a trailing legal-form suffix
   (Inc, Corp, Co Ltd, GmbH, ...). "Raytheon Co" and "RAYTHEON COMPANY" become the same key;
   "Google" and "Alphabet" do not.
2. **An alias file** (``config/applicant_aliases.yaml``, versioned, human-curated) for real
   ownership or naming relations the rules cannot know. Every alias group must state a
   reason. No alias is ever inferred automatically; candidate merges are only *listed* for
   human review (``similar_keys``).
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from patsquire_plr.errors import PlrError

RULES_VERSION = "2"  # 2: combining marks kept (category-based symbol stripping)

# Legal-form words and phrases, matched as whole trailing tokens after punctuation removal.
# Longest first, so "co ltd" is removed as one suffix rather than leaving "co".
LEGAL_SUFFIXES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        (
            tuple(s.split())
            for s in (
                "inc", "incorporated", "corp", "corporation", "co", "company", "co ltd",
                "co limited", "ltd", "limited", "llc", "l l c", "plc", "gmbh", "gmbh co kg",
                "co kg", "kg", "ag", "sa", "s a", "spa", "s p a", "srl", "bv", "b v", "nv",
                "n v", "oy", "oyj", "ab", "as", "a s", "sas", "sarl", "pty ltd", "pty",
                "lp", "llp", "kk", "k k", "kabushiki kaisha", "se", "pte ltd", "pvt ltd",
                "private limited",
            )
        ),
        key=len,
        reverse=True,
    )
)  # fmt: skip


def _strip_symbols(text: str) -> str:
    """Punctuation, symbols and control characters become spaces. Letters, digits and
    combining marks (Unicode categories L, N, M; e.g. Devanagari vowel signs) are kept."""
    return "".join(c if unicodedata.category(c)[0] in "LNM" or c.isspace() else " " for c in text)


class AliasFileError(PlrError):
    """The alias file is malformed or contradicts itself."""


class NameKey(BaseModel):
    """The normalised form of one raw name, with the steps that produced it."""

    model_config = ConfigDict(frozen=True, strict=True)

    raw: str
    key: str = Field(min_length=1)
    steps: tuple[str, ...]


def normalize_name(raw: str) -> NameKey:
    """Apply the formatting rules. Never returns an empty key: a name that consists only of
    a legal suffix (e.g. "Inc.") keeps that suffix."""
    steps: list[str] = []
    text = unicodedata.normalize("NFKC", raw)
    folded = text.casefold()
    if folded != text:
        steps.append("casefold")
    replaced = folded.replace("&", " and ")
    if replaced != folded:
        steps.append("ampersand")
    stripped = _strip_symbols(replaced)
    if stripped != replaced:
        steps.append("punctuation")
    tokens = stripped.split()
    removed = True
    while removed:  # repeated, so normalising a key again never changes it
        removed = False
        for suffix in LEGAL_SUFFIXES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                tokens = tokens[: -len(suffix)]
                steps.append("suffix:" + " ".join(suffix))
                removed = True
                break
    key = " ".join(tokens) or " ".join(stripped.split())
    if not key:
        raise ValueError(f"name {raw!r} has no letters or digits")
    return NameKey(raw=raw, key=key, steps=tuple(steps))


class AliasGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonical: str = Field(min_length=1, description="display name for the merged entity")
    variants: tuple[str, ...] = Field(min_length=1, description="raw names merged into it")
    reason: str = Field(min_length=10, description="why these are one entity, and who decided")


class AliasFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: int = Field(ge=1)
    groups: tuple[AliasGroup, ...]

    @model_validator(mode="after")
    def _each_key_in_one_group(self) -> Self:
        owner: dict[str, str] = {}
        for group in self.groups:
            for name in (group.canonical, *group.variants):
                key = normalize_name(name).key
                if owner.setdefault(key, group.canonical) != group.canonical:
                    raise ValueError(
                        f"{name!r} (key {key!r}) appears in alias groups {owner[key]!r} and "
                        f"{group.canonical!r}"
                    )
        return self

    def canonical_for(self) -> dict[str, tuple[str, str]]:
        """normalised key -> (canonical display name, reason)."""
        mapping: dict[str, tuple[str, str]] = {}
        for group in self.groups:
            for name in (group.canonical, *group.variants):
                mapping[normalize_name(name).key] = (group.canonical, group.reason)
        return mapping


def load_aliases(path: Path) -> AliasFile:
    if not path.is_file():
        raise AliasFileError(f"alias file not found: {path}")
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        return AliasFile.model_validate_json(json.dumps(data))
    except (yaml.YAMLError, ValidationError) as exc:
        raise AliasFileError(f"invalid alias file {path}: {exc}") from exc


def similar_keys(keys: Iterable[str], *, threshold: float = 0.88) -> list[tuple[str, str, float]]:
    """Pairs of distinct keys that look alike, for **human review only** (never applied).

    A pair qualifies when one key's tokens start the other's (e.g. "sony" / "sony group"),
    or when their character similarity reaches ``threshold``.
    """
    ordered = sorted(set(keys))
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            ta, tb = a.split(), b.split()
            prefix = ta == tb[: len(ta)] or tb == ta[: len(tb)]
            ratio = SequenceMatcher(None, a, b).ratio()
            if prefix or ratio >= threshold:
                pairs.append((a, b, round(ratio, 3)))
    return pairs
