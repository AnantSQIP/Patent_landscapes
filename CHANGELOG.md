# Changelog

All notable changes are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added: Phase 5, scope, taxonomy and key strings
- **Official CPC scheme:** the CPC Title List 2026.08 (254,314 entries) is stored
  hash-verified and parsed exactly. It supports checking codes, title search and the
  hierarchy (`plr cpc`).
- **Landscapes:** scope files, taxonomy versions, approvals, query sets, query counts,
  discovery runs and recall checks. All are append-only (migration 0007).
- **Taxonomy drafting:** the model writes the language (segments, keywords), and code
  finds official CPC candidates. The model may choose codes only from those candidates.
  Rejected terms, codes and repeated segments are recorded. People edit YAML, and each
  edit is imported as a new version.
- **Approval gate** (`landscape.approval_mode`: human or automatic). Automatic approvals
  are recorded as automatic.
- **Deterministic key strings** for EPO OPS CQL, Lens JSON and BigQuery SQL, with syntax
  sources in docs/architecture/query_syntax.md. PatentsView is skipped while its API is
  offline.
- **Local counts and recall** over stored records, where records that cannot be evaluated
  make a count a lower bound.
- **Citation expansion** from seed patents as key-less discovery. It is resumable, and
  every excluded candidate has a reason.
- `StructuredResult.cache_key` (provenance of model calls); read-only `batch_summary`.
- `cli_common` / `cli_landscape` split out of `cli.py`. ADR 0010.

### Added: user PDF folders
- `plr ingest folder FOLDER`: publication numbers come from the file names, and a data
  source supplies the records. Each file's name, size and sha256 are recorded as batch
  provenance. Numbers are cross-checked against the PDF text layer where one exists.
  `--scan-only` only checks the files. See ADR 0009.
- Google Patents adapter v4: when a number's kind code is not found, it retries once with
  the bare number (e.g. reissue `E` listed as `E1`). The served document is kept only if its
  kind letter matches the request. A failed retry stays `failed`, so it can be resumed.
- **Review fixes:**
  * whole-number text check;
  * absolute folder path in provenance;
  * unreadable files are reported;
  * `--scan-only` needs no settings;
  * `pypdfium2` is declared as a dependency.

### Added: Phase 4, cleaning, patent families, applicant names
- Stated family members captured from sources (Google "Also Published As"; migration 0005,
  with a `not_requested` backfill for older documents).
- Immutable dataset snapshots (migration 0006), with:
  * one copy per publication;
  * logged copy conflicts;
  * union-find families from stated members, family IDs and applications;
  * normalised applicants;
  * conservation checks.
- Applicant name rules (formatting only, idempotent), a human-curated alias file (ships
  empty), and look-alike pairs listed for review.
- `plr dataset build` / `plr dataset report` (Markdown or JSON), ADR 0008.
- Parsing fixes: Singapore check-letter numbers are accepted, and an unparseable entry in a
  family or citation list marks only that field (adapter v3).
- **Review fixes:**
  * Family evidence is recorded only where it linked publications, and family IDs are
    scoped by source.
  * The family definition is stated honestly (`source_stated_family`), and asymmetric family
    statements are reported.
  * Conservation now reconciles family counts and applicant rows.
  * The report shows alias merges, unknown family members and family warnings.
  * Name rules v2 keep combining marks.
  * The migration 0005 downgrade removes its backfill.


### Added: Phase 3, patent data ingestion
- Content-addressed raw payload store on S3/MinIO: payloads are never overwritten, and
  every read is hash-verified.
- Lookup batches. Each item's raw record, documents or quarantine entry, and outcome commit
  together. Batches reconcile (requested = stored + quarantined + not found + invalid),
  count mismatches fail loudly, failed items are resumable, and start and finish are
  audit-logged.
- Google Patents page adapter (lookup by number):
  * reads the original-language page's microdata;
  * stores office text only (OCR and machine translations are recorded as `unparseable`);
  * maps legal status through a fixed table, keeping the raw text;
  * polite rate limit and bounded retries;
  * refuses redirects outside `/patent/`.
- Migration 0003: `ingest_item` (append-only) and the `active` legal-status category.
- **Review fixes:**
  * Complete citation lists: forward citations were a per-family subset.
  * Examiner markers kept on non-patent citations.
  * Grant date set only on the grant publication.
  * Translated titles recorded as unparseable.
  * Requests de-duplicated by normalised number, and the served publication must match the
    request.
  * New `duplicate` outcome (migration 0004).
  * Per-batch advisory lock; crashed runs are marked failed and resumable, with the batch
    ID printed first.
  * Resume checks the adapter version.
- `plr ingest lookup` / `plr ingest resume`, the `data_sources` config section, and ADR 0007.
- Contract tests on six real recorded pages. A real ingest of 10 numbers reconciled 100%
  (8 stored, 1 not found, 1 invalid, 1 duplicate ignored).


### Added: Phase 2, data model, provenance, fact store, audit log, model gateway
- **Canonical `PatentDocument`:**
  * every field has a value or an explicit missing reason;
  * raw identifiers are kept next to normalised ones;
  * publication numbers and CPC/IPC codes are parsed strictly;
  * it maps to every template record field.
- **Schema:** PostgreSQL schema and Alembic migration 0001, with pgvector. Append-only
  triggers block UPDATE, DELETE and TRUNCATE on raw records, documents, facts, the audit log
  and model calls. An ORM-vs-migration drift test keeps them in sync.
- **Documents:** exact store/load round trip for canonical documents.
- **Audit log:** hash-chained, with serialised appends and tamper detection (`verify_chain`).
- **Fact store (Layer 3):** facts are accepted only when at least two implementations agree
  exactly on the same inputs. Floats are banned in favour of Decimal, and mismatches are
  kept as evidence.
- **Model gateway:**
  * backends, five roles and four provider adapters, verified against the current SDKs;
  * persistent cache and append-only call log;
  * retries, rate limits, and strict structured output with repair turns.
- **`plr models health`,** plus an Ollama compose service. Live tests make real model calls
  and show that switching backends is configuration-only.
- `plr db upgrade/current`, ADRs 0005 (data sources) and 0006 (model gateway), and the
  provider API reference.
- **Review fixes:**
  * Only complete model output is accepted; filtered, guardrailed and context-overflow
    output fails instead of being cached.
  * Unclassified adapter exceptions are logged.
  * Embedding cache hits are logged per text, and batch rows list every key.
  * Bedrock embeddings are rate-limited and retried per request.
  * The structured cache stores the exact validated text.
  * The cache key includes the server's URL and region.
  * Temperature 0 and a seed are enforced, and prices must be set in pairs.
  * Migration 0002: positional keys for repeated CPC codes and citations, and
    `ingest_batch.source_api_version` (§6).


### Added: Phase 1, reference analysis and template specification
- `plr reference extract/render`: structure extraction (PDF outline or font-size headings,
  figure/table/box captions, pages without text) and page rendering for the reference PLRs.
  Uses pdfplumber/pypdfium2, not PyMuPDF (AGPL).
- `docs/reference_analysis.md`: per-report structure, counting rules, search disclosure,
  metrics and charts, a cross-report comparison, and the design decisions taken.
- `template/plr_template.yaml`, validated by `patsquire_plr.template.spec`:
  * 15 canonical definitions (including an explicit key-patent formula);
  * 42 metrics (including a limitations register);
  * 17 chart types;
  * 18 §9 sections, plus a per-segment profile block and 9 appendices.

  Cross-reference checks reject unknown or unused items and missing or out-of-order sections.
  Consistency rules reject metrics missing the record fields their definitions need, time
  series that don't use the time basis, and sections whose source type contradicts their facts.
- `docs/definitions.md`, generated from the template and kept in sync by a test.
- `plr reference check-originality` (principle 9). It finds no copied phrases in the Phase 1
  documents.
- ADR 0004.

### Fixed
- `.gitignore` rule `reference/` also ignored `src/patsquire_plr/reference/` (and hid it from
  ruff). It is now anchored as `/reference/`.


### Added: Phase 0, project foundation
- `uv`-managed Python 3.12 package `patsquire_plr` with a lockfile.
- Tooling: ruff (lint + format), `mypy --strict` (explicit `Any` disallowed), pytest,
  hypothesis, pytest-cov (90% gate), pre-commit, and a GitHub Actions CI workflow
  (checks job + compose job that runs `plr health` inside the app image).
- Configuration: YAML + environment/dotenv with strict validation. All keys are required,
  unknown sections/keys are rejected from every source, empty secrets are rejected, secrets are forbidden in YAML, and errors never echo input values.
- Structured JSON logging with recursive redaction of credential-like keys and `SecretStr`.
  The safe config is applied on import, so tracebacks never render local variables.
- `docker-compose.yml`: Postgres 16 + pgvector, Redis 7, MinIO, and a one-shot bucket
  creation job (versioning enabled). The app image runs as a non-root user.
- `plr health` / `plr config show` CLI.
- ADRs 0001 (stack and layout), 0002 (configuration and secrets) and 0003 (object storage
  image: MinIO upstream is discontinued, so the final release is pinned by digest).
- Consistency tests: compose images equal the integration-test images, there are no
  `:latest` tags, and host ports and credentials in `.env.example` match `settings.yaml`.
- Git workflow rules (`CONTRIBUTING.md`, `.gitignore` header) and `scripts/backup.sh`
  (verified git bundles outside the repo).
