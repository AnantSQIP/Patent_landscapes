"""Key strings: exact provider syntax and local evaluation (TEST-ONLY taxonomy and records)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from patsquire_plr.classification.cpc import CpcScheme, parse_title_list
from patsquire_plr.landscape.queries import (
    LocalMatcher,
    LogicalQuery,
    MatchRecord,
    QueryError,
    build_queries,
    expand_codes,
    to_bigquery_sql,
    to_epo_cql,
    to_lens_json,
)
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.taxonomy import (
    KeywordGroup,
    ResolvedCpc,
    SegmentSpec,
    TaxonomyContent,
    TaxonomySpec,
)
from tests.unit.test_cpc import title_list_zip


@pytest.fixture(scope="module")
def scheme() -> CpcScheme:
    return parse_title_list(title_list_zip())


def _scope(countries: tuple[str, ...] = ("US", "EP")) -> Scope:
    return Scope(
        topic="Test topic",
        notes=None,
        date_from=date(2017, 1, 1),
        date_to=date(2024, 12, 31),
        countries=countries,
        cpc_hints=(),
        seeds=(),
        known_relevant=(),
        confirmed_by=None,
    )


def _segment(sid: str, cpc: tuple[str, ...]) -> SegmentSpec:
    return SegmentSpec(
        id=sid,
        name=sid.title(),
        definition="A test segment definition.",
        includes=(),
        excludes=(),
        keywords=(KeywordGroup(term="language model", synonyms=("LLM", "Language Model")),),
        cpc=cpc,
    )


def _content(scheme: CpcScheme, *segments: SegmentSpec) -> TaxonomyContent:
    return TaxonomyContent(
        spec=TaxonomySpec(topic="Test topic", segments=segments),
        cpc_scheme_version=scheme.version,
        cpc={
            s.id: tuple(ResolvedCpc(symbol=c, title_path=scheme.title_path(c)) for c in s.cpc)
            for s in segments
        },
        suggestions={s.id: () for s in segments},
        rejected=(),
    )


def _query(**overrides: object) -> LogicalQuery:
    values: dict[str, object] = {
        "phrases": ("language model", "LLM"),
        "cpc": ("G06N3/045", "G06F40/00"),
        "date_from": date(2017, 1, 1),
        "date_to": date(2024, 12, 31),
        "offices": ("US", "EP"),
        **overrides,
    }
    return LogicalQuery.model_validate(values)


def test_each_segment_gets_keyword_cpc_and_combined_parts(scheme: CpcScheme) -> None:
    content = _content(scheme, _segment("models", ("G06N3/045",)), _segment("plain", ()))
    queries = build_queries(content, _scope(), scheme)

    assert [(q.segment_id, q.part) for q in queries] == [
        ("models", "keywords"),
        ("models", "cpc"),
        ("models", "combined"),
        ("plain", "keywords"),
    ]
    combined = queries[2].logical
    assert combined.phrases == ("language model", "LLM")  # case-insensitive de-duplication
    assert combined.cpc == ("G06N3/045",)
    assert queries[1].logical.phrases is None
    assert set(queries[0].rendered) == {"epo_ops_cql", "lens_json", "bigquery_sql", "local"}


def test_scheme_version_must_match_the_taxonomy(scheme: CpcScheme) -> None:
    content = _content(scheme, _segment("models", ())).model_copy(
        update={"cpc_scheme_version": "2025.01"}
    )
    with pytest.raises(QueryError, match=r"taxonomy uses CPC 2025\.01"):
        build_queries(content, _scope(), scheme)


def test_epo_cql() -> None:
    assert to_epo_cql(_query()) == (
        '(ta="language model" or ta="LLM") and (cpc=/low G06N3/045 or cpc=/low G06F40/00) '
        'and pd within "20170101 20241231" and (pn=US or pn=EP)'
    )
    assert to_epo_cql(_query(phrases=None, cpc=("G06N3/045",), offices=())) == (
        'cpc=/low G06N3/045 and pd within "20170101 20241231"'
    )


def test_lens_json_lists_every_code_below_each_chosen_code(scheme: CpcScheme) -> None:
    body = json.loads(to_lens_json(_query(cpc=("G06N3/045",)), scheme))
    must = body["query"]["bool"]["must"]
    assert body["size"] == 0
    assert must[0]["bool"]["should"][:2] == [
        {"match_phrase": {"title": "language model"}},
        {"match_phrase": {"abstract": "language model"}},
    ]
    codes = must[1]["terms"]["class_cpc.symbol"]
    assert codes[0] == "G06N3/045"
    assert set(codes) == {"G06N3/045", *scheme.descendants("G06N3/045")}
    assert must[2] == {"range": {"date_published": {"gte": "2017-01-01", "lte": "2024-12-31"}}}
    assert must[3] == {"terms": {"jurisdiction": ["US", "EP"]}}


def test_bigquery_sql(scheme: CpcScheme) -> None:
    sql = to_bigquery_sql(
        _query(cpc=("G06N", "G06N3/045"), phrases=("large-language model",)), scheme
    )
    assert sql.startswith(
        "SELECT COUNT(DISTINCT p.publication_number) AS matches\n"
        "FROM `patents-public-data.patents.publications` AS p\n"
        "WHERE p.publication_date BETWEEN 20170101 AND 20241231\n"
        "  AND p.country_code IN ('US', 'EP')\n"
    )
    assert r"r'\b(?:large[\s\-]+language[\s\-]+model)\b'" in sql
    assert "UNNEST(p.title_localized)" in sql
    assert "UNNEST(p.abstract_localized)" in sql
    assert "SUBSTR(c.code, 1, 4) = 'G06N'" in sql
    assert "'G06N3/0455'" in sql  # a code below G06N3/045, listed explicitly


def test_values_that_are_not_safe_in_sql_are_refused(scheme: CpcScheme) -> None:
    with pytest.raises(QueryError, match="cannot be placed in SQL"):
        to_bigquery_sql(_query(phrases=("it's",)), scheme)


def test_expand_codes_refuses_unknown_codes(scheme: CpcScheme) -> None:
    assert expand_codes(["G06N3/0455"], scheme) == ["G06N3/0455"]
    with pytest.raises(QueryError, match="not in CPC"):
        expand_codes(["G06N3/99999"], scheme)


def _record(**overrides: object) -> MatchRecord:
    values: dict[str, object] = {
        "publication": "US1B2",
        "office": "US",
        "publication_date": date(2020, 5, 1),
        "text": "Fine-tuning a Large-Language   Model for dialogue",
        "cpc": ("G06N3/0455",),
        **overrides,
    }
    return MatchRecord(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "matched", "undecidable"),
    [
        ({}, True, None),
        ({"office": "CN"}, False, None),
        ({"publication_date": date(2016, 1, 1)}, False, None),
        ({"text": "A modelling language for circuits"}, False, None),
        ({"text": "LLMs"}, False, None),  # literal: no stemming locally
        ({"cpc": ("G06F40/40",)}, True, None),  # below G06F40/00
        ({"cpc": ("G10L15/183",)}, False, None),
        ({"publication_date": None}, False, "no publication date"),
        ({"text": ""}, False, "no title or abstract text"),
        ({"cpc": ()}, False, "no CPC codes"),
        ({"cpc": ("G99Z1/00",)}, False, "CPC codes not in 2026.08"),
        ({"publication_date": None, "office": "CN"}, False, None),  # a definite no decides
        ({"publication_date": None, "text": "unrelated"}, False, None),
    ],
)
def test_local_evaluation(
    scheme: CpcScheme, overrides: dict[str, object], matched: bool, undecidable: str | None
) -> None:
    query = _query(phrases=("large language model",), cpc=("G06N3/045", "G06F40/00"))
    outcome = LocalMatcher(query, scheme).evaluate(_record(**overrides))
    assert (outcome.matched, outcome.undecidable) == (matched, undecidable)


def test_local_evaluation_without_text_or_code_conditions(scheme: CpcScheme) -> None:
    only_codes = LocalMatcher(_query(phrases=None, offices=()), scheme)
    assert only_codes.evaluate(_record(text="", office="CN")).matched is True
    only_text = LocalMatcher(_query(cpc=None, phrases=("dialogue",)), scheme)
    assert only_text.evaluate(_record(cpc=())).matched is True
