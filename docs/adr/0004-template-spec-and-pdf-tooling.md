# ADR 0004: Template specification as a validated source of truth; PDF tooling

* Status: accepted (Phase 1; template approved by the owner on 2026-09-25 with default options)
* Date: 2026-09-25

## Decision 1: one validated template spec
`template/plr_template.yaml` is the single definition of what a report contains:
* the canonical definitions (build prompt §7);
* the metric catalog, with each metric's formula, counting unit, source and the canonical
  record fields it needs;
* the chart-type catalog;
* the sections (§9) with their facts, charts, tables, caveats and missing-data behaviour.

`patsquire_plr.template.spec` validates it strictly:
* **Cross-references:** no unknown or unused metrics, chart types, definitions or references,
  and every §9 section present, in order.
* **Field sufficiency:** a metric that uses a definition must list the record fields that
  definition needs. For example, using `time_basis` requires `priority_date`, and
  `forward_citation_window` requires `forward_citations` and `publication_date`.
* **Time consistency:** any metric that places families in time must use `time_basis`, and
  any metric that excludes incomplete periods must use `incomplete_period`.
* **Source consistency:** a `patent_data` section may only use `patent_data` metrics, and
  segment metrics must come from `classification`.

`docs/definitions.md` is generated from the template, and a test fails if it is stale.

Later phases consume the same file:
* Phase 2 implements `RecordField` as the canonical schema.
* Phase 7 implements each metric twice (Layer 3).
* Phase 8 implements each chart type.
* Phase 10 renders the sections.

A metric the template needs but the code lacks will therefore be a test failure, not a
silent gap.

## Decision 2: pdfplumber + pypdfium2, not PyMuPDF
The build prompt suggests PyMuPDF or pdfplumber. PyMuPDF is AGPL-3.0, which would impose
copyleft obligations on a commercial product. pdfplumber (MIT, on pdfminer.six, MIT) and
pypdfium2 (Apache-2.0/BSD-3) cover text, outline and page rendering. fpdf2 (LGPL-3.0) is a
**dev-only** dependency used to generate test PDFs.

## Decision 3: an originality check for principle 9
`plr reference check-originality` flags any 8-word phrase that our documents share with
reference headings or captions. It also flags any whole heading of 4+ words, since headings
are usually too short for 8-word matching. Cover-page titles are exempt because citing a
report means quoting its title. The check is evidence, not proof, because only headings and
captions are compared. It runs locally, because the extracts contain copyrighted text and
are not committed.
