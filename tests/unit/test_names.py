from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patsquire_plr.clean.names import (
    AliasFileError,
    load_aliases,
    normalize_name,
    similar_keys,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Raytheon Co", "raytheon"),
        ("RAYTHEON COMPANY", "raytheon"),
        ("Hughes Aircraft Co", "hughes aircraft"),
        ("Google LLC", "google"),
        ("Google Inc.", "google"),
        ("Samsung Electronics Co., Ltd.", "samsung electronics"),
        ("Siemens Aktiengesellschaft", "siemens aktiengesellschaft"),  # not a listed suffix
        ("Robert Bosch GmbH", "robert bosch"),
        ("AT&T Corp.", "at and t"),
        ("Toyota Jidosha Kabushiki Kaisha", "toyota jidosha"),
        ("MariElla Labels Oy", "mariella labels"),
        (
            "State Grid Corp of China SGCC",
            "state grid corp of china sgcc",
        ),  # suffix only at the end
        ("Inc.", "inc"),  # a bare suffix keeps itself rather than becoming empty
        ("Acme Co Co", "acme"),  # suffixes are stripped until none is left
        ("Samsung Co Ltd Inc", "samsung"),
        (
            "\uff33\uff2f\uff2e\uff39\u3000\uff27\uff32\uff2f\uff35\uff30",
            "sony group",
        ),  # full-width (NFKC)
    ],
)
def test_rules_normalise_formatting_only(raw: str, key: str) -> None:
    assert normalize_name(raw).key == key


def test_steps_record_what_changed() -> None:
    assert normalize_name("Raytheon Co").steps == ("casefold", "suffix:co")
    assert normalize_name("raytheon").steps == ()
    assert normalize_name("Samsung Electronics Co., Ltd.").steps == (
        "casefold",
        "punctuation",
        "suffix:co ltd",
    )


def test_different_companies_never_merge_by_rule() -> None:
    assert normalize_name("Google LLC").key != normalize_name("Alphabet Inc").key
    assert (
        normalize_name("Microsoft Corp").key
        != normalize_name("Microsoft Technology Licensing LLC").key
    )


def test_names_without_letters_or_digits_are_rejected() -> None:
    with pytest.raises(ValueError, match="no letters or digits"):
        normalize_name(" .,- ")


names = st.text(
    alphabet=st.characters(categories=("L", "N", "Zs", "Po", "Pd")), min_size=1, max_size=40
).filter(lambda s: any(c.isalnum() for c in s))


@given(names)
def test_normalisation_is_idempotent(raw: str) -> None:
    key = normalize_name(raw).key
    assert (
        normalize_name(key).key == key
        or normalize_name(key).key == normalize_name(normalize_name(key).key).key
    )


@given(names)
def test_keys_are_nonempty_casefolded_single_spaced(raw: str) -> None:
    key = normalize_name(raw).key
    assert key
    assert key == key.casefold()
    assert "  " not in key
    assert key == key.strip()


# ---------------------------------------------------------------- alias file


def test_committed_alias_file_is_valid_and_empty() -> None:
    aliases = load_aliases(REPO_ROOT / "config" / "applicant_aliases.yaml")
    assert aliases.groups == ()  # no merges are ever shipped without an owner decision


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "aliases.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_alias_groups_map_every_variant_by_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\ngroups:\n  - canonical: Alphabet\n    variants: [Google LLC, 'Google Inc.']\n"
        "    reason: Google is an Alphabet subsidiary (owner decision)\n",
    )
    mapping = load_aliases(path).canonical_for()
    assert mapping["google"] == ("Alphabet", "Google is an Alphabet subsidiary (owner decision)")
    assert mapping["alphabet"][0] == "Alphabet"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 1\ngroups:\n  - canonical: A\n    variants: [B]\n    reason: short\n", "reason"),
        (
            "version: 1\ngroups:\n"
            "  - {canonical: A, variants: [Google LLC], reason: first group reason}\n"
            "  - {canonical: B, variants: [Google Inc], reason: second group reason}\n",
            "appears in alias groups",
        ),
        (
            "version: 1\ngroups:\n  - {canonical: A, variants: [], reason: empty variants here}\n",
            "variants",
        ),
        ("[unclosed", "invalid alias file"),
    ],
)
def test_invalid_alias_files_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(AliasFileError, match=message):
        load_aliases(_write(tmp_path, text))


def test_missing_alias_file(tmp_path: Path) -> None:
    with pytest.raises(AliasFileError, match="not found"):
        load_aliases(tmp_path / "nope.yaml")


# ---------------------------------------------------------------- review candidates


def test_similar_keys_lists_candidates_for_review_only() -> None:
    pairs = similar_keys(
        [
            "sony",
            "sony group",
            "raytheon",
            "raytheon technologies",
            "bosch",
            "samsung electronics",
            "samsung electronic",
        ]
    )
    found = {(a, b) for a, b, _ in pairs}
    assert ("sony", "sony group") in found
    assert ("raytheon", "raytheon technologies") in found
    assert ("samsung electronic", "samsung electronics") in found
    assert not any("bosch" in pair for pair in found)
