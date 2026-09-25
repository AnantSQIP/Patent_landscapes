"""Alembic environment. Migrations always run on a connection handed over by
``patsquire_plr.db.migrate``; there is no second, implicit way to find the database."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from patsquire_plr.db.models import Base

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise TypeError(
        "Run migrations through `plr db upgrade` (or patsquire_plr.db.migrate), which "
        "supplies the database connection from the validated configuration."
    )

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    compare_type=True,
    compare_server_default=True,
)
with context.begin_transaction():
    context.run_migrations()
