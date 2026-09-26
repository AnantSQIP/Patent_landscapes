# Patsquire PLR: Automated Patent Landscape Reports

Generates Patent Landscape Reports for any technology topic. Every number, name and
patent ID in a report must trace back to retrieved patent data through an audit trail.
The full specification is in [`PLR_System_Build_Prompt.md`](PLR_System_Build_Prompt.md),
and background on PLRs is in [`PLR_Complete_Guide.md`](PLR_Complete_Guide.md).

**Status:** Phases 0–3 are done. Phase 4 (cleaning, families, applicant names) is in review. See [`CHANGELOG.md`](CHANGELOG.md).

## Quick start (WSL / Linux)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker with Compose v2, and `make`
(`sudo apt install make`). On WSL:
* Start Docker Desktop and enable *Settings → Resources → WSL integration* for your distro.
* Keep the repo on the Linux filesystem (e.g. `~/patsquire-plr`), not under `/mnt/c`, where
  file access is 10–20× slower.

Services listen on unusual host ports (Postgres 55432, Redis 56379, MinIO 59000/59001) so
they don't clash with other local stacks. The ports are set in `.env`, and a test checks
they match `config/settings.yaml`.

```bash
make install                  # uv sync --frozen + pre-commit hooks
cp .env.example .env          # then replace every value in .env
make up                       # Postgres+pgvector, Redis, MinIO (+ creates the bucket)
make health                   # uv run plr health --env-file .env ; exit 0 = all healthy
make check                    # lint + mypy --strict + full test suite with coverage gate
```

## Configuration

| Source | Holds | Committed |
|---|---|---|
| `config/settings.yaml` | Every non-secret setting. All keys are required; there are no code defaults. | yes |
| `.env` / environment | Secrets, plus optional overrides `PLR__<SECTION>__<KEY>` | **never** |

Loading fails, with a message that names the field, if a setting is missing, a key is
unknown or misspelled, a value is out of range, or a secret appears in the YAML file.
Secrets are masked in `repr`, in `plr config show` and in logs. See
[ADR 0002](docs/adr/0002-configuration-and-secrets.md).

## Commands

| Command | Purpose |
|---|---|
| `plr health` | Checks Postgres (and that pgvector is available), Redis and the object-storage bucket. Prints a JSON report and exits 1 on any failure. |
| `plr config show` | Prints the resolved configuration with secrets masked. |
| `plr reference extract` | Extracts headings and captions from `reference/*.pdf` to `reference/extracted/` (git-ignored). |
| `plr reference render PDF PAGES...` | Renders pages to PNG to inspect charts. |
| `plr reference check-originality FILES...` | Fails if FILES share an 8-word phrase with reference headings or captions. |
| `plr template validate` | Validates `template/plr_template.yaml` and its cross-references. |
| `plr template definitions` | Prints `docs/definitions.md`, which is generated from the template. |
| `plr db upgrade` / `plr db current` | Applies or shows database migrations. |
| `plr models health` | Makes a real call to every model role (see Models below). Exits 1 on any failure. |
| `plr ingest lookup NUMBERS... [--numbers-file F]` | Fetches publications by number from a data source, stores the raw pages, normalises and reconciles them. Exits 1 if any item failed. |
| `plr ingest folder FOLDER [--scan-only]` | Looks up every patent in a folder of PDFs named by publication number (e.g. `US10123456B2.pdf`). The folder and file hashes are recorded with the batch. `--scan-only` only checks the files. |
| `plr ingest resume BATCH_ID` | Retries only the failed items of a batch. |
| `plr dataset build --batch ID... --name N` | Builds an immutable dataset: one copy per publication, patent families, normalised applicant names. Prints the review report. |
| `plr dataset report DATASET_ID [--output json]` | Reprints the normalisation report (name merges, look-alikes to review, conflicts). |
| `plr cpc load CPCTitleList<YYYYMM>.zip` | Stores an official CPC title list (hash-verified; one version is never replaced). |
| `plr cpc check CODES...` / `plr cpc search TERMS...` | Checks codes against the scheme, or searches CPC titles. |
| `plr landscape create SCOPE.yaml --name N` | Records a landscape scope (topic, publication dates, offices, seeds, confirmed patents). See `examples/scopes/`. |
| `plr taxonomy draft LANDSCAPE_ID` | The reasoner model drafts segments and keywords, then picks CPC codes only from official candidates. Prints editable YAML. |
| `plr taxonomy show` / `import` / `approve` | Shows a version, imports a person's edit as a new version, and records approval or rejection (`--by NAME`). |
| `plr queries generate VERSION_ID` | Generates the query set (EPO OPS CQL, Lens JSON, BigQuery SQL, local) from an approved taxonomy. |
| `plr queries show` / `count` / `recall` / `approve` | Prints queries, counts them over stored batches, checks recall of confirmed patents, and approves the set. |
| `plr discover citations QUERY_SET_ID` | Key-less discovery: follows citations from the seeds (approved query set required). Re-running resumes. |

## Tests

* `make test-unit`: no external services needed.
* `make test-integration`: real Postgres, Redis and MinIO containers via testcontainers
  (needs Docker). These tests fail rather than skip when Docker is missing.
* `make test`: everything, with the 90% coverage gate. This is what CI runs.

## Models

Roles (`embedding`, `bulk_classifier`, `reasoner`, `writer`, `critic`) map to backends in
`config/settings.yaml` under `models:`. Supported backend types are `openai_compatible`
(OpenAI, vLLM, SGLang, Ollama), `anthropic`, `gemini` and `bedrock`. API keys go in `.env`
and are referenced by name (`api_key_env`). For local development:

```bash
docker compose --env-file .env --profile llm up -d ollama
docker compose --env-file .env exec ollama ollama pull qwen2.5:0.5b
docker compose --env-file .env exec ollama ollama pull all-minilm:22m
uv run plr db upgrade --env-file .env
uv run plr models health --env-file .env
```

See [ADR 0006](docs/adr/0006-model-gateway.md) and the verified
[provider API reference](docs/architecture/provider_apis.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first. It covers the branch-per-change workflow, backups
(`scripts/backup.sh`) and rollback recipes.

## Layout

```
src/patsquire_plr/   application package (config, log, health, cli)
scripts/             operational scripts (backup.sh)
tests/unit/          fast tests, no services
tests/integration/   testcontainers-backed tests
config/              committed non-secret configuration
template/            report template specification (plr_template.yaml)
docker/              container images
docs/adr/            architecture decision records
reference/           reference PLR PDFs (git-ignored, copyrighted)
```
