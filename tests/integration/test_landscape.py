"""Phase 5 on real Postgres + MinIO: CPC files, taxonomy versions, approvals, query sets,
counts, recall and citation expansion over the recorded Google pages (TEST-ONLY taxonomy)."""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from patsquire_plr.classification.cpc import CpcScheme, parse_title_list
from patsquire_plr.classification.store import (
    SchemeNotLoadedError,
    load_cpc_title_list,
    open_cpc_scheme,
)
from patsquire_plr.cli import app
from patsquire_plr.config import CitationExpansionSettings, ObjectStorageSettings
from patsquire_plr.db.models import (
    AuditEvent,
    DiscoveryRun,
    IngestBatch,
    QueryCount,
    RecallCheck,
    SearchQuery,
)
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.runner import run_lookup_batch, start_lookup_batch
from patsquire_plr.landscape.discovery import DiscoveryError, HopReport, expand_citations
from patsquire_plr.landscape.queries import QueryError
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.search import check_recall, count_locally, create_query_set
from patsquire_plr.landscape.store import (
    NotApprovedError,
    NotFoundError,
    add_taxonomy_version,
    create_landscape,
    get_scope,
    get_taxonomy,
    latest_decision,
    record_approval,
)
from patsquire_plr.landscape.taxonomy import (
    KeywordGroup,
    ResolvedCpc,
    SegmentSpec,
    TaxonomyContent,
    TaxonomySpec,
)
from tests.integration.test_ingestion import _pages, _serve, _source
from tests.support import TEST_DB_PASSWORD, base_config, base_secrets
from tests.unit.test_cpc import title_list_zip

pytestmark = pytest.mark.integration

SCOPE = Scope(
    topic="Coherent ladar",
    notes=None,
    date_from=date(1990, 1, 1),
    date_to=date(2024, 12, 31),
    countries=("US",),
    cpc_hints=(),
    seeds=("US5093563A",),
    known_relevant=("US10000000B2", "US5093563A", "US9999999B2"),
    confirmed_by="Test Reviewer",
)


def _content(scheme: CpcScheme) -> TaxonomyContent:
    segment = SegmentSpec(
        id="ladar",
        name="Ladar",
        definition="Laser detection and ranging systems.",
        includes=(),
        excludes=(),
        keywords=(KeywordGroup(term="ladar", synonyms=("laser radar",)),),
        cpc=("G01S7/48",),
    )
    return TaxonomyContent(
        spec=TaxonomySpec(topic=SCOPE.topic, segments=(segment,)),
        cpc_scheme_version=scheme.version,
        cpc={"ladar": (ResolvedCpc(symbol="G01S7/48", title_path=scheme.title_path("G01S7/48")),)},
        suggestions={"ladar": ()},
        rejected=(),
    )


def _prepared(
    engine: Engine, store: ObjectStorageSettings
) -> tuple[uuid.UUID, uuid.UUID, CpcScheme]:
    """A landscape with one approved taxonomy version; returns (landscape, version, scheme)."""
    row, _ = load_cpc_title_list(engine, RawStore(store), title_list_zip(), source_url="test")
    _, scheme = open_cpc_scheme(engine, RawStore(store))
    landscape_id = create_landscape(engine, name="test", scope=SCOPE)
    version = add_taxonomy_version(
        engine,
        landscape_id=landscape_id,
        content=_content(scheme),
        origin="llm_draft",
        scheme_id=row.id,
        cache_keys=["k1"],
        parent_id=None,
        actor="system",
    )
    return landscape_id, version.id, scheme


def _approve(engine: Engine, subject: str, subject_id: uuid.UUID) -> None:
    record_approval(
        engine,
        subject_type=subject,  # type: ignore[arg-type]
        subject_id=subject_id,
        decision="approved",
        mode="human",
        decided_by="Test Reviewer",
        note=None,
    )


# ------------------------------------------------------------------ CPC files


def test_cpc_versions_are_stored_once_and_never_replaced(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    store = RawStore(object_storage)
    with pytest.raises(SchemeNotLoadedError, match="no CPC title list loaded"):
        open_cpc_scheme(db_engine, store)

    first, added = load_cpc_title_list(db_engine, store, title_list_zip(), source_url="u")
    again, added_again = load_cpc_title_list(db_engine, store, title_list_zip(), source_url="u")
    assert (added, added_again, again.id) == (True, False, first.id)
    assert first.entry_count == 154

    changed = title_list_zip({"cpc-section-G_20260801.txt": "G\t\tPHYSICS\n"})
    with pytest.raises(PlrError, match="already loaded from a different file"):
        load_cpc_title_list(db_engine, store, changed, source_url="u")
    row, scheme = open_cpc_scheme(db_engine, store, version="2026.08")
    assert (row.id, len(scheme)) == (first.id, 154)


# ------------------------------------------------------------------ versions and approvals


def test_taxonomy_versions_number_up_and_approval_gates_query_generation(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    landscape_id, v1, scheme = _prepared(db_engine, object_storage)
    _, scope = get_scope(db_engine, landscape_id)
    assert scope == SCOPE

    with pytest.raises(NotApprovedError, match="no decision"):
        create_query_set(db_engine, taxonomy_version_id=v1, scope=SCOPE, scheme=scheme)
    record_approval(
        db_engine,
        subject_type="taxonomy_version",
        subject_id=v1,
        decision="rejected",
        mode="human",
        decided_by="Test Reviewer",
        note="too broad",
    )
    with pytest.raises(NotApprovedError, match="rejected by Test Reviewer"):
        create_query_set(db_engine, taxonomy_version_id=v1, scope=SCOPE, scheme=scheme)

    scheme_id = get_taxonomy(db_engine, version_id=v1)[0].classification_scheme_id
    v2 = add_taxonomy_version(
        db_engine,
        landscape_id=landscape_id,
        content=_content(scheme),
        origin="user_edit",
        scheme_id=scheme_id,
        cache_keys=[],
        parent_id=v1,
        actor="Test Reviewer",
    )
    latest, _ = get_taxonomy(db_engine, landscape_id=landscape_id)
    assert (latest.id, latest.version, latest.parent_id) == (v2.id, 2, v1)
    _approve(db_engine, "taxonomy_version", v2.id)
    decision = latest_decision(db_engine, "taxonomy_version", v2.id)
    assert decision is not None
    assert decision.decision == "approved"

    query_set_id = create_query_set(
        db_engine, taxonomy_version_id=v2.id, scope=SCOPE, scheme=scheme
    )
    with Session(db_engine) as session:
        rows = session.scalars(select(SearchQuery).where(SearchQuery.query_set_id == query_set_id))
        keys = sorted((r.part, r.provider) for r in rows)
        events = session.scalars(select(AuditEvent.event_type)).all()
    assert len(keys) == 12  # 3 parts x 4 providers
    assert {"landscape_created", "taxonomy_version_added", "taxonomy_version_rejected"} <= set(
        events
    )
    assert "query_set_created" in events


def test_missing_things_are_reported(db_engine: Engine) -> None:
    with pytest.raises(NotFoundError):
        get_scope(db_engine, uuid.uuid4())
    with pytest.raises(NotFoundError):
        get_taxonomy(db_engine, landscape_id=uuid.uuid4())
    with pytest.raises(PlrError, match="must name who decided"):
        record_approval(
            db_engine,
            subject_type="query_set",
            subject_id=uuid.uuid4(),
            decision="approved",
            mode="human",
            decided_by=" ",
            note=None,
        )


# ------------------------------------------------------------------ discovery, counts, recall


def test_citation_expansion_counts_and_recall(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    landscape_id, version_id, scheme = _prepared(db_engine, object_storage)
    _approve(db_engine, "taxonomy_version", version_id)
    query_set_id = create_query_set(
        db_engine, taxonomy_version_id=version_id, scope=SCOPE, scheme=scheme
    )
    source = _source(_serve(_pages()))  # recorded pages; every other number is "not found"
    settings = CitationExpansionSettings(
        max_hops=2, max_fetch_per_hop=500, directions=("backward", "forward")
    )

    def expand() -> tuple[uuid.UUID, list[HopReport]]:
        run_id, hops = expand_citations(
            db_engine,
            RawStore(object_storage),
            source,
            landscape_id=landscape_id,
            query_set_id=query_set_id,
            scope=SCOPE,
            scheme=scheme,
            settings=settings,
        )
        return run_id, hops

    run_id, hops = expand()

    assert [h.hop for h in hops] == [0, 1, 2]
    assert hops[0].outcomes == {"stored": 1}
    hop1 = hops[1]
    assert hop1.frontier == 1
    assert hop1.outcomes["stored"] == 2  # the two ladar publications citing the seed
    assert hop1.excluded["office_out_of_scope"] > 0
    assert hop1.candidates == sum(hop1.excluded.values()) + hop1.requested
    hop2 = hops[2]
    assert hop2.frontier == 2  # only the records the search matches are followed
    assert hop2.excluded["already_requested"] >= 1

    # Running again continues the same batches instead of starting new ones.
    _, again = expand()
    assert [h.batch_id for h in again] == [h.batch_id for h in hops]
    with Session(db_engine) as session:
        run = session.get(DiscoveryRun, run_id)
        assert run is not None
        batch_ids = [uuid.UUID(str(b)) for b in run.batch_ids]
        assert session.scalar(select(func.count()).select_from(IngestBatch)) == 3

    counts = {
        (c.segment_id, c.part): c.count
        for c in count_locally(
            db_engine, query_set_id=query_set_id, batch_ids=batch_ids, scheme=scheme
        )
    }
    assert counts == {("ladar", "keywords"): 2, ("ladar", "cpc"): 2, ("ladar", "combined"): 2}

    recall = check_recall(
        db_engine,
        query_set_id=query_set_id,
        batch_ids=batch_ids,
        scheme=scheme,
        known=SCOPE.known_relevant,
    )
    assert recall.recall == (1, 3)
    assert recall.missed == {
        "US5093563A": "retrieved, but no segment query matches it",
        "US9999999B2": "not among the retrieved records",
    }
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(QueryCount)) == 3
        stored = session.scalars(select(RecallCheck)).one()
        assert stored.found == ["US10000000B2"]


def test_expansion_needs_seeds(db_engine: Engine, object_storage: ObjectStorageSettings) -> None:
    landscape_id, version_id, scheme = _prepared(db_engine, object_storage)
    _approve(db_engine, "taxonomy_version", version_id)
    query_set_id = create_query_set(
        db_engine, taxonomy_version_id=version_id, scope=SCOPE, scheme=scheme
    )
    with pytest.raises(DiscoveryError, match="no seeds"):
        expand_citations(
            db_engine,
            RawStore(object_storage),
            _source(_serve({})),
            landscape_id=landscape_id,
            query_set_id=query_set_id,
            scope=SCOPE.model_copy(update={"seeds": ()}),
            scheme=scheme,
            settings=CitationExpansionSettings(
                max_hops=1, max_fetch_per_hop=5, directions=("backward",)
            ),
        )


def test_a_failed_hop_stops_the_expansion_and_can_be_resumed(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    landscape_id, version_id, scheme = _prepared(db_engine, object_storage)
    _approve(db_engine, "taxonomy_version", version_id)
    query_set_id = create_query_set(
        db_engine, taxonomy_version_id=version_id, scope=SCOPE, scheme=scheme
    )
    settings = CitationExpansionSettings(max_hops=1, max_fetch_per_hop=5, directions=("backward",))
    kwargs = {
        "landscape_id": landscape_id,
        "query_set_id": query_set_id,
        "scope": SCOPE,
        "scheme": scheme,
        "settings": settings,
    }
    failing = _source(_serve(_pages(), fail={"US5093563A"}))
    with pytest.raises(DiscoveryError, match="run the expansion again to resume"):
        expand_citations(db_engine, RawStore(object_storage), failing, **kwargs)  # type: ignore[arg-type]

    _, hops = expand_citations(
        db_engine,
        RawStore(object_storage),
        _source(_serve(_pages())),
        **kwargs,  # type: ignore[arg-type]
    )
    assert hops[0].outcomes == {"stored": 1}


# ------------------------------------------------------------------ the commands


def test_phase_5_commands(
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
    secrets = {
        **base_secrets(),
        "PLR__DATABASE__PASSWORD": TEST_DB_PASSWORD,
        "PLR__OBJECT_STORAGE__ACCESS_KEY_ID": object_storage.access_key_id.get_secret_value(),
        "PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY": (
            object_storage.secret_access_key.get_secret_value()
        ),
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    def plr(*args: str) -> str:
        result = CliRunner().invoke(app, [*args, "--config", str(config)])
        assert result.exit_code == 0, result.output
        return result.stdout

    archive = tmp_path / "CPCTitleList202608.zip"
    archive.write_bytes(title_list_zip())
    assert "CPC 2026.08 loaded: 154 entries" in plr(
        "cpc", "load", str(archive), "--source-url", "https://example.test/cpc.zip"
    )
    assert "G06N3/0455\tG06N3/0455\t" in plr("cpc", "check", "G06N3/0455")
    invalid = CliRunner().invoke(app, ["cpc", "check", "G06N3/9", "--config", str(config)])
    assert invalid.exit_code == 1
    assert "G06N3/02\tneural network\t" in plr("cpc", "search", "neural network")

    scope_file = tmp_path / "scope.yaml"
    scope_file.write_text(
        yaml.safe_dump(
            {
                **SCOPE.model_dump(mode="json"),
                "date_from": "1990-01-01",
                "date_to": "2024-12-31",
            }
        ),
        encoding="utf-8",
    )
    landscape_id = plr("landscape", "create", str(scope_file), "--name", "cli").strip()

    # A person writes the taxonomy (no model here) and imports it.
    _, scheme = open_cpc_scheme(db_engine, RawStore(object_storage))
    _, v1_id, _ = _import_first_version(db_engine, object_storage, uuid.UUID(landscape_id), scheme)
    shown = plr("taxonomy", "show", landscape_id)
    edited = tmp_path / "edited.yaml"
    edited.write_text(shown.replace("- G01S7/48", "- G01S7/483"), encoding="utf-8")
    imported = plr("taxonomy", "import", landscape_id, str(edited), "--by", "Test Reviewer")
    v2 = imported.split(": ")[1].strip()
    assert "G01S7/483" in plr("taxonomy", "show", landscape_id, "--output", "json")
    assert "approved by Test Reviewer" in plr("taxonomy", "approve", v2, "--by", "Test Reviewer")

    query_set = plr("queries", "generate", v2).strip()
    cql = plr("queries", "show", query_set, "--provider", "epo_ops_cql")
    assert 'ta="ladar"' in cql
    assert "cpc=/low G01S7/483" in cql
    assert "approved" in plr("queries", "approve", query_set, "--by", "Test Reviewer")
    assert v1_id != v2

    source = _source(_serve(_pages()))
    batch = start_lookup_batch(db_engine, source, ["US10000000B2", "US5093563A"])
    run_lookup_batch(db_engine, RawStore(object_storage), source, batch)
    counted = plr("queries", "count", query_set, "--batch", str(batch))
    assert "ladar\tcombined\t1" in counted
    recall = json.loads(plr("queries", "recall", query_set, "--batch", str(batch)))
    assert (recall["found"], recall["known"]) == (1, 3)


def _import_first_version(
    engine: Engine, store: ObjectStorageSettings, landscape_id: uuid.UUID, scheme: CpcScheme
) -> tuple[uuid.UUID, str, CpcScheme]:
    row, _ = open_cpc_scheme(engine, RawStore(store))
    version = add_taxonomy_version(
        engine,
        landscape_id=landscape_id,
        content=_content(scheme),
        origin="llm_draft",
        scheme_id=row.id,
        cache_keys=[],
        parent_id=None,
        actor="system",
    )
    return landscape_id, str(version.id), scheme


def test_counts_refuse_other_scheme_versions_and_unknown_batches(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    _, version_id, scheme = _prepared(db_engine, object_storage)
    _approve(db_engine, "taxonomy_version", version_id)
    query_set_id = create_query_set(
        db_engine, taxonomy_version_id=version_id, scope=SCOPE, scheme=scheme
    )
    source = _source(_serve(_pages()))
    batch = start_lookup_batch(db_engine, source, ["US10000000B2"])
    run_lookup_batch(db_engine, RawStore(object_storage), source, batch)

    other = parse_title_list(title_list_zip({"cpc-section-G_20270101.txt": "G\t\tPHYSICS\n"}))
    with pytest.raises(QueryError, match=r"generated with CPC 2026\.08; CPC 2027\.01 was given"):
        count_locally(db_engine, query_set_id=query_set_id, batch_ids=[batch], scheme=other)
    with pytest.raises(QueryError, match="no such ingest batch"):
        count_locally(db_engine, query_set_id=query_set_id, batch_ids=[uuid.uuid4()], scheme=scheme)
    unfinished = start_lookup_batch(db_engine, source, ["US5093563A"])
    with pytest.raises(QueryError, match="not complete"):
        check_recall(
            db_engine,
            query_set_id=query_set_id,
            batch_ids=[batch, unfinished],
            scheme=scheme,
            known=("US10000000B2",),
        )
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(QueryCount)) == 0


def test_expansion_refuses_to_reuse_batches_after_settings_change(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    landscape_id, version_id, scheme = _prepared(db_engine, object_storage)
    _approve(db_engine, "taxonomy_version", version_id)
    query_set_id = create_query_set(
        db_engine, taxonomy_version_id=version_id, scope=SCOPE, scheme=scheme
    )

    def expand(cap: int) -> None:
        expand_citations(
            db_engine,
            RawStore(object_storage),
            _source(_serve(_pages())),
            landscape_id=landscape_id,
            query_set_id=query_set_id,
            scope=SCOPE,
            scheme=scheme,
            settings=CitationExpansionSettings(
                max_hops=1, max_fetch_per_hop=cap, directions=("forward",)
            ),
        )

    expand(500)
    with pytest.raises(DiscoveryError, match="asked for different publications"):
        expand(1)


def test_approvals_need_an_existing_subject(db_engine: Engine) -> None:
    with pytest.raises(NotFoundError, match="no query_set"):
        record_approval(
            db_engine,
            subject_type="query_set",
            subject_id=uuid.uuid4(),
            decision="approved",
            mode="human",
            decided_by="Test Reviewer",
            note=None,
        )
