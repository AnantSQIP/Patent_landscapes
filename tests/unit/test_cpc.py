"""The official CPC title list (fixture: a real excerpt of CPC 2026.08, see its README)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from patsquire_plr.classification.cpc import (
    CpcScheme,
    CpcSchemeError,
    normalize_cpc_symbol,
    parse_title_list,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "cpc"


def title_list_zip(files: dict[str, str] | None = None) -> bytes:
    """The fixture excerpt (or ``files``) as a CPCTitleList archive."""
    contents = files or {p.name: p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.txt")}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in sorted(contents.items()):
            # A fixed timestamp keeps the archive byte-identical between calls.
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 8, 1, 0, 0, 0)), text)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def scheme() -> CpcScheme:
    return parse_title_list(title_list_zip())


def test_version_and_hierarchy(scheme: CpcScheme) -> None:
    assert scheme.version == "2026.08"
    entry = scheme.get("G06N3/0455")
    assert entry is not None
    assert (entry.kind, entry.dot_level, entry.parent) == ("subgroup", 4, "G06N3/045")
    assert [e.symbol for e in scheme.path("G06N3/0455")] == [
        "G",
        "G06",
        "G06N",
        "G06N3/00",
        "G06N3/02",
        "G06N3/04",
        "G06N3/045",
        "G06N3/0455",
    ]
    assert scheme.title_path("G06N3/0455") == (
        "Computing arrangements based on biological models > Neural networks > "
        "Architecture, e.g. interconnection topology > Combinations of networks > "
        "Auto-encoder networks; Encoder-decoder networks"
    )
    main = scheme.get("G06N3/00")
    assert main is not None
    assert (main.kind, main.parent) == ("main_group", "G06N")


def test_braces_mark_cpc_only_titles_and_are_dropped_from_paths(scheme: CpcScheme) -> None:
    braced = [s for s in ("G06N3/0409", "G06N3/0418") if scheme.get(s) is not None]
    assert braced
    for symbol in braced:
        entry = scheme.get(symbol)
        assert entry is not None
        assert entry.title.startswith("{")
        assert "{" not in scheme.title_path(symbol)


def test_check_accepts_real_codes_and_names_the_problem_otherwise(scheme: CpcScheme) -> None:
    ok = scheme.check("g06n 3/0455")
    assert (ok.symbol, ok.problem) == ("G06N3/0455", None)
    assert scheme.check("G06N").symbol == "G06N"
    missing = scheme.check("G06N3/99999")
    assert missing.symbol is None
    assert missing.problem == "G06N3/99999 is not in CPC version 2026.08"
    assert "unrecognised" in (scheme.check("not a code").problem or "")


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [("g", "G"), ("G06", "G06"), (" g06n ", "G06N"), ("G06N 3/08", "G06N3/08")],
)
def test_symbols_normalise_at_every_level(raw: str, symbol: str) -> None:
    assert normalize_cpc_symbol(raw) == symbol


def test_search_matches_whole_words_plurals_and_ignores_references(scheme: CpcScheme) -> None:
    by_symbol = {m.symbol: m for m in scheme.search(["neural network"], limit=500)}
    assert "G06N3/02" in by_symbol  # title "Neural networks"
    language = {m.symbol for m in scheme.search(["natural language"], limit=500)}
    assert "G06F40/00" in language
    # G06F40/10 mentions natural language only inside a reference to other places.
    assert "G06F40/10" not in language


def test_search_ranks_by_terms_matched_then_symbol(scheme: CpcScheme) -> None:
    results = scheme.search(["neural network", "recurrent"], within=["G06N"], limit=500)
    counts = [len(m.matched_terms) for m in results]
    assert counts == sorted(counts, reverse=True)
    assert results[0].matched_terms == ("neural network", "recurrent")
    assert all(m.symbol.startswith("G06N") for m in results)
    assert scheme.search(["   "]) == []


def test_descendants_and_membership(scheme: CpcScheme) -> None:
    below = scheme.descendants("G06N3/04")
    assert "G06N3/0455" in below
    assert "G06N3/04" not in below
    assert scheme.child_count("G06N3/04") > 0
    assert scheme.is_within("G06N3/0455", "G06N3/04") is True
    assert scheme.is_within("G06N3/0455", "G06F40/00") is False
    # Codes missing from the scheme are decided from their symbol only where that settles it.
    assert scheme.is_within("G99Z1/00", "G06N3/04") is False  # another subclass
    assert scheme.is_within("G06N5/99", "G06N3/04") is False  # another main group
    assert scheme.is_within("G06N3/9999", "G06N3/04") is None  # same main group: unknown
    assert scheme.is_within("G06N2003/99", "G06N3/04") is None  # its indexing mirror
    assert scheme.is_within("G06N3/9999", "G06N") is True  # inside a queried subclass


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"notes.txt": "x"}, "unexpected files"),
        (
            {"cpc-section-G_20260801.txt": "G\t\tPHYSICS\n", "cpc-section-H_20260101.txt": ""},
            "different dates",
        ),
        ({"cpc-section-G_20260801.txt": "G\tPHYSICS\n"}, "expected SYMBOL<TAB>LEVEL<TAB>TITLE"),
        ({"cpc-section-G_20260801.txt": "G06N3/02\t1\tNeural networks\n"}, "no parent group"),
        ({"cpc-section-G_20260801.txt": "G06N3/02\tx\tNeural\n"}, "is not a number"),
        (
            {"cpc-section-G_20260801.txt": "G06N3/00\t0\tBio\nG06N5/02\t1\tWrong group\n"},
            "no parent group",
        ),
        ({"cpc-section-G_20260801.txt": "G06N3/00\t0\tNo subclass above\n"}, "parent is not"),
        ({"cpc-section-G_20260801.txt": "G0\t\tBad\n"}, "not a section, class or subclass"),
        ({"cpc-section-G_20260801.txt": "G06N 3/00\t0\tSpaced\n"}, "not in normalised form"),
        ({"cpc-section-G_20260801.txt": "G06N3/0\t0\tShort\n"}, "unrecognised"),
        ({"cpc-section-G_20260801.txt": "G\t\tPHYSICS\nG\t\tPHYSICS\n"}, "repeats a symbol"),
    ],
)
def test_anything_unexpected_in_the_file_is_an_error(files: dict[str, str], message: str) -> None:
    with pytest.raises(CpcSchemeError, match=message):
        parse_title_list(title_list_zip(files))


def test_a_non_zip_is_an_error() -> None:
    with pytest.raises(CpcSchemeError, match="not a zip file"):
        parse_title_list(b"plain text")


def test_indexing_codes_sit_under_their_mirror_group() -> None:
    # Real lines from CPC 2026.08 (cpc-section-A): A01C2001/048 is listed under A01C1/04.
    lines = (
        "A\t\tHUMAN NECESSITIES\nA01\t\tAGRICULTURE\nA01C\t\tPLANTING\n"
        "A01C1/00\t0\tApparatus, or methods of use thereof, for testing or treating seed\n"
        "A01C1/04\t1\tArranging seed on carriers, e.g. on tapes, on cords\n"
        "A01C2001/048\t2\t{Machines}\n"
    )
    scheme = parse_title_list(title_list_zip({"cpc-section-A_20260801.txt": lines}))
    entry = scheme.get("A01C2001/048")
    assert entry is not None
    assert entry.parent == "A01C1/04"
    wrong = lines.replace("A01C2001/048", "A01C2002/048")
    with pytest.raises(CpcSchemeError, match="no parent group"):
        parse_title_list(title_list_zip({"cpc-section-A_20260801.txt": wrong}))


def test_search_terms_that_title_search_cannot_use_are_named() -> None:
    from patsquire_plr.classification.cpc import search_term_problems  # noqa: PLC0415

    assert search_term_problems(["c++", "--", "GPT-4", "Ascorbinsäure", "neural network"]) == {
        "c++": "contains characters that CPC title search ignores",
        "--": "has no letters or digits",
    }


def test_search_keeps_non_ascii_words_whole() -> None:
    lines = (
        "A\t\tHUMAN NECESSITIES\nA61\t\tMEDICAL\nA61K\t\tPREPARATIONS\n"
        "A61K31/00\t0\tMedicinal preparations containing Ascorbinsäure\n"
        "A61K31/01\t1\tContaining ure compounds\n"
    )
    scheme = parse_title_list(title_list_zip({"cpc-section-A_20260801.txt": lines}))
    assert [m.symbol for m in scheme.search(["Ascorbinsäure"])] == ["A61K31/00"]
    assert [m.symbol for m in scheme.search(["ure"])] == ["A61K31/01"]
    assert scheme.search(["c++"]) == []  # refused, not searched as "c"


def test_the_test_archive_is_deterministic() -> None:
    assert title_list_zip() == title_list_zip()
