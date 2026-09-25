# ADR 0001: Technology stack and repository layout

* Status: accepted (Phase 0)
* Date: 2026-09-25

## Context
The build prompt (§5) recommends a stack and asks for any deviation to be justified.

## Decision
* We follow the recommended stack. Phase 0 installs only what it uses:
  * pydantic, plus python-dotenv and PyYAML for configuration
  * SQLAlchemy 2 + psycopg 3
  * redis-py
  * boto3, for S3-compatible storage (MinIO on-prem, S3 on AWS)
  * structlog
  * typer

  Later phases add the rest when they first need it: FastAPI, Alembic, DuckDB/Polars, a
  workflow engine, Plotly, Jinja2/WeasyPrint and Next.js.
* We use **uv** as the package manager and lockfile (`uv.lock`), so installs are
  reproducible (build prompt principle 6). CI and Docker both install with `--frozen`.
* We use a src layout: `src/patsquire_plr`.
* Tests are split into `tests/unit` (no services) and `tests/integration` (testcontainers).
  Integration tests are selected with a marker and are never auto-skipped: a missing Docker
  daemon makes them fail, in line with principle 4 (fail loudly).
* One coverage gate (90%) applies to the combined suite. Service-dependent success paths
  are only reachable in integration tests.

## Deviations
* **No pydantic-settings.** Its environment source silently ignores misspelled section
  names (`PLR__DATABSE__HOST`), and with `extra="forbid"` it rejects the non-`PLR__` lines
  that docker-compose needs in the same `.env`. A ~60-line explicit loader enforces both
  rules and is fully tested.
* **Config models are not pydantic "strict mode".** The environment only supplies strings,
  so `"5432"` must parse to an int. Config models still forbid extra keys, are frozen and
  have no defaults. All *domain* data models (Phase 2 onward) use `strict=True`.

## Consequences
* The venv must live on a fast filesystem. On WSL, keeping the repo under `/mnt/c` makes
  imports very slow (boto3 client creation takes about 15 s). Moving the repo to the Linux
  filesystem (e.g. `~/patsquire-plr`) is recommended.
