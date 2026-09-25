"""Phase 2 review fixes: positional keys for repeated codes/citations; provider API version.

* document_classification: primary key (document_id, scheme, code) -> (document_id, scheme,
  ordinal). Sources may list a code twice, or spell two entries that normalise to the same
  code, and each must be stored as delivered.
* document_forward_citation: primary key (document_id, citing_publication_number) ->
  (document_id, ordinal), for the same reason.
* ingest_batch.source_api_version: build prompt §6 requires the provider's API version with
  every retrieval. NOT NULL without a default: no batch may exist without it (the table is
  empty until Phase 3 adapters exist; a populated table makes this migration fail loudly).

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("pk_document_classification", "document_classification", type_="primary")
    op.create_primary_key(
        "pk_document_classification",
        "document_classification",
        ["document_id", "scheme", "ordinal"],
    )
    op.drop_constraint("pk_document_forward_citation", "document_forward_citation", type_="primary")
    op.create_primary_key(
        "pk_document_forward_citation", "document_forward_citation", ["document_id", "ordinal"]
    )
    op.add_column("ingest_batch", sa.Column("source_api_version", sa.Text(), nullable=False))


def downgrade() -> None:
    op.drop_column("ingest_batch", "source_api_version")
    op.drop_constraint("pk_document_forward_citation", "document_forward_citation", type_="primary")
    op.create_primary_key(
        "pk_document_forward_citation",
        "document_forward_citation",
        ["document_id", "citing_publication_number"],
    )
    op.drop_constraint("pk_document_classification", "document_classification", type_="primary")
    op.create_primary_key(
        "pk_document_classification", "document_classification", ["document_id", "scheme", "code"]
    )
