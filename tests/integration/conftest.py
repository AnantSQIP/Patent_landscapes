"""Database fixtures: one pgvector container per session, one migrated template database,
and a fresh clone of it for every test (append-only tables cannot be cleaned otherwise)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import boto3
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer

from patsquire_plr.config import ObjectStorageSettings
from patsquire_plr.db.migrate import upgrade
from tests.support import (
    MINIO_IMAGE,
    PGVECTOR_IMAGE,
    TEST_DB_PASSWORD,
    TEST_S3_ACCESS_KEY,
    TEST_S3_SECRET_KEY,
)

TEMPLATE_DB = "plr_template"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[URL]:
    with PostgresContainer(
        PGVECTOR_IMAGE, username="plr", password=TEST_DB_PASSWORD, dbname="plr"
    ) as container:
        yield URL.create(
            "postgresql+psycopg",
            username="plr",
            password=TEST_DB_PASSWORD,
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database="plr",
        )


@pytest.fixture(scope="session")
def admin_engine(postgres_url: URL) -> Iterator[Engine]:
    engine = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated_template(admin_engine: Engine, postgres_url: URL) -> str:
    with admin_engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {TEMPLATE_DB}"))
    engine = create_engine(postgres_url.set(database=TEMPLATE_DB))
    upgrade(engine)
    engine.dispose()
    return TEMPLATE_DB


@pytest.fixture
def db_engine(admin_engine: Engine, postgres_url: URL, migrated_template: str) -> Iterator[Engine]:
    name = f"t_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name} TEMPLATE {migrated_template}"))
    engine = create_engine(postgres_url.set(database=name))
    yield engine
    engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))


@pytest.fixture
def empty_db_engine(admin_engine: Engine, postgres_url: URL) -> Iterator[Engine]:
    """A database with no schema at all, for migration up/down tests."""
    name = f"e_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(postgres_url.set(database=name))
    yield engine
    engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))


@pytest.fixture(scope="session")
def object_storage() -> Iterator[ObjectStorageSettings]:
    """A MinIO container with an empty bucket, as validated ObjectStorageSettings."""
    container = MinioContainer(
        MINIO_IMAGE, access_key=TEST_S3_ACCESS_KEY, secret_key=TEST_S3_SECRET_KEY
    )
    container.with_env("MINIO_ROOT_USER", TEST_S3_ACCESS_KEY)
    container.with_env("MINIO_ROOT_PASSWORD", TEST_S3_SECRET_KEY)
    container.with_kwargs(user="0:0")  # the image's /data is root-owned (see docker-compose.yml)
    with container:
        settings = ObjectStorageSettings(
            endpoint_url=f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}",
            region="us-east-1",
            bucket="plr-test-raw",
            access_key_id=SecretStr(TEST_S3_ACCESS_KEY),
            secret_access_key=SecretStr(TEST_S3_SECRET_KEY),
            connect_timeout_s=5,
            read_timeout_s=10,
        )
        boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name="us-east-1",
            aws_access_key_id=TEST_S3_ACCESS_KEY,
            aws_secret_access_key=TEST_S3_SECRET_KEY,
        ).create_bucket(Bucket=settings.bucket)
        yield settings
