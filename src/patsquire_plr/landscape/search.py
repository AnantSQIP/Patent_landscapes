"""Query sets, per-query counts and the recall check (ADR 0010, verification Layer 4).

A query set is generated only from an **approved** taxonomy version. Counting always names
its universe, for example ``batches:<id>,<id>`` for records already stored. A count over
stored records is not a count over a whole patent database. Provider counts (EPO OPS, Lens,
BigQuery) are recorded in the same table once credentials exist.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.classification.cpc import CpcScheme
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import (
    DocumentClassification,
    PatentDocumentRow,
    QueryCount,
    QuerySet,
    RawRecord,
    RecallCheck,
    SearchQuery,
)
from patsquire_plr.domain.patent import normalize_publication_number
from patsquire_plr.landscape.queries import (
    GENERATOR_VERSION,
    LocalMatcher,
    LogicalQuery,
    MatchRecord,
    build_queries,
)
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.store import NotFoundError, get_taxonomy, require_approved


def create_query_set(
    engine: Engine, *, taxonomy_version_id: uuid.UUID, scope: Scope, scheme: CpcScheme
) -> uuid.UUID:
    require_approved(engine, "taxonomy_version", taxonomy_version_id)
    _, content = get_taxonomy(engine, version_id=taxonomy_version_id)
    generated = build_queries(content, scope, scheme)
    with Session(engine) as session, session.begin():
        query_set = QuerySet(
            taxonomy_version_id=taxonomy_version_id,
            generator_version=GENERATOR_VERSION,
            config={"cpc_scheme_version": scheme.version, "scope_sha256": scope.sha256},
        )
        session.add(query_set)
        session.flush()
        for query in generated:
            logical = query.logical.model_dump(mode="json")
            session.add_all(
                SearchQuery(
                    query_set_id=query_set.id,
                    segment_id=query.segment_id,
                    part=query.part,
                    provider=provider,
                    query_text=text,
                    logical_query=logical,
                )
                for provider, text in query.rendered.items()
            )
        append_event(
            session,
            step="key_strings",
            event_type="query_set_created",
            actor="system",
            payload={
                "query_set_id": str(query_set.id),
                "taxonomy_version_id": str(taxonomy_version_id),
                "queries": len(generated),
                "generator_version": GENERATOR_VERSION,
            },
        )
        return query_set.id


def local_queries(engine: Engine, query_set_id: uuid.UUID) -> list[SearchQuery]:
    with Session(engine, expire_on_commit=False) as session:
        if session.get(QuerySet, query_set_id) is None:
            raise NotFoundError(f"no query set {query_set_id}")
        return list(
            session.scalars(
                select(SearchQuery)
                .where(SearchQuery.query_set_id == query_set_id, SearchQuery.provider == "local")
                .order_by(SearchQuery.segment_id, SearchQuery.part)
            )
        )


# ------------------------------------------------------------------ stored records as a universe


def universe_name(batch_ids: Sequence[uuid.UUID]) -> str:
    return "batches:" + ",".join(sorted(str(b) for b in batch_ids))


def load_match_records(engine: Engine, batch_ids: Sequence[uuid.UUID]) -> list[MatchRecord]:
    """One record per publication stored by the batches (the most recent retrieval)."""
    with Session(engine) as session:
        rows = session.execute(
            select(PatentDocumentRow, RawRecord.retrieved_at)
            .join(RawRecord, RawRecord.id == PatentDocumentRow.raw_record_id)
            .where(RawRecord.batch_id.in_(batch_ids))
            .order_by(RawRecord.retrieved_at, PatentDocumentRow.id)
        ).all()
        latest: dict[str, PatentDocumentRow] = {}
        for document, _ in rows:
            latest[_publication_text(document)] = document
        codes: dict[uuid.UUID, list[str]] = {}
        ids = [d.id for d in latest.values()]
        for chunk in (ids[i : i + 5000] for i in range(0, len(ids), 5000)):
            for document_id, code in session.execute(
                select(DocumentClassification.document_id, DocumentClassification.code)
                .where(
                    DocumentClassification.document_id.in_(chunk),
                    DocumentClassification.scheme == "cpc",
                )
                .order_by(DocumentClassification.document_id, DocumentClassification.ordinal)
            ):
                codes.setdefault(document_id, []).append(code)
    return [
        MatchRecord(
            publication=publication,
            office=d.publication_country,
            publication_date=d.publication_date,
            text=" ".join(t for t in (d.title, d.abstract) if t),
            cpc=tuple(codes.get(d.id, ())),
        )
        for publication, d in sorted(latest.items())
    ]


def _publication_text(document: PatentDocumentRow) -> str:
    return (
        f"{document.publication_country}{document.publication_number}"
        f"{document.publication_kind or ''}"
    )


@dataclass(frozen=True)
class LocalCount:
    search_query_id: uuid.UUID
    segment_id: str
    part: str
    count: int
    undecidable: dict[str, int]


def count_locally(
    engine: Engine,
    *,
    query_set_id: uuid.UUID,
    batch_ids: Sequence[uuid.UUID],
    scheme: CpcScheme,
) -> list[LocalCount]:
    """Count every query of the set over the batches' records, and record the counts."""
    records = load_match_records(engine, batch_ids)
    universe = universe_name(batch_ids)
    results = []
    for query in local_queries(engine, query_set_id):
        matcher = LocalMatcher(
            LogicalQuery.model_validate(query.logical_query, strict=False), scheme
        )
        outcomes = [matcher.evaluate(r) for r in records]
        undecidable = Counter(o.undecidable for o in outcomes if o.undecidable is not None)
        results.append(
            LocalCount(
                search_query_id=query.id,
                segment_id=query.segment_id,
                part=query.part,
                count=sum(o.matched for o in outcomes),
                undecidable=dict(sorted(undecidable.items())),
            )
        )
    with Session(engine) as session, session.begin():
        session.add_all(
            QueryCount(
                search_query_id=r.search_query_id,
                universe=universe,
                count=r.count,
                # Records that could not be evaluated might have matched.
                count_is_lower_bound=bool(r.undecidable),
                detail={"records": len(records), "undecidable": r.undecidable},
            )
            for r in results
        )
        append_event(
            session,
            step="key_strings",
            event_type="queries_counted",
            actor="system",
            payload={
                "query_set_id": str(query_set_id),
                "universe": universe,
                "records": len(records),
                "queries": len(results),
            },
        )
    return results


# ------------------------------------------------------------------ recall check


@dataclass(frozen=True)
class RecallResult:
    universe: str
    known: tuple[str, ...]
    found: tuple[str, ...]
    missed: dict[str, str]  # publication -> why

    @property
    def recall(self) -> tuple[int, int]:
        return len(self.found), len(self.known)


def check_recall(
    engine: Engine,
    *,
    query_set_id: uuid.UUID,
    batch_ids: Sequence[uuid.UUID],
    scheme: CpcScheme,
    known: Sequence[str],
) -> RecallResult:
    """Which confirmed publications the search (the ``combined`` queries, or ``keywords``
    for segments without codes) finds among the stored records. Matching by country and
    number, so a known ``B2`` is found by its ``A1`` publication too."""
    records = {(r.office, _number(r.publication)): r for r in load_match_records(engine, batch_ids)}
    queries = local_queries(engine, query_set_id)
    with_codes = {q.segment_id for q in queries if q.part == "combined"}
    search = [
        LocalMatcher(LogicalQuery.model_validate(q.logical_query, strict=False), scheme)
        for q in queries
        if q.part == "combined" or (q.part == "keywords" and q.segment_id not in with_codes)
    ]
    found, missed = [], {}
    for publication in known:
        record = records.get((publication[:2], _number(publication)))
        if record is None:
            missed[publication] = "not among the retrieved records"
            continue
        outcomes = [m.evaluate(record) for m in search]
        if any(o.matched for o in outcomes):
            found.append(publication)
        else:
            reasons = sorted({o.undecidable for o in outcomes if o.undecidable})
            missed[publication] = "retrieved, but no segment query matches it" + (
                f" ({'; '.join(reasons)})" if reasons else ""
            )
    result = RecallResult(
        universe=universe_name(batch_ids),
        known=tuple(known),
        found=tuple(found),
        missed=missed,
    )
    with Session(engine) as session, session.begin():
        session.add(
            RecallCheck(
                query_set_id=query_set_id,
                universe=result.universe,
                known=list(result.known),
                found=list(result.found),
                missed=dict(result.missed),
            )
        )
        append_event(
            session,
            step="key_strings",
            event_type="recall_checked",
            actor="system",
            payload={
                "query_set_id": str(query_set_id),
                "universe": result.universe,
                "found": len(found),
                "known": len(known),
            },
        )
    return result


def _number(publication: str) -> str:
    """The number part of a normalised publication (no country, no kind)."""
    return normalize_publication_number(publication).number
