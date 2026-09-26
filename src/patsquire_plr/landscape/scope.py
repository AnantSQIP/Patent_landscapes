"""The scope of a landscape study: what the user asked for (pipeline step 1, ADR 0010).

The scope is written as a small YAML file and stored with its SHA-256 as the first record of
a landscape, so everything later (taxonomy, queries, counts) traces back to it.

* ``date_from`` / ``date_to`` bound the **publication date** used in search queries. This is
  the one date field every supported provider can search (EPO OPS has no priority-date
  index; see docs/architecture/query_syntax.md). Report statistics use priority dates later.
* ``countries`` are WIPO ST.3 office codes (``EP``, ``WO`` included); empty means all.
* ``seeds`` are publications to start citation expansion from.
* ``known_relevant`` are publications a person has confirmed as on-topic. The recall check
  reports which of them the search finds. They must be confirmed by a named person
  (``confirmed_by``); the system never treats its own suggestions as confirmed.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from patsquire_plr.canonical import canonical_sha256
from patsquire_plr.domain.patent import (
    COUNTRY_CODE,
    NormalizationError,
    normalize_publication_number,
)
from patsquire_plr.errors import PlrError


class ScopeError(PlrError):
    """The scope file is invalid."""


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    topic: str = Field(min_length=3, max_length=300)
    notes: str | None = Field(
        max_length=4000, description="optional context for the taxonomy draft"
    )
    date_from: date
    date_to: date
    countries: tuple[str, ...] = Field(description="WIPO ST.3 codes; empty = all offices")
    cpc_hints: tuple[str, ...] = Field(description="CPC codes the user expects to be relevant")
    seeds: tuple[str, ...] = Field(description="normalised publication numbers")
    known_relevant: tuple[str, ...] = Field(description="normalised publication numbers")
    confirmed_by: str | None = Field(description="who confirmed known_relevant as on-topic")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        bad = [c for c in self.countries if not _is_country(c)]
        if bad:
            raise ValueError(f"countries must be two-letter WIPO ST.3 codes: {bad}")
        if len(set(self.countries)) != len(self.countries):
            raise ValueError("countries contains repeats")
        for name in ("seeds", "known_relevant"):
            numbers: tuple[str, ...] = getattr(self, name)
            if len(set(numbers)) != len(numbers):
                raise ValueError(f"{name} contains repeats")
            for number in numbers:
                if normalize_publication_number(number).text != number:
                    raise ValueError(f"{name}: {number!r} is not a normalised publication number")
        if self.known_relevant and not self.confirmed_by:
            raise ValueError("known_relevant needs confirmed_by (the person who confirmed them)")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def _is_country(code: str) -> bool:
    return re.fullmatch(COUNTRY_CODE, code) is not None


def load_scope(path: Path) -> Scope:
    """Read a scope YAML file. Publication numbers are normalised; anything invalid fails."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScopeError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScopeError(f"{path}: expected a mapping")
    try:
        values = {
            "notes": None,
            "countries": (),
            "cpc_hints": (),
            "seeds": (),
            "known_relevant": (),
            "confirmed_by": None,
            **raw,
        }
        for key in ("seeds", "known_relevant"):
            values[key] = tuple(normalize_publication_number(str(n)).text for n in values[key])
        for key in ("countries", "cpc_hints"):
            values[key] = tuple(str(v).strip().upper() for v in values[key])
        return Scope.model_validate(values, strict=False)
    except (ValidationError, NormalizationError, TypeError) as exc:
        raise ScopeError(f"{path}: {exc}") from exc
