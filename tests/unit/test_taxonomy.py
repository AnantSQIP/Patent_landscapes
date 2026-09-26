"""Scope files and taxonomy drafting (TEST-ONLY scripted model replies; real CPC excerpt)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel

from patsquire_plr.classification.cpc import CpcScheme, parse_title_list
from patsquire_plr.config import ModelRole
from patsquire_plr.gateway.gateway import StructuredResult
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.landscape.scope import Scope, ScopeError, load_scope
from patsquire_plr.landscape.taxonomy import (
    CpcSelection,
    TaxonomyContent,
    TaxonomyDraft,
    TaxonomyError,
    content_from_edit,
    draft_taxonomy,
    export_yaml,
    segment_id,
)
from tests.unit.test_cpc import title_list_zip


@pytest.fixture(scope="module")
def scheme() -> CpcScheme:
    return parse_title_list(title_list_zip())


# ------------------------------------------------------------------ scope


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "scope.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_scope_file_is_read_and_numbers_normalised(tmp_path: Path) -> None:
    scope = load_scope(
        _write(
            tmp_path,
            "topic: Test topic\ndate_from: 2017-01-01\ndate_to: 2024-12-31\n"
            "countries: [us, ep]\nseeds: ['US 10,452,978 B2']\n"
            "known_relevant: [US10452978B2]\nconfirmed_by: A. Reviewer\n",
        )
    )
    assert scope.countries == ("US", "EP")
    assert scope.seeds == ("US10452978B2",)
    assert scope.notes is None
    assert len(scope.sha256) == 64


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- a list", "expected a mapping"),
        ("topic: x: y: z", "scope.yaml"),
        ("topic: Topic\ndate_from: 2020-01-01\ndate_to: 2019-01-01\n", "date_from is after"),
        ("topic: Topic\ndate_from: 2020-01-01\ndate_to: 2021-01-01\nseeds: [junk]\n", "junk"),
        (
            "topic: Topic\ndate_from: 2020-01-01\ndate_to: 2021-01-01\ncountries: [USA]\n",
            "two-letter",
        ),
        (
            "topic: Topic\ndate_from: 2020-01-01\ndate_to: 2021-01-01\ncountries: [US, us]\n",
            "repeats",
        ),
        (
            "topic: Topic\ndate_from: 2020-01-01\ndate_to: 2021-01-01\nknown_relevant: [US1B1]\n",
            "needs confirmed_by",
        ),
        ("topic: Topic\ndate_from: 2020-01-01\n", "date_to"),
    ],
)
def test_invalid_scope_files_fail_with_the_reason(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ScopeError, match=message):
        load_scope(_write(tmp_path, text))


def test_scope_needs_normalised_numbers_when_built_directly() -> None:
    with pytest.raises(ValueError, match="not a normalised publication number"):
        _scope(seeds=("US 1 B1",))


def _scope(**overrides: object) -> Scope:
    values: dict[str, object] = {
        "topic": "Test topic",
        "notes": None,
        "date_from": date(2017, 1, 1),
        "date_to": date(2024, 12, 31),
        "countries": (),
        "cpc_hints": (),
        "seeds": (),
        "known_relevant": (),
        "confirmed_by": None,
        **overrides,
    }
    return Scope.model_validate(values)


# ------------------------------------------------------------------ drafting


class ScriptedModel:
    """TEST-ONLY stand-in for the model gateway: replies are scripted per prompt ID."""

    def __init__(self, replies: Mapping[str, list[BaseModel]]) -> None:
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def structured[T: BaseModel](
        self,
        role: ModelRole,
        prompt: PromptTemplate,
        variables: Mapping[str, str],
        output_model: type[T],
        *,
        use_cache: bool = True,
    ) -> StructuredResult[T]:
        prompt.render(variables)  # the variables must fit the template exactly
        self.calls.append((prompt.id, dict(variables)))
        value = self.replies[prompt.id].pop(0)
        assert isinstance(value, output_model)
        return StructuredResult(
            value=value,
            cached=False,
            backend="test",
            model="test",
            cache_key=f"key{len(self.calls)}",
        )


def _draft(*segments: Mapping[str, object]) -> TaxonomyDraft:
    return TaxonomyDraft.model_validate({"segments": list(segments)})


NETWORKS: dict[str, object] = {
    "name": "Neural network architectures",
    "definition": "Network architectures used for language models.",
    "includes": ["encoder-decoder networks", " "],
    "excludes": ["hardware"],
    "keywords": [
        {"term": "neural network", "synonyms": ["Neural Network", "encoder-decoder", "a (b)"]},
        {"term": "x", "synonyms": []},
    ],
}
LANGUAGE: dict[str, object] = {
    "name": "Natural language processing",
    "definition": "Processing and generation of natural language text.",
    "includes": [],
    "excludes": [],
    "keywords": [{"term": "natural language", "synonyms": []}],
}


def test_draft_uses_the_model_for_language_and_code_for_cpc(scheme: CpcScheme) -> None:
    model = ScriptedModel(
        {
            "taxonomy.draft": [_draft(NETWORKS, LANGUAGE)],
            "taxonomy.cpc_select": [
                CpcSelection.model_validate(
                    {
                        "picks": [
                            {"symbol": "G06N3/02", "reason": "neural networks"},
                            {"symbol": "G06N3/02", "reason": "again"},
                            {"symbol": "G06N9/99", "reason": "invented"},
                            {"symbol": "G06N3/0455", "reason": "over the limit"},
                        ]
                    }
                ),
                CpcSelection.model_validate({"picks": []}),
            ],
        }
    )

    content, calls = draft_taxonomy(
        model,
        scheme,
        _scope(cpc_hints=("G06N3/00", "G06F40/00")),
        max_segments=4,
        candidates_per_segment=10,
        max_cpc_per_segment=1,
    )

    assert calls == ["key1", "key2", "key3"]
    networks, language = content.spec.segments
    assert (networks.id, language.id) == (
        "neural_network_architectures",
        "natural_language_processing",
    )
    assert networks.includes == ("encoder-decoder networks",)
    assert [k.term for k in networks.keywords] == ["neural network"]
    assert networks.keywords[0].synonyms == ("encoder-decoder",)  # case-duplicate dropped
    assert networks.cpc == ("G06N3/02",)
    assert content.cpc["neural_network_architectures"][0].title_path.endswith("Neural networks")
    assert language.cpc == ()
    assert content.suggestions["natural_language_processing"]  # candidates stay visible
    assert {(r.kind, r.value, r.problem) for r in content.rejected} == {
        (
            "term",
            "a (b)",
            "not usable as a search term: contains query-syntax characters "
            "(quotes, brackets, wildcards, operators)",
        ),
        ("term", "x", "not usable as a search term: must be 2 to 80 characters"),
        ("cpc", "G06N3/02", "chosen twice"),
        ("cpc", "G06N9/99", "not in the candidate list offered to the model"),
        ("cpc", "G06N3/0455", "over the limit of 1 codes per segment"),
    }
    # Candidates were limited to the hinted subclasses and offered with their titles.
    offered = model.calls[1][1]["candidates"]
    assert all(line.split(":")[0].startswith(("G06N", "G06F")) for line in offered.splitlines())


def test_segments_without_cpc_candidates_skip_the_selection_call(scheme: CpcScheme) -> None:
    lonely: dict[str, object] = {
        **LANGUAGE,
        "name": "Other",
        "keywords": [{"term": "zzzz qqqq", "synonyms": []}],
    }
    repeated = {**lonely, "name": " OTHER "}
    other = {**lonely, "name": "Other!"}
    model = ScriptedModel({"taxonomy.draft": [_draft(lonely, other, repeated)]})
    content, calls = draft_taxonomy(
        model, scheme, _scope(), max_segments=4, candidates_per_segment=10, max_cpc_per_segment=3
    )
    assert calls == ["key1"]
    assert [s.id for s in content.spec.segments] == ["other", "other_2"]
    assert [(r.kind, r.segment_id, r.value) for r in content.rejected] == [
        ("cpc_search", "other", "Other"),
        ("cpc_search", "other_2", "Other!"),
        ("segment", "other", " OTHER "),
    ]


def test_draft_segment_count_and_usability_are_enforced(scheme: CpcScheme) -> None:
    model = ScriptedModel({"taxonomy.draft": [_draft(LANGUAGE)]})
    with pytest.raises(TaxonomyError, match="drafted 1 distinct segments; expected 2 to 4"):
        draft_taxonomy(
            model, scheme, _scope(), max_segments=4, candidates_per_segment=5, max_cpc_per_segment=3
        )
    unusable: dict[str, object] = {
        **LANGUAGE,
        "name": "Unusable",
        "keywords": [{"term": "x", "synonyms": []}],
    }
    model = ScriptedModel({"taxonomy.draft": [_draft(unusable, LANGUAGE)]})
    with pytest.raises(TaxonomyError, match="is not usable"):
        draft_taxonomy(
            model, scheme, _scope(), max_segments=4, candidates_per_segment=5, max_cpc_per_segment=3
        )


def test_unknown_cpc_hints_fail(scheme: CpcScheme) -> None:
    model = ScriptedModel({"taxonomy.draft": [_draft(LANGUAGE, NETWORKS)]})
    with pytest.raises(TaxonomyError, match="cpc_hints"):
        draft_taxonomy(
            model,
            scheme,
            _scope(cpc_hints=("G99Z1/00",)),
            max_segments=4,
            candidates_per_segment=5,
            max_cpc_per_segment=3,
        )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Model training", "model_training"),
        ("5G & LLMs", "s_5g_llms"),
        ("Ünïcode", "unicode"),
        ("!!", "segment"),
    ],
)
def test_segment_ids(name: str, expected: str) -> None:
    assert segment_id(name, set()) == expected


# ------------------------------------------------------------------ editing


def _drafted(scheme: CpcScheme) -> TaxonomyContent:
    model = ScriptedModel(
        {
            "taxonomy.draft": [_draft(NETWORKS, LANGUAGE)],
            "taxonomy.cpc_select": [
                CpcSelection.model_validate({"picks": [{"symbol": "G06N3/02", "reason": "r"}]}),
                CpcSelection.model_validate({"picks": []}),
            ],
        }
    )
    content, _ = draft_taxonomy(
        model, scheme, _scope(), max_segments=4, candidates_per_segment=10, max_cpc_per_segment=3
    )
    return content


def test_export_and_reimport_round_trip(scheme: CpcScheme) -> None:
    content = _drafted(scheme)
    text = export_yaml(content)
    assert "# Suggested CPC entries for natural_language_processing (not chosen):" in text
    assert "# Proposed by the model but rejected:" in text

    edited = content_from_edit(text.replace("- G06N3/02", "- g06n 3/045"), scheme, previous=content)

    assert edited.spec.segments[0].cpc == ("G06N3/045",)
    assert edited.cpc["neural_network_architectures"][0].symbol == "G06N3/045"
    assert edited.rejected == content.rejected  # the audit trail is carried forward
    assert content_from_edit(text, scheme, previous=content).spec == content.spec


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("- G06N3/02", "- G06N3/99999"), "invalid CPC codes"),
        (("id: natural_language_processing", "id: neural_network_architectures"), "unique"),
        (("topic:", "topics:"), "edited taxonomy is invalid"),
        (("- G06N3/02", "- G06"), "is a CPC class"),
        (("term: neural network", "term: 'neural\\nnetwork'"), "edited taxonomy is invalid"),
    ],
)
def test_invalid_edits_fail(scheme: CpcScheme, change: tuple[str, str], message: str) -> None:
    content = _drafted(scheme)
    with pytest.raises(TaxonomyError, match=message):
        content_from_edit(export_yaml(content).replace(*change), scheme, previous=content)


def test_synonyms_over_the_limit_are_recorded(scheme: CpcScheme) -> None:
    many = {
        **LANGUAGE,
        "name": "Many",
        "keywords": [
            {"term": "natural language", "synonyms": [f"synonym {i:02}" for i in range(14)]}
        ],
    }
    model = ScriptedModel({"taxonomy.draft": [_draft(many, NETWORKS)], "taxonomy.cpc_select": []})
    model.replies["taxonomy.cpc_select"] = [
        CpcSelection.model_validate({"picks": []}),
        CpcSelection.model_validate({"picks": []}),
    ]
    content, _ = draft_taxonomy(
        model, scheme, _scope(), max_segments=4, candidates_per_segment=5, max_cpc_per_segment=3
    )
    assert len(content.spec.segments[0].keywords[0].synonyms) == 12
    dropped = [r.value for r in content.rejected if "over the limit of 12 synonyms" in r.problem]
    assert dropped == ["synonym 12", "synonym 13"]


@pytest.mark.parametrize(
    ("term", "message"),
    [
        (" padded", "leading or trailing spaces"),
        ("--", "no letters or digits"),
        ("a" * 81, "2 to 80"),
    ],
)
def test_keyword_terms_are_checked(term: str, message: str) -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    from patsquire_plr.landscape.taxonomy import KeywordGroup  # noqa: PLC0415

    with pytest.raises(ValidationError, match=message):
        KeywordGroup(term=term, synonyms=())
    with pytest.raises(ValidationError, match=message):
        KeywordGroup(term="valid term", synonyms=(term,))


def test_edits_dedupe_codes_and_return_removed_codes_to_suggestions(scheme: CpcScheme) -> None:
    from patsquire_plr.landscape.taxonomy import base_version_of  # noqa: PLC0415

    content = _drafted(scheme)
    text = export_yaml(content, base_version="00000000-0000-0000-0000-000000000001")
    assert base_version_of(text) == "00000000-0000-0000-0000-000000000001"
    assert base_version_of(export_yaml(content)) is None

    doubled = content_from_edit(
        text.replace("- G06N3/02", "- G06N3/02\n  - g06n 3/02"), scheme, previous=content
    )
    assert doubled.spec.segments[0].cpc == ("G06N3/02",)

    removed = content_from_edit(text.replace("  - G06N3/02\n", ""), scheme, previous=content)
    assert removed.spec.segments[0].cpc == ()
    assert "G06N3/02" in {c.symbol for c in removed.suggestions["neural_network_architectures"]}


def test_scope_refuses_two_kinds_of_one_publication(tmp_path: Path) -> None:
    with pytest.raises(ScopeError, match="more than once"):
        load_scope(
            _write(
                tmp_path,
                "topic: Topic\ndate_from: 2020-01-01\ndate_to: 2021-01-01\n"
                "seeds: [EP1234567A1, EP1234567B1]\n",
            )
        )
