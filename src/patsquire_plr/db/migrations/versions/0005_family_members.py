"""Stated family members per document (Phase 4 family grouping input).

document_family_member (APPEND-ONLY): the publications a source lists as members of the
document's simple family, e.g. Google Patents' "Also Published As" table.

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


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS document_family_member_no_truncate ON document_family_member"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS document_family_member_append_only ON document_family_member"
    )
    op.drop_table("document_family_member")
