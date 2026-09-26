"""Re-normalise stored pages with the current adapter, without fetching anything.

Every fetched payload is kept byte-for-byte in the raw store, so when the parser improves
(e.g. adapter version 5 accepts DOCDB abstracts from the national office), existing records
can be rebuilt from exactly the pages that were retrieved. Documents are append-only, so the
result is a **new batch**:

* provenance ``{"type": "replay", "from_batch": ...}``;
* the original ``retrieved_at`` (the data is as of the original retrieval);
* the same raw objects (content-addressed, hash-verified on read).

It reconciles like any batch. Keys that were never fetched in the original batch (not
found, invalid, failed) are not replayed; they are counted in the provenance.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.db.models import IngestBatch, IngestItem, RawRecord
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.runner import FETCHED_OUTCOMES, start_lookup_batch
from patsquire_plr.ingest.sources import Fetched, LookupSource, Normalized, SourceInfo

# The adapter's note when it looked a number up without its kind code (ADR 0009).
KIND_FALLBACK = re.compile(r"kind \S+ not found; looked up \S+")


@dataclass(frozen=True)
class _StoredPage:
    object_key: str
    sha256: str
    content_type: str
    retrieved_at: datetime
    fallback_note: str | None


class StoredPageSource:
    """A ``LookupSource`` that serves pages from the raw store instead of the network. It
    reports the wrapped adapter's identity and normalises with it."""

    def __init__(
        self, adapter: LookupSource, raw_store: RawStore, pages: dict[str, _StoredPage]
    ) -> None:
        self._adapter = adapter
        self._raw_store = raw_store
        self._pages = pages

    @property
    def info(self) -> SourceInfo:
        return self._adapter.info

    def fetch(self, keys: Sequence[str]) -> Iterator[Fetched]:
        for key in keys:
            page = self._pages.get(key)
            if page is None:
                raise PlrError(f"{key}: no stored page to replay")
            yield Fetched(
                requested_key=key,
                status="ok",
                content=self._raw_store.get(page.object_key, page.sha256),
                content_type=page.content_type,
                retrieved_at=page.retrieved_at,
                kind_fallback=page.fallback_note is not None,
                detail=page.fallback_note,
            )

    def normalize(
        self, content: bytes, *, raw_record_id: uuid.UUID, retrieved_at: datetime
    ) -> Normalized:
        return self._adapter.normalize(
            content, raw_record_id=raw_record_id, retrieved_at=retrieved_at
        )


def start_replay_batch(
    engine: Engine, raw_store: RawStore, adapter: LookupSource, from_batch: uuid.UUID
) -> tuple[uuid.UUID, StoredPageSource]:
    """Create the replay batch and the source that serves its stored pages."""
    with Session(engine) as session:
        batch = session.get(IngestBatch, from_batch)
        if batch is None:
            raise PlrError(f"ingest batch {from_batch} does not exist")
        if batch.source_id != adapter.info.source_id:
            raise PlrError(
                f"batch {from_batch} came from source {batch.source_id}, "
                f"not {adapter.info.source_id}"
            )
        items = session.execute(
            select(IngestItem, RawRecord)
            .outerjoin(RawRecord, RawRecord.id == IngestItem.raw_record_id)
            .where(IngestItem.batch_id == from_batch)
            .order_by(IngestItem.created_at, IngestItem.id)
        ).all()
    latest: dict[str, tuple[IngestItem, RawRecord | None]] = {}
    for row_item, row_raw in items:
        latest[row_item.requested_key] = (row_item, row_raw)
    pages: dict[str, _StoredPage] = {}
    skipped: Counter[str] = Counter()
    for key, (item, raw) in latest.items():
        if item.outcome not in FETCHED_OUTCOMES or raw is None:
            skipped[item.outcome] += 1
            continue
        fallback = KIND_FALLBACK.search(item.detail or "")
        pages[key] = _StoredPage(
            object_key=raw.object_key,
            sha256=raw.sha256,
            content_type=raw.content_type,
            retrieved_at=raw.retrieved_at,
            fallback_note=fallback.group(0) if fallback else None,
        )
    if not pages:
        raise PlrError(f"batch {from_batch} has no fetched pages to replay")
    source = StoredPageSource(adapter, raw_store, pages)
    batch_id = start_lookup_batch(
        engine,
        source,
        list(pages),
        provenance={
            "type": "replay",
            "from_batch": str(from_batch),
            "from_adapter_version": batch.adapter_version,
            "not_replayed": dict(sorted(skipped.items())),
        },
    )
    return batch_id, source
