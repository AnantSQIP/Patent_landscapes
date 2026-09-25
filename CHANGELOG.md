# Changelog

All notable changes are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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
