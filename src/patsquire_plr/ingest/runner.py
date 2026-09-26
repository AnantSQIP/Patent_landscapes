"""Run a lookup batch: fetch -> immutable raw storage -> normalise -> store or quarantine.

Guarantees (build prompt §6, §8 Layers 1-2):

* **Raw first:** every fetched payload is stored in object storage (content-addressed,
  hash-verified) before anything is derived from it.
* **One transaction per item:** its raw_record row, its documents or quarantine entry, and
  its outcome row commit together, so a crash never leaves half an item.
* **Reconciliation:** each requested key ends as ``stored``, ``quarantined``, ``not_found``
  or ``invalid_request``. Any ``failed`` item leaves the batch ``failed`` and resumable.
  Row counts are cross-checked against the outcomes, and a mismatch raises
  ``ReconciliationError``.
* **Resume:** re-running a batch fetches only the keys whose latest outcome is ``failed``.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from patsquire_plr.db.audit import append_event
from patsquire_plr.db.documents import store_document
from patsquire_plr.db.models import (
    IngestBatch,
    IngestItem,
    PatentDocumentRow,
    QuarantinedRecord,
    RawRecord,
)
from patsquire_plr.domain.patent import (
    NormalizationError,
    PatentDocument,
    normalize_publication_number,
)
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.sources import Fetched, LookupSource

FINAL_OUTCOMES = frozenset({"stored", "quarantined", "duplicate", "not_found", "invalid_request"})
FETCHED_OUTCOMES = ("stored", "quarantined", "duplicate")


class ReconciliationError(PlrError):
    """Stored rows do not add up to the recorded outcomes: stop, never paper over it."""


class BatchReport(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    batch_id: uuid.UUID
    source_id: str
    status: str
    requested: int
    duplicates_ignored: int
    outcomes: dict[str, int]
    documents_stored: int
    failed_keys: tuple[str, ...]


def _identity(key: str) -> str:
    """Spelling-independent identity of a requested key (the raw text if unparseable)."""
    try:
        return normalize_publication_number(key).text
    except NormalizationError:
        return key


def _dedupe(keys: Sequence[str]) -> tuple[list[str], int]:
    """Keep the first spelling of each distinct publication number, in request order."""
    first: dict[str, str] = {}
    cleaned = [k.strip() for k in keys if k.strip()]
    for key in cleaned:
        first.setdefault(_identity(key), key)
    return list(first.values()), len(cleaned) - len(first)


def start_lookup_batch(
    engine: Engine,
    source: LookupSource,
    keys: Sequence[str],
    *,
    provenance: dict[str, object] | None = None,
) -> uuid.UUID:
    """Create a lookup batch. ``provenance`` describes where the request came from (e.g. the
    user's files) and is stored with the batch."""
    requested, duplicates = _dedupe(keys)
    if not requested:
        raise ValueError("no keys to look up")
    with Session(engine) as session, session.begin():
        batch = IngestBatch(
            source_id=source.info.source_id,
            source_type=source.info.source_type,
            adapter_version=source.info.adapter_version,
            source_api_version=source.info.source_api_version,
            query_id="lookup",
            query_text=json.dumps(
                {"keys": requested, "duplicates_ignored": duplicates}
                | ({"provenance": provenance} if provenance is not None else {})
            ),
            started_at=datetime.now(UTC),
            finished_at=None,
            reported_count=len(requested),
            stored_count=None,
            status="running",
        )
        session.add(batch)
        session.flush()
        append_event(
            session,
            step="ingest",
            event_type="batch_started",
            actor="system",
            payload={
                "batch_id": str(batch.id),
                "source_id": source.info.source_id,
                "source_api_version": source.info.source_api_version,
                "requested": len(requested),
            },
        )
        return batch.id


class BatchBusyError(PlrError):
    """Another process is already running this batch."""


def run_lookup_batch(
    engine: Engine, raw_store: RawStore, source: LookupSource, batch_id: uuid.UUID
) -> BatchReport:
    """Process every key of the batch that has no final outcome yet, then reconcile.

    A session-level advisory lock on the batch makes concurrent runs of one batch impossible.
    Any unexpected error marks the batch ``failed`` (audit-logged) before propagating, so it
    can be resumed.
    """
    with engine.connect() as lock_conn:
        locked: bool = lock_conn.execute(
            select(func.pg_try_advisory_lock(_lock_key(batch_id)))
        ).scalar_one()
        if not locked:
            raise BatchBusyError(f"batch {batch_id} is being processed by another run")
        try:
            pending = _prepare(engine, source, batch_id)
            try:
                for fetched in source.fetch(pending):
                    _record(engine, raw_store, source, batch_id, fetched)
            except Exception as exc:
                _abort(engine, batch_id, exc)
                raise
            return _reconcile(engine, batch_id)
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(_lock_key(batch_id))))


def _lock_key(batch_id: uuid.UUID) -> int:
    return batch_id.int % (2**63)  # advisory locks take a signed 64-bit key


def _prepare(engine: Engine, source: LookupSource, batch_id: uuid.UUID) -> list[str]:
    with Session(engine) as session, session.begin():
        batch = session.get(IngestBatch, batch_id)
        if batch is None:
            raise PlrError(f"ingest batch {batch_id} does not exist")
        if batch.source_id != source.info.source_id:
            raise PlrError(
                f"batch {batch_id} belongs to source {batch.source_id}, not {source.info.source_id}"
            )
        versions = (batch.adapter_version, batch.source_api_version)
        if versions != (source.info.adapter_version, source.info.source_api_version):
            raise PlrError(
                f"batch {batch_id} was created with adapter {versions[0]} / {versions[1]}; the "
                f"current adapter is {source.info.adapter_version} / "
                f"{source.info.source_api_version}. Start a new batch instead of mixing versions."
            )
        if batch.status == "complete":
            raise PlrError(f"batch {batch_id} is already complete")
        requested: list[str] = json.loads(batch.query_text or "{}")["keys"]
        latest = _latest_outcomes(session, batch_id)
        session.execute(
            update(IngestBatch)
            .where(IngestBatch.id == batch_id)
            .values(status="running", finished_at=None)
        )
    return [k for k in requested if latest.get(k) not in FINAL_OUTCOMES]


def _abort(engine: Engine, batch_id: uuid.UUID, exc: Exception) -> None:
    with Session(engine) as session, session.begin():
        session.execute(
            update(IngestBatch)
            .where(IngestBatch.id == batch_id)
            .values(status="failed", finished_at=datetime.now(UTC))
        )
        append_event(
            session,
            step="ingest",
            event_type="batch_aborted",
            actor="system",
            payload={"batch_id": str(batch_id), "error": f"{type(exc).__name__}: {exc}"[:2000]},
        )


def _record(
    engine: Engine, raw_store: RawStore, source: LookupSource, batch_id: uuid.UUID, fetched: Fetched
) -> None:
    if fetched.status != "ok":
        with Session(engine) as session, session.begin():
            session.add(
                IngestItem(
                    batch_id=batch_id,
                    requested_key=fetched.requested_key,
                    outcome=fetched.status,
                    raw_record_id=None,
                    document_count=0,
                    detail=fetched.detail,
                )
            )
        return
    if fetched.content is None or fetched.retrieved_at is None:
        raise PlrError(f"{fetched.requested_key}: 'ok' fetch without content or timestamp")
    content_type = fetched.content_type or "application/octet-stream"
    object_key, sha256 = raw_store.put(source.info.source_id, fetched.content, content_type)
    with Session(engine) as session, session.begin():
        raw = RawRecord(
            batch_id=batch_id,
            source_record_key=fetched.requested_key,
            object_key=object_key,
            sha256=sha256,
            content_type=content_type,
            byte_size=len(fetched.content),
            retrieved_at=fetched.retrieved_at,
        )
        session.add(raw)
        session.flush()
        normalized = source.normalize(
            fetched.content, raw_record_id=raw.id, retrieved_at=fetched.retrieved_at
        )
        reasons = list(normalized.quarantine_reasons)
        reasons += [
            f"source served {d.publication.text} for requested {fetched.requested_key}"
            for d, _ in normalized.documents
            if not _matches_request(d, fetched.requested_key, ignore_kind=fetched.kind_fallback)
        ]
        notes = list(normalized.warnings)
        if fetched.kind_fallback:
            served = ", ".join(d.publication.text for d, _ in normalized.documents)
            notes.insert(0, f"{fetched.detail}; stored {served}")
        duplicate_of = None if reasons else _already_stored(session, batch_id, normalized.documents)
        if duplicate_of is not None:
            session.add(
                IngestItem(
                    batch_id=batch_id,
                    requested_key=fetched.requested_key,
                    outcome="duplicate",
                    raw_record_id=raw.id,
                    document_count=0,
                    detail=f"same publication as requested key {duplicate_of}",
                )
            )
            return
        stored = () if reasons else normalized.documents
        for document, pointer in stored:
            store_document(session, document, raw_pointer=pointer)
        if reasons:
            session.add(
                QuarantinedRecord(
                    raw_record_id=raw.id, raw_pointer="(whole payload)", reasons=reasons
                )
            )
        session.add(
            IngestItem(
                batch_id=batch_id,
                requested_key=fetched.requested_key,
                outcome="quarantined" if reasons else "stored",
                raw_record_id=raw.id,
                document_count=len(stored),
                detail="; ".join(reasons or notes) or None,
            )
        )


def _matches_request(
    document: PatentDocument, requested_key: str, *, ignore_kind: bool = False
) -> bool:
    """The served publication is the requested one. The kind code must match when one was
    requested, unless the source had to look the number up without it (recorded)."""
    requested = normalize_publication_number(requested_key)
    served = document.publication
    same_number = (served.country, served.number) == (requested.country, requested.number)
    return same_number and (ignore_kind or requested.kind in (None, served.kind))


def _already_stored(
    session: Session, batch_id: uuid.UUID, documents: tuple[tuple[PatentDocument, str], ...]
) -> str | None:
    """The requested key under which an identical publication was already stored."""
    for document, _ in documents:
        key = session.scalar(
            select(RawRecord.source_record_key)
            .join(PatentDocumentRow, PatentDocumentRow.raw_record_id == RawRecord.id)
            .where(
                RawRecord.batch_id == batch_id,
                PatentDocumentRow.publication_country == document.publication.country,
                PatentDocumentRow.publication_number == document.publication.number,
                PatentDocumentRow.publication_kind == document.publication.kind,
            )
        )
        if key is not None:
            return key
    return None


def _latest_outcomes(session: Session, batch_id: uuid.UUID) -> dict[str, str]:
    rows = session.execute(
        select(IngestItem.requested_key, IngestItem.outcome)
        .where(IngestItem.batch_id == batch_id)
        .order_by(IngestItem.created_at, IngestItem.id)
    ).all()
    return dict(rows)  # rows are (key, outcome); later rows win


def _reconcile(engine: Engine, batch_id: uuid.UUID) -> BatchReport:
    with Session(engine) as session, session.begin():
        batch = session.get(IngestBatch, batch_id)
        if batch is None:  # pragma: no cover - checked by the caller
            raise PlrError(f"ingest batch {batch_id} vanished")
        requested_info = json.loads(batch.query_text or "{}")
        requested: list[str] = requested_info["keys"]
        latest = _latest_outcomes(session, batch_id)
        problems = []
        if unknown := sorted(set(latest) - set(requested)):
            problems.append(f"outcomes for keys that were never requested: {unknown}")
        if missing := [k for k in requested if k not in latest]:
            problems.append(f"requested keys without any outcome: {missing}")

        final_items = session.execute(
            select(
                IngestItem.requested_key, IngestItem.raw_record_id, IngestItem.document_count
            ).where(IngestItem.batch_id == batch_id, IngestItem.outcome.in_(FETCHED_OUTCOMES))
        ).all()
        raw_ids = {row.raw_record_id for row in final_items}
        raw_count = session.scalar(
            select(func.count()).select_from(RawRecord).where(RawRecord.batch_id == batch_id)
        )
        documents = session.scalar(
            select(func.count())
            .select_from(PatentDocumentRow)
            .join(RawRecord, RawRecord.id == PatentDocumentRow.raw_record_id)
            .where(RawRecord.batch_id == batch_id)
        )
        if raw_count != len(raw_ids):
            problems.append(f"{raw_count} raw records but {len(raw_ids)} fetched items")
        expected_documents = sum(row.document_count for row in final_items)
        if documents != expected_documents:
            problems.append(
                f"{documents} documents stored but outcomes account for {expected_documents}"
            )

        counts = Counter(latest.get(k, "missing") for k in requested)
        failed = tuple(k for k in requested if latest.get(k) == "failed")
        status = "failed" if problems or failed else "complete"
        session.execute(
            update(IngestBatch)
            .where(IngestBatch.id == batch_id)
            .values(status=status, finished_at=datetime.now(UTC), stored_count=documents)
        )
        report = BatchReport(
            batch_id=batch_id,
            source_id=batch.source_id,
            status=status,
            requested=len(requested),
            duplicates_ignored=int(requested_info.get("duplicates_ignored", 0)),
            outcomes=dict(sorted(counts.items())),
            documents_stored=int(documents or 0),
            failed_keys=failed,
        )
        append_event(
            session,
            step="ingest",
            event_type="batch_finished",
            actor="system",
            payload={**report.model_dump(mode="json"), "problems": problems},
        )
    if problems:
        raise ReconciliationError(f"batch {batch_id}: " + "; ".join(problems))
    return report
