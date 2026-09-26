"""Taxonomy: the topic split into segments, with key strings and CPC codes (steps 2-3, ADR 0010).

Division of labour (build prompt principle 2):

1. **Model, language only:** ``taxonomy.draft`` splits the topic into segments with a
   definition, include/exclude notes, and keywords with synonyms.
2. **Code:** for each segment, the official CPC title list is searched with the segment's
   keywords. The result is a list of real CPC entries with their titles.
3. **Model, choosing from a closed list:** ``taxonomy.cpc_select`` picks the relevant entries
   *from that list only*. A pick outside the list, or a code the scheme does not contain, is
   rejected and recorded, never kept.
4. **Code:** the rest of the candidates stay visible as suggestions a person can promote.

A person can export the taxonomy as YAML, edit it and import it as a new version. On import,
every CPC code must exist in the scheme, or the import fails. Versions are never changed in
place.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Annotated, Literal, Protocol, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from patsquire_plr.classification.cpc import CpcScheme
from patsquire_plr.config import ModelRole
from patsquire_plr.errors import PlrError
from patsquire_plr.gateway.gateway import StructuredResult
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.landscape.scope import Scope

SEGMENT_ID = r"^[a-z][a-z0-9_]{0,39}$"
# Search terms are rendered into several query languages, so characters that are syntax in
# any of them (quotes, brackets, wildcards, operators) are not allowed in a term.
TERM = r"^[^\"'\\()\[\]{}*?#=<>|~^:;,\n\r\t]+$"


class StructuredModel(Protocol):
    """The part of the model gateway drafting needs (``ModelGateway`` satisfies it)."""

    def structured[T: BaseModel](
        self,
        role: ModelRole,
        prompt: PromptTemplate,
        variables: Mapping[str, str],
        output_model: type[T],
        *,
        use_cache: bool = True,
    ) -> StructuredResult[T]: ...


class TaxonomyError(PlrError):
    """A taxonomy is invalid or cannot be drafted."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# ------------------------------------------------------------------ the editable taxonomy


MIN_TERM_LENGTH, MAX_TERM_LENGTH, MAX_SYNONYMS = 2, 80, 12
Term = Annotated[str, Field(min_length=MIN_TERM_LENGTH, max_length=MAX_TERM_LENGTH, pattern=TERM)]


class KeywordGroup(_Strict):
    term: Term
    synonyms: tuple[Term, ...] = Field(max_length=MAX_SYNONYMS)


class SegmentSpec(_Strict):
    """What a person can edit. CPC codes are normalised symbols that exist in the scheme."""

    id: str = Field(pattern=SEGMENT_ID)
    name: str = Field(min_length=2, max_length=120)
    definition: str = Field(min_length=10, max_length=1000)
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    keywords: tuple[KeywordGroup, ...] = Field(min_length=1, max_length=20)
    cpc: tuple[str, ...] = Field(max_length=30)

    def terms(self) -> list[str]:
        """Every keyword and synonym, de-duplicated case-insensitively, in order."""
        seen: dict[str, str] = {}
        for group in self.keywords:
            for term in (group.term, *group.synonyms):
                seen.setdefault(term.casefold(), term)
        return list(seen.values())


class TaxonomySpec(_Strict):
    topic: str
    segments: tuple[SegmentSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [s.id for s in self.segments]
        if len(set(ids)) != len(ids):
            raise ValueError(f"segment ids must be unique: {ids}")
        return self


# ------------------------------------------------------------------ what is stored


class ResolvedCpc(_Strict):
    symbol: str
    title_path: str


class Rejected(_Strict):
    """Something the model proposed that was not accepted, kept with the reason."""

    segment_id: str
    kind: Literal["segment", "cpc", "term"]
    value: str
    problem: str


class TaxonomyContent(_Strict):
    """A stored taxonomy version: the spec plus everything code derived from it."""

    spec: TaxonomySpec
    cpc_scheme_version: str
    cpc: dict[str, tuple[ResolvedCpc, ...]] = Field(description="segment id -> chosen codes")
    suggestions: dict[str, tuple[ResolvedCpc, ...]] = Field(
        description="segment id -> official entries matching the keywords, not chosen"
    )
    rejected: tuple[Rejected, ...] = Field(
        description="codes and terms the model proposed that were not accepted"
    )


# ------------------------------------------------------------------ model output shapes


class DraftKeyword(_Strict):
    term: str
    synonyms: list[str]


class DraftSegment(_Strict):
    name: str
    definition: str
    includes: list[str]
    excludes: list[str]
    keywords: list[DraftKeyword]


class TaxonomyDraft(_Strict):
    segments: list[DraftSegment]


class CpcPick(_Strict):
    symbol: str
    reason: str


class CpcSelection(_Strict):
    picks: list[CpcPick]


DRAFT_PROMPT = PromptTemplate(
    id="taxonomy.draft",
    version="1",
    system=(
        "You are a patent analyst preparing a patent landscape study. You split a technology "
        "topic into segments (sub-areas) that will drive patent searches and report chapters. "
        "Segments must be distinct from each other and together cover the topic. Use the "
        "vocabulary patent documents use in titles and abstracts. Do not include company "
        "names, product names or patent numbers. Reply only with JSON matching the schema."
    ),
    user=(
        "Topic: {topic}\n"
        "Notes from the requester: {notes}\n\n"
        "Draft between 2 and {max_segments} segments. For each segment give:\n"
        "- name: a short name;\n"
        "- definition: one or two sentences saying what belongs in the segment;\n"
        "- includes: examples of subject matter that belongs;\n"
        "- excludes: examples of related subject matter that does not belong;\n"
        "- keywords: 3 to 10 search terms or short phrases, each with synonyms and "
        "alternative spellings used in patents."
    ),
)

CPC_SELECT_PROMPT = PromptTemplate(
    id="taxonomy.cpc_select",
    version="1",
    system=(
        "You are a patent classification expert. You choose CPC classification entries that "
        "patents in a given technology segment are likely to carry. You may only choose "
        "symbols from the numbered candidate list you are given, copied exactly. Reply only "
        "with JSON matching the schema."
    ),
    user=(
        "Segment: {name}\n"
        "Definition: {definition}\n"
        "Keywords: {keywords}\n\n"
        "Candidate CPC entries (symbol: title path):\n{candidates}\n\n"
        "Choose at most {max_codes} candidates that are specific to this segment. Prefer "
        "fewer, precise entries over broad ones. For each, give the symbol exactly as listed "
        "and a one-sentence reason. Choose none if no candidate fits."
    ),
)


# ------------------------------------------------------------------ drafting


def segment_id(name: str, taken: set[str]) -> str:
    """A stable identifier from a segment name, e.g. "Model training" -> "model_training"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")[:36] or "segment"
    if not base[0].isalpha():
        base = f"s_{base}"[:36]
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


def draft_taxonomy(
    gateway: StructuredModel,
    scheme: CpcScheme,
    scope: Scope,
    *,
    max_segments: int,
    candidates_per_segment: int,
    max_cpc_per_segment: int,
) -> tuple[TaxonomyContent, list[str]]:
    """Draft a taxonomy. Returns the content and the model calls' cache keys (provenance)."""
    draft = gateway.structured(
        "reasoner",
        DRAFT_PROMPT,
        {
            "topic": scope.topic,
            "notes": scope.notes or "(none)",
            "max_segments": str(max_segments),
        },
        TaxonomyDraft,
    )
    calls = [draft.cache_key]
    distinct = {" ".join(d.name.casefold().split()) for d in draft.value.segments}
    if not 2 <= len(distinct) <= max_segments:  # noqa: PLR2004
        raise TaxonomyError(
            f"the model drafted {len(distinct)} distinct segments; expected 2 to {max_segments}"
        )
    taken: set[str] = set()
    segments: list[SegmentSpec] = []
    chosen: dict[str, tuple[ResolvedCpc, ...]] = {}
    suggestions: dict[str, tuple[ResolvedCpc, ...]] = {}
    rejected: list[Rejected] = []
    within = _subclasses(scheme, scope.cpc_hints)
    names: dict[str, str] = {}
    for drafted in draft.value.segments:
        name_key = " ".join(drafted.name.casefold().split())
        if name_key in names:
            rejected.append(
                Rejected(
                    segment_id=names[name_key],
                    kind="segment",
                    value=drafted.name,
                    problem="a segment with the same name was already drafted",
                )
            )
            continue
        spec, bad_terms = _segment_from_draft(drafted, segment_id(drafted.name, taken))
        names[name_key] = spec.id
        rejected += [
            Rejected(segment_id=spec.id, kind="term", value=t, problem=p) for t, p in bad_terms
        ]
        candidates = [
            ResolvedCpc(symbol=m.symbol, title_path=m.title_path)
            for m in scheme.search(spec.terms(), within=within, limit=candidates_per_segment)
        ]
        picks: list[CpcPick] = []
        if candidates:
            selection = gateway.structured(
                "reasoner",
                CPC_SELECT_PROMPT,
                {
                    "name": spec.name,
                    "definition": spec.definition,
                    "keywords": "; ".join(spec.terms()),
                    "candidates": "\n".join(f"{c.symbol}: {c.title_path}" for c in candidates),
                    "max_codes": str(max_cpc_per_segment),
                },
                CpcSelection,
            )
            calls.append(selection.cache_key)
            picks = selection.value.picks
        accepted, problems = _accept_picks(picks, candidates, max_cpc_per_segment)
        rejected += [
            Rejected(segment_id=spec.id, kind="cpc", value=c, problem=p) for c, p in problems
        ]
        chosen[spec.id] = accepted
        suggestions[spec.id] = tuple(c for c in candidates if c not in accepted)
        segments.append(spec.model_copy(update={"cpc": tuple(c.symbol for c in accepted)}))
    content = TaxonomyContent(
        spec=TaxonomySpec(topic=scope.topic, segments=tuple(segments)),
        cpc_scheme_version=scheme.version,
        cpc=chosen,
        suggestions=suggestions,
        rejected=tuple(rejected),
    )
    return content, calls


def _subclasses(scheme: CpcScheme, hints: tuple[str, ...]) -> tuple[str, ...]:
    """Subclasses of the user's CPC hints limit the candidate search; unknown hints fail."""
    subclasses = []
    for hint in hints:
        check = scheme.check(hint)
        if check.symbol is None:
            raise TaxonomyError(f"scope cpc_hints: {check.problem}")
        subclasses.append(check.symbol[:4])
    return tuple(dict.fromkeys(subclasses))


def _segment_from_draft(
    drafted: DraftSegment, sid: str
) -> tuple[SegmentSpec, list[tuple[str, str]]]:
    """The drafted segment, with terms that cannot be searched set aside (and returned)."""
    bad: list[tuple[str, str]] = []

    def usable(term: str) -> bool:
        ok = MIN_TERM_LENGTH <= len(term) <= MAX_TERM_LENGTH and re.fullmatch(TERM, term)
        if not ok:
            bad.append((term, "not usable as a search term (length or query syntax characters)"))
        return bool(ok)

    groups = []
    for keyword in drafted.keywords:
        term = keyword.term.strip()
        synonyms = [s.strip() for s in keyword.synonyms if s.strip()]
        synonyms = [s for s in dict.fromkeys(synonyms) if s.casefold() != term.casefold()]
        synonyms = [s for s in synonyms if usable(s)]
        bad += [(s, f"over the limit of {MAX_SYNONYMS} synonyms") for s in synonyms[MAX_SYNONYMS:]]
        synonyms = synonyms[:MAX_SYNONYMS]
        if usable(term):
            groups.append(KeywordGroup(term=term, synonyms=tuple(synonyms)))
    try:
        spec = SegmentSpec(
            id=sid,
            name=drafted.name.strip(),
            definition=drafted.definition.strip(),
            includes=tuple(i.strip() for i in drafted.includes if i.strip()),
            excludes=tuple(e.strip() for e in drafted.excludes if e.strip()),
            keywords=tuple(groups),
            cpc=(),
        )
    except ValidationError as exc:
        raise TaxonomyError(f"drafted segment {drafted.name!r} is not usable: {exc}") from exc
    return spec, bad


def _accept_picks(
    picks: list[CpcPick], candidates: list[ResolvedCpc], limit: int
) -> tuple[tuple[ResolvedCpc, ...], list[tuple[str, str]]]:
    by_symbol = {c.symbol: c for c in candidates}
    accepted: list[ResolvedCpc] = []
    problems: list[tuple[str, str]] = []
    for pick in picks:
        symbol = pick.symbol.strip()
        if symbol not in by_symbol:
            problems.append((pick.symbol, "not in the candidate list offered to the model"))
        elif by_symbol[symbol] in accepted:
            problems.append((pick.symbol, "chosen twice"))
        elif len(accepted) >= limit:
            problems.append((pick.symbol, f"over the limit of {limit} codes per segment"))
        else:
            accepted.append(by_symbol[symbol])
    return tuple(accepted), problems


# ------------------------------------------------------------------ editing by a person


def export_yaml(content: TaxonomyContent) -> str:
    """The editable part as YAML, with suggestions listed in a comment block."""
    lines = [
        "# Taxonomy for editing. Change segments, keywords and CPC codes, then import this",
        f"# file as a new version. CPC codes must exist in CPC {content.cpc_scheme_version}.",
        "",
        yaml.safe_dump(content.spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
    ]
    for sid, suggested in content.suggestions.items():
        if suggested:
            lines.append(f"# Suggested CPC entries for {sid} (not chosen):")
            lines += [f"#   {c.symbol}: {c.title_path}" for c in suggested]
    if content.rejected:
        lines.append("# Proposed by the model but rejected:")
        lines += [f"#   {r.segment_id} {r.kind}: {r.value} ({r.problem})" for r in content.rejected]
    return "\n".join(lines) + "\n"


def content_from_edit(
    text: str, scheme: CpcScheme, *, previous: TaxonomyContent
) -> TaxonomyContent:
    """A person's edited YAML as a new version's content. Invalid CPC codes fail the import."""
    try:
        raw = yaml.safe_load(text)
        spec = TaxonomySpec.model_validate(raw, strict=False)
    except (yaml.YAMLError, ValidationError) as exc:
        raise TaxonomyError(f"edited taxonomy is invalid: {exc}") from exc
    problems = []
    chosen: dict[str, tuple[ResolvedCpc, ...]] = {}
    segments = []
    for segment in spec.segments:
        resolved = []
        for code in segment.cpc:
            check = scheme.check(code)
            entry = scheme.get(check.symbol) if check.symbol else None
            if check.symbol is None or check.title_path is None or entry is None:
                problems.append(f"{segment.id}: {check.problem}")
            elif entry.kind in ("section", "class"):
                problems.append(
                    f"{segment.id}: {check.symbol} is a CPC {entry.kind}; use a subclass or "
                    "group, since a whole section or class makes queries too broad to run"
                )
            else:
                resolved.append(ResolvedCpc(symbol=check.symbol, title_path=check.title_path))
        chosen[segment.id] = tuple(resolved)
        segments.append(segment.model_copy(update={"cpc": tuple(r.symbol for r in resolved)}))
    if problems:
        raise TaxonomyError("edited taxonomy has invalid CPC codes: " + "; ".join(problems))
    kept = {sid: previous.suggestions.get(sid, ()) for sid in chosen}
    return TaxonomyContent(
        spec=spec.model_copy(update={"segments": tuple(segments)}),
        cpc_scheme_version=scheme.version,
        cpc=chosen,
        suggestions={sid: tuple(c for c in kept[sid] if c not in chosen[sid]) for sid in chosen},
        rejected=(),
    )


Origin = Literal["llm_draft", "user_edit"]
