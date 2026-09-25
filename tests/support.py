"""Test-only helpers: config builders and fixture credentials.

All credentials here are TEST-ONLY values for throwaway containers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

TEST_DB_PASSWORD = "test-only-db-password"  # noqa: S105 - test fixture value
TEST_S3_ACCESS_KEY = "test-only-access-key"
TEST_S3_SECRET_KEY = "test-only-secret-key"  # noqa: S105 - test fixture value


def base_config() -> dict[str, dict[str, object]]:
    """A complete, valid non-secret config mapping (fresh copy each call)."""
    return {
        "app": {"environment": "test", "log_level": "INFO"},
        "database": {
            "host": "localhost",
            "port": 5432,
            "name": "plr",
            "user": "plr",
            "connect_timeout_s": 2,
        },
        "redis": {
            "host": "localhost",
            "port": 6379,
            "db": 0,
            "password": None,
            "socket_timeout_s": 2,
        },
        "object_storage": {
            "endpoint_url": "http://localhost:9000",
            "region": "us-east-1",
            "bucket": "plr-artifacts",
            "connect_timeout_s": 2,
            "read_timeout_s": 5,
        },
    }


def base_secrets() -> dict[str, str]:
    return {
        "PLR__DATABASE__PASSWORD": TEST_DB_PASSWORD,
        "PLR__OBJECT_STORAGE__ACCESS_KEY_ID": TEST_S3_ACCESS_KEY,
        "PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY": TEST_S3_SECRET_KEY,
    }


WriteConfig = Callable[[dict[str, dict[str, object]]], Path]

# Container images, shared by docker-compose.yml and the integration tests.
# tests/unit/test_compose.py asserts the compose file uses exactly these.
PGVECTOR_IMAGE = "pgvector/pgvector:pg16"
REDIS_IMAGE = "redis:7-alpine"
# Final MinIO release (upstream discontinued), pinned by digest. See docs/adr/0003.
MINIO_IMAGE = "alpine/minio@sha256:cf23643a6cf9ce159c57643ceb88279e431262282428c9e0bf3a7ef1a97e84b4"
