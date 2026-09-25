"""Health checks against real services started by testcontainers. Requires Docker."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from pydantic import SecretStr
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from patsquire_plr.config import DatabaseSettings, ObjectStorageSettings, RedisSettings
from patsquire_plr.errors import HealthCheckError
from patsquire_plr.health import (
    CheckStatus,
    ObjectStorageCheck,
    PostgresCheck,
    RedisCheck,
    run_checks,
)
from tests.support import (
    MINIO_IMAGE,
    PGVECTOR_IMAGE,
    REDIS_IMAGE,
    TEST_DB_PASSWORD,
    TEST_S3_ACCESS_KEY,
    TEST_S3_SECRET_KEY,
)

pytestmark = pytest.mark.integration

PLAIN_POSTGRES_IMAGE = "postgres:16-alpine"
BUCKET = "plr-artifacts"


def _db_settings(container: PostgresContainer) -> DatabaseSettings:
    return DatabaseSettings(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        name="plr",
        user="plr",
        password=SecretStr(TEST_DB_PASSWORD),
        connect_timeout_s=5,
    )


def _postgres(image: str) -> PostgresContainer:
    return PostgresContainer(image, username="plr", password=TEST_DB_PASSWORD, dbname="plr")


@pytest.fixture(scope="module")
def pgvector_db() -> Iterator[DatabaseSettings]:
    with _postgres(PGVECTOR_IMAGE) as container:
        yield _db_settings(container)


@pytest.fixture(scope="module")
def redis_settings() -> Iterator[RedisSettings]:
    with RedisContainer(REDIS_IMAGE) as container:
        yield RedisSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(6379)),
            db=0,
            password=None,
            socket_timeout_s=5,
        )


@pytest.fixture(scope="module")
def minio_endpoint() -> Iterator[str]:
    container = MinioContainer(
        MINIO_IMAGE, access_key=TEST_S3_ACCESS_KEY, secret_key=TEST_S3_SECRET_KEY
    )
    # testcontainers only sets the legacy MINIO_ACCESS_KEY/SECRET_KEY names; set the
    # current ones too so the credentials don't depend on deprecated behaviour.
    container.with_env("MINIO_ROOT_USER", TEST_S3_ACCESS_KEY)
    container.with_env("MINIO_ROOT_PASSWORD", TEST_S3_SECRET_KEY)
    # Same reason as in docker-compose.yml: the image's /data is root-owned.
    container.with_kwargs(user="0:0")
    with container:
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id=TEST_S3_ACCESS_KEY,
            aws_secret_access_key=TEST_S3_SECRET_KEY,
        )
        s3.create_bucket(Bucket=BUCKET)
        yield endpoint


def _storage_settings(endpoint: str, bucket: str = BUCKET) -> ObjectStorageSettings:
    return ObjectStorageSettings(
        endpoint_url=endpoint,
        region="us-east-1",
        bucket=bucket,
        access_key_id=SecretStr(TEST_S3_ACCESS_KEY),
        secret_access_key=SecretStr(TEST_S3_SECRET_KEY),
        connect_timeout_s=5,
        read_timeout_s=10,
    )


def test_postgres_with_pgvector_passes(pgvector_db: DatabaseSettings) -> None:
    detail = PostgresCheck(pgvector_db).run()

    assert detail.startswith("postgres 16")
    assert "pgvector" in detail


def test_postgres_without_pgvector_fails_loudly() -> None:
    with _postgres(PLAIN_POSTGRES_IMAGE) as container:
        check = PostgresCheck(_db_settings(container))
        with pytest.raises(HealthCheckError, match="'vector' extension is not available"):
            check.run()


def test_postgres_wrong_password_fails(pgvector_db: DatabaseSettings) -> None:
    wrong = pgvector_db.model_copy(update={"password": SecretStr("wrong-password")})

    report = run_checks([PostgresCheck(wrong)])

    assert report.results[0].status is CheckStatus.FAILED
    assert "password authentication failed" in report.results[0].detail
    assert "wrong-password" not in report.results[0].detail


def test_redis_passes(redis_settings: RedisSettings) -> None:
    assert RedisCheck(redis_settings).run().startswith("redis 7")


def test_object_storage_passes(minio_endpoint: str) -> None:
    assert BUCKET in ObjectStorageCheck(_storage_settings(minio_endpoint)).run()


def test_object_storage_missing_bucket_fails(minio_endpoint: str) -> None:
    report = run_checks([ObjectStorageCheck(_storage_settings(minio_endpoint, "no-such-bucket"))])

    assert report.results[0].status is CheckStatus.FAILED
    assert "404" in report.results[0].detail


def test_object_storage_bad_credentials_fail(minio_endpoint: str) -> None:
    settings = _storage_settings(minio_endpoint).model_copy(
        update={"secret_access_key": SecretStr("wrong-secret")}
    )

    report = run_checks([ObjectStorageCheck(settings)])

    assert report.results[0].status is CheckStatus.FAILED
    assert "wrong-secret" not in report.results[0].detail


def test_full_stack_is_healthy(
    pgvector_db: DatabaseSettings, redis_settings: RedisSettings, minio_endpoint: str
) -> None:
    report = run_checks(
        [
            PostgresCheck(pgvector_db),
            RedisCheck(redis_settings),
            ObjectStorageCheck(_storage_settings(minio_endpoint)),
        ]
    )

    assert report.healthy, report.model_dump_json(indent=2)
