"""Relational schema (PostgreSQL). Migrations in ``db/migrations`` must match this exactly;
``tests/integration/test_db_schema.py`` fails if they drift apart.

Provenance chain: ``ingest_batch`` -> ``raw_record`` (immutable payload in object storage)
-> ``patent_document`` (+ child tables). Facts, audit events and model calls are append-only.
Tables marked APPEND-ONLY reject UPDATE and DELETE by database trigger (see the migration).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Tables whose rows can never be updated or deleted once written.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "ingest_item",
    "raw_record",
    "quarantined_record",
    "patent_document",
    "document_party",
    "document_priority",
    "document_classification",
    "document_citation",
    "document_forward_citation",
    "document_family_member",
    "fact_computation",
    "fact",
    "audit_event",
    "llm_call",
    "llm_cache",
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        str: Text(),
        dict[str, object]: JSONB(),
        list[object]: JSONB(),
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


# ------------------------------------------------------------------ ingestion / provenance


class IngestBatch(Base):
    """One adapter run for one query (or one set of local files)."""

    __tablename__ = "ingest_batch"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'complete', 'failed')", name="status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[str]
    source_type: Mapped[str]
    adapter_version: Mapped[str]
    source_api_version: Mapped[str] = mapped_column(
        doc="the provider's API/format version (build prompt §6), e.g. 'OPS 3.2' or 'DOCDB 2.5'"
    )
    query_id: Mapped[str | None]
    query_text: Mapped[str | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    reported_count: Mapped[int | None] = mapped_column(Integer)
    stored_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str]


class RawRecord(Base):
    """APPEND-ONLY. Pointer to one immutable raw payload in object storage."""

    __tablename__ = "raw_record"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "source_record_key", name="uq_raw_record_batch_id_source_record_key"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingest_batch.id"))
    source_record_key: Mapped[str]
    object_key: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str]
    byte_size: Mapped[int] = mapped_column(BigInteger)
    retrieved_at: Mapped[datetime]


class IngestItem(Base):
    """APPEND-ONLY. One outcome per requested record per attempt (reconciliation, resume).

    The latest row for a ``requested_key`` within a batch is its current state; a resumed
    batch appends new rows for items that previously failed.
    """

    __tablename__ = "ingest_item"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('stored', 'quarantined', 'duplicate', 'not_found', 'invalid_request', "
            "'failed')",
            name="outcome",
        ),
        CheckConstraint(
            "(outcome IN ('stored', 'quarantined', 'duplicate')) = (raw_record_id IS NOT NULL)",
            name="raw_record_iff_fetched",
        ),
        CheckConstraint("document_count >= 0", name="document_count_non_negative"),
        Index("ix_ingest_item_batch_key", "batch_id", "requested_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingest_batch.id"))
    requested_key: Mapped[str]
    outcome: Mapped[str]
    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_record.id"))
    document_count: Mapped[int] = mapped_column(Integer)
    detail: Mapped[str | None]
    created_at: Mapped[datetime] = _created_at()


class QuarantinedRecord(Base):
    """APPEND-ONLY. A raw record (or part of one) that failed validation, with reasons."""

    __tablename__ = "quarantined_record"

    id: Mapped[uuid.UUID] = _uuid_pk()
    raw_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_record.id"))
    raw_pointer: Mapped[str]
    reasons: Mapped[list[object]]
    created_at: Mapped[datetime] = _created_at()


class PatentDocumentRow(Base):
    """APPEND-ONLY. One normalised publication from one source (canonical record)."""

    __tablename__ = "patent_document"
    __table_args__ = (
        CheckConstraint("publication_country ~ '^[A-Z]{2}$'", name="publication_country"),
        CheckConstraint(
            "legal_status_category IS NULL OR legal_status_category IN ('pending', 'granted', "
            "'active', 'lapsed', 'expired', 'withdrawn', 'refused', 'revoked', 'other')",
            name="legal_status_category",
        ),
        CheckConstraint(
            "(legal_status_category IS NULL) = (legal_status_raw IS NULL) "
            "AND (legal_status_category IS NULL) = (legal_status_as_of IS NULL)",
            name="legal_status_complete",
        ),
        CheckConstraint("jsonb_typeof(missing) = 'object'", name="missing_is_object"),
        Index(
            "ix_patent_document_publication",
            "publication_country",
            "publication_number",
            "publication_kind",
        ),
        Index("ix_patent_document_family_id_simple", "family_id_simple"),
        Index("ix_patent_document_family_id_extended", "family_id_extended"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    raw_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_record.id"))
    raw_pointer: Mapped[str]
    source_id: Mapped[str]
    publication_country: Mapped[str] = mapped_column(String(2))
    publication_number: Mapped[str]
    publication_kind: Mapped[str | None]
    publication_number_raw: Mapped[str]
    application_number_raw: Mapped[str | None]
    family_id_simple: Mapped[str | None]
    family_id_extended: Mapped[str | None]
    earliest_priority_date: Mapped[date | None]
    filing_date: Mapped[date | None]
    publication_date: Mapped[date | None]
    grant_date: Mapped[date | None]
    title: Mapped[str | None]
    abstract: Mapped[str | None]
    claims: Mapped[str | None]
    language: Mapped[str | None] = mapped_column(String(2))
    legal_status_category: Mapped[str | None]
    legal_status_raw: Mapped[str | None]
    legal_status_as_of: Mapped[date | None]
    forward_citations_as_of: Mapped[date | None]
    missing: Mapped[dict[str, object]]
    created_at: Mapped[datetime] = _created_at()


class DocumentParty(Base):
    """APPEND-ONLY."""

    __tablename__ = "document_party"
    __table_args__ = (
        CheckConstraint("role IN ('applicant', 'inventor')", name="role"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("(country IS NULL) <> (country_missing IS NULL)", name="country_or_reason"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patent_document.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    name_raw: Mapped[str]
    country: Mapped[str | None] = mapped_column(String(2))
    country_missing: Mapped[str | None]


class DocumentPriority(Base):
    """APPEND-ONLY."""

    __tablename__ = "document_priority"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patent_document.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    number_raw: Mapped[str]
    country: Mapped[str] = mapped_column(String(2))
    priority_date: Mapped[date]


class DocumentClassification(Base):
    """APPEND-ONLY. Keyed by position, so repeated or differently spelled codes that
    normalise to the same code are all kept exactly as the source listed them."""

    __tablename__ = "document_classification"
    __table_args__ = (CheckConstraint("scheme IN ('cpc', 'ipc')", name="scheme"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patent_document.id"), primary_key=True
    )
    scheme: Mapped[str] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(
        Integer, primary_key=True, doc="position in the source's list"
    )
    code: Mapped[str]
    code_raw: Mapped[str]


class DocumentCitation(Base):
    """APPEND-ONLY. Backward citations."""

    __tablename__ = "document_citation"
    __table_args__ = (
        CheckConstraint("kind IN ('patent', 'npl')", name="kind"),
        CheckConstraint(
            "origin IN ('examiner', 'applicant', 'third_party', 'unknown')", name="origin"
        ),
        CheckConstraint(
            "(kind = 'patent' AND publication_number IS NOT NULL AND npl_text IS NULL) OR "
            "(kind = 'npl' AND npl_text IS NOT NULL AND publication_number IS NULL)",
            name="shape",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patent_document.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str]
    publication_number: Mapped[str | None]
    publication_number_raw: Mapped[str | None]
    npl_text: Mapped[str | None]
    origin: Mapped[str]


class DocumentForwardCitation(Base):
    """APPEND-ONLY. Publications citing the document, as reported by its source."""

    __tablename__ = "document_forward_citation"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patent_document.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(
        Integer, primary_key=True, doc="position in the source's list"
    )
    citing_publication_number: Mapped[str]


class DocumentFamilyMember(Base):
    """APPEND-ONLY. Publications the source states are in the document's simple family."""

    __tablename__ = "document_family_member"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patent_document.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_number: Mapped[str]


# ------------------------------------------------------------------ fact store (Layer 3)


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'complete', 'failed', 'not_publishable')", name="status"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _created_at()
    template_version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, object]]
    data_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str]


class FactComputation(Base):
    """APPEND-ONLY. One implementation's result for one fact; kept even when rejected."""

    __tablename__ = "fact_computation"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_run.id"))
    metric_id: Mapped[str]
    dimensions_key: Mapped[str] = mapped_column(String(64))
    implementation: Mapped[str]
    value: Mapped[object] = mapped_column(JSONB)
    input_set_sha256: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = _created_at()


class Fact(Base):
    """APPEND-ONLY. A value accepted after independent implementations agreed exactly."""

    __tablename__ = "fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "metric_id", "dimensions_key", name="uq_fact_run_id_metric_id_dimensions_key"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_run.id"))
    metric_id: Mapped[str]
    dimensions: Mapped[dict[str, object]]
    dimensions_key: Mapped[str] = mapped_column(String(64))
    value: Mapped[object] = mapped_column(JSONB)
    unit: Mapped[str]
    definition_options: Mapped[dict[str, object]]
    query_spec: Mapped[dict[str, object]]
    input_set_sha256: Mapped[str] = mapped_column(String(64))
    implementations: Mapped[list[object]]
    created_at: Mapped[datetime] = _created_at()


# ------------------------------------------------------------------ audit log (Layer 8)


class AuditEvent(Base):
    """APPEND-ONLY, hash-chained: each row's hash covers its content and the previous hash."""

    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint("hash ~ '^[0-9a-f]{64}$' AND prev_hash ~ '^[0-9a-f]{64}$'", name="hashes"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID | None]
    occurred_at: Mapped[datetime]
    step: Mapped[str]
    event_type: Mapped[str]
    actor: Mapped[str]
    payload: Mapped[dict[str, object]]
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


# ------------------------------------------------------------------ model gateway


class LlmCall(Base):
    """APPEND-ONLY. Every model call attempt, including cache hits and failures."""

    __tablename__ = "llm_call"

    id: Mapped[uuid.UUID] = _uuid_pk()
    occurred_at: Mapped[datetime] = _created_at()
    role: Mapped[str]
    backend: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    model_version_reported: Mapped[str | None]
    operation: Mapped[str]
    prompt_id: Mapped[str | None]
    prompt_version: Mapped[str | None]
    params: Mapped[dict[str, object]]
    request_sha256: Mapped[str] = mapped_column(String(64))
    cache_key: Mapped[str] = mapped_column(String(64))
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    attempt: Mapped[int] = mapped_column(Integer)
    output: Mapped[object] = mapped_column(JSONB, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    cost_estimate_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error: Mapped[str | None]


class LlmCache(Base):
    """APPEND-ONLY. Validated model responses keyed by input, prompt version, model, params."""

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = _created_at()
    backend: Mapped[str]
    model: Mapped[str]
    operation: Mapped[str]
    response: Mapped[dict[str, object]]
