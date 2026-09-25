"""Stated family members per document (Phase 4 family grouping input).

document_family_member (APPEND-ONLY): the publications a source lists as members of the
document's simple family, e.g. Google Patents' "Also Published As" table.

Backfill: documents stored before this field existed were never asked for family members.
They are marked ``family_members: not_requested`` so they cannot be misread as "the source
lists no members". patent_document is append-only; this is the one controlled exception: the
trigger is disabled for this single statement inside the migration transaction.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_family_member",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("publication_number", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["patent_document.id"],
            name=op.f("fk_document_family_member_document_id_patent_document"),
        ),
        sa.PrimaryKeyConstraint("document_id", "ordinal", name=op.f("pk_document_family_member")),
    )
    for trigger, timing in (("append_only", "UPDATE OR DELETE"), ("no_truncate", "TRUNCATE")):
        scope = "ROW" if trigger == "append_only" else "STATEMENT"
        op.execute(
            f"CREATE TRIGGER document_family_member_{trigger} BEFORE {timing} "
            f"ON document_family_member FOR EACH {scope} EXECUTE FUNCTION plr_forbid_modification()"
        )

    op.execute("ALTER TABLE patent_document DISABLE TRIGGER patent_document_append_only")
    op.execute(
        'UPDATE patent_document SET missing = missing || \'{"family_members": "not_requested"}\'::jsonb '
        "WHERE NOT missing ? 'family_members'"
    )
    op.execute("ALTER TABLE patent_document ENABLE TRIGGER patent_document_append_only")


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS document_family_member_no_truncate ON document_family_member"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS document_family_member_append_only ON document_family_member"
    )
    op.drop_table("document_family_member")
