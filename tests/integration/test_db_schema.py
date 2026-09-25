"""Schema, migrations, append-only enforcement and document round trips on real Postgres."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from patsquire_plr.cli import app
from patsquire_plr.db.documents import DocumentNotFoundError, load_document, store_document
from patsquire_plr.db.migrate import alembic_config, current_revision, downgrade, upgrade
from patsquire_plr.db.models import APPEND_ONLY_TABLES, Base, IngestBatch, RawRecord
from patsquire_plr.domain.patent import (
    CANONICAL_OPTIONAL_FIELDS,
    CitedReference,
    ClassificationCode,
    ForwardCitations,
    LegalStatus,
    MissingReason,
    Party,
    PatentDocument,
    Priority,
    normalize_publication_number,
)
from tests.support import TEST_DB_PASSWORD, base_config, base_secrets

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- schema vs models


def test_migrations_produce_exactly_the_orm_schema(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"ORM models and migrations have drifted: {diff}"


def test_database_is_at_head(db_engine: Engine) -> None:
    assert current_revision(db_engine) == "0003"


def test_pgvector_extension_is_installed(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        version = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    assert version is not None


def test_downgrade_to_base_and_upgrade_again(empty_db_engine: Engine) -> None:
    upgrade(empty_db_engine)
    downgrade(empty_db_engine, "base")
    tables = set(inspect(empty_db_engine).get_table_names()) - {"alembic_version"}
    assert tables == set()
    upgrade(empty_db_engine)
    assert current_revision(empty_db_engine) == "0003"


def test_migrations_refuse_to_run_without_a_supplied_connection() -> None:
    with pytest.raises(TypeError, match="plr db upgrade"):
        command.upgrade(alembic_config(), "head")


# ---------------------------------------------------------------- append-only enforcement


def test_every_append_only_table_has_row_and_truncate_triggers(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal"
            )
        ).all()
    protected = {rel for rel, name in rows if name == f"{rel}_append_only"}
    no_truncate = {rel for rel, name in rows if name == f"{rel}_no_truncate"}
    assert protected == set(APPEND_ONLY_TABLES)
    assert no_truncate == set(APPEND_ONLY_TABLES)


def _raw_record(session: Session) -> uuid.UUID:
    batch = IngestBatch(
        source_id="test_source",
        source_type="local_json",
        adapter_version="test",
        source_api_version="test-fixture-1",
        query_id=None,
        query_text=None,
        started_at=datetime(2026, 9, 25, tzinfo=UTC),
        finished_at=None,
        reported_count=None,
        stored_count=None,
        status="running",
    )
    session.add(batch)
    session.flush()
    payload = b'{"test": "fixture"}'
    record = RawRecord(
        batch_id=batch.id,
        source_record_key="fixture-1",
        object_key="raw/test/fixture-1.json",
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type="application/json",
        byte_size=len(payload),
        retrieved_at=datetime(2026, 9, 25, tzinfo=UTC),
    )
    session.add(record)
    session.flush()
    return record.id


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE raw_record SET object_key = 'tampered'",
        "DELETE FROM raw_record",
        "TRUNCATE raw_record CASCADE",
    ],
)
def test_append_only_rows_cannot_be_changed(db_engine: Engine, statement: str) -> None:
    with Session(db_engine) as session, session.begin():
        _raw_record(session)
    with db_engine.connect() as conn, pytest.raises(DBAPIError, match="append-only"):
        conn.execute(text(statement))


def test_mutable_tables_are_not_blocked(db_engine: Engine) -> None:
    with Session(db_engine) as session, session.begin():
        _raw_record(session)
    with db_engine.begin() as conn:
        updated = conn.execute(text("UPDATE ingest_batch SET status = 'complete'")).rowcount
    assert updated == 1


def test_check_constraints_back_the_domain_rules(db_engine: Engine) -> None:
    with Session(db_engine) as session, session.begin():
        raw_id = _raw_record(session)
    with db_engine.connect() as conn, pytest.raises(IntegrityError, match="publication_country"):
        conn.execute(
            text(
                "INSERT INTO patent_document (id, raw_record_id, raw_pointer, source_id, "
                "publication_country, publication_number, publication_number_raw, missing) "
                "VALUES (gen_random_uuid(), :raw, 'p', 's', 'us', '1', 'x', '{}'::jsonb)"
            ),
            {"raw": raw_id},
        )


# ---------------------------------------------------------------- document round trips


def _rich_document(raw_record_id: uuid.UUID) -> PatentDocument:
    return PatentDocument(
        raw_record_id=raw_record_id,
        source_id="test_source",
        publication=normalize_publication_number("EP 3 123 456 A1"),
        publication_number_raw="EP 3 123 456 A1",
        application_number_raw="EP20190001234",
        family_id_simple="54321",
        family_id_extended="98765",
        earliest_priority_date=date(2019, 1, 2),
        priorities=(
            Priority(number_raw="GB 1900001", country="GB", date=date(2019, 1, 2)),
            Priority(number_raw="US 62/000,001", country="US", date=date(2019, 5, 1)),
        ),
        filing_date=date(2020, 1, 2),
        publication_date=date(2021, 7, 8),
        grant_date=None,
        title="Fixture title",
        abstract="Fixture abstract.",
        claims=None,
        language="en",
        applicants=(
            Party(
                role="applicant",
                sequence=1,
                name_raw="Acme Corp",
                country="GB",
                country_missing=None,
            ),
            Party(
                role="applicant",
                sequence=2,
                name_raw="Beta Labs",
                country=None,
                country_missing=MissingReason.NOT_PROVIDED_BY_SOURCE,
            ),
        ),
        inventors=(),
        cpc=(
            ClassificationCode(scheme="cpc", code="H01M10/0525", code_raw="H01M 10/0525"),
            ClassificationCode(scheme="cpc", code="G06N3/08", code_raw="G06N 3/08"),
        ),
        ipc=(ClassificationCode(scheme="ipc", code="G06N3/08", code_raw="G06N 3/08"),),
        legal_status=LegalStatus(
            category="pending",
            status_raw="Request for examination was made",
            as_of=date(2026, 9, 1),
        ),
        backward_citations=(
            CitedReference(
                kind="patent",
                publication_number="US10123456B2",
                publication_number_raw="US 10,123,456 B2",
                npl_text=None,
                origin="examiner",
            ),
            CitedReference(
                kind="npl",
                publication_number=None,
                publication_number_raw=None,
                npl_text="Smith et al. 2018",
                origin="applicant",
            ),
        ),
        forward_citations=ForwardCitations(
            citing_publication_numbers=("WO2022000002A1", "US11000001B1"), as_of=date(2026, 9, 1)
        ),
        missing={"grant_date": MissingReason.NOT_APPLICABLE, "claims": MissingReason.NOT_REQUESTED},
    )


def test_rich_document_round_trips_exactly(db_engine: Engine) -> None:
    with Session(db_engine) as session, session.begin():
        document = _rich_document(_raw_record(session))
        document_id = store_document(session, document, raw_pointer="/doc[1]")
    with Session(db_engine) as session:
        assert load_document(session, document_id) == document


def test_fully_missing_document_round_trips_exactly(db_engine: Engine) -> None:
    with Session(db_engine) as session, session.begin():
        raw_id = _raw_record(session)
        document = PatentDocument(
            raw_record_id=raw_id,
            source_id="test_source",
            publication=normalize_publication_number("US1B1"),
            publication_number_raw="US1B1",
            missing=dict.fromkeys(CANONICAL_OPTIONAL_FIELDS, MissingReason.NOT_PROVIDED_BY_SOURCE),
            **dict.fromkeys(CANONICAL_OPTIONAL_FIELDS),
        )
        document_id = store_document(session, document, raw_pointer="row 1")
    with Session(db_engine) as session:
        assert load_document(session, document_id) == document


def test_loading_an_unknown_document_fails(db_engine: Engine) -> None:
    with Session(db_engine) as session, pytest.raises(DocumentNotFoundError):
        load_document(session, uuid.uuid4())


def test_cli_db_upgrade_and_current(
    empty_db_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = empty_db_engine.url
    data = base_config()
    data["database"].update(host=url.host, port=url.port, name=url.database)
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    for name, value in {**base_secrets(), "PLR__DATABASE__PASSWORD": TEST_DB_PASSWORD}.items():
        monkeypatch.setenv(name, value)

    before = CliRunner().invoke(app, ["db", "current", "--config", str(config)])
    upgraded = CliRunner().invoke(app, ["db", "upgrade", "--config", str(config)])

    assert before.stdout.strip() == "none", before.output
    assert upgraded.exit_code == 0, upgraded.output
    assert upgraded.stdout.strip() == "database at revision 0003"


def test_repeated_codes_and_citations_are_stored_as_delivered(db_engine: Engine) -> None:
    """Two raw spellings of one CPC code and a repeated citing number are both kept."""
    with Session(db_engine) as session, session.begin():
        raw_id = _raw_record(session)
        base = _rich_document(raw_id)
        document = base.model_copy(
            update={
                "cpc": (
                    ClassificationCode(scheme="cpc", code="G06N3/08", code_raw="G06N 3/08"),
                    ClassificationCode(scheme="cpc", code="G06N3/08", code_raw="G06N0003/08"),
                ),
                "forward_citations": ForwardCitations(
                    citing_publication_numbers=("US11000001B1", "US11000001B1"),
                    as_of=date(2026, 9, 1),
                ),
            }
        )
        document_id = store_document(session, document, raw_pointer="/doc[2]")
    with Session(db_engine) as session:
        assert load_document(session, document_id) == document


def test_ingest_batch_requires_the_provider_api_version(db_engine: Engine) -> None:
    with db_engine.connect() as conn, pytest.raises(IntegrityError, match="source_api_version"):
        conn.execute(
            text(
                "INSERT INTO ingest_batch (id, source_id, source_type, adapter_version, "
                "started_at, status) VALUES (gen_random_uuid(), 's', 't', 'v', now(), 'running')"
            )
        )
