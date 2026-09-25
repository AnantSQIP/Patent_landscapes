"""Canonical patent record: the one shape every data-source adapter must produce.

Rules (build prompt §6, principle 1):

* Every canonical field either has a value or is listed in ``missing`` with a reason. A
  value of ``None`` without a reason, or a value *and* a reason, is a validation error. No
  default ever stands in for data.
* An empty list is a real value meaning "the source states there are none" (e.g. no
  citations). "The source does not say" is ``None`` plus a missing reason.
* Normalised identifiers keep their original text next to them (``*_raw``).
* Every document names the raw record it came from, so every value traces back to an
  immutable source payload.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Literal, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patsquire_plr.errors import PlrError

COUNTRY_CODE = r"^[A-Z]{2}$"  # WIPO ST.3 two-letter office/country codes (incl. EP, WO)


class NormalizationError(PlrError):
    """An identifier could not be normalised; the record must be quarantined, not guessed."""


class MissingReason(StrEnum):
    NOT_PROVIDED_BY_SOURCE = "not_provided_by_source"
    NOT_APPLICABLE = "not_applicable"  # e.g. grant_date of an unexamined application
    UNPARSEABLE = "unparseable"  # present in the source but could not be read reliably
    NOT_REQUESTED = "not_requested"  # the adapter did not fetch it (e.g. claims)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# ------------------------------------------------------------------ identifiers

_PUB_NUMBER = re.compile(r"^(?P<country>[A-Z]{2})(?P<number>[0-9A-Z]+?)(?P<kind>[A-Z][0-9]?)?$")


class PublicationNumber(_Strict):
    """``country`` + ``number`` + optional ``kind``, e.g. US 10123456 B2."""

    country: str = Field(pattern=COUNTRY_CODE)
    number: str = Field(pattern=r"^[0-9A-Z]+$")
    kind: str | None = Field(pattern=r"^[A-Z][0-9]?$")

    @property
    def text(self) -> str:
        return f"{self.country}{self.number}{self.kind or ''}"


def normalize_publication_number(raw: str) -> PublicationNumber:
    """Parse common publication-number spellings, e.g. ``US 10,123,456 B2`` or ``EP-1234567-A1``.

    Separators (space, comma, hyphen, slash, dot) are removed. The kind code is the trailing
    letter with an optional digit, and only exists if the number part ends in a digit before
    it. Anything else raises ``NormalizationError``. The number is never guessed or
    zero-padded.
    """
    compact = re.sub(r"[\s,\-/.]", "", raw.strip().upper())
    match = _PUB_NUMBER.fullmatch(compact)
    if match is None or not any(c.isdigit() for c in match["number"]):
        raise NormalizationError(f"unrecognised publication number: {raw!r}")
    number, kind = match["number"], match["kind"]
    if kind is not None and not number[-1].isdigit():
        raise NormalizationError(f"ambiguous kind code in publication number: {raw!r}")
    return PublicationNumber(country=match["country"], number=number, kind=kind)


_CPC = re.compile(
    r"^(?P<section>[A-HY])(?P<cls>\d{2})(?P<sub>[A-Z])(?P<group>\d{1,4})/(?P<subgroup>\d{2,6})$"
)


def normalize_classification_code(raw: str) -> str:
    """``G06N 3/08`` -> ``G06N3/08``. Only full group/subgroup codes are accepted."""
    compact = re.sub(r"\s+", "", raw.strip().upper())
    match = _CPC.fullmatch(compact)
    if match is None:
        raise NormalizationError(f"unrecognised CPC/IPC code: {raw!r}")
    group = str(int(match["group"]))  # "0003" -> "3": leading zeros are formatting, not data
    return f"{match['section']}{match['cls']}{match['sub']}{group}/{match['subgroup']}"


# ------------------------------------------------------------------ parts of a record


class Party(_Strict):
    """An applicant or inventor exactly as the source gives it."""

    role: Literal["applicant", "inventor"]
    sequence: int = Field(ge=1, description="1-based order in the source")
    name_raw: str = Field(min_length=1)
    country: str | None = Field(pattern=COUNTRY_CODE)
    country_missing: MissingReason | None

    @model_validator(mode="after")
    def _country_xor_reason(self) -> Self:
        _check_value_xor_reason("country", self.country, self.country_missing)
        return self


class Priority(_Strict):
    number_raw: str = Field(min_length=1)
    country: str = Field(pattern=COUNTRY_CODE)
    date: date


class ClassificationCode(_Strict):
    scheme: Literal["cpc", "ipc"]
    code: str = Field(description="normalised, e.g. G06N3/08")
    code_raw: str = Field(min_length=1)


class CitedReference(_Strict):
    """A backward citation: either a patent publication or non-patent literature (NPL)."""

    kind: Literal["patent", "npl"]
    publication_number: str | None = Field(description="normalised text, patent citations only")
    publication_number_raw: str | None
    npl_text: str | None
    origin: Literal["examiner", "applicant", "third_party", "unknown"]

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        if self.kind == "patent":
            if self.publication_number is None or self.publication_number_raw is None:
                raise ValueError("a patent citation needs publication_number and its raw text")
            if self.npl_text is not None:
                raise ValueError("a patent citation cannot carry npl_text")
        elif self.npl_text is None or self.publication_number is not None:
            raise ValueError("an NPL citation needs npl_text and no publication_number")
        return self


class LegalStatus(_Strict):
    category: Literal[
        "pending", "granted", "lapsed", "expired", "withdrawn", "refused", "revoked", "other"
    ]
    status_raw: str = Field(min_length=1, description="the source's own wording, kept verbatim")
    as_of: date


class ForwardCitations(_Strict):
    """Publications citing this one, as reported by the source on ``as_of``."""

    citing_publication_numbers: tuple[str, ...]
    as_of: date


# ------------------------------------------------------------------ the record

# Canonical optional fields: each must be either set (not None) or listed in ``missing``.
CanonicalField = Literal[
    "application_number_raw",
    "family_id_simple",
    "family_id_extended",
    "earliest_priority_date",
    "priorities",
    "filing_date",
    "publication_date",
    "grant_date",
    "title",
    "abstract",
    "claims",
    "language",
    "applicants",
    "inventors",
    "cpc",
    "ipc",
    "legal_status",
    "backward_citations",
    "forward_citations",
]
CANONICAL_OPTIONAL_FIELDS: tuple[CanonicalField, ...] = get_args(CanonicalField)


class PatentDocument(_Strict):
    """One publication as delivered by one source."""

    raw_record_id: UUID
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="configured source name")
    publication: PublicationNumber
    publication_number_raw: str = Field(min_length=1)

    application_number_raw: str | None
    family_id_simple: str | None
    family_id_extended: str | None
    earliest_priority_date: date | None
    priorities: tuple[Priority, ...] | None
    filing_date: date | None
    publication_date: date | None
    grant_date: date | None
    title: str | None
    abstract: str | None
    claims: str | None
    language: str | None = Field(pattern=r"^[a-z]{2}$")
    applicants: tuple[Party, ...] | None
    inventors: tuple[Party, ...] | None
    cpc: tuple[ClassificationCode, ...] | None
    ipc: tuple[ClassificationCode, ...] | None
    legal_status: LegalStatus | None
    backward_citations: tuple[CitedReference, ...] | None
    forward_citations: ForwardCitations | None

    missing: dict[CanonicalField, MissingReason]

    @property
    def filing_office(self) -> str:
        """The authority that published this document (WIPO ST.3 code)."""
        return self.publication.country

    @model_validator(mode="after")
    def _every_field_present_or_explained(self) -> Self:
        problems = []
        for name in CANONICAL_OPTIONAL_FIELDS:
            value = getattr(self, name)
            reason = self.missing.get(name)
            if value is None and reason is None:
                problems.append(f"{name} is None without a missing reason")
            elif value is not None and reason is not None:
                problems.append(f"{name} has a value and a missing reason ({reason})")
        problems += self._role_and_scheme_problems()
        problems += self._date_order_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _role_and_scheme_problems(self) -> list[str]:
        problems = []
        for field_name, role in (("applicants", "applicant"), ("inventors", "inventor")):
            parties: tuple[Party, ...] | None = getattr(self, field_name)
            if parties is None:
                continue
            if any(p.role != role for p in parties):
                problems.append(f"{field_name} contains a party whose role is not {role}")
            if [p.sequence for p in parties] != list(range(1, len(parties) + 1)):
                problems.append(f"{field_name} sequences must be 1..n in order")
        for scheme in ("cpc", "ipc"):
            codes: tuple[ClassificationCode, ...] | None = getattr(self, scheme)
            if codes is not None and any(c.scheme != scheme for c in codes):
                problems.append(f"{scheme} contains codes of another scheme")
        return problems

    def _date_order_problems(self) -> list[str]:
        problems = []
        if self.priorities and self.earliest_priority_date is not None:
            earliest = min(p.date for p in self.priorities)
            if earliest != self.earliest_priority_date:
                problems.append(
                    f"earliest_priority_date {self.earliest_priority_date} differs from the "
                    f"earliest listed priority {earliest}"
                )
        if (
            self.filing_date is not None
            and self.publication_date is not None
            and self.publication_date < self.filing_date
        ):
            problems.append("publication_date is before filing_date")
        return problems


# Template ``RecordField`` names (template/plr_template.yaml) -> where the value lives here.
# A test asserts this covers exactly the template's RecordField literal.
RECORD_FIELD_PATHS: dict[str, str] = {
    "publication_number": "publication",
    "application_number": "application_number_raw",
    "family_id": "family_id_simple | family_id_extended (per the family_definition option)",
    "kind_code": "publication.kind",
    "filing_office": "publication.country",
    "priority_date": "earliest_priority_date",
    "priority_country": "priorities[].country (earliest)",
    "filing_date": "filing_date",
    "publication_date": "publication_date",
    "title": "title",
    "abstract": "abstract",
    "claims": "claims",
    "applicants": "applicants",
    "applicant_countries": "applicants[].country",
    "inventors": "inventors",
    "inventor_countries": "inventors[].country",
    "cpc": "cpc",
    "ipc": "ipc",
    "legal_status": "legal_status",
    "grant_date": "grant_date",
    "backward_citations": "backward_citations",
    "forward_citations": "forward_citations",
    "npl_citations": "backward_citations[kind=npl]",
}


def _check_value_xor_reason(name: str, value: object, reason: MissingReason | None) -> None:
    if value is None and reason is None:
        raise ValueError(f"{name} is None without a missing reason")
    if value is not None and reason is not None:
        raise ValueError(f"{name} has a value and a missing reason ({reason})")
