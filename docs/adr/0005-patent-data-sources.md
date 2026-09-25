# ADR 0005: Patent data sources are user-selectable adapters

* Status: accepted (Phase 2; adapters are implemented in Phase 3)
* Date: 2026-09-25

## Context
The owner wants to choose, per deployment and per report, where patent data comes from:

* **Local files on the owner's machine**, in four formats: XML bulk data, CSV/Excel exports
  from patent tools, JSON, and PDF documents.
* **Patent APIs** using the owner's own API keys, e.g. Lens.org, EPO OPS, PatentsView, or
  commercial providers.
* **Google Patents, where this is possible.** Checked on 2026-09-25:
  * `patents.google.com/robots.txt` disallows all automated access except individual
    patent pages (`/patent/...`), the home page and sitemaps. Search pages and the internal
    query endpoint are disallowed.
  * There is no official Google Patents search API.
  * Google's supported route for bulk and search access is the **Google Patents Public
    Datasets on BigQuery**. It needs a Google Cloud project and credentials, and queries are
    billed.

## Decision
One `PatentDataSource` interface with one adapter per source type (build prompt §6). A
report run may use several sources. Every stored record carries its source, so results
can always be traced and the sources compared.

| Adapter | Topic search | Notes |
|---|---|---|
| `local_xml` | yes (filters files) | Parses official bulk formats. The dialect (USPTO, EPO DOCDB, ...) is detected and must be a supported one; unknown dialects fail loudly. |
| `local_tabular` | yes (filters rows) | CSV/XLSX exports. Needs a column-mapping profile per exporting tool, and unmapped required columns fail loudly. |
| `local_json` | yes (filters records) | Needs a mapping profile, like `local_tabular`. |
| `local_pdf` | no | Text-based PDFs only: the publication number and any bibliographic fields that can be read reliably. Image-only (scanned) PDFs are reported as not ingestible (no OCR, build prompt §3 step 4). The system can then look up the structured record by that number from another configured source. |
| API adapters (`lens`, `epo_ops`, `patentsview`, ...) | yes | Built from official documentation and recorded real responses, one at a time. The key is referenced by an environment-variable name, never stored in config. |
| `google_bigquery` | yes | Google Patents Public Datasets. Cost per query is estimated and logged before running (dry run). |
| `google_patents_page` | no (lookup by number only) | Fetches `/patent/<number>` pages, which robots.txt allows, at a polite configured rate. Used to enrich known patents, e.g. seeds or numbers from PDFs. Page-structure changes break it loudly via contract tests. |

## Consequences for the Phase 2 canonical schema
* Every raw payload (API response, file, page) is stored **immutably** in object storage,
  with its SHA-256, source ID, adapter version and retrieval time. The database row that
  points to it cannot be updated or deleted (trigger-enforced).
* Normalised documents keep **the original value next to the normalised one**, e.g. the
  raw and normalised publication number.
* Every canonical field is either present or **explicitly missing with a reason**:
  `not_provided_by_source`, `not_applicable`, `unparseable` or `not_requested`. A default
  value is never stored.
* The same publication can arrive from several sources. Each arrival is its own document
  row with its own provenance. De-duplication across sources happens in Phase 4, with
  conflicts between sources logged rather than silently resolved.
