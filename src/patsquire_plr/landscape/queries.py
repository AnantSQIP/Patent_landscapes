"""Key strings: one logical query per segment, rendered by code into each provider's syntax.

The logical query is always the same shape (ADR 0010):

    (any keyword or synonym in title or abstract)
    AND (any chosen CPC code, including everything below it)
    AND publication date in the scope's range
    AND (published by one of the scope's offices, if any)

Each segment gets three parts, each of which is counted:

* ``keywords``: the text condition only;
* ``cpc``: the classification condition only;
* ``combined``: both. **This is the search.** The other two are diagnostics that show how
  much each condition contributes.

Provider syntax follows the official documentation; the sources are in
docs/architecture/query_syntax.md. Wherever a documented form was not available, the
closest documented building block is used and the gap is listed there.

The ``local`` evaluator applies the same logical query in code to records already stored
(e.g. those found by citation expansion). Its text match is literal: whole words,
case-insensitive, with hyphens and spaces treated alike and no stemming. Provider engines
stem, so the same query can match slightly more there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from patsquire_plr.canonical import canonical_json
from patsquire_plr.classification.cpc import CpcScheme
from patsquire_plr.errors import PlrError
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.taxonomy import TaxonomyContent

GENERATOR_VERSION = "1"
Part = Literal["keywords", "cpc", "combined"]
Provider = Literal["epo_ops_cql", "lens_json", "bigquery_sql", "local"]
PROVIDERS: tuple[Provider, ...] = ("epo_ops_cql", "lens_json", "bigquery_sql", "local")
PARTS: tuple[Part, ...] = ("keywords", "cpc", "combined")
BIGQUERY_TABLE = "patents-public-data.patents.publications"
_SUBCLASS = re.compile(r"^[A-HY]\d{2}[A-Z]$")


class QueryError(PlrError):
    """A query cannot be generated exactly."""


class LogicalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phrases: tuple[str, ...] | None = Field(description="any of, in title or abstract")
    cpc: tuple[str, ...] | None = Field(description="any of, each including all entries below")
    date_from: date
    date_to: date
    offices: tuple[str, ...] = Field(description="WIPO ST.3 codes; empty means all")


@dataclass(frozen=True)
class GeneratedQuery:
    segment_id: str
    part: Part
    logical: LogicalQuery
    rendered: dict[Provider, str]


def build_queries(
    content: TaxonomyContent, scope: Scope, scheme: CpcScheme
) -> list[GeneratedQuery]:
    if scheme.version != content.cpc_scheme_version:
        raise QueryError(
            f"taxonomy uses CPC {content.cpc_scheme_version} but scheme {scheme.version} was given"
        )
    generated = []
    for segment in content.spec.segments:
        phrases = tuple(segment.terms())
        codes = tuple(segment.cpc)
        for part in PARTS:
            if part != "keywords" and not codes:
                continue  # a segment without codes has only its keyword query
            logical = LogicalQuery(
                phrases=phrases if part != "cpc" else None,
                cpc=codes if part != "keywords" else None,
                date_from=scope.date_from,
                date_to=scope.date_to,
                offices=scope.countries,
            )
            generated.append(
                GeneratedQuery(
                    segment_id=segment.id,
                    part=part,
                    logical=logical,
                    rendered={p: render(logical, p, scheme) for p in PROVIDERS},
                )
            )
    return generated


def render(query: LogicalQuery, provider: Provider, scheme: CpcScheme) -> str:
    if provider == "epo_ops_cql":
        return to_epo_cql(query)
    if provider == "lens_json":
        return to_lens_json(query, scheme)
    if provider == "bigquery_sql":
        return to_bigquery_sql(query, scheme)
    return canonical_json(query.model_dump(mode="json"))


# ------------------------------------------------------------------ EPO OPS (CQL)


def to_epo_cql(query: LogicalQuery) -> str:
    """OPS v3.2 CQL: ``ta`` (title or abstract), ``cpc=/low`` (entry and everything below),
    ``pd within "yyyyMMdd yyyyMMdd"``, and office via the ``pn`` country prefix."""
    clauses = []
    if query.phrases:
        clauses.append(_or(f'ta="{p}"' for p in query.phrases))
    if query.cpc:
        clauses.append(_or(f"cpc=/low {c}" for c in query.cpc))
    clauses.append(f'pd within "{query.date_from:%Y%m%d} {query.date_to:%Y%m%d}"')
    if query.offices:
        clauses.append(_or(f"pn={o}" for o in query.offices))
    return " and ".join(clauses)


def _or(items: Iterable[str]) -> str:
    parts = list(items)
    return parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")"


# ------------------------------------------------------------------ Lens (JSON query DSL)


def to_lens_json(query: LogicalQuery, scheme: CpcScheme) -> str:
    """Lens patent search body: ``match_phrase`` on ``title``/``abstract``, ``terms`` on
    ``class_cpc.symbol`` with every entry below each code listed explicitly (Lens documents
    no hierarchy operator), ``range`` on ``date_published``, ``terms`` on ``jurisdiction``.
    ``size: 0`` asks for the total only."""
    must: list[object] = []
    if query.phrases:
        must.append(
            {
                "bool": {
                    "should": [
                        {"match_phrase": {field: phrase}}
                        for phrase in query.phrases
                        for field in ("title", "abstract")
                    ]
                }
            }
        )
    if query.cpc:
        must.append({"terms": {"class_cpc.symbol": expand_codes(query.cpc, scheme)}})
    must.append(
        {
            "range": {
                "date_published": {
                    "gte": query.date_from.isoformat(),
                    "lte": query.date_to.isoformat(),
                }
            }
        }
    )
    if query.offices:
        must.append({"terms": {"jurisdiction": list(query.offices)}})
    return canonical_json({"query": {"bool": {"must": must}}, "size": 0})


def expand_codes(codes: Sequence[str], scheme: CpcScheme) -> list[str]:
    """Each code and every group below it, de-duplicated, in scheme order per code."""
    expanded: dict[str, None] = {}
    for code in codes:
        entry = scheme.get(code)
        if entry is None:
            raise QueryError(f"{code} is not in CPC {scheme.version}")
        if entry.dot_level is not None:
            expanded[code] = None
        expanded.update(dict.fromkeys(d for d in scheme.descendants(code) if "/" in d))
    return list(expanded)


# ------------------------------------------------------------------ Google BigQuery (SQL)


def to_bigquery_sql(query: LogicalQuery, scheme: CpcScheme) -> str:
    """A count over ``patents-public-data.patents.publications``. Dates are INT64 YYYYMMDD;
    text is matched with RE2 on lower-cased ``title_localized`` / ``abstract_localized``.

    The SQL is text for a person or a later executor to run. Every value placed in it is
    checked first: office codes are two capital letters, CPC symbols come from the scheme,
    and terms cannot contain quotes or backslashes (``TERM`` in taxonomy.py).
    """
    _check_sql_safe(query)
    where = [
        f"p.publication_date BETWEEN {query.date_from:%Y%m%d} AND {query.date_to:%Y%m%d}",
    ]
    if query.offices:
        offices = ", ".join(f"'{o}'" for o in query.offices)
        where.append(f"p.country_code IN ({offices})")
    if query.phrases:
        pattern = _bigquery_pattern(query.phrases)
        where.append(
            "("  # noqa: S608 - values checked by _check_sql_safe
            + " OR ".join(
                f"EXISTS (SELECT 1 FROM UNNEST(p.{field}) AS t "  # noqa: S608
                f"WHERE REGEXP_CONTAINS(LOWER(t.text), r'{pattern}'))"
                for field in ("title_localized", "abstract_localized")
            )
            + ")"
        )
    if query.cpc:
        subclasses = [c for c in query.cpc if _SUBCLASS.fullmatch(c)]
        groups = expand_codes([c for c in query.cpc if not _SUBCLASS.fullmatch(c)], scheme)
        conditions = [f"SUBSTR(c.code, 1, 4) = '{s}'" for s in subclasses]
        if groups:
            listed = ", ".join(f"'{g}'" for g in groups)
            conditions.append(f"c.code IN ({listed})")
        where.append(f"EXISTS (SELECT 1 FROM UNNEST(p.cpc) AS c WHERE {' OR '.join(conditions)})")  # noqa: S608
    return (
        "SELECT COUNT(DISTINCT p.publication_number) AS matches\n"
        f"FROM `{BIGQUERY_TABLE}` AS p\n"
        "WHERE " + "\n  AND ".join(where)
    )


def _check_sql_safe(query: LogicalQuery) -> None:
    unsafe = [
        v
        for v in (*query.offices, *(query.cpc or ()), *(query.phrases or ()))
        if re.search(r"['\"\\`]", v)
    ]
    if unsafe or any(not re.fullmatch(r"[A-Z]{2}", o) for o in query.offices):
        raise QueryError(f"values cannot be placed in SQL safely: {unsafe or query.offices}")


def _bigquery_pattern(phrases: Sequence[str]) -> str:
    return r"\b(?:" + "|".join(_phrase_regex(p) for p in phrases) + r")\b"


def _phrase_regex(phrase: str) -> str:
    """Words of a phrase, lower-cased and escaped, separated by spaces or hyphens."""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.lower().strip()) if w]
    return r"[\s\-]+".join(words)


# ------------------------------------------------------------------ local evaluation


@dataclass(frozen=True)
class MatchRecord:
    """The fields a query looks at, for one stored publication."""

    publication: str
    office: str
    publication_date: date | None
    text: str  # title and abstract
    cpc: tuple[str, ...]


@dataclass(frozen=True)
class LocalOutcome:
    matched: bool
    undecidable: str | None  # why the record could not be evaluated, if it could not


class LocalMatcher:
    """Applies a logical query to stored records, exactly as documented above."""

    def __init__(self, query: LogicalQuery, scheme: CpcScheme) -> None:
        self._query = query
        self._scheme = scheme
        self._pattern = (
            re.compile(
                r"(?<![\w])(?:" + "|".join(_phrase_regex(p) for p in query.phrases) + r")(?![\w])"
            )
            if query.phrases
            else None
        )

    def evaluate(self, record: MatchRecord) -> LocalOutcome:
        """A definite "no" from any condition decides; otherwise a condition that cannot be
        evaluated (missing date, text or known codes) makes the record undecidable."""
        if self._query.offices and record.office not in self._query.offices:
            return LocalOutcome(matched=False, undecidable=None)
        verdicts = (self._date(record), self._text(record), self._codes(record))
        if any(v is False for v, _ in verdicts):
            return LocalOutcome(matched=False, undecidable=None)
        reasons = [r for v, r in verdicts if v is None and r is not None]
        if reasons:
            return LocalOutcome(matched=False, undecidable="; ".join(reasons))
        return LocalOutcome(matched=True, undecidable=None)

    def _date(self, record: MatchRecord) -> tuple[bool | None, str | None]:
        if record.publication_date is None:
            return None, "no publication date"
        return self._query.date_from <= record.publication_date <= self._query.date_to, None

    def _text(self, record: MatchRecord) -> tuple[bool | None, str | None]:
        if self._pattern is None:
            return True, None
        if not record.text.strip():
            return None, "no title or abstract text"
        return self._pattern.search(record.text.lower()) is not None, None

    def _codes(self, record: MatchRecord) -> tuple[bool | None, str | None]:
        if not self._query.cpc:
            return True, None
        verdicts = [self._scheme.is_within(code, c) for code in record.cpc for c in self._query.cpc]
        if any(verdicts):
            return True, None
        if not record.cpc:
            return None, "no CPC codes"
        if any(v is None for v in verdicts):
            return None, f"CPC codes not in {self._scheme.version}"
        return False, None
