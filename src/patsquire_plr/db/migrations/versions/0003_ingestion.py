"""Phase 3 ingestion: per-item outcomes and the 'active' legal-status category.

* ingest_item (APPEND-ONLY): every requested record gets an outcome row, so a batch can be
  reconciled (requested = stored + quarantined + not_found + invalid_request, no failures)
  and resumed (failed items are retried and get a new row).
* patent_document.legal_status_category gains 'active' (granted and in force), which some
  sources (e.g. Google Patents) report distinctly from 'granted'.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CATEGORIES = (
    "'pending', 'granted', 'lapsed', 'expired', 'withdrawn', 'refused', 'revoked', 'other'"
)
_NEW_CATEGORIES = "'pending', 'granted', 'active', 'lapsed', 'expired', 'withdrawn', 'refused', 'revoked', 'other'"


def _category_check(categories: str) -> str:
    return f"legal_status_category IS NULL OR legal_status_category IN ({categories})"


def upgrade() -> None:
    op.create_table(
        "ingest_item",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("requested_key", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("raw_record_id", sa.UUID(), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('stored', 'quarantined', 'not_found', 'invalid_request', 'failed')",
            name=op.f("ck_ingest_item_outcome"),
        ),
        sa.CheckConstraint(
            "(outcome IN ('stored', 'quarantined')) = (raw_record_id IS NOT NULL)",
            name=op.f("ck_ingest_item_raw_record_iff_fetched"),
        ),
        sa.CheckConstraint(
            "document_count >= 0", name=op.f("ck_ingest_item_document_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["ingest_batch.id"], name=op.f("fk_ingest_item_batch_id_ingest_batch")
        ),
        sa.ForeignKeyConstraint(
            ["raw_record_id"],
            ["raw_record.id"],
            name=op.f("fk_ingest_item_raw_record_id_raw_record"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_item")),
    )
    op.create_index("ix_ingest_item_batch_key", "ingest_item", ["batch_id", "requested_key"])
    op.execute(
        "CREATE TRIGGER ingest_item_append_only BEFORE UPDATE OR DELETE ON ingest_item "
        "FOR EACH ROW EXECUTE FUNCTION plr_forbid_modification()"
    )
    op.execute(
        "CREATE TRIGGER ingest_item_no_truncate BEFORE TRUNCATE ON ingest_item "
        "FOR EACH STATEMENT EXECUTE FUNCTION plr_forbid_modification()"
    )
    op.drop_constraint(
        op.f("ck_patent_document_legal_status_category"), "patent_document", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_patent_document_legal_status_category"),
        "patent_document",
        _category_check(_NEW_CATEGORIES),
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_patent_document_legal_status_category"), "patent_document", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_patent_document_legal_status_category"),
        "patent_document",
        _category_check(_OLD_CATEGORIES),
    )
    op.execute("DROP TRIGGER IF EXISTS ingest_item_no_truncate ON ingest_item")
    op.execute("DROP TRIGGER IF EXISTS ingest_item_append_only ON ingest_item")
    op.drop_index("ix_ingest_item_batch_key", table_name="ingest_item")
    op.drop_table("ingest_item")
