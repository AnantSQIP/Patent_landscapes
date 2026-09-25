# ADR 0004: Template specification as a validated source of truth; PDF tooling

* Status: proposed (Phase 1; accepted when the owner approves the template)
* Date: 2026-09-25

## Decision 1: one validated template spec
`template/plr_template.yaml` is the single definition of what a report contains:
* the canonical definitions (build prompt §7);
* the metric catalog, with each metric's formula, counting unit, source and the canonical
  record fields it needs;
* the chart-type catalog;
* the sections (§9) with their facts, charts, tables, caveats and missing-data behaviour.

`patsquire_plr.template.spec` validates it strictly, including cross-references:
* no unknown or unused metrics, chart types, definitions or references;
* every §9 section present, in order.

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
reference headings or captions. It is evidence, not proof, because only headings and
captions are compared. It runs locally, because the extracts contain copyrighted text and
are not committed.
