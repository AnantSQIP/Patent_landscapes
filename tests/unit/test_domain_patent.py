from __future__ import annotations

import typing
import uuid
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from patsquire_plr.domain.patent import (
    CANONICAL_OPTIONAL_FIELDS,
    RECORD_FIELD_PATHS,
    CitedReference,
    ClassificationCode,
    MissingReason,
    NormalizationError,
    Party,
    PatentDocument,
    Priority,
    normalize_classification_code,
    normalize_publication_number,
)
from patsquire_plr.template.spec import RecordField

# ---------------------------------------------------------------- builders (TEST-ONLY data)

ALL_MISSING = dict.fromkeys(CANONICAL_OPTIONAL_FIELDS, MissingReason.NOT_PROVIDED_BY_SOURCE)


def _bare(**overrides: object) -> PatentDocument:
    """A document where every optional field is explicitly missing, plus ``overrides``."""
    missing = {k: v for k, v in ALL_MISSING.items() if k not in overrides}
    values: dict[str, object] = dict.fromkeys(CANONICAL_OPTIONAL_FIELDS)
    values.update(overrides)
    return PatentDocument(
        raw_record_id=uuid.UUID(int=1),
        source_id="test_source",
        publication=normalize_publication_number("US 10,123,456 B2"),
        publication_number_raw="US 10,123,456 B2",
        missing=missing,
        **values,  # type: ignore[arg-type]
    )


def _party(role: str, seq: int, country: str | None = "US") -> Party:
    return Party(
        role=role,  # type: ignore[arg-type]
        sequence=seq,
        name_raw=f"{role} {seq}",
        country=country,
        country_missing=None if country else MissingReason.NOT_PROVIDED_BY_SOURCE,
    )


# ---------------------------------------------------------------- publication numbers


@pytest.mark.parametrize(
    ("raw", "country", "number", "kind"),
    [
        ("US 10,123,456 B2", "US", "10123456", "B2"),
        ("US10123456B2", "US", "10123456", "B2"),
        ("EP-1234567-A1", "EP", "1234567", "A1"),
        ("WO 2020/123456 A1", "WO", "2020123456", "A1"),
        ("us 2020/0123456 a1", "US", "20200123456", "A1"),
        ("JP2020123456A", "JP", "2020123456", "A"),
        ("USRE45678E", "US", "RE45678", "E"),
        ("USD890123S", "US", "D890123", "S"),
        ("CN112345678", "CN", "112345678", None),
    ],
)
def test_publication_numbers_normalise(
    raw: str, country: str, number: str, kind: str | None
) -> None:
    result = normalize_publication_number(raw)

    assert (result.country, result.number, result.kind) == (country, number, kind)
    assert result.text == f"{country}{number}{kind or ''}"


@pytest.mark.parametrize("raw", ["", "12345", "USABC", "U1234", "unknown"])
def test_unrecognised_publication_numbers_raise(raw: str) -> None:
    with pytest.raises(NormalizationError, match="publication number"):
        normalize_publication_number(raw)


@given(
    country=st.sampled_from(["US", "EP", "WO", "CN", "JP", "KR", "DE"]),
    number=st.integers(min_value=1, max_value=10**11).map(str),
    kind=st.sampled_from([None, "A", "A1", "B1", "B2", "U"]),
    separator=st.sampled_from(["", " ", "-", "/", "."]),
)
def test_separators_never_change_the_result(
    country: str, number: str, kind: str | None, separator: str
) -> None:
    spelled = separator.join([country, number, kind or ""]).rstrip(separator)

    result = normalize_publication_number(spelled)

    assert (result.country, result.number, result.kind) == (country, number, kind)


# ---------------------------------------------------------------- classification codes


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("G06N 3/08", "G06N3/08"),
        ("g06n3/0455", "G06N3/0455"),
        ("Y02E 10/727", "Y02E10/727"),
        ("H04L 0009/08", "H04L9/08"),
    ],
)
def test_classification_codes_normalise(raw: str, code: str) -> None:
    assert normalize_classification_code(raw) == code


@pytest.mark.parametrize("raw", ["G06N", "G06N3", "Z99A1/00", "G06N 3/", "G06N 3/0"])
def test_partial_or_invalid_classification_codes_raise(raw: str) -> None:
    with pytest.raises(NormalizationError, match="CPC/IPC"):
        normalize_classification_code(raw)


@given(st.sampled_from(["G06N3/08", "H01M10/0525", "Y02E10/727", "A61K31/00"]))
def test_classification_normalisation_is_idempotent(code: str) -> None:
    assert normalize_classification_code(normalize_classification_code(code)) == code


# ---------------------------------------------------------------- the value-or-reason rule


def test_every_field_missing_with_reasons_is_valid() -> None:
    document = _bare()

    assert document.filing_office == "US"
    assert set(document.missing) == set(CANONICAL_OPTIONAL_FIELDS)


def test_none_without_reason_is_rejected() -> None:
    with pytest.raises(ValidationError, match="title is None without a missing reason"):
        PatentDocument(
            raw_record_id=uuid.UUID(int=1),
            source_id="s",
            publication=normalize_publication_number("US1B1"),
            publication_number_raw="US1B1",
            missing={k: v for k, v in ALL_MISSING.items() if k != "title"},
            **dict.fromkeys(CANONICAL_OPTIONAL_FIELDS),
        )


def test_value_and_reason_together_are_rejected() -> None:
    with pytest.raises(ValidationError, match="title has a value and a missing reason"):
        PatentDocument(
            raw_record_id=uuid.UUID(int=1),
            source_id="s",
            publication=normalize_publication_number("US1B1"),
            publication_number_raw="US1B1",
            missing=ALL_MISSING,
            **{**dict.fromkeys(CANONICAL_OPTIONAL_FIELDS), "title": "A title"},  # type: ignore[arg-type]
        )


def test_empty_list_is_a_real_value_meaning_none_exist() -> None:
    document = _bare(backward_citations=())

    assert document.backward_citations == ()
    assert "backward_citations" not in document.missing


def test_unknown_missing_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PatentDocument(
            raw_record_id=uuid.UUID(int=1),
            source_id="s",
            publication=normalize_publication_number("US1B1"),
            publication_number_raw="US1B1",
            missing={**ALL_MISSING, "colour": MissingReason.NOT_APPLICABLE},  # type: ignore[dict-item]
            **dict.fromkeys(CANONICAL_OPTIONAL_FIELDS),
        )


def test_documents_are_immutable() -> None:
    document = _bare()
    with pytest.raises(ValidationError, match="frozen"):
        document.source_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------- parts


def test_party_country_needs_value_or_reason() -> None:
    assert (
        _party("applicant", 1, country=None).country_missing is MissingReason.NOT_PROVIDED_BY_SOURCE
    )
    with pytest.raises(ValidationError, match="country is None without a missing reason"):
        Party(role="applicant", sequence=1, name_raw="A", country=None, country_missing=None)


def test_party_roles_and_sequences_are_checked() -> None:
    ok = _bare(applicants=(_party("applicant", 1), _party("applicant", 2)))
    assert ok.applicants is not None
    with pytest.raises(ValidationError, match="role is not applicant"):
        _bare(applicants=(_party("inventor", 1),))
    with pytest.raises(ValidationError, match=r"sequences must be 1\.\.n"):
        _bare(applicants=(_party("applicant", 2),))


def test_classification_scheme_must_match_field() -> None:
    ipc_code = ClassificationCode(scheme="ipc", code="G06N3/08", code_raw="G06N 3/08")
    with pytest.raises(ValidationError, match="cpc contains codes of another scheme"):
        _bare(cpc=(ipc_code,))


def test_citation_shapes() -> None:
    CitedReference(
        kind="npl",
        publication_number=None,
        publication_number_raw=None,
        npl_text="Smith 2020",
        origin="examiner",
    )
    with pytest.raises(ValidationError, match="patent citation needs"):
        CitedReference(
            kind="patent",
            publication_number=None,
            publication_number_raw=None,
            npl_text=None,
            origin="unknown",
        )
    with pytest.raises(ValidationError, match="NPL citation needs"):
        CitedReference(
            kind="npl",
            publication_number="US1B1",
            publication_number_raw="US1B1",
            npl_text="x",
            origin="unknown",
        )


def test_earliest_priority_must_match_priority_list() -> None:
    priorities = (
        Priority(number_raw="US 62/000,001", country="US", date=date(2019, 5, 1)),
        Priority(number_raw="GB 1900001", country="GB", date=date(2019, 1, 2)),
    )
    assert _bare(priorities=priorities, earliest_priority_date=date(2019, 1, 2)).priorities
    with pytest.raises(ValidationError, match="differs from the earliest listed priority"):
        _bare(priorities=priorities, earliest_priority_date=date(2019, 5, 1))


def test_publication_cannot_precede_filing() -> None:
    with pytest.raises(ValidationError, match="publication_date is before filing_date"):
        _bare(filing_date=date(2021, 1, 1), publication_date=date(2020, 1, 1))


# ---------------------------------------------------------------- consistency with the template


def test_every_template_record_field_maps_to_the_canonical_model() -> None:
    assert set(RECORD_FIELD_PATHS) == set(typing.get_args(RecordField))


def test_family_members_cannot_list_the_document_itself_or_repeat() -> None:
    with pytest.raises(ValidationError, match="must not list the document itself"):
        _bare(family_members=("US10123456B2",))
    with pytest.raises(ValidationError, match="contains repeats"):
        _bare(family_members=("EP1A1", "EP1A1"))
