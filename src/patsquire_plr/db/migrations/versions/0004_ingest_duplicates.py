"""Ingest items may end as 'duplicate': a key that resolved to a publication already stored
in the same batch (e.g. EP3123456 and EP3123456A1). The raw page is kept; no second
document is stored.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTCOMES_OLD = "'stored', 'quarantined', 'not_found', 'invalid_request', 'failed'"
_OUTCOMES_NEW = "'stored', 'quarantined', 'duplicate', 'not_found', 'invalid_request', 'failed'"


def _replace(outcomes: str, fetched: str) -> None:
    op.drop_constraint(op.f("ck_ingest_item_outcome"), "ingest_item", type_="check")
    op.create_check_constraint(
        op.f("ck_ingest_item_outcome"), "ingest_item", f"outcome IN ({outcomes})"
    )
    op.drop_constraint(op.f("ck_ingest_item_raw_record_iff_fetched"), "ingest_item", type_="check")
    op.create_check_constraint(
        op.f("ck_ingest_item_raw_record_iff_fetched"),
        "ingest_item",
        f"(outcome IN ({fetched})) = (raw_record_id IS NOT NULL)",
    )


def upgrade() -> None:
    _replace(_OUTCOMES_NEW, "'stored', 'quarantined', 'duplicate'")


def downgrade() -> None:
    _replace(_OUTCOMES_OLD, "'stored', 'quarantined'")
