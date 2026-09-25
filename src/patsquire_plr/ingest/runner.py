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
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.sources import Fetched, LookupSource

FINAL_OUTCOMES = frozenset({"stored", "quarantined", "not_found", "invalid_request"})


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


def _dedupe(keys: Sequence[str]) -> tuple[list[str], int]:
    seen: dict[str, None] = {}
    for key in keys:
        cleaned = key.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen), len([k for k in keys if k.strip()]) - len(seen)


def start_lookup_batch(engine: Engine, source: LookupSource, keys: Sequence[str]) -> uuid.UUID:
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
            query_text=json.dumps({"keys": requested, "duplicates_ignored": duplicates}),
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


def run_lookup_batch(
    engine: Engine, raw_store: RawStore, source: LookupSource, batch_id: uuid.UUID
) -> BatchReport:
    """Process every key of the batch that has no final outcome yet, then reconcile."""
    with Session(engine) as session:
        batch = session.get(IngestBatch, batch_id)
        if batch is None:
            raise PlrError(f"ingest batch {batch_id} does not exist")
        if batch.source_id != source.info.source_id:
            raise PlrError(
                f"batch {batch_id} belongs to source {batch.source_id}, not {source.info.source_id}"
            )
        if batch.status == "complete":
            raise PlrError(f"batch {batch_id} is already complete")
        requested: list[str] = json.loads(batch.query_text or "{}")["keys"]
        latest = _latest_outcomes(session, batch_id)
    pending = [k for k in requested if latest.get(k) not in FINAL_OUTCOMES]

    with Session(engine) as session, session.begin():
        session.execute(
            update(IngestBatch)
            .where(IngestBatch.id == batch_id)
            .values(status="running", finished_at=None)
        )
    for fetched in source.fetch(pending):
        _record(engine, raw_store, source, batch_id, fetched)
    return _reconcile(engine, batch_id)


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
        for document, pointer in normalized.documents:
            store_document(session, document, raw_pointer=pointer)
        if normalized.quarantine_reasons:
            session.add(
                QuarantinedRecord(
                    raw_record_id=raw.id,
                    raw_pointer="(whole payload)",
                    reasons=list(normalized.quarantine_reasons),
                )
            )
        session.add(
            IngestItem(
                batch_id=batch_id,
                requested_key=fetched.requested_key,
                outcome="quarantined" if normalized.quarantine_reasons else "stored",
                raw_record_id=raw.id,
                document_count=len(normalized.documents),
                detail="; ".join(normalized.quarantine_reasons) or None,
            )
        )


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
            ).where(
                IngestItem.batch_id == batch_id, IngestItem.outcome.in_(["stored", "quarantined"])
            )
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
