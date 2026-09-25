"""The PLR template specification: schema, cross-reference checks and loader.

``template/plr_template.yaml`` defines what every generated report contains: the canonical
definitions (build prompt §7), a catalog of metrics and chart types, and the report sections
(§9) with the facts, charts, tables and caveats each needs. Later phases consume this spec:
the analytics engine implements its metrics, the chart layer its chart types, and report
assembly its sections.

Loading fails with ``TemplateError`` if the spec is internally inconsistent, for example:

* a section references an unknown metric, chart type, definition or reference report;
* a §9 section is missing;
* a metric or chart type is defined but never used.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from patsquire_plr.errors import PlrError

Identifier = str  # snake_case identifiers; the pattern is enforced where fields are declared
_ID = Field(pattern=r"^[a-z][a-z0-9_]*$")

# The sections required by the build prompt §9, in its order. Appendices are separate.
REQUIRED_SECTION_IDS: tuple[str, ...] = (
    "executive_summary",
    "technology_overview",
    "scope_and_methodology",
    "search_strategy",
    "patent_dataset",
    "filing_trends",
    "top_applicants",
    "top_inventors",
    "geographical_distribution",
    "technology_segmentation",
    "patent_family_analysis",
    "legal_status",
    "key_patents_and_portfolios",
    "competitive_landscape",
    "technology_trends",
    "white_space",
    "key_findings",
    "figures_and_maps",
)

# Canonical patent-record fields a metric may depend on (Phase 2 schema implements these).
RecordField = Literal[
    "publication_number",
    "application_number",
    "family_id",
    "kind_code",
    "filing_office",
    "priority_date",
    "priority_country",
    "filing_date",
    "publication_date",
    "title",
    "abstract",
    "claims",
    "applicants",
    "applicant_countries",
    "inventors",
    "inventor_countries",
    "cpc",
    "ipc",
    "legal_status",
    "grant_date",
    "backward_citations",
    "forward_citations",
    "npl_citations",
]

CountingUnit = Literal["family", "international_family", "document", "applicant", "inventor"]
FactSource = Literal[
    "patent_data",  # computed deterministically from retrieved records
    "classification",  # AI relevance/segment labels, counted deterministically
    "system_metadata",  # pipeline logs, configuration, reconciliation counts
    "external_cited",  # background from cited sources, expert-reviewed (Layer 7)
]
SectionSource = Literal[
    "verified_narrative",
    "external_cited",
    "system_metadata",
    "patent_data",
    "classification",
    "mixed",
]


# Fields a metric must list when it uses a definition. Each inner set is "any of".
_DEFINITION_NEEDS: dict[str, tuple[frozenset[str], ...]] = {
    "time_basis": (frozenset({"priority_date"}),),
    "incomplete_period": (frozenset({"priority_date"}),),
    "growth_rate": (frozenset({"priority_date"}),),
    "international_family": (frozenset({"filing_office"}),),
    "applicant_normalization": (frozenset({"applicants"}),),
    "legal_status_basis": (frozenset({"legal_status"}),),
    "self_citation": (frozenset({"applicants"}), frozenset({"forward_citations"})),
    "forward_citation_window": (frozenset({"forward_citations"}), frozenset({"publication_date"})),
    "country_attribution": (frozenset({"applicant_countries", "inventor_countries"}),),
    "key_patent_formula": (
        frozenset({"forward_citations"}),
        frozenset({"filing_office"}),
        frozenset({"legal_status"}),
    ),
}

# Fact sources a section of each source type may draw on (unlisted types are unconstrained).
_SECTION_ALLOWED_SOURCES: dict[str, frozenset[str]] = {
    "patent_data": frozenset({"patent_data"}),
    "classification": frozenset({"patent_data", "classification"}),
}


class TemplateError(PlrError):
    """The template specification is missing, malformed or internally inconsistent."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReferenceReport(_Model):
    id: Identifier = _ID
    title: str
    publisher: str
    year: int = Field(ge=1990, le=2100)
    pages: int = Field(ge=1)


class Definition(_Model):
    """A canonical definition (build prompt §7), printed in the methodology section."""

    id: Identifier = _ID
    term: str
    definition: str = Field(min_length=20)
    default: str
    options: tuple[str, ...] = Field(
        default=(), description="allowed values when the definition is configurable"
    )
    needs_owner_decision: bool = False

    @model_validator(mode="after")
    def _default_is_an_option(self) -> Self:
        if self.options and self.default not in self.options:
            raise ValueError(f"default {self.default!r} is not one of options {self.options}")
        return self


class Metric(_Model):
    id: Identifier = _ID
    name: str
    definition: str = Field(min_length=20)
    unit: str
    counting_unit: CountingUnit | None
    source: FactSource
    requires_fields: tuple[RecordField, ...]
    uses_definitions: tuple[Identifier, ...] = ()
    excludes_incomplete_periods: bool = False
    reference_practice: tuple[Identifier, ...] = Field(
        default=(), description="reference reports that use an equivalent metric"
    )

    @model_validator(mode="after")
    def _fields_match_definitions(self) -> Self:
        problems: list[str] = []
        fields = set(self.requires_fields)
        if self.source in ("patent_data", "classification") and not fields:
            problems.append("is computed from data but lists no fields")
        for definition in self.uses_definitions:
            for any_of in _DEFINITION_NEEDS.get(definition, ()):
                if not fields & any_of:
                    problems.append(f"uses {definition} but lists none of {sorted(any_of)}")
        if "priority_date" in fields and "time_basis" not in self.uses_definitions:
            problems.append("places families in time (priority_date) but does not use time_basis")
        if self.excludes_incomplete_periods and "incomplete_period" not in self.uses_definitions:
            problems.append("excludes incomplete periods but does not use incomplete_period")
        if "segment_labels" in self.uses_definitions and self.source != "classification":
            problems.append("uses segment_labels, so its source must be classification")
        if problems:
            raise ValueError(f"metric {self.id} " + "; ".join(problems))
        return self


class ChartType(_Model):
    id: Identifier = _ID
    name: str
    description: str
    reference_practice: tuple[Identifier, ...] = ()


class ChartRequirement(_Model):
    id: Identifier = _ID
    chart_type: Identifier
    metrics: tuple[Identifier, ...] = Field(min_length=1)
    shows: str = Field(min_length=10)


class TableRequirement(_Model):
    id: Identifier = _ID
    metrics: tuple[Identifier, ...] = Field(min_length=1)
    shows: str = Field(min_length=10)


class Section(_Model):
    id: Identifier = _ID
    title: str
    purpose: str = Field(min_length=20)
    source_type: SectionSource
    required_facts: tuple[Identifier, ...] = ()
    charts: tuple[ChartRequirement, ...] = ()
    tables: tuple[TableRequirement, ...] = ()
    caveats: tuple[str, ...] = ()
    when_data_missing: str = Field(min_length=10)
    repeat_per: Literal["segment"] | None = None
    subsections: tuple[Section, ...] = ()
    reference_practice: tuple[Identifier, ...] = ()

    def walk(self) -> Iterator[Section]:
        yield self
        for sub in self.subsections:
            yield from sub.walk()


class Appendix(_Model):
    id: Identifier = _ID
    title: str
    contents: str = Field(min_length=10)
    source_type: SectionSource
    required_facts: tuple[Identifier, ...] = ()


class TemplateSpec(_Model):
    version: int = Field(ge=1)
    references: tuple[ReferenceReport, ...] = Field(min_length=1)
    definitions: tuple[Definition, ...] = Field(min_length=1)
    metrics: tuple[Metric, ...] = Field(min_length=1)
    chart_types: tuple[ChartType, ...] = Field(min_length=1)
    sections: tuple[Section, ...] = Field(min_length=1)
    appendices: tuple[Appendix, ...] = ()

    def all_sections(self) -> Iterator[Section]:
        for section in self.sections:
            yield from section.walk()

    def _section_facts(self, section: Section) -> set[str]:
        facts = set(section.required_facts)
        facts.update(m for c in section.charts for m in c.metrics)
        facts.update(m for t in section.tables for m in t.metrics)
        return facts

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        problems = [
            *self._duplicate_ids(),
            *self._unknown_references(),
            *self._required_section_problems(),
            *self._source_consistency_problems(),
            *self._unused_items(),
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _duplicate_ids(self) -> list[str]:
        sections = list(self.all_sections())
        groups = {
            "reference": [r.id for r in self.references],
            "definition": [d.id for d in self.definitions],
            "metric": [m.id for m in self.metrics],
            "chart type": [c.id for c in self.chart_types],
            "section": [s.id for s in sections] + [a.id for a in self.appendices],
            "chart/table": [c.id for s in sections for c in s.charts]
            + [t.id for s in sections for t in s.tables],
        }
        problems = []
        for kind, ids in groups.items():
            if duplicates := sorted(i for i, n in Counter(ids).items() if n > 1):
                problems.append(f"duplicate {kind} ids: {duplicates}")
        return problems

    def _unknown_references(self) -> list[str]:
        known = {
            "reference": {r.id for r in self.references},
            "definition": {d.id for d in self.definitions},
            "metric": {m.id for m in self.metrics},
            "chart type": {c.id for c in self.chart_types},
        }
        uses: list[tuple[str, str, tuple[str, ...]]] = []
        for metric in self.metrics:
            uses.append((f"metric {metric.id}", "definition", metric.uses_definitions))
            uses.append((f"metric {metric.id}", "reference", metric.reference_practice))
        for chart_type in self.chart_types:
            uses.append((f"chart type {chart_type.id}", "reference", chart_type.reference_practice))
        for section in self.all_sections():
            owner = f"section {section.id}"
            uses.append((owner, "metric", section.required_facts))
            uses.append((owner, "reference", section.reference_practice))
            for chart in section.charts:
                uses.append((f"{owner} chart {chart.id}", "chart type", (chart.chart_type,)))
                uses.append((f"{owner} chart {chart.id}", "metric", chart.metrics))
            for table in section.tables:
                uses.append((f"{owner} table {table.id}", "metric", table.metrics))
        for appendix in self.appendices:
            uses.append((f"appendix {appendix.id}", "metric", appendix.required_facts))
        return [
            f"{owner}: unknown {kind} '{item}'"
            for owner, kind, items in uses
            for item in items
            if item not in known[kind]
        ]

    def _required_section_problems(self) -> list[str]:
        top_level = [s.id for s in self.sections]
        problems = []
        if missing := [s for s in REQUIRED_SECTION_IDS if s not in top_level]:
            problems.append(f"missing required sections (build prompt §9): {missing}")
        present_required = [s for s in top_level if s in REQUIRED_SECTION_IDS]
        if present_required != [s for s in REQUIRED_SECTION_IDS if s in top_level]:
            problems.append("required sections are not in build prompt §9 order")
        return problems

    def _source_consistency_problems(self) -> list[str]:
        metric_source = {m.id: m.source for m in self.metrics}
        problems = []
        for section in self.all_sections():
            allowed = _SECTION_ALLOWED_SOURCES.get(section.source_type)
            if allowed is None:
                continue
            for fact in sorted(self._section_facts(section)):
                source = metric_source.get(fact)
                if source is not None and source not in allowed:
                    problems.append(
                        f"section {section.id} is {section.source_type} but uses {fact} "
                        f"({source}); make the section 'mixed' or move the fact"
                    )
        return problems

    def _unused_items(self) -> list[str]:
        sections = list(self.all_sections())
        used_metrics = {f for s in sections for f in self._section_facts(s)}
        used_metrics.update(f for a in self.appendices for f in a.required_facts)
        used_charts = {c.chart_type for s in sections for c in s.charts}
        used_definitions = {d for m in self.metrics for d in m.uses_definitions}
        used_references = {r for m in self.metrics for r in m.reference_practice}
        used_references.update(r for c in self.chart_types for r in c.reference_practice)
        used_references.update(r for s in sections for r in s.reference_practice)
        unused = {
            "metrics defined but never used": {m.id for m in self.metrics} - used_metrics,
            "chart types defined but never used": {c.id for c in self.chart_types} - used_charts,
            "definitions never used by a metric": {d.id for d in self.definitions}
            - used_definitions,
            "references never cited": {r.id for r in self.references} - used_references,
        }
        return [f"{label}: {sorted(ids)}" for label, ids in unused.items() if ids]


def load_template(path: Path) -> TemplateSpec:
    if not path.is_file():
        raise TemplateError(f"Template spec not found: {path}")
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateError(f"Template spec is not valid YAML: {path}: {exc}") from exc
    try:
        # JSON mode: strict models accept YAML lists for tuple fields only in JSON mode.
        return TemplateSpec.model_validate_json(json.dumps(data))
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        ]
        raise TemplateError(f"Invalid template spec {path}:\n" + "\n".join(lines)) from exc


def render_definitions_markdown(spec: TemplateSpec) -> str:
    """Render ``docs/definitions.md``: the canonical definitions and metric formulas.

    Generated from the template so the documentation cannot drift from the spec.
    """
    lines = [
        "# Canonical definitions and metrics",
        "",
        "<!-- GENERATED by `plr template definitions` from template/plr_template.yaml."
        " Do not edit by hand. -->",
        "",
        "Every metric in a generated report refers to these definitions, and the methodology"
        " section prints them with the option used in that run (build prompt §7).",
        "",
        "## Definitions",
        "",
    ]
    for definition in spec.definitions:
        lines.append(f"### {definition.term} (`{definition.id}`)")
        lines.append("")
        lines.append(definition.definition)
        lines.append("")
        lines.append(f"* Default: `{definition.default}`")
        if definition.options:
            lines.append(f"* Options: {', '.join(f'`{o}`' for o in definition.options)}")
        if definition.needs_owner_decision:
            lines.append("* **Needs owner decision** before the first production report.")
        lines.append("")
    lines += [
        "## Metrics",
        "",
        "| ID | Metric | Unit | Counts | Source | Definition |",
        "|---|---|---|---|---|---|",
    ]
    for metric in spec.metrics:
        text = metric.definition.replace("|", "\\|")
        counts = metric.counting_unit or "n/a"
        lines.append(
            f"| `{metric.id}` | {metric.name} | {metric.unit} | {counts} | {metric.source} "
            f"| {text} |"
        )
    lines.append("")
    return "\n".join(lines)
