from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from patsquire_plr.cli import app
from patsquire_plr.reference.extract import Caption, Heading, ReferenceExtract, copied_phrases
from patsquire_plr.template.spec import (
    REQUIRED_SECTION_IDS,
    TemplateError,
    TemplateSpec,
    load_template,
    render_definitions_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template" / "plr_template.yaml"
DEFINITIONS_DOC = REPO_ROOT / "docs" / "definitions.md"
runner = CliRunner()

Data = dict[str, list[dict[str, object]]]


@pytest.fixture(scope="module")
def spec() -> TemplateSpec:
    return load_template(TEMPLATE)


def _raw() -> Data:
    data: Data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return data


def _load_mutated(tmp_path: Path, mutate: Callable[[Data], None]) -> TemplateSpec:
    data = _raw()
    mutate(data)
    path = tmp_path / "template.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_template(path)


def _section(data: Data, section_id: str) -> dict[str, object]:
    return next(s for s in data["sections"] if s["id"] == section_id)


# ---------------------------------------------------------------- the committed template


def test_committed_template_is_valid(spec: TemplateSpec) -> None:
    assert spec.version == 1


def test_all_required_sections_present_in_build_prompt_order(spec: TemplateSpec) -> None:
    assert tuple(s.id for s in spec.sections) == REQUIRED_SECTION_IDS


def test_every_section_says_what_happens_when_data_is_missing(spec: TemplateSpec) -> None:
    for section in spec.all_sections():
        assert section.when_data_missing.strip()


def test_data_sections_declare_the_facts_they_need(spec: TemplateSpec) -> None:
    for section in spec.all_sections():
        if section.source_type in ("patent_data", "classification", "mixed"):
            assert section.required_facts, section.id


def test_segment_profile_repeats_per_segment(spec: TemplateSpec) -> None:
    profile = next(s for s in spec.all_sections() if s.id == "segment_profile")
    assert profile.repeat_per == "segment"


def test_background_section_is_cited_or_reviewed(spec: TemplateSpec) -> None:
    overview = next(s for s in spec.sections if s.id == "technology_overview")
    assert overview.source_type == "external_cited"


@pytest.mark.parametrize(
    ("section_id", "phrase"),
    [
        ("legal_status", "not legal advice"),
        ("white_space", "indicative"),
        ("technology_segmentation", "overlap"),
        ("scope_and_methodology", "freedom-to-operate"),
    ],
)
def test_required_caveats_are_present(spec: TemplateSpec, section_id: str, phrase: str) -> None:
    section = next(s for s in spec.all_sections() if s.id == section_id)
    assert any(phrase in caveat for caveat in section.caveats)


def test_build_prompt_section_7_definitions_are_covered(spec: TemplateSpec) -> None:
    ids = {d.id for d in spec.definitions}
    assert {
        "counting_unit",
        "time_basis",
        "incomplete_period",
        "applicant_normalization",
        "jurisdiction_kinds",
        "international_family",
        "legal_status_basis",
        "growth_rate",
    } <= ids


def test_default_time_basis_is_priority_year(spec: TemplateSpec) -> None:
    time_basis = next(d for d in spec.definitions if d.id == "time_basis")
    assert time_basis.default == "earliest_priority_year"


def test_definitions_doc_is_in_sync_with_template(spec: TemplateSpec) -> None:
    assert DEFINITIONS_DOC.read_text(encoding="utf-8") == render_definitions_markdown(spec), (
        "docs/definitions.md is stale: run `uv run plr template definitions > docs/definitions.md`"
    )


# ---------------------------------------------------------------- consistency checks catch drift


def test_unknown_metric_in_section(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        _section(data, "filing_trends")["required_facts"] = ["no_such_metric"]

    with pytest.raises(
        TemplateError, match="section filing_trends: unknown metric 'no_such_metric'"
    ):
        _load_mutated(tmp_path, mutate)


def test_unknown_chart_type(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        charts = _section(data, "legal_status")["charts"]
        assert isinstance(charts, list)
        charts[0]["chart_type"] = "pie_3d"

    with pytest.raises(TemplateError, match="unknown chart type 'pie_3d'"):
        _load_mutated(tmp_path, mutate)


def test_unknown_reference_and_definition(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["metrics"][0]["reference_practice"] = ["made_up_report"]
        data["metrics"][0]["uses_definitions"] = ["made_up_definition"]

    with pytest.raises(TemplateError) as exc_info:
        _load_mutated(tmp_path, mutate)
    assert "unknown reference 'made_up_report'" in str(exc_info.value)
    assert "unknown definition 'made_up_definition'" in str(exc_info.value)


def test_missing_required_section(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["sections"] = [s for s in data["sections"] if s["id"] != "white_space"]

    with pytest.raises(TemplateError, match=r"missing required sections .*white_space"):
        _load_mutated(tmp_path, mutate)


def test_sections_out_of_order(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["sections"][0], data["sections"][1] = data["sections"][1], data["sections"][0]

    with pytest.raises(TemplateError, match="not in build prompt §9 order"):
        _load_mutated(tmp_path, mutate)


def test_unused_metric_and_chart_type(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["metrics"].append(
            {
                "id": "orphan_metric",
                "name": "Orphan",
                "definition": "A metric that no section uses at all.",
                "unit": "x",
                "counting_unit": None,
                "source": "system_metadata",
                "requires_fields": [],
            }
        )
        data["chart_types"].append({"id": "orphan_chart", "name": "Orphan", "description": "d"})

    with pytest.raises(TemplateError) as exc_info:
        _load_mutated(tmp_path, mutate)
    assert "metrics defined but never used: ['orphan_metric']" in str(exc_info.value)
    assert "chart types defined but never used: ['orphan_chart']" in str(exc_info.value)


def test_duplicate_ids(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["definitions"].append(dict(data["definitions"][0]))

    with pytest.raises(TemplateError, match="duplicate definition ids"):
        _load_mutated(tmp_path, mutate)


def test_default_must_be_one_of_the_options(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["definitions"][0]["default"] = "not_an_option"

    with pytest.raises(TemplateError, match="is not one of options"):
        _load_mutated(tmp_path, mutate)


def test_data_metric_must_list_its_fields(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        metric = next(m for m in data["metrics"] if m["id"] == "families_per_year")
        metric["requires_fields"] = []

    with pytest.raises(TemplateError, match="computed from data but lists no fields"):
        _load_mutated(tmp_path, mutate)


def test_unknown_record_field_is_rejected(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["metrics"][1]["requires_fields"] = ["family_idd"]

    with pytest.raises(TemplateError, match="requires_fields"):
        _load_mutated(tmp_path, mutate)


def test_extra_keys_are_rejected(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        _section(data, "legal_status")["colour"] = "blue"

    with pytest.raises(TemplateError, match="colour"):
        _load_mutated(tmp_path, mutate)


def test_missing_file_and_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(TemplateError, match="not found"):
        load_template(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("sections: [unclosed", encoding="utf-8")
    with pytest.raises(TemplateError, match="not valid YAML"):
        load_template(bad)


# ---------------------------------------------------------------- CLI


def test_cli_validate_ok_and_failure(tmp_path: Path) -> None:
    ok = runner.invoke(app, ["template", "validate", "--template", str(TEMPLATE)])
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text("version: 1\n", encoding="utf-8")
    bad = runner.invoke(app, ["template", "validate", "--template", str(bad_path)])

    assert ok.exit_code == 0, ok.output
    assert "valid (" in ok.stdout
    assert bad.exit_code == 1
    assert "references" in bad.stderr


def test_cli_definitions_matches_committed_doc() -> None:
    result = runner.invoke(app, ["template", "definitions", "--template", str(TEMPLATE)])

    assert result.exit_code == 0
    assert result.stdout == DEFINITIONS_DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------- originality check (principle 9)


def _extract(caption_text: str) -> ReferenceExtract:
    return ReferenceExtract(
        file_name="ref.pdf",
        sha256="0" * 64,
        page_count=1,
        extractor_version="1",
        heading_source="bookmark",
        headings=(Heading(level=0, title="Overview of the landscape", page=1, source="bookmark"),),
        captions=(Caption(kind="figure", label="Figure 1", text=caption_text, page=1),),
        pages_without_text=(),
    )


def test_copied_phrases_detects_a_shared_eight_word_run() -> None:
    extract = _extract(
        "Share of international families by applicant country and year, 2010 to 2020"
    )
    ours = "Our text: share of international families by applicant country and year differs."

    found = copied_phrases(ours, [extract])

    assert ("ref.pdf", "share of international families by applicant country and") in found


def test_copied_phrases_ignores_short_common_phrases() -> None:
    extract = _extract("Top applicants by number of patent families")

    assert copied_phrases("We rank top applicants by number of families.", [extract]) == []


def test_cli_originality_check(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    extract = _extract(
        "Share of international families by applicant country and year, 2010 to 2020"
    )
    (extracted / "ref.json").write_text(extract.model_dump_json(), encoding="utf-8")
    clean = tmp_path / "clean.md"
    clean.write_text("Entirely original wording about patent filings.", encoding="utf-8")
    copied = tmp_path / "copied.md"
    copied.write_text(
        "share of international families by applicant country and year", encoding="utf-8"
    )

    ok = runner.invoke(
        app, ["reference", "check-originality", str(clean), "--extracted-dir", str(extracted)]
    )
    bad = runner.invoke(
        app, ["reference", "check-originality", str(copied), "--extracted-dir", str(extracted)]
    )
    none = runner.invoke(
        app,
        ["reference", "check-originality", str(clean), "--extracted-dir", str(tmp_path / "empty")],
    )

    assert ok.exit_code == 0, ok.output
    assert bad.exit_code == 1
    assert "shares 'share of international families" in bad.stderr
    assert none.exit_code == 2


# ---------------------------------------------------------------- consistency rules (review fixes)


def _metric(data: Data, metric_id: str) -> dict[str, object]:
    return next(m for m in data["metrics"] if m["id"] == metric_id)


@pytest.mark.parametrize(
    ("metric_id", "field", "message"),
    [
        (
            "families_per_year",
            "filing_office",
            "uses international_family but lists none of ['filing_office']",
        ),
        ("forward_citations", "publication_date", "uses forward_citation_window"),
        ("key_patent_score", "legal_status", "uses key_patent_formula"),
        ("pct_usage", "applicant_countries", "uses country_attribution"),
    ],
)
def test_metric_must_list_fields_its_definitions_need(
    tmp_path: Path, metric_id: str, field: str, message: str
) -> None:
    def mutate(data: Data) -> None:
        metric = _metric(data, metric_id)
        fields = metric["requires_fields"]
        assert isinstance(fields, list)
        metric["requires_fields"] = [f for f in fields if f != field]

    with pytest.raises(TemplateError, match=message.replace("[", r"\[").replace("]", r"\]")):
        _load_mutated(tmp_path, mutate)


def test_time_placed_metric_must_use_time_basis(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        metric = _metric(data, "top_classification_codes")
        metric["uses_definitions"] = []

    with pytest.raises(TemplateError, match="does not use time_basis"):
        _load_mutated(tmp_path, mutate)


def test_excluding_incomplete_periods_requires_the_definition(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        metric = _metric(data, "filing_velocity")
        metric["uses_definitions"] = ["time_basis", "applicant_normalization"]

    with pytest.raises(TemplateError, match="does not use incomplete_period"):
        _load_mutated(tmp_path, mutate)


def test_segment_metrics_must_come_from_classification(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        _metric(data, "families_per_segment")["source"] = "patent_data"

    with pytest.raises(TemplateError, match="source must be classification"):
        _load_mutated(tmp_path, mutate)


def test_patent_data_section_cannot_use_classification_facts(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        _section(data, "geographical_distribution")["source_type"] = "patent_data"

    with pytest.raises(
        TemplateError,
        match="geographical_distribution is patent_data but uses country_specialisation",
    ):
        _load_mutated(tmp_path, mutate)


def test_unused_definition_and_reference(tmp_path: Path) -> None:
    def mutate(data: Data) -> None:
        data["definitions"].append(
            {
                "id": "orphan_def",
                "term": "Orphan",
                "definition": "A definition nobody uses at all.",
                "default": "x",
            }
        )
        data["references"].append(
            {"id": "orphan_ref", "title": "t", "publisher": "p", "year": 2020, "pages": 1}
        )

    with pytest.raises(TemplateError) as exc_info:
        _load_mutated(tmp_path, mutate)
    assert "definitions never used by a metric: ['orphan_def']" in str(exc_info.value)
    assert "references never cited: ['orphan_ref']" in str(exc_info.value)


def test_limitations_are_carried_in_the_methodology(spec: TemplateSpec) -> None:
    methodology = next(s for s in spec.sections if s.id == "scope_and_methodology")
    assert "limitations" in methodology.required_facts
    assert any("limitations" in t.metrics for t in methodology.tables)


def test_key_patent_formula_is_explicit(spec: TemplateSpec) -> None:
    formula = next(d for d in spec.definitions if d.id == "key_patent_formula")
    assert "score =" in formula.definition
    assert formula.needs_owner_decision


def test_ipf_definition_states_how_ep_and_wo_count(spec: TemplateSpec) -> None:
    ipf = next(d for d in spec.definitions if d.id == "international_family")
    assert "PCT (WO) application does not" in ipf.definition
    assert "EPO counts as one office" in ipf.definition


def test_copied_whole_heading_is_detected_but_cover_title_is_not() -> None:
    extract = ReferenceExtract(
        file_name="ref.pdf",
        sha256="0" * 64,
        page_count=3,
        extractor_version="1",
        heading_source="bookmark",
        headings=(
            Heading(level=0, title="Patents for a better tomorrow", page=1, source="bookmark"),
            Heading(level=1, title="Key locations of inventors", page=3, source="bookmark"),
            Heading(level=1, title="Top applicants", page=3, source="bookmark"),
        ),
        captions=(),
        pages_without_text=(),
    )
    text = "We cite 'Patents for a better tomorrow'. Key locations of inventors. Top applicants."

    assert copied_phrases(text, [extract]) == [("ref.pdf", "key locations of inventors")]
