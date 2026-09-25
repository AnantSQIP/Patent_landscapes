"""Health checks for the infrastructure services every report run depends on."""

from __future__ import annotations

import boto3
import redis
from botocore.config import Config as BotoConfig
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import create_engine, text

from patsquire_plr.config import (
    DatabaseSettings,
    ObjectStorageSettings,
    RedisSettings,
    Settings,
)
from patsquire_plr.errors import HealthCheckError
from patsquire_plr.health.runner import HealthCheck

PGVECTOR_EXTENSION = "vector"


class PostgresCheck:
    """Postgres is reachable, and the pgvector extension is installable on the server."""

    name = "postgres"

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings

    def run(self) -> str:
        engine = create_engine(
            self._settings.sqlalchemy_url(),
            connect_args={"connect_timeout": self._settings.connect_timeout_s},
            pool_pre_ping=True,
        )
        try:
            with engine.connect() as conn:
                server_version: str = conn.execute(text("SHOW server_version")).scalar_one()
                vector_version: str | None = conn.execute(
                    text("SELECT default_version FROM pg_available_extensions WHERE name = :name"),
                    {"name": PGVECTOR_EXTENSION},
                ).scalar_one_or_none()
        finally:
            engine.dispose()
        if vector_version is None:
            raise HealthCheckError(
                f"Postgres {server_version} is reachable but the '{PGVECTOR_EXTENSION}' "
                "extension is not available on the server"
            )
        return f"postgres {server_version}, pgvector {vector_version} available"


class RedisCheck:
    """Redis answers PING."""

    name = "redis"

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings

    def run(self) -> str:
        password = self._settings.password
        client = redis.Redis(
            host=self._settings.host,
            port=self._settings.port,
            db=self._settings.db,
            password=None if password is None else password.get_secret_value(),
            socket_timeout=self._settings.socket_timeout_s,
            socket_connect_timeout=self._settings.socket_timeout_s,
            # One attempt, matching the object-storage check: report state, don't mask it.
            retry=Retry(NoBackoff(), retries=0),
        )
        try:
            # Annotated as object: the stubs claim ping() always returns True, but a
            # misbehaving proxy in front of Redis can answer something else.
            pong: object = client.ping()
            if pong is not True:
                raise HealthCheckError(f"Redis PING returned {pong!r} instead of PONG")
            version: str = client.info("server")["redis_version"]
        finally:
            client.close()
        return f"redis {version}"


class ObjectStorageCheck:
    """The configured bucket exists and is reachable with the configured credentials."""

    name = "object_storage"

    def __init__(self, settings: ObjectStorageSettings) -> None:
        self._settings = settings

    def run(self) -> str:
        client = boto3.client(
            "s3",
            endpoint_url=self._settings.endpoint_url,
            region_name=self._settings.region,
            aws_access_key_id=self._settings.access_key_id.get_secret_value(),
            aws_secret_access_key=self._settings.secret_access_key.get_secret_value(),
            # One attempt: a health check reports the current state; retrying would only
            # delay a failure report.
            config=BotoConfig(
                connect_timeout=self._settings.connect_timeout_s,
                read_timeout=self._settings.read_timeout_s,
                retries={"max_attempts": 1},
            ),
        )
        try:
            client.head_bucket(Bucket=self._settings.bucket)
        finally:
            client.close()
        return f"bucket '{self._settings.bucket}' reachable at {self._settings.endpoint_url}"


def default_checks(settings: Settings) -> list[HealthCheck]:
    return [
        PostgresCheck(settings.database),
        RedisCheck(settings.redis),
        ObjectStorageCheck(settings.object_storage),
    ]
