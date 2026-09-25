.PHONY: install lint typecheck test test-unit test-integration check up down health backup

install:
	uv sync --frozen
	uv run pre-commit install

lint:
	uv run ruff check
	uv run ruff format --check

typecheck:
	uv run mypy

test-unit:
	uv run pytest -m "not integration"

# Needs Docker (testcontainers starts real Postgres/Redis/MinIO).
test-integration:
	uv run pytest -m integration

test:
	uv run pytest --cov --cov-report=term-missing

# Everything CI runs, in the same order.
check: lint typecheck test

up:
	docker compose --env-file .env up -d --wait postgres redis minio
	docker compose --env-file .env run --rm minio-init

down:
	docker compose --env-file .env down

health:
	uv run plr health --env-file .env

# Verified git bundle of all branches and tags, written outside the repo. Run before merges.
backup:
	scripts/backup.sh
