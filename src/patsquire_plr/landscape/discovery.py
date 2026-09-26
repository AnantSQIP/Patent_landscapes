"""Key-less discovery: citation expansion from seed patents (ADR 0010).

Without a search API, candidates are found by following citations:

* hop 0 retrieves the seeds;
* hop *n* retrieves the publications that the previous hop's **frontier** cites (backward)
  or is cited by (forward), as the source states them;
* the frontier is every seed at hop 0. After that it is only the records that the approved
  search (the ``combined`` queries, or ``keywords`` where a segment has no codes) matches,
  so expansion follows on-topic patents, not everything.

Each hop is an ordinary lookup batch (provenance ``citation_expansion``), so it reconciles,
is audit-logged and can be resumed. Running the expansion again continues the same batches
rather than starting new ones.

Candidates are ranked by how many frontier records link to them, then by number, and at most
``max_fetch_per_hop`` are fetched. Every candidate not fetched is counted with a reason
(already retrieved, office outside the scope, over the cap).

**Recall caveat (stated in the methodology):** this finds what is connected to the seeds by
citations within the hop limit. Relevant patents with no citation path to the seeds are not
found. The counts describe this universe, not a whole patent database.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.classification.cpc import CpcScheme
from patsquire_plr.config import CitationExpansionSettings
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import (
    DiscoveryRun,
    DocumentCitation,
    DocumentForwardCitation,
    IngestBatch,
    PatentDocumentRow,
    RawRecord,
)
from patsquire_plr.domain.patent import NormalizationError, normalize_publication_number
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.runner import (
    BatchReport,
    batch_summary,
    run_lookup_batch,
    start_lookup_batch,
)
from patsquire_plr.ingest.sources import LookupSource
from patsquire_plr.landscape.queries import LocalMatcher, LogicalQuery
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.search import load_match_records, local_queries


class DiscoveryError(PlrError):
    """Citation expansion cannot continue."""


@dataclass
class HopReport:
    hop: int
    batch_id: str | None
    frontier: int
    candidates: int
    excluded: Counter[str] = field(default_factory=Counter)
    requested: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "hop": self.hop,
            "batch_id": self.batch_id,
            "frontier": self.frontier,
            "candidates": self.candidates,
            "excluded": dict(sorted(self.excluded.items())),
            "requested": self.requested,
            "outcomes": dict(sorted(self.outcomes.items())),
        }


Progress = Callable[[str], None]


def expand_citations(
    engine: Engine,
    raw_store: RawStore,
    source: LookupSource,
    *,
    landscape_id: uuid.UUID,
    query_set_id: uuid.UUID,
    scope: Scope,
    scheme: CpcScheme,
    settings: CitationExpansionSettings,
    progress: Progress = lambda _: None,
) -> tuple[uuid.UUID, list[HopReport]]:
    if not scope.seeds:
        raise DiscoveryError("the scope has no seeds to expand from")
    search = _search_matchers(engine, query_set_id, scheme)
    hops: list[HopReport] = []
    batch_ids: list[uuid.UUID] = []
    seen = {_identity(s) for s in scope.seeds}
    report = _run_hop(
        engine, raw_store, source, _hop_key(landscape_id, query_set_id, 0), list(scope.seeds)
    )
    batch_ids.append(report.batch_id)
    hops.append(
        HopReport(
            hop=0,
            batch_id=str(report.batch_id),
            frontier=0,
            candidates=len(scope.seeds),
            requested=report.requested,
            outcomes=report.outcomes,
        )
    )
    progress(f"hop 0 (seeds): {report.outcomes}")
    for hop in range(1, settings.max_hops + 1):
        frontier = _frontier(engine, batch_ids[-1], search if hop > 1 else None)
        seen |= _retrieved(engine, batch_ids)
        links = _links(engine, frontier, settings.directions)
        hop_report = HopReport(
            hop=hop, batch_id=None, frontier=len(frontier), candidates=len(links)
        )
        chosen = _choose(links, seen, scope, settings.max_fetch_per_hop, hop_report.excluded)
        if not chosen:
            hops.append(hop_report)
            progress(f"hop {hop}: nothing new to fetch ({hop_report.excluded})")
            break
        report = _run_hop(
            engine, raw_store, source, _hop_key(landscape_id, query_set_id, hop), chosen
        )
        seen |= {_identity(c) for c in chosen}
        batch_ids.append(report.batch_id)
        hop_report.batch_id = str(report.batch_id)
        hop_report.requested = report.requested
        hop_report.outcomes = report.outcomes
        hops.append(hop_report)
        progress(f"hop {hop}: frontier {len(frontier)}, fetched {report.outcomes}")
    with Session(engine) as session, session.begin():
        run = DiscoveryRun(
            landscape_id=landscape_id,
            query_set_id=query_set_id,
            config=settings.model_dump(mode="json"),
            hops=[h.as_json() for h in hops],
            batch_ids=[str(b) for b in batch_ids],
        )
        session.add(run)
        session.flush()
        append_event(
            session,
            step="discovery",
            event_type="citation_expansion_finished",
            actor="system",
            payload={
                "discovery_run_id": str(run.id),
                "landscape_id": str(landscape_id),
                "batches": [str(b) for b in batch_ids],
            },
        )
        return run.id, hops


def _hop_key(landscape_id: uuid.UUID, query_set_id: uuid.UUID, hop: int) -> str:
    return f"citation_expansion:{landscape_id}:{query_set_id}:hop{hop}"


def _run_hop(
    engine: Engine, raw_store: RawStore, source: LookupSource, key: str, numbers: list[str]
) -> BatchReport:
    """Continue this hop's batch if an earlier run created it; otherwise start it."""
    with Session(engine) as session:
        existing = session.scalars(
            select(IngestBatch.id).where(IngestBatch.query_text.contains(f'"{key}"'))
        ).all()
    if len(existing) > 1:
        raise DiscoveryError(f"more than one batch for {key}: {existing}")
    if existing and batch_summary(engine, existing[0]).status == "complete":
        return batch_summary(engine, existing[0])
    batch_id = (
        existing[0]
        if existing
        else start_lookup_batch(
            engine, source, numbers, provenance={"type": "citation_expansion", "key": key}
        )
    )
    report = run_lookup_batch(engine, raw_store, source, batch_id)
    if report.status != "complete":
        raise DiscoveryError(
            f"{key}: batch {batch_id} is {report.status} (failed: {list(report.failed_keys)}); "
            "run the expansion again to resume it"
        )
    return report


def _search_matchers(
    engine: Engine, query_set_id: uuid.UUID, scheme: CpcScheme
) -> list[LocalMatcher]:
    queries = local_queries(engine, query_set_id)
    with_codes = {q.segment_id for q in queries if q.part == "combined"}
    return [
        LocalMatcher(LogicalQuery.model_validate(q.logical_query, strict=False), scheme)
        for q in queries
        if q.part == "combined" or (q.part == "keywords" and q.segment_id not in with_codes)
    ]


def _frontier(
    engine: Engine, batch_id: uuid.UUID, search: Sequence[LocalMatcher] | None
) -> list[uuid.UUID]:
    """Documents stored by the batch; after hop 1 only those the search matches."""
    records = {r.publication: r for r in load_match_records(engine, [batch_id])}
    with Session(engine) as session:
        rows = session.execute(
            select(PatentDocumentRow)
            .join(RawRecord, RawRecord.id == PatentDocumentRow.raw_record_id)
            .where(RawRecord.batch_id == batch_id)
        ).scalars()
        documents = {
            f"{d.publication_country}{d.publication_number}{d.publication_kind or ''}": d.id
            for d in rows
        }
    if search is None:
        return sorted(documents.values())
    return sorted(
        documents[p]
        for p, record in records.items()
        if p in documents and any(m.evaluate(record).matched for m in search)
    )


def _links(
    engine: Engine, frontier: Sequence[uuid.UUID], directions: Sequence[str]
) -> Counter[str]:
    """Publication -> number of frontier documents linking to it."""
    links: Counter[str] = Counter()
    with Session(engine) as session:
        for chunk in (frontier[i : i + 1000] for i in range(0, len(frontier), 1000)):
            if "backward" in directions:
                for _, number in session.execute(
                    select(DocumentCitation.document_id, DocumentCitation.publication_number)
                    .where(
                        DocumentCitation.document_id.in_(chunk),
                        DocumentCitation.kind == "patent",
                    )
                    .distinct()
                ):
                    if number is not None:
                        links[number] += 1
            if "forward" in directions:
                for _, number in session.execute(
                    select(
                        DocumentForwardCitation.document_id,
                        DocumentForwardCitation.citing_publication_number,
                    )
                    .where(DocumentForwardCitation.document_id.in_(chunk))
                    .distinct()
                ):
                    links[number] += 1
    return links


def _choose(
    links: Counter[str],
    seen: set[tuple[str, str]],
    scope: Scope,
    cap: int,
    excluded: Counter[str],
) -> list[str]:
    eligible = []
    for number, count in links.items():
        try:
            identity = _identity(number)
        except NormalizationError:
            excluded["unparseable_number"] += 1
            continue
        if identity in seen:
            excluded["already_retrieved"] += 1
        elif scope.countries and identity[0] not in scope.countries:
            excluded["office_out_of_scope"] += 1
        else:
            eligible.append((-count, number, identity))
    eligible.sort()
    chosen: list[str] = []
    picked: set[tuple[str, str]] = set()
    for _, number, identity in eligible:
        if identity in picked:  # two kinds of one publication: one fetch
            excluded["same_publication_other_kind"] = (
                excluded.get("same_publication_other_kind", 0) + 1
            )
        elif len(chosen) >= cap:
            excluded["over_cap"] += 1
        else:
            chosen.append(number)
            picked.add(identity)
    return chosen


def _identity(number: str) -> tuple[str, str]:
    publication = normalize_publication_number(number)
    return publication.country, publication.number


def _retrieved(engine: Engine, batch_ids: Sequence[uuid.UUID]) -> set[tuple[str, str]]:
    with Session(engine) as session:
        return {
            (country, number)
            for country, number in session.execute(
                select(PatentDocumentRow.publication_country, PatentDocumentRow.publication_number)
                .join(RawRecord, RawRecord.id == PatentDocumentRow.raw_record_id)
                .where(RawRecord.batch_id.in_(batch_ids))
            )
        }
