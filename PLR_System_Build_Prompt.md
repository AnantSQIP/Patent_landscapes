
# Build Prompt: Patsquire Automated Patent Landscape Report (PLR) System

> **How to use this file:** Give this entire document to your AI coding agent at the root of the project folder. The agent must work **phase by phase**, stop at the end of each phase, report results, and wait for approval before continuing.

---

## 0. Your Role and Mission

You are a senior software architect and engineer building a **production-grade system that automatically generates Patent Landscape Reports (PLRs)** for any technology topic.

A user enters a topic (e.g. "Large Language Models"), a date range, jurisdictions, and optional classification codes. The system searches patent data, filters and classifies patents, computes statistics, generates charts and narrative, and produces a complete, professional PLR, with no page limit.

**The single most important requirement is truthfulness.** The report must contain **only facts that are derived from retrieved patent data or explicitly cited sources**. No assumptions, no invented numbers, no invented patents, no invented companies, no invented trends. Every number, name, patent ID and claim in the final report must be traceable to its source data through an automated audit trail.

---

## 1. Non-Negotiable Principles (apply to every line of code)

1. **No fabrication, ever.** The system must never generate, estimate, or "fill in" data. If data is missing, the report must say it is missing. It must not guess.
2. **Code computes, LLMs describe.** Every number (counts, percentages, rankings, growth rates) is computed by deterministic code. LLMs are only used for language tasks: topic understanding, query term suggestion, relevance judgment, classification, and writing narrative *from supplied facts*.
3. **Full provenance.** Every fact has a lineage: source record(s) → transformation steps → computed value → where it appears in the report.
4. **Fail loudly, never silently.** No silent fallbacks, no swallowed exceptions, no default values that hide missing data. If a verification check fails, the pipeline stops or the affected content is removed and flagged.
5. **Measure what cannot be guaranteed.** Deterministic calculations must be exactly correct relative to the source data (zero tolerance). AI judgments (relevance, classification) cannot be mathematically guaranteed. Their accuracy must therefore be **measured, reported in the methodology section, and kept above configured thresholds**, and uncertain items must be routed to "unclassified" or human review, never silently forced into a category.
6. **Reproducibility.** Given the same raw data snapshot, configuration, model versions and prompts, the system must produce the same statistics and charts. All LLM calls use temperature 0 (or lowest supported), fixed seeds where supported, versioned prompts, and a result cache.
7. **No placeholder or mock data in production code paths.** Mocks and fixtures exist only in tests and must be clearly labelled.
8. **Don't invent APIs.** Never guess patent API fields, endpoints, or library functions. Read the official documentation, inspect real responses, and ask the user when unsure.
9. **Respect copyright.** Reference PLRs are used to learn **structure** (sections, chart types, metrics, methodology practices). Never copy their text into templates or generated reports.

---

## 2. Reference Material Available in This Folder

- **Sample Patent Landscape Report PDFs** already exist in this project folder. Locate them by searching the folder recursively for `*.pdf`. If you cannot find them or the location is ambiguous, ask the user for the path. Do not proceed without them in Phase 1.
- The user may also have placed markdown notes (e.g. an explanation of PLR concepts) in the folder. Read any `*.md` files that describe PLRs or this project.

---

## 3. Pipeline Flow (must be implemented exactly in this order)

| #  | Step                                                                  | Performed by                                                                                    | Why                                                                                          |
| -- | --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 1  | User enters topic + date range + countries (+ optional CPC/IPC codes) | UI / API                                                                                        | Sets the scope so every later step searches and counts the same patent universe              |
| 2  | Understand topic → draft taxonomy (segments)                         | LLM                                                                                             | Splits the topic into sub-areas that drive queries and report chapters                       |
| 3  | Key String Preparer → keywords, synonyms, CPC/IPC codes, API queries | LLM + classification lookup, then code                                                          | One keyword misses patents; synonyms + codes improve coverage; code formats exact API syntax |
| 4  | Patent API → fetch records (structured data, no OCR)                 | Code                                                                                            | APIs return structured fields; OCR is unnecessary and error-prone                            |
| 5  | Clean: remove duplicates, group into families, merge company names    | Code (rules), LLM only for suggesting name merges that require human/deterministic confirmation | Prevents double counting and split rankings                                                  |
| 6  | Relevance filtering → keep only truly relevant families              | Embeddings + LLM                                                                                | Keyword results are noisy; embeddings score cheaply, LLM judges borderline cases             |
| 7  | Classify each family into segments                                    | LLM / embeddings                                                                                | Enables segment analysis and applicant × segment comparisons                                |
| 8  | Compute statistics                                                    | Code only, never LLM                                                                            | Numbers must be exact and repeatable                                                         |
| 9  | Generate charts (Plotly / Vega-Lite)                                  | LLM chooses chart spec → code draws from real data                                             | Accurate, consistent, interactive; image-generation models are forbidden for charts          |
| 10 | Write narrative                                                       | LLM, fed only computed facts                                                                    | Readable insights without invented facts                                                     |
| 11 | Automatic verification                                                | Code (+ LLM critic as secondary check)                                                          | Every number, entity and claim in text must match the fact store                             |
| 12 | Fill template → live web report + PDF/HTML export                    | Code                                                                                            | Standard professional structure; editable live report; shareable export                      |

Every step must persist its inputs, outputs, configuration and version metadata so it can be inspected and re-run independently.

---

## 4. Pluggable LLM / Model Backends (required feature)

The user must be able to choose, per deployment **and per pipeline role**, where models run. No backend may be hard-coded.

### Supported backend types

1. **Hosted LLM APIs** using the user's API keys (e.g. Anthropic, OpenAI, Google Gemini, Amazon Bedrock).
2. **Self-hosted models on the user's own on-premises GPUs** served via an OpenAI-compatible server (vLLM or SGLang).
3. **Self-hosted models on AWS GPUs** (e.g. EC2 g6e / p5) served the same way.

### Model roles (each independently configurable)

- `embedding`: embeddings for similarity scoring
- `bulk_classifier`: relevance filtering and segment classification (high volume)
- `reasoner`: taxonomy, query generation, chart spec selection, key-patent summaries
- `writer`: narrative generation
- `critic`: independent verification of narrative claims and QA sampling (should be a different model from `writer` where possible)

### Requirements

- Implement a **Model Gateway** abstraction with a single interface for chat/completion, structured (JSON-schema) output, and embeddings. Provider adapters sit behind it.
- Configuration via a YAML file + environment variables (secrets never committed; support `.env` and a secrets manager).
- Every call records: provider, model name, model version/revision, prompt template ID + version, parameters, input hash, output, token counts, latency, cost estimate.
- **Structured outputs only** for any machine-consumed response. Validate against a JSON schema; on validation failure, retry with a bounded count, then fail loudly.
- A persistent **LLM result cache** keyed by (input hash, prompt version, model, parameters).
- Rate limiting, retries with exponential backoff, and timeouts per provider.
- A health-check command that verifies every configured backend responds correctly before a report run starts.
- Changing the backend must not change any code outside configuration.

---

## 5. Technology Stack (recommended; justify any deviation)

- **Language:** Python 3.12+ for the backend and pipeline; TypeScript for the frontend.
- **API:** FastAPI with Pydantic v2 (strict mode) for all data models.
- **Database:** PostgreSQL 16+ with `pgvector`; SQLAlchemy 2.x + Alembic migrations.
- **Analytics:** DuckDB and/or Polars for statistics; SQL in PostgreSQL as the independent second implementation (see Section 8, Layer 3).
- **Jobs/orchestration:** a durable workflow engine (Prefect, Temporal, or Celery + Redis). Every step is resumable and idempotent.
- **Cache/queue:** Redis.
- **Object storage:** S3-compatible storage (AWS S3 or MinIO on-prem) for raw API responses, snapshots, exports.
- **Charts:** Plotly or Vega-Lite specs generated from data; rendered in the browser for the live report and to static SVG/PNG for PDF.
- **Report rendering:** Jinja2 HTML templates → PDF via WeasyPrint or Playwright; optional DOCX export.
- **Frontend:** React / Next.js live report editor with block-based layout.
- **Quality tooling:** `ruff`, `mypy --strict`, `pytest`, `hypothesis` (property-based tests), `pytest-cov`, `mutmut` (mutation testing), `testcontainers` (real Postgres/Redis in tests), `pre-commit`.
- **Observability:** structured JSON logging, OpenTelemetry traces, per-run audit logs.
- **Deployment:** Docker + docker-compose for local/on-prem; infrastructure notes for AWS.

---

## 6. Patent Data Source Layer

- Implement a **Patent Data Source interface** with adapters. The data provider is not fixed; ask the user which source(s) they have access to (e.g. Lens.org API, EPO OPS, PatentsView, Google Patents BigQuery, PATSTAT, or a commercial provider).
- Build **one adapter first**, fully tested against real responses, before adding more.
- Read the provider's official API documentation. Record exactly which fields exist and their semantics (e.g. which date is the priority date, what family ID type is provided: DOCDB simple family vs INPADOC extended family).
- **Store every raw API response immutably** with: query, timestamp, provider, API version, response hash. The report must state the data retrieval date.
- Normalize into a canonical internal schema (publication, application, family, applicants/assignees, inventors, dates, jurisdiction, kind code, CPC/IPC, title, abstract, claims, legal status, citations) **without losing the original values**. Keep raw fields alongside normalized ones.
- Handle pagination, rate limits, retries, partial failures, and resumption. The ingest must reconcile: records reported by the API vs records stored. Any mismatch fails the step.
- Where a field is missing for a record, store it as explicitly missing (null with reason), never as a default value.

---

## 7. Canonical Definitions (must be documented and used consistently)

Create a `docs/definitions.md` and a matching code module. Every metric in the report references these definitions, and the methodology section prints them.

- **Counting unit:** patent **family** by default (state which family definition is used). Document-level counts only where explicitly labelled.
- **Time axis:** earliest priority year by default (configurable to filing or publication year). State this on every trend chart.
- **Publication lag:** mark the most recent ~18–24 months as incomplete in charts and text, computed from the data retrieval date.
- **Applicant/assignee normalization:** deterministic rules (case, punctuation, legal suffixes, known aliases table) + an explicit, versioned alias mapping file. LLM may *suggest* merges, but merges only apply after deterministic confirmation or human approval. Every merge is logged.
- **Jurisdiction metrics:** distinguish (a) filing office / where protection is sought, (b) inventor country, (c) applicant country. Never mix them.
- **International family:** family with filings in ≥2 distinct offices (define whether EP and WO count as offices).
- **Legal status:** use only what the data source provides, report "as of" retrieval date, and show "unknown" explicitly. State that this is not legal advice.
- **Key / influential patent metrics:** define formulas explicitly, e.g. forward citations (optionally age-normalized), family size, number of jurisdictions. No subjective "importance" without a formula.
- **Growth metrics:** CAGR and period-over-period change, with the exact periods printed; exclude incomplete years from growth calculations by default.

---

## 8. Verification Architecture (multi-layer, mandatory)

The target is **zero errors in deterministic content** and **measured, disclosed, threshold-controlled accuracy for AI-derived content**.

**Layer 1: Ingest validation.** Every record is validated against the canonical schema. Invalid records are quarantined with reasons, counted, and reported. Never silently dropped.

**Layer 2: Conservation / reconciliation checks.** At every step, counts must balance: `input = kept + excluded (with reason codes)`. Family grouping must be verified (every document belongs to exactly one family; family counts reconcile with document counts).

**Layer 3: Dual independent computation.** Every statistic in the fact store is computed by **two independent implementations** (e.g. DuckDB/Polars and PostgreSQL SQL, written separately). Results must match **exactly**. Any mismatch stops the pipeline.

**Layer 4: AI classification quality control.**

- Build an evaluation harness with a **human-labelled gold set** per topic (the UI must support quick labelling of a random sample, e.g. 200–500 families).
- Compute precision, recall, and F1 with confidence intervals for relevance and for each segment.
- Use agreement between two independent methods/models (e.g. embedding classifier + LLM, or two LLMs). Disagreements and low-confidence items go to an `uncertain` bucket and a human review queue.
- Configurable minimum thresholds. If not met, the run is marked "not publishable" until resolved.
- A **recall check**: seed patents / known key players supplied by the user or confirmed by the user must be found by the search; missing ones are reported.
- The measured accuracy figures are printed in the methodology section.

**Layer 5: Narrative claim verification.**

- The writer LLM receives only a structured **Fact Store** subset (IDs, values, definitions) and must output **structured claims**, each referencing fact IDs, rendered to prose by a template or constrained generation.
- A deterministic verifier checks every claim: every number matches its referenced fact (exact, with defined rounding rules); every entity name exists in the dataset; every patent number exists in the dataset; comparative words ("largest", "fastest-growing", "declined", "doubled") are checked against the facts by rule.
- A second model (`critic`) checks that each sentence is fully supported by its referenced facts and flags unsupported interpretation.
- Any unsupported sentence is removed or regenerated (bounded retries). Nothing unverified reaches the report.
- Interpretive statements (e.g. "this suggests…") must be labelled as interpretation and must be grounded in cited facts.

**Layer 6: Chart verification.** Chart specs are generated from Fact Store tables only. Automated tests assert that the data embedded in each chart spec equals the corresponding fact table exactly (values, labels, ordering, units, axis definitions).

**Layer 7: External-context content.** The Technology Overview section may require background knowledge not in patent data. It must either (a) be generated only from user-supplied or cited sources with citations, or (b) be clearly marked as "AI-drafted background, requires expert review" and blocked from final export until approved. No uncited background facts in a final report.

**Layer 8: Final audit.**

- Generate an **Audit Appendix** and a machine-readable **audit manifest** (JSON): data snapshot hashes, configuration, model versions, prompt versions, every fact with lineage, every verification result.
- Re-run statistics from the stored snapshot and confirm identical output hashes before export.
- The final export is only allowed when all blocking checks pass. Non-blocking warnings are listed in the report's limitations section.

---

## 9. Report Template: Required Sections

The template is derived in Phase 1 from the reference PDFs **plus** the sections below. Each section must specify: purpose, required facts, required charts/tables, data source, and caveats. If data for a section is unavailable, the section states this explicitly rather than being filled with generic text.

| #  | Section                         | Content                                                                                                                                               | Source type                                                   |
| -- | ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| 1  | Executive Summary               | Headline findings, each linked to facts                                                                                                               | Verified narrative from Fact Store                            |
| 2  | Technology / Topic Overview     | What the technology is, why it matters                                                                                                                | Cited external sources or flagged for expert review (Layer 7) |
| 3  | Scope and Methodology           | Topic definition, date range, jurisdictions, data source, retrieval date, counting unit, definitions, limitations, measured AI accuracy               | System metadata                                               |
| 4  | Search Strategy                 | Full keyword lists, CPC/IPC codes, exact query strings, result counts per query, recall check results                                                 | Key String Preparer logs                                      |
| 5  | Patent Dataset                  | Records retrieved, duplicates removed, families formed, excluded (with reasons), final dataset size, data completeness per field                      | Pipeline reconciliation logs                                  |
| 6  | Patent Filing Trends            | Families per priority year, growth rates, incomplete-year marking                                                                                     | Pure patent data                                              |
| 7  | Top Assignees / Applicants      | Rankings by families, growth, new entrants, normalization notes                                                                                       | Pure patent data + alias table                                |
| 8  | Top Inventors                   | Most active inventors, affiliations where data allows                                                                                                 | Pure patent data                                              |
| 9  | Geographical Distribution       | Filing offices, inventor countries, applicant countries (kept separate)                                                                               | Pure patent data                                              |
| 10 | Technology Segmentation         | Segment sizes, growth, multi-label overlap, unclassified share                                                                                        | AI classification + deterministic counting                    |
| 11 | Patent Family Analysis          | Family sizes, international families, PCT usage                                                                                                       | Pure patent data                                              |
| 12 | Legal Status                    | Status distribution as of retrieval date, unknown share                                                                                               | Pure patent data                                              |
| 13 | Key Patent / Portfolio Analysis | Top patents by defined metrics; portfolio profiles of top applicants                                                                                  | Pure patent data + verified summaries                         |
| 14 | Competitive Landscape           | Applicant × segment matrix, specialization, co-applicant networks                                                                                    | Pure patent data + classification                             |
| 15 | Technology / Patent Trends      | Segment growth over time, emerging segments, CPC trend shifts                                                                                         | Pure patent data + classification                             |
| 16 | White-space Analysis            | Low-density / high-growth areas, segment × applicant gaps, CPC co-occurrence gaps; always labelled as**indicative, based on patent data only** | Deterministic metrics + labelled interpretation               |
| 17 | Key Findings                    | Consolidated verified findings                                                                                                                        | Verified narrative                                            |
| 18 | Charts, Graphs, Patent Maps     | Integrated throughout: trends, rankings, heatmaps, maps, networks, bubble charts, segment × time                                                     | Fact Store → chart specs                                     |
| +  | Appendices                      | Queries, codes, definitions, full family list with links, alias table, AI evaluation results, audit summary, glossary                                 | System output                                                 |

---

## 10. Live Report Model

- A report is a **structured document of blocks** (text, chart, table, callout), stored as JSON, not a static PDF.
- Each chart/table block stores its **data query spec** and **chart spec**, so it re-renders from data at any time.
- Users can reorder, remove, and edit blocks, and **prompt the system to add a new chart**. The LLM translates the prompt into a query spec using only fields in a defined metrics catalog. The spec is validated, executed deterministically, verified (Layer 6), and captioned with verified text (Layer 5).
- The system must classify requests as: (a) re-slice existing data (instant), (b) needs a new derived label (re-classification run), (c) needs new data (new retrieval). It must tell the user which, and never answer (b) or (c) from existing data.
- Drill-down: every chart element links to the underlying patent families.
- Versioned report snapshots; exports are generated from a frozen snapshot.

---

## 11. Code Quality and Continuous Self-Testing

- `mypy --strict` clean, `ruff` clean, no `Any` in core modules without justification.
- Test coverage ≥ 90% for core pipeline and analytics modules; mutation testing score target ≥ 80% for analytics and verification modules.
- Test types required:
  - Unit tests for every transformation and metric.
  - **Property-based tests** (hypothesis) for statistics: conservation of counts, ranking invariants, percentages summing correctly, idempotence of cleaning.
  - **Golden dataset tests:** a small, hand-verified patent dataset with manually computed expected statistics; outputs must match exactly.
  - Integration tests with real Postgres/Redis via testcontainers.
  - Contract tests for each patent data adapter using recorded real responses.
  - LLM gateway tests with recorded responses; schema-violation and timeout handling tests.
  - End-to-end test producing a full small report and passing all verification layers.
- **Continuous self-verification:** a scheduled "canary" job that runs the golden dataset through the full pipeline and alerts on any change in outputs; CI runs the full test suite on every change.
- Documentation: architecture decision records (ADRs), module READMEs, and a runbook.

---

## 12. Build Phases (stop after each phase for review)

At the end of **every** phase: run all tests, report results, list what was built, list open questions and risks, and **wait for user approval** before starting the next phase.

### Phase 0: Project Foundation

- Repository structure, tooling (ruff, mypy, pytest, pre-commit), CI pipeline, Docker compose (Postgres + pgvector, Redis, object storage).
- Configuration system and secrets handling.
- **Acceptance:** CI green; `docker compose up` brings up all services; health checks pass.

### Phase 1: Reference PLR Analysis → Template Specification

- Locate all PDFs in the project folder (Section 2). Extract text, headings, and figure/table captions (PyMuPDF / pdfplumber); render pages to images where needed to understand charts.
- Produce `docs/reference_analysis.md`: for each report, its section structure, chart types, metrics used, methodology practices (search strategy disclosure, counting units, limitations).
- Produce a cross-report comparison and a **template specification** (`template/plr_template.yaml`) merging the best practices with the required sections in Section 9, with a fact/chart requirement list per section.
- Do not copy any text from the reference reports.
- **Acceptance:** user reviews and approves the template specification.

### Phase 2: Core Data Model, Provenance, and Model Gateway

- Canonical schema, database migrations, Fact Store design with lineage, audit log design.
- Model Gateway with all three backend types (hosted API, on-prem OpenAI-compatible, AWS OpenAI-compatible), all five roles, caching, structured outputs, health checks.
- **Acceptance:** tests pass; switching backends via config only is demonstrated.

### Phase 3: Patent Data Ingestion (Step 4)

- Confirm the data provider with the user. Implement one adapter from official docs and real responses.
- Immutable raw storage, normalization, reconciliation checks, resumable ingestion.
- **Acceptance:** real ingest of a small query with 100% reconciliation; contract tests pass.

### Phase 4: Cleaning, Families, Name Normalization (Step 5)

- Deduplication, family grouping, deterministic assignee normalization with alias file and merge logs.
- **Acceptance:** conservation checks and property tests pass; normalization report reviewed by user.

### Phase 5: Taxonomy and Key String Preparer (Steps 1–3)

- Topic input, LLM taxonomy drafting (user-editable), synonym/keyword generation, CPC/IPC lookup from an official classification source, exact query generation per provider syntax, per-query result counts.
- Optional user approval gate for taxonomy and queries (configurable for minimum-interaction mode).
- **Acceptance:** queries execute correctly; recall check against user-confirmed seed patents reported.

### Phase 6: Relevance Filtering and Classification (Steps 6–7)

- Embedding pipeline (pgvector), borderline routing to LLM, multi-label segment classification, uncertainty bucket, human review queue, gold-set labelling UI, evaluation harness with confidence intervals.
- **Acceptance:** measured precision/recall reported; thresholds enforced; all decisions logged with reasons.

### Phase 7: Analytics Engine and Fact Store (Step 8)

- All metrics from Section 7 and Section 9, each implemented twice independently (Layer 3), golden dataset tests, property tests.
- **Acceptance:** 100% agreement between implementations; golden tests exact; mutation score target met.

### Phase 8: Charts (Step 9)

- Chart spec generation from Fact Store; chart catalog covering all required visual types; static rendering for PDF; chart data verification tests (Layer 6).
- **Acceptance:** every chart's embedded data equals its fact table in automated tests.

### Phase 9: Narrative and Claim Verification (Steps 10–11)

- Structured-claim generation, deterministic verifier, critic model check, bounded regeneration, interpretation labelling, external-context handling (Layer 7).
- **Acceptance:** adversarial tests (deliberately wrong numbers, invented entities, unsupported comparatives) are all caught.

### Phase 10: Report Assembly and Export (Step 12)

- Block-based report model, template rendering, HTML and PDF export, appendices, audit manifest, reproducibility re-run check (Layer 8).
- **Acceptance:** full report generated end-to-end on a real topic; all blocking checks pass; export blocked when a check is forced to fail.

### Phase 11: Web Application and Live Report

- Report creation UI (topic, dates, jurisdictions, codes, backend selection per role), job progress, review gates, live editor, prompt-to-chart, drill-down, versioning.
- **Acceptance:** user completes a full run from the UI and adds a verified custom chart by prompt.

### Phase 12: Hardening, Performance, and Deployment

- Load tests for large datasets (≥100,000 documents), cost/time logging per step, canary self-test job, deployment guides for on-prem GPU, AWS GPU, and hosted-API setups.
- **Acceptance:** documented run times and costs from real runs; canary job running; runbook complete.

---

## 13. Working Rules for the Coding Agent

- Before each phase, write a short plan and list assumptions; ask the user about anything uncertain instead of guessing.
- Never mark a task done without running its tests and showing results.
- Never use fabricated sample data outside test fixtures; never hard-code topic-specific values.
- Keep a `CHANGELOG.md` and update ADRs for significant decisions.
- If a requirement in this document cannot be met exactly, stop and explain why, with options.
- Report honestly: known limitations, measured error rates, and anything not yet verified.

---

## 14. Definition of Done for a Generated PLR

A PLR may be exported as final only if:

1. All ingest, reconciliation, and dual-computation checks pass.
2. AI relevance/classification accuracy meets configured thresholds, and measured figures are printed in the methodology.
3. Every number, entity, patent ID and claim in the text passes claim verification.
4. Every chart passes data verification.
5. External background content is cited or expert-approved.
6. The audit manifest is generated and the reproducibility re-run matches.
7. All limitations and warnings are disclosed in the report.
