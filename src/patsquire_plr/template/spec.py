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
    def _patent_metrics_need_fields(self) -> Self:
        if self.source in ("patent_data", "classification") and not self.requires_fields:
            raise ValueError(f"metric {self.id} is computed from data but lists no fields")
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

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        problems: list[str] = []

        def unique(kind: str, ids: list[str]) -> set[str]:
            duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
            if duplicates:
                problems.append(f"duplicate {kind} ids: {duplicates}")
            return set(ids)

        reference_ids = unique("reference", [r.id for r in self.references])
        definition_ids = unique("definition", [d.id for d in self.definitions])
        metric_ids = unique("metric", [m.id for m in self.metrics])
        chart_ids = unique("chart type", [c.id for c in self.chart_types])
        sections = list(self.all_sections())
        unique("section", [s.id for s in sections] + [a.id for a in self.appendices])
        unique(
            "chart/table",
            [c.id for s in sections for c in s.charts] + [t.id for s in sections for t in s.tables],
        )

        def check(owner: str, kind: str, used: tuple[str, ...], known: set[str]) -> None:
            for item in used:
                if item not in known:
                    problems.append(f"{owner}: unknown {kind} '{item}'")

        for metric in self.metrics:
            check(f"metric {metric.id}", "definition", metric.uses_definitions, definition_ids)
            check(f"metric {metric.id}", "reference", metric.reference_practice, reference_ids)
        for chart_type in self.chart_types:
            check(
                f"chart type {chart_type.id}",
                "reference",
                chart_type.reference_practice,
                reference_ids,
            )

        used_metrics: set[str] = set()
        used_charts: set[str] = set()
        for section in sections:
            owner = f"section {section.id}"
            check(owner, "metric", section.required_facts, metric_ids)
            check(owner, "reference", section.reference_practice, reference_ids)
            used_metrics.update(section.required_facts)
            for chart in section.charts:
                check(f"{owner} chart {chart.id}", "chart type", (chart.chart_type,), chart_ids)
                check(f"{owner} chart {chart.id}", "metric", chart.metrics, metric_ids)
                used_charts.add(chart.chart_type)
                used_metrics.update(chart.metrics)
            for table in section.tables:
                check(f"{owner} table {table.id}", "metric", table.metrics, metric_ids)
                used_metrics.update(table.metrics)
        for appendix in self.appendices:
            check(f"appendix {appendix.id}", "metric", appendix.required_facts, metric_ids)
            used_metrics.update(appendix.required_facts)

        top_level = [s.id for s in self.sections]
        missing = [s for s in REQUIRED_SECTION_IDS if s not in top_level]
        if missing:
            problems.append(f"missing required sections (build prompt §9): {missing}")
        present_required = [s for s in top_level if s in REQUIRED_SECTION_IDS]
        if present_required != [s for s in REQUIRED_SECTION_IDS if s in top_level]:
            problems.append("required sections are not in build prompt §9 order")

        if unused := sorted(metric_ids - used_metrics):
            problems.append(f"metrics defined but never used: {unused}")
        if unused := sorted(chart_ids - used_charts):
            problems.append(f"chart types defined but never used: {unused}")

        if problems:
            raise ValueError("; ".join(problems))
        return self


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
