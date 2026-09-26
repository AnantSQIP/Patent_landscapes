"""Ingestion end to end on real Postgres + MinIO, using the real Google Patents adapter
served the recorded fixture pages (TEST-ONLY transport; no network)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx
import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from patsquire_plr import cli
from patsquire_plr.cli import app
from patsquire_plr.config import GooglePatentsPageSettings, ObjectStorageSettings
from patsquire_plr.db.audit import verify_chain
from patsquire_plr.db.documents import load_document
from patsquire_plr.db.models import (
    AuditEvent,
    IngestBatch,
    IngestItem,
    PatentDocumentRow,
    QuarantinedRecord,
    RawRecord,
)
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.google_patents import GooglePatentsPageSource
from patsquire_plr.ingest.rawstore import RawStore, RawStoreError, object_key
from patsquire_plr.ingest.runner import (
    BatchBusyError,
    ReconciliationError,
    run_lookup_batch,
    start_lookup_batch,
)
from tests.support import TEST_DB_PASSWORD, base_config, base_secrets

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "google_patents"
SETTINGS = GooglePatentsPageSettings(
    type="google_patents_page",
    base_url="https://patents.google.com",
    user_agent="tests",
    requests_per_minute=60,
    timeout_s=5,
    max_retries=0,
)


def _pages() -> dict[str, bytes]:
    return {
        p.name.removesuffix(".html.gz"): gzip.decompress(p.read_bytes())
        for p in FIXTURES.glob("*.html.gz")
    }


class _SimulatedTime:
    """TEST-ONLY clock: sleeping advances it instantly, so rate limits cost no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _source(handler: Callable[[httpx.Request], httpx.Response]) -> GooglePatentsPageSource:
    time = _SimulatedTime()
    return GooglePatentsPageSource(
        "google_patents",
        SETTINGS,
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        clock=time.clock,
        sleep=time.sleep,
    )


def _serve(
    pages: dict[str, bytes], fail: set[str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        number = request.url.path.strip("/").removeprefix("patent/")
        if fail and number in fail:
            return httpx.Response(503)
        if number in pages:
            return httpx.Response(
                200, content=pages[number], headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(404)

    return handler


# ---------------------------------------------------------------- raw store


def test_raw_store_is_content_addressed_and_verified(object_storage: ObjectStorageSettings) -> None:
    store = RawStore(object_storage)
    key, sha = store.put("unit_source", b"payload-1", "text/plain")
    assert key == object_key("unit_source", sha)
    assert store.put("unit_source", b"payload-1", "text/plain") == (key, sha)  # idempotent
    assert store.get(key, sha) == b"payload-1"


def test_raw_store_detects_corruption_and_missing_objects(
    object_storage: ObjectStorageSettings,
) -> None:
    store = RawStore(object_storage)
    key, sha = store.put("unit_source", b"original bytes", "text/plain")
    boto3.client(
        "s3",
        endpoint_url=object_storage.endpoint_url,
        region_name=object_storage.region,
        aws_access_key_id=object_storage.access_key_id.get_secret_value(),
        aws_secret_access_key=object_storage.secret_access_key.get_secret_value(),
    ).put_object(Bucket=object_storage.bucket, Key=key, Body=b"tampered bytes")

    with pytest.raises(RawStoreError, match="corrupt"):
        store.get(key, sha)
    with pytest.raises(RawStoreError, match="corrupt"):
        store.put(
            "unit_source", b"original bytes", "text/plain"
        )  # re-put verifies, never overwrites
    with pytest.raises(RawStoreError, match="cannot be read"):
        store.get("raw/unit_source/00/" + "0" * 64, "0" * 64)


# ---------------------------------------------------------------- runner


def test_lookup_batch_reconciles_every_requested_key(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    pages = _pages()
    source = _source(_serve(pages))
    keys = [*sorted(pages), "US99999999999B9", "NOT-A-NUMBER", "US10000000B2"]

    batch_id = start_lookup_batch(db_engine, source, keys)
    report = run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id)

    assert report.status == "complete"
    assert report.requested == 8
    assert report.duplicates_ignored == 1
    assert report.outcomes == {"invalid_request": 1, "not_found": 1, "stored": 6}
    assert report.documents_stored == 6
    with Session(db_engine) as session:
        batch = session.get(IngestBatch, batch_id)
        assert batch is not None
        assert (batch.status, batch.stored_count, batch.source_api_version) == (
            "complete",
            6,
            source.info.source_api_version,
        )
        raws = session.scalars(select(RawRecord).where(RawRecord.batch_id == batch_id)).all()
        assert len(raws) == 6
        store = RawStore(object_storage)
        for raw in raws:  # every raw payload is retrievable and verified
            assert store.get(raw.object_key, raw.sha256) == pages[raw.source_record_key]
        us = session.scalar(
            select(PatentDocumentRow.id).where(PatentDocumentRow.publication_number == "10000000")
        )
        assert us is not None
        assert (
            load_document(session, us).title
            == "Coherent LADAR using intra-pixel quadrature detection"
        )
        events = [
            e.event_type for e in session.scalars(select(AuditEvent).order_by(AuditEvent.seq))
        ]
        assert events == ["batch_started", "batch_finished"]
        assert verify_chain(session).ok


def test_unparseable_page_is_quarantined_with_its_raw_payload(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    broken = _pages()["US10000000B2"].replace(
        b'itemprop="kindCode" content="B2"', b'itemprop="kindCode" content="A9"'
    )
    source = _source(_serve({"US10000000B2": broken}))

    report = run_lookup_batch(
        db_engine,
        RawStore(object_storage),
        source,
        start_lookup_batch(db_engine, source, ["US10000000B2"]),
    )

    assert report.status == "complete"
    assert report.outcomes == {"quarantined": 1}
    assert report.documents_stored == 0
    with Session(db_engine) as session:
        [quarantined] = session.scalars(select(QuarantinedRecord)).all()
        assert "disagrees with countryCode=US kindCode=A9" in str(quarantined.reasons[0])
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 1


def test_failed_items_leave_the_batch_resumable(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    pages = _pages()
    store = RawStore(object_storage)
    flaky = _source(_serve(pages, fail={"CN112345678A"}))
    batch_id = start_lookup_batch(db_engine, flaky, ["US10000000B2", "CN112345678A"])

    first = run_lookup_batch(db_engine, store, flaky, batch_id)
    assert first.status == "failed"
    assert first.failed_keys == ("CN112345678A",)

    fetched: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.path)
        return _serve(pages)(request)

    second = run_lookup_batch(db_engine, store, _source(recording), batch_id)

    assert second.status == "complete"
    assert fetched == ["/patent/CN112345678A/"]  # only the failed item was retried
    assert second.outcomes == {"stored": 2}
    with Session(db_engine) as session:
        outcomes = session.scalars(
            select(IngestItem.outcome)
            .where(IngestItem.requested_key == "CN112345678A")
            .order_by(IngestItem.created_at)
        ).all()
    assert outcomes == ["failed", "stored"]  # history kept, append-only


def test_completed_batches_and_wrong_sources_are_refused(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    pages = _pages()
    source = _source(_serve(pages))
    batch_id = start_lookup_batch(db_engine, source, ["US10000000B2"])
    run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id)

    with pytest.raises(PlrError, match="already complete"):
        run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id)
    other = GooglePatentsPageSource(
        "other_source", SETTINGS, client=httpx.Client(transport=httpx.MockTransport(_serve(pages)))
    )
    with pytest.raises(PlrError, match="belongs to source google_patents"):
        run_lookup_batch(db_engine, RawStore(object_storage), other, batch_id)
    with pytest.raises(ValueError, match="no keys"):
        start_lookup_batch(db_engine, source, ["  ", ""])


def test_batch_records_its_request_for_reproducibility(db_engine: Engine) -> None:
    source = _source(_serve({}))
    batch_id = start_lookup_batch(db_engine, source, ["US1B1", "US1B1", " US2B2 "])
    with Session(db_engine) as session:
        batch = session.get(IngestBatch, batch_id)
        assert batch is not None
        assert json.loads(batch.query_text or "") == {
            "keys": ["US1B1", "US2B2"],
            "duplicates_ignored": 1,
        }


def test_cli_lookup_and_resume(
    db_engine: Engine,
    object_storage: ObjectStorageSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = db_engine.url
    data = base_config()
    data["database"].update(host=url.host, port=url.port, name=url.database)
    data["object_storage"].update(
        endpoint_url=object_storage.endpoint_url, bucket=object_storage.bucket
    )
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    for name, value in {**base_secrets(), "PLR__DATABASE__PASSWORD": TEST_DB_PASSWORD}.items():
        monkeypatch.setenv(name, value)
    pages = _pages()
    state = {"fail": {"EP3123456A1"}}
    monkeypatch.setattr(
        cli, "build_source", lambda source_id, settings: _source(_serve(pages, fail=state["fail"]))
    )
    numbers = tmp_path / "numbers.txt"
    numbers.write_text("EP3123456A1\nUS99999999999B9\n", encoding="utf-8")

    first = CliRunner().invoke(
        app,
        [
            "ingest",
            "lookup",
            "US10000000B2",
            "--numbers-file",
            str(numbers),
            "--config",
            str(config),
        ],
    )
    assert first.exit_code == 1, first.output  # one item failed: batch is resumable, not complete
    report = json.loads(first.stdout)
    assert report["outcomes"] == {"failed": 1, "not_found": 1, "stored": 1}

    state["fail"] = set()
    resumed = CliRunner().invoke(
        app, ["ingest", "resume", report["batch_id"], "--config", str(config)]
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["outcomes"] == {"not_found": 1, "stored": 2}

    unknown = CliRunner().invoke(
        app, ["ingest", "lookup", "US1B1", "--source", "nope", "--config", str(config)]
    )
    empty = CliRunner().invoke(app, ["ingest", "lookup", "--config", str(config)])
    assert (unknown.exit_code, empty.exit_code) == (2, 2)
    assert "unknown data source 'nope'" in unknown.stderr


def test_count_mismatch_fails_reconciliation_loudly(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    """A raw record no outcome accounts for (e.g. written by a buggy or concurrent writer)."""
    source = _source(_serve(_pages()))
    batch_id = start_lookup_batch(db_engine, source, ["US10000000B2"])
    with Session(db_engine) as session, session.begin():
        session.add(
            RawRecord(
                batch_id=batch_id,
                source_record_key="stray",
                object_key="raw/x",
                sha256="0" * 64,
                content_type="text/html",
                byte_size=0,
                retrieved_at=datetime.now(UTC),
            )
        )

    with pytest.raises(ReconciliationError, match="2 raw records but 1 fetched items"):
        run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id)
    with Session(db_engine) as session:
        batch = session.get(IngestBatch, batch_id)
        assert batch is not None
        assert batch.status == "failed"
        finished = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "batch_finished")
        ).one()
        assert "raw records" in str(finished.payload["problems"])


# ---------------------------------------------------------------- review fixes


def test_spelling_variants_are_one_request(db_engine: Engine) -> None:
    source = _source(_serve({}))
    batch_id = start_lookup_batch(
        db_engine, source, ["US 10,000,000 B2", "us10000000b2", "US10000000B2"]
    )
    with Session(db_engine) as session:
        batch = session.get(IngestBatch, batch_id)
        assert batch is not None
        assert json.loads(batch.query_text or "") == {
            "keys": ["US 10,000,000 B2"],
            "duplicates_ignored": 2,
        }


def test_keys_resolving_to_the_same_publication_store_it_once(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    page = _pages()["EP3123456A1"]

    def handler(request: httpx.Request) -> httpx.Response:  # both URLs serve the A1 page
        return httpx.Response(200, content=page)

    source = _source(handler)
    report = run_lookup_batch(
        db_engine,
        RawStore(object_storage),
        source,
        start_lookup_batch(db_engine, source, ["EP3123456A1", "EP3123456"]),
    )

    assert report.status == "complete"
    assert report.outcomes == {"duplicate": 1, "stored": 1}
    assert report.documents_stored == 1
    with Session(db_engine) as session:
        detail = session.scalar(select(IngestItem.detail).where(IngestItem.outcome == "duplicate"))
    assert detail == "same publication as requested key EP3123456A1"


def test_a_different_publication_than_requested_is_quarantined(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    page = _pages()["US10000000B2"]
    source = _source(lambda request: httpx.Response(200, content=page))

    report = run_lookup_batch(
        db_engine,
        RawStore(object_storage),
        source,
        start_lookup_batch(db_engine, source, ["US10000001B2"]),
    )

    assert report.outcomes == {"quarantined": 1}
    with Session(db_engine) as session:
        [quarantined] = session.scalars(select(QuarantinedRecord)).all()
    assert quarantined.reasons == ["source served US10000000B2 for requested US10000001B2"]


def test_a_batch_cannot_run_twice_at_once(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    source = _source(_serve(_pages()))
    batch_id = start_lookup_batch(db_engine, source, ["US10000000B2"])
    with db_engine.connect() as other_run:
        other_run.execute(select(func.pg_advisory_lock(batch_id.int % (2**63))))
        with pytest.raises(BatchBusyError, match="another run"):
            run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id)
        other_run.execute(select(func.pg_advisory_unlock(batch_id.int % (2**63))))
    assert (
        run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id).status == "complete"
    )


class _BrokenStore(RawStore):
    """TEST-ONLY raw store whose writes fail, e.g. object storage is down."""

    def put(self, source_id: str, content: bytes, content_type: str) -> tuple[str, str]:
        raise RawStoreError("object storage unavailable")


def test_unexpected_errors_mark_the_batch_failed_and_resumable(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    source = _source(_serve(_pages()))
    batch_id = start_lookup_batch(db_engine, source, ["US10000000B2"])

    with pytest.raises(RawStoreError, match="unavailable"):
        run_lookup_batch(db_engine, _BrokenStore(object_storage), source, batch_id)
    with Session(db_engine) as session:
        batch = session.get(IngestBatch, batch_id)
        assert batch is not None
        assert batch.status == "failed"
        aborted = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "batch_aborted")
        ).one()
        assert "object storage unavailable" in str(aborted.payload["error"])

    assert (
        run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id).status == "complete"
    )


def test_resume_refuses_a_different_adapter_version(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    source = _source(_serve(_pages(), fail={"US10000000B2"}))
    batch_id = start_lookup_batch(db_engine, source, ["US10000000B2"])
    run_lookup_batch(db_engine, RawStore(object_storage), source, batch_id)

    upgraded = _source(_serve(_pages()))
    upgraded._info = upgraded.info.model_copy(update={"adapter_version": "99"})
    with pytest.raises(PlrError, match="created with adapter 4"):
        run_lookup_batch(db_engine, RawStore(object_storage), upgraded, batch_id)


def test_kind_fallback_is_stored_with_the_difference_recorded(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    page = _pages()["US10000000B2"]

    def handler(request: httpx.Request) -> httpx.Response:  # requested "B1" exists only as B2
        if request.url.path == "/patent/US10000000/":
            return httpx.Response(200, content=page)
        return httpx.Response(404)

    source = _source(handler)
    report = run_lookup_batch(
        db_engine,
        RawStore(object_storage),
        source,
        start_lookup_batch(db_engine, source, ["US10000000B1"]),
    )

    assert report.outcomes == {"stored": 1}
    with Session(db_engine) as session:
        detail = session.scalar(select(IngestItem.detail))
    assert detail == "kind B1 not found; looked up US10000000; served US10000000B2"


def _bare_number_only(page: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/patent/US10000000/":
            return httpx.Response(200, content=page)
        return httpx.Response(404)

    return handler


def test_kind_fallback_never_stores_a_different_kind_letter(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    source = _source(_bare_number_only(_pages()["US10000000B2"]))  # the grant, for an "A1"
    report = run_lookup_batch(
        db_engine,
        RawStore(object_storage),
        source,
        start_lookup_batch(db_engine, source, ["US10000000A1"]),
    )

    assert report.outcomes == {"quarantined": 1}
    with Session(db_engine) as session:
        detail = session.scalar(select(IngestItem.detail))
    assert detail == (
        "source served US10000000B2 for requested US10000000A1; "
        "kind A1 not found; looked up US10000000; served US10000000B2"
    )


def test_kind_fallback_note_is_kept_on_a_duplicate(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    page = _pages()["US10000000B2"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/patent/US10000000/", "/patent/US10000000B2/"):
            return httpx.Response(200, content=page)
        return httpx.Response(404)

    source = _source(handler)
    report = run_lookup_batch(
        db_engine,
        RawStore(object_storage),
        source,
        start_lookup_batch(db_engine, source, ["US10000000B2", "US10000000B1"]),
    )

    assert report.outcomes == {"stored": 1, "duplicate": 1}
    with Session(db_engine) as session:
        detail = session.scalar(
            select(IngestItem.detail).where(IngestItem.requested_key == "US10000000B1")
        )
    assert detail == (
        "same publication as requested key US10000000B2; "
        "kind B1 not found; looked up US10000000; served US10000000B2"
    )


def test_batch_provenance_is_stored_with_the_query(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    source = _source(_serve(_pages()))
    provenance: dict[str, object] = {"type": "user_folder", "folder": "/data", "files": []}
    batch_id = start_lookup_batch(db_engine, source, ["US10000000B2"], provenance=provenance)

    with Session(db_engine) as session:
        batch = session.get(IngestBatch, batch_id)
        assert batch is not None
        assert batch.query_text is not None
        assert json.loads(batch.query_text)["provenance"] == provenance
