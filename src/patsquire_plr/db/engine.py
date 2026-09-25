"""Engine construction from validated settings (the only way the app reaches Postgres)."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from patsquire_plr.config import DatabaseSettings


def create_db_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(
        settings.sqlalchemy_url(),
        connect_args={"connect_timeout": settings.connect_timeout_s},
        pool_pre_ping=True,
    )
