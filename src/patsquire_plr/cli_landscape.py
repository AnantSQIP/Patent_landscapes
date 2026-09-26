"""``plr cpc``, ``plr landscape``, ``plr taxonomy``, ``plr queries`` and ``plr discover``:
scope, taxonomy and key strings (Phase 5, ADR 0010)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import typer
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.classification.cpc import CpcScheme, CpcSchemeError
from patsquire_plr.classification.store import load_cpc_title_list, open_cpc_scheme
from patsquire_plr.cli_common import (
    DEFAULT_CONFIG_FILE,
    ConfigFileOption,
    EnvFileOption,
    load_cli_settings,
)
from patsquire_plr.config import Settings
from patsquire_plr.db.engine import create_db_engine
from patsquire_plr.db.models import QuerySet, SearchQuery, TaxonomyVersion
from patsquire_plr.errors import PlrError
from patsquire_plr.gateway.gateway import ModelGateway
from patsquire_plr.gateway.secrets import SecretResolver
from patsquire_plr.gateway.store import PostgresCallStore
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.sources import build_source
from patsquire_plr.landscape.discovery import expand_citations
from patsquire_plr.landscape.scope import load_scope
from patsquire_plr.landscape.search import (
    check_recall,
    count_locally,
    create_query_set,
    query_set_scheme,
)
from patsquire_plr.landscape.store import (
    add_taxonomy_version,
    create_landscape,
    get_scope,
    get_taxonomy,
    latest_decision,
    record_approval,
    require_approved,
)
from patsquire_plr.landscape.taxonomy import content_from_edit, draft_taxonomy, export_yaml

CPC_TITLE_LIST_URL = (
    "https://www.cooperativepatentclassification.org/sites/default/files/cpc/bulk/"
    "CPCTitleList{yyyymm}.zip"
)

cpc_app = typer.Typer(no_args_is_help=True, help="The official CPC scheme (title list).")
landscape_app = typer.Typer(no_args_is_help=True, help="Create a landscape from a scope file.")
taxonomy_app = typer.Typer(no_args_is_help=True, help="Draft, edit and approve taxonomies.")
queries_app = typer.Typer(no_args_is_help=True, help="Generate, count and check search queries.")
discover_app = typer.Typer(no_args_is_help=True, help="Find candidate patents without a key.")

ByOption = Annotated[str, typer.Option("--by", help="Name of the person deciding.")]
BatchOption = Annotated[
    list[uuid.UUID], typer.Option("--batch", help="Ingest batch whose records to use.")
]


def register(app: typer.Typer) -> None:
    app.add_typer(cpc_app, name="cpc")
    app.add_typer(landscape_app, name="landscape")
    app.add_typer(taxonomy_app, name="taxonomy")
    app.add_typer(queries_app, name="queries")
    app.add_typer(discover_app, name="discover")


@contextmanager
def _session_of(config_file: Path, env_file: Path | None) -> Iterator[tuple[Settings, Engine]]:
    """Settings and an engine; any PlrError becomes a message and exit code 1."""
    settings = load_cli_settings(config_file, env_file)
    engine = create_db_engine(settings.database)
    try:
        yield settings, engine
    except PlrError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()


def _scheme(settings: Settings, engine: Engine, scheme_id: uuid.UUID | None = None) -> CpcScheme:
    _, scheme = open_cpc_scheme(engine, RawStore(settings.object_storage), scheme_id=scheme_id)
    return scheme


# ------------------------------------------------------------------ cpc


@cpc_app.command("load")
def cpc_load(
    zip_file: Annotated[Path, typer.Argument(help="CPCTitleList<YYYYMM>.zip from the CPC site.")],
    source_url: Annotated[
        str,
        typer.Option(
            help="Where the file was downloaded from (recorded as its provenance), e.g. "
            + CPC_TITLE_LIST_URL.format(yyyymm="202608")
        ),
    ],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Store an official CPC title list and record its version and hash."""
    content = zip_file.read_bytes()
    with _session_of(config_file, env_file) as (settings, engine):
        try:
            row, added = load_cpc_title_list(
                engine, RawStore(settings.object_storage), content, source_url=source_url
            )
        except CpcSchemeError as exc:
            raise PlrError(f"{zip_file}: {exc}") from exc
    state = "loaded" if added else "already loaded"
    typer.echo(
        f"CPC {row.version} {state}: {row.entry_count} entries, sha256 {row.sha256}, "
        f"from {row.source_url}"
    )


@cpc_app.command("check")
def cpc_check(
    codes: Annotated[list[str], typer.Argument(help="CPC symbols to check.")],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Check codes against the newest loaded CPC version. Exits 1 if any is invalid."""
    with _session_of(config_file, env_file) as (settings, engine):
        scheme = _scheme(settings, engine)
    checks = [scheme.check(c) for c in codes]
    for c in checks:
        typer.echo(f"{c.code_raw}\t{c.symbol or 'INVALID'}\t{c.title_path or c.problem}")
    if any(c.symbol is None for c in checks):
        raise typer.Exit(code=1)


@cpc_app.command("search")
def cpc_search(
    terms: Annotated[list[str], typer.Argument(help="Words or phrases to find in CPC titles.")],
    within: Annotated[
        list[str] | None, typer.Option(help="Only symbols starting with this (repeatable).")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 25,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Search CPC titles (whole words, case-insensitive)."""
    with _session_of(config_file, env_file) as (settings, engine):
        scheme = _scheme(settings, engine)
    for match in scheme.search(terms, within=within or (), limit=limit):
        typer.echo(f"{match.symbol}\t{', '.join(match.matched_terms)}\t{match.title_path}")


# ------------------------------------------------------------------ landscape


@landscape_app.command("create")
def landscape_create(
    scope_file: Annotated[Path, typer.Argument(help="Scope YAML (topic, dates, offices, seeds).")],
    name: Annotated[str, typer.Option(help="A name for the landscape.")],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Record a new landscape with its scope. Prints the landscape ID."""
    with _session_of(config_file, env_file) as (_, engine):
        scope = load_scope(scope_file)
        landscape_id = create_landscape(engine, name=name, scope=scope)
    typer.echo(str(landscape_id))


# ------------------------------------------------------------------ taxonomy


@taxonomy_app.command("draft")
def taxonomy_draft(
    landscape_id: Annotated[uuid.UUID, typer.Argument()],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Draft a taxonomy with the reasoner model and official CPC entries."""
    with _session_of(config_file, env_file) as (settings, engine):
        _, scope = get_scope(engine, landscape_id)
        scheme_row, scheme = open_cpc_scheme(engine, RawStore(settings.object_storage))
        gateway = ModelGateway(
            settings.models,
            secrets=SecretResolver(env_file=env_file),
            store=PostgresCallStore(engine),
        )
        content, calls = draft_taxonomy(
            gateway,
            scheme,
            scope,
            max_segments=settings.landscape.max_segments,
            candidates_per_segment=settings.landscape.cpc_candidates_per_segment,
            max_cpc_per_segment=settings.landscape.max_cpc_per_segment,
        )
        row = add_taxonomy_version(
            engine,
            landscape_id=landscape_id,
            content=content,
            origin="llm_draft",
            scheme_id=scheme_row.id,
            cache_keys=calls,
            parent_id=None,
            actor="system",
        )
        _auto_approve(settings, engine, "taxonomy_version", row.id)
    typer.echo(f"taxonomy version {row.version}: {row.id}", err=True)
    typer.echo(export_yaml(content))


def _auto_approve(
    settings: Settings,
    engine: Engine,
    subject_type: Literal["taxonomy_version", "query_set"],
    subject_id: uuid.UUID,
) -> None:
    if settings.landscape.approval_mode == "automatic":
        record_approval(
            engine,
            subject_type=subject_type,
            subject_id=subject_id,
            decision="approved",
            mode="automatic",
            decided_by="system (approval_mode: automatic)",
            note=None,
        )


@taxonomy_app.command("show")
def taxonomy_show(
    landscape_id: Annotated[uuid.UUID, typer.Argument()],
    version_id: Annotated[uuid.UUID | None, typer.Option(help="A specific version.")] = None,
    output: Annotated[str, typer.Option(help="yaml (for editing) or json.")] = "yaml",
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Print the newest (or a given) taxonomy version and its approval state."""
    with _session_of(config_file, env_file) as (_, engine):
        row, content = get_taxonomy(engine, version_id=version_id, landscape_id=landscape_id)
        decision = latest_decision(engine, "taxonomy_version", row.id)
    state = "not decided" if decision is None else f"{decision.decision} by {decision.decided_by}"
    typer.echo(f"taxonomy version {row.version} ({row.origin}): {row.id}, {state}", err=True)
    typer.echo(export_yaml(content) if output == "yaml" else content.model_dump_json(indent=2))


@taxonomy_app.command("import")
def taxonomy_import(
    landscape_id: Annotated[uuid.UUID, typer.Argument()],
    edited_file: Annotated[Path, typer.Argument(help="The edited taxonomy YAML.")],
    by: ByOption,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Store an edited taxonomy as a new version (CPC codes are checked)."""
    with _session_of(config_file, env_file) as (settings, engine):
        previous_row, previous = get_taxonomy(engine, landscape_id=landscape_id)
        scheme_row, scheme = open_cpc_scheme(engine, RawStore(settings.object_storage))
        content = content_from_edit(
            edited_file.read_text(encoding="utf-8"), scheme, previous=previous
        )
        row = add_taxonomy_version(
            engine,
            landscape_id=landscape_id,
            content=content,
            origin="user_edit",
            scheme_id=scheme_row.id,
            cache_keys=[],
            parent_id=previous_row.id,
            actor=by,
        )
    typer.echo(f"taxonomy version {row.version}: {row.id}")


@taxonomy_app.command("approve")
def taxonomy_approve(
    version_id: Annotated[uuid.UUID, typer.Argument()],
    *,
    by: ByOption,
    note: Annotated[str | None, typer.Option()] = None,
    reject: Annotated[bool, typer.Option("--reject", help="Record a rejection.")] = False,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Record a person's approval (or rejection) of a taxonomy version."""
    with _session_of(config_file, env_file) as (_, engine):
        get_taxonomy(engine, version_id=version_id)  # must exist
        record_approval(
            engine,
            subject_type="taxonomy_version",
            subject_id=version_id,
            decision="rejected" if reject else "approved",
            mode="human",
            decided_by=by,
            note=note,
        )
    typer.echo(f"taxonomy version {version_id} {'rejected' if reject else 'approved'} by {by}")


# ------------------------------------------------------------------ queries


@queries_app.command("generate")
def queries_generate(
    version_id: Annotated[uuid.UUID, typer.Argument(help="An approved taxonomy version.")],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Generate the query set for an approved taxonomy version. Prints its ID."""
    with _session_of(config_file, env_file) as (settings, engine):
        row, _ = get_taxonomy(engine, version_id=version_id)
        _, scope = get_scope(engine, row.landscape_id)
        scheme = _scheme(settings, engine, row.classification_scheme_id)
        query_set_id = create_query_set(
            engine, taxonomy_version_id=version_id, scope=scope, scheme=scheme
        )
        _auto_approve(settings, engine, "query_set", query_set_id)
    typer.echo(str(query_set_id))


@queries_app.command("approve")
def queries_approve(
    query_set_id: Annotated[uuid.UUID, typer.Argument()],
    *,
    by: ByOption,
    note: Annotated[str | None, typer.Option()] = None,
    reject: Annotated[bool, typer.Option("--reject", help="Record a rejection.")] = False,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Record a person's approval (or rejection) of a query set."""
    with _session_of(config_file, env_file) as (_, engine):
        _query_set_landscape(engine, query_set_id)  # must exist
        record_approval(
            engine,
            subject_type="query_set",
            subject_id=query_set_id,
            decision="rejected" if reject else "approved",
            mode="human",
            decided_by=by,
            note=note,
        )
    typer.echo(f"query set {query_set_id} {'rejected' if reject else 'approved'} by {by}")


@queries_app.command("show")
def queries_show(
    query_set_id: Annotated[uuid.UUID, typer.Argument()],
    provider: Annotated[
        str | None, typer.Option(help="epo_ops_cql, lens_json, bigquery_sql or local.")
    ] = None,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Print the queries of a set."""
    with _session_of(config_file, env_file) as (_, engine), Session(engine) as session:
        query = select(SearchQuery).where(SearchQuery.query_set_id == query_set_id)
        if provider:
            query = query.where(SearchQuery.provider == provider)
        rows = session.scalars(
            query.order_by(SearchQuery.segment_id, SearchQuery.part, SearchQuery.provider)
        ).all()
    if not rows:
        typer.echo(f"no queries for {query_set_id} (provider: {provider or 'any'})", err=True)
        raise typer.Exit(code=1)
    for r in rows:
        typer.echo(f"## {r.segment_id} / {r.part} / {r.provider}\n{r.query_text}\n")


@queries_app.command("count")
def queries_count(
    query_set_id: Annotated[uuid.UUID, typer.Argument()],
    batch: BatchOption,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Count each query over the records stored by the given batches (recorded)."""
    with _session_of(config_file, env_file) as (settings, engine):
        _, scheme = query_set_scheme(engine, RawStore(settings.object_storage), query_set_id)
        counts = count_locally(engine, query_set_id=query_set_id, batch_ids=batch, scheme=scheme)
    for c in counts:
        extra = f"  (lower bound; not evaluable: {c.undecidable})" if c.undecidable else ""
        typer.echo(f"{c.segment_id}\t{c.part}\t{c.count}{extra}")


@queries_app.command("recall")
def queries_recall(
    query_set_id: Annotated[uuid.UUID, typer.Argument()],
    batch: BatchOption,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Check which user-confirmed patents (scope ``known_relevant``) the search finds."""
    with _session_of(config_file, env_file) as (settings, engine):
        query_set_row = _query_set_landscape(engine, query_set_id)
        _, scope = get_scope(engine, query_set_row)
        if not scope.known_relevant:
            raise PlrError(
                "the scope lists no known_relevant patents confirmed by a person; "
                "recall cannot be measured"
            )
        _, scheme = query_set_scheme(engine, RawStore(settings.object_storage), query_set_id)
        result = check_recall(
            engine,
            query_set_id=query_set_id,
            batch_ids=batch,
            scheme=scheme,
            known=scope.known_relevant,
        )
    found, known = result.recall
    typer.echo(json.dumps({"found": found, "known": known, "missed": result.missed}, indent=2))


def _query_set_landscape(engine: Engine, query_set_id: uuid.UUID) -> uuid.UUID:
    with Session(engine) as session:
        landscape_id = session.scalar(
            select(TaxonomyVersion.landscape_id)
            .join(QuerySet, QuerySet.taxonomy_version_id == TaxonomyVersion.id)
            .where(QuerySet.id == query_set_id)
        )
    if landscape_id is None:
        raise PlrError(f"no query set {query_set_id}")
    return landscape_id


# ------------------------------------------------------------------ discovery


@discover_app.command("citations")
def discover_citations(
    query_set_id: Annotated[uuid.UUID, typer.Argument()],
    source: Annotated[str, typer.Option(help="Configured data source.")] = "google_patents",
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Expand from the scope's seeds along citations; re-running resumes the same batches."""
    with _session_of(config_file, env_file) as (settings, engine):
        if source not in settings.data_sources:
            raise PlrError(f"unknown data source {source!r}")
        landscape_id = _query_set_landscape(engine, query_set_id)
        require_approved(engine, "query_set", query_set_id)
        _, scope = get_scope(engine, landscape_id)
        run_id, hops = expand_citations(
            engine,
            RawStore(settings.object_storage),
            build_source(source, settings.data_sources[source]),
            landscape_id=landscape_id,
            query_set_id=query_set_id,
            scope=scope,
            scheme=query_set_scheme(engine, RawStore(settings.object_storage), query_set_id)[1],
            settings=settings.landscape.citation_expansion,
            progress=lambda line: typer.echo(line, err=True),
        )
    typer.echo(
        json.dumps({"discovery_run": str(run_id), "hops": [h.as_json() for h in hops]}, indent=2)
    )
