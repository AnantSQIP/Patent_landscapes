"""Persistence for the gateway: the append-only call log and the response cache."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from patsquire_plr.db.models import LlmCache, LlmCall


class CallRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    role: str
    backend: str
    provider: str
    model: str
    model_version_reported: str | None
    operation: str
    prompt_id: str | None
    prompt_version: str | None
    params: dict[str, object]
    request_sha256: str
    cache_key: str
    cache_hit: bool
    attempt: int
    output: object
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost_estimate_usd: Decimal | None
    error: str | None


class CallStore(Protocol):
    def get_cached(self, cache_key: str) -> dict[str, object] | None: ...

    def put_cached(
        self,
        cache_key: str,
        *,
        backend: str,
        model: str,
        operation: str,
        response: dict[str, object],
    ) -> None: ...

    def log_call(self, record: CallRecord) -> None: ...


class PostgresCallStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_cached(self, cache_key: str) -> dict[str, object] | None:
        with Session(self._engine) as session:
            return session.scalar(select(LlmCache.response).where(LlmCache.cache_key == cache_key))

    def put_cached(
        self,
        cache_key: str,
        *,
        backend: str,
        model: str,
        operation: str,
        response: dict[str, object],
    ) -> None:
        # Concurrent identical requests may race; the first stored response wins and the
        # table stays append-only.
        with Session(self._engine) as session, session.begin():
            session.execute(
                insert(LlmCache)
                .values(
                    cache_key=cache_key,
                    backend=backend,
                    model=model,
                    operation=operation,
                    response=response,
                )
                .on_conflict_do_nothing(index_elements=["cache_key"])
            )

    def log_call(self, record: CallRecord) -> None:
        with Session(self._engine) as session, session.begin():
            session.add(LlmCall(**record.model_dump()))
