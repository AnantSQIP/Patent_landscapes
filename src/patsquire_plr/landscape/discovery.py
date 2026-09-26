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

import json
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
from patsquire_plr.landscape.queries import LocalMatcher
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.search import load_match_records, search_matchers


class DiscoveryError(PlrError):
    """Citation expansion cannot continue."""


@dataclass
class HopReport:
    hop: int
    batch_id: str | None
    frontier: int
    candidates: int
    frontier_undecidable: int = 0  # records the search could not evaluate; not followed
    excluded: Counter[str] = field(default_factory=Counter)
    requested: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "hop": self.hop,
            "batch_id": self.batch_id,
            "frontier": self.frontier,
            "frontier_undecidable": self.frontier_undecidable,
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
    search = search_matchers(engine, query_set_id, scheme)
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
        frontier, undecidable = _frontier(engine, batch_ids[-1], search if hop > 1 else None)
        seen |= _retrieved(engine, batch_ids)
        links = _links(engine, frontier, settings.directions)
        hop_report = HopReport(
            hop=hop,
            batch_id=None,
            frontier=len(frontier),
            candidates=len(links),
            frontier_undecidable=undecidable,
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
    """Continue this hop's batch if an earlier run created it; otherwise start it. A batch
    is reused only if it asked for exactly the same publications, so changed settings or
    seeds can never be recorded against an old batch."""
    with Session(engine) as session:
        existing = session.execute(
            select(IngestBatch.id, IngestBatch.query_text).where(
                IngestBatch.query_text.contains(f'"{key}"', autoescape=True)
            )
        ).all()
    if len(existing) > 1:
        raise DiscoveryError(f"more than one batch for {key}: {[r.id for r in existing]}")
    if existing:
        stored_keys = json.loads(existing[0].query_text or "{}").get("keys", [])
        if {_identity(k) for k in stored_keys} != {_identity(n) for n in numbers}:
            raise DiscoveryError(
                f"{key}: batch {existing[0].id} asked for different publications than this run "
                "would (seeds or expansion settings changed); generate a new query set to "
                "expand again"
            )
        if batch_summary(engine, existing[0].id).status == "complete":
            return batch_summary(engine, existing[0].id)
    batch_id = (
        existing[0].id
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


def _frontier(
    engine: Engine, batch_id: uuid.UUID, search: Sequence[LocalMatcher] | None
) -> tuple[list[uuid.UUID], int]:
    """Documents stored by the batch; after hop 1 only those the search matches. Also
    returns how many records the search could not evaluate (they are not followed)."""
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
        return sorted(documents.values()), 0
    matched, undecidable = [], 0
    for record in load_match_records(engine, [batch_id]):
        outcomes = [m.evaluate(record) for m in search]
        if any(o.matched for o in outcomes):
            matched.append(documents[record.publication])
        elif any(o.undecidable for o in outcomes):
            undecidable += 1
    return sorted(matched), undecidable


def _links(
    engine: Engine, frontier: Sequence[uuid.UUID], directions: Sequence[str]
) -> dict[tuple[str, str], tuple[int, str]]:
    """Publication (country, number) -> (frontier documents linking to it, the number to
    request). Kinds of one publication (A1, B2) count as one; the lowest spelling is
    requested, so the choice is deterministic."""
    linked: dict[tuple[str, str], set[uuid.UUID]] = {}
    spellings: dict[tuple[str, str], str] = {}
    unparseable: set[str] = set()

    def add(document_id: uuid.UUID, number: str | None) -> None:
        if number is None:
            return
        try:
            identity = _identity(number)
        except NormalizationError:
            unparseable.add(number)
            return
        linked.setdefault(identity, set()).add(document_id)
        spellings[identity] = min(spellings.get(identity, number), number)

    with Session(engine) as session:
        for chunk in (frontier[i : i + 1000] for i in range(0, len(frontier), 1000)):
            if "backward" in directions:
                for document_id, number in session.execute(
                    select(DocumentCitation.document_id, DocumentCitation.publication_number).where(
                        DocumentCitation.document_id.in_(chunk),
                        DocumentCitation.kind == "patent",
                    )
                ):
                    add(document_id, number)
            if "forward" in directions:
                for document_id, number in session.execute(
                    select(
                        DocumentForwardCitation.document_id,
                        DocumentForwardCitation.citing_publication_number,
                    ).where(DocumentForwardCitation.document_id.in_(chunk))
                ):
                    add(document_id, number)
    if unparseable:  # stored numbers are normalised, so this would be a data error
        raise DiscoveryError(f"stored citations with unparseable numbers: {sorted(unparseable)}")
    return {identity: (len(docs), spellings[identity]) for identity, docs in linked.items()}


def _choose(
    links: dict[tuple[str, str], tuple[int, str]],
    seen: set[tuple[str, str]],
    scope: Scope,
    cap: int,
    excluded: Counter[str],
) -> list[str]:
    """Ranked by links (descending), then number. ``already_requested`` includes
    publications requested earlier that were not found."""
    eligible = []
    for identity, (count, number) in links.items():
        if identity in seen:
            excluded["already_requested"] += 1
        elif scope.countries and identity[0] not in scope.countries:
            excluded["office_out_of_scope"] += 1
        else:
            eligible.append((-count, number))
    eligible.sort()
    excluded["over_cap"] += max(0, len(eligible) - cap)
    if not excluded["over_cap"]:
        del excluded["over_cap"]
    return [number for _, number in eligible[:cap]]


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
