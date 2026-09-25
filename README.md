# Patsquire PLR: Automated Patent Landscape Reports

Generates Patent Landscape Reports for any technology topic. Every number, name and
patent ID in a report must trace back to retrieved patent data through an audit trail.
The full specification is in [`PLR_System_Build_Prompt.md`](PLR_System_Build_Prompt.md),
and background on PLRs is in [`PLR_Complete_Guide.md`](PLR_Complete_Guide.md).

**Status:** Phase 0 (project foundation) is implemented. See [`CHANGELOG.md`](CHANGELOG.md).

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

## Tests

* `make test-unit`: no external services needed.
* `make test-integration`: real Postgres, Redis and MinIO containers via testcontainers
  (needs Docker). These tests fail rather than skip when Docker is missing.
* `make test`: everything, with the 90% coverage gate. This is what CI runs.

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
docker/              container images
docs/adr/            architecture decision records
reference/           reference PLR PDFs (git-ignored, copyrighted)
```
