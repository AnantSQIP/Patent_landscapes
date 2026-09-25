"""Command-line entry point: ``plr``."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from patsquire_plr.config import Settings, load_settings
from patsquire_plr.db import migrate
from patsquire_plr.db.engine import create_db_engine
from patsquire_plr.errors import ConfigError
from patsquire_plr.gateway.gateway import ModelGateway
from patsquire_plr.gateway.health import check_models
from patsquire_plr.gateway.secrets import SecretResolver
from patsquire_plr.gateway.store import PostgresCallStore
from patsquire_plr.health import default_checks, run_checks
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.runner import ReconciliationError, run_lookup_batch, start_lookup_batch
from patsquire_plr.ingest.sources import build_source
from patsquire_plr.log import configure_logging
from patsquire_plr.reference.extract import (
    ExtractionError,
    ReferenceExtract,
    copied_phrases,
    extract_directory,
    render_pages,
)
from patsquire_plr.template.spec import TemplateError, load_template, render_definitions_markdown

app = typer.Typer(no_args_is_help=True, add_completion=False)
config_app = typer.Typer(no_args_is_help=True, help="Inspect configuration.")
app.add_typer(config_app, name="config")
reference_app = typer.Typer(
    no_args_is_help=True, help="Study reference PLR PDFs (structure only; output is git-ignored)."
)
app.add_typer(reference_app, name="reference")
template_app = typer.Typer(no_args_is_help=True, help="Validate and document the report template.")
app.add_typer(template_app, name="template")
db_app = typer.Typer(no_args_is_help=True, help="Database schema migrations.")
app.add_typer(db_app, name="db")
models_app = typer.Typer(no_args_is_help=True, help="Model backends and roles.")
app.add_typer(models_app, name="models")
ingest_app = typer.Typer(no_args_is_help=True, help="Ingest patent records from data sources.")
app.add_typer(ingest_app, name="ingest")

TemplateOption = Annotated[Path, typer.Option("--template", help="Template specification YAML.")]
DEFAULT_TEMPLATE = Path("template/plr_template.yaml")

ConfigFileOption = Annotated[
    Path,
    typer.Option(
        "--config",
        envvar="PLR_CONFIG_FILE",
        help="YAML config file (non-secret settings).",
    ),
]
EnvFileOption = Annotated[
    Path | None,
    typer.Option(
        "--env-file",
        envvar="PLR_ENV_FILE",
        help="Dotenv file with secrets. Omit to read only the process environment.",
    ),
]

DEFAULT_CONFIG_FILE = Path("config/settings.yaml")


def _load(config_file: Path, env_file: Path | None) -> Settings:
    try:
        settings = load_settings(config_file, env_file=env_file)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    configure_logging(settings.app.log_level)
    return settings


@app.command()
def health(
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Check every infrastructure dependency. Exits 1 if any check fails."""
    settings = _load(config_file, env_file)
    report = run_checks(default_checks(settings))
    typer.echo(
        json.dumps(
            {
                "healthy": report.healthy,
                "checks": [r.model_dump(mode="json") for r in report.results],
            },
            indent=2,
        )
    )
    if not report.healthy:
        raise typer.Exit(code=1)


@config_app.command("show")
def config_show(
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Print the resolved configuration with secrets masked."""
    settings = _load(config_file, env_file)
    typer.echo(settings.model_dump_json(indent=2))


@reference_app.command("extract")
def reference_extract(
    pdf_dir: Annotated[Path, typer.Option(help="Directory containing reference PDFs.")] = Path(
        "reference"
    ),
    out_dir: Annotated[Path, typer.Option(help="Where to write one JSON file per PDF.")] = Path(
        "reference/extracted"
    ),
) -> None:
    """Extract headings and figure/table/box captions from every PDF in PDF_DIR."""
    try:
        written = extract_directory(pdf_dir, out_dir)
    except ExtractionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    for path in written:
        typer.echo(str(path))


@reference_app.command("render")
def reference_render(
    pdf: Annotated[Path, typer.Argument(help="Reference PDF.")],
    pages: Annotated[list[int], typer.Argument(help="1-based page numbers to render.")],
    out_dir: Annotated[Path, typer.Option(help="Where to write PNG files.")] = Path(
        "reference/rendered"
    ),
) -> None:
    """Render pages to PNG to inspect charts that text extraction cannot capture."""
    try:
        written = render_pages(pdf, pages, out_dir)
    except ExtractionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    for path in written:
        typer.echo(str(path))


@template_app.command("validate")
def template_validate(template: TemplateOption = DEFAULT_TEMPLATE) -> None:
    """Validate the template spec and its cross-references. Exits 1 on any problem."""
    try:
        spec = load_template(template)
    except TemplateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    sections = sum(1 for _ in spec.all_sections())
    typer.echo(
        f"{template}: valid ({sections} sections, {len(spec.metrics)} metrics, "
        f"{len(spec.chart_types)} chart types, {len(spec.definitions)} definitions)"
    )


@template_app.command("definitions")
def template_definitions(template: TemplateOption = DEFAULT_TEMPLATE) -> None:
    """Print docs/definitions.md generated from the template spec."""
    try:
        spec = load_template(template)
    except TemplateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(render_definitions_markdown(spec), nl=False)


@reference_app.command("check-originality")
def reference_check_originality(
    files: Annotated[list[Path], typer.Argument(help="Our documents to check.")],
    extracted_dir: Annotated[Path, typer.Option(help="Directory of extract JSON files.")] = Path(
        "reference/extracted"
    ),
) -> None:
    """Fail if FILES share any 8-word phrase with reference headings or captions."""
    extract_files = sorted(extracted_dir.glob("*.json"))
    if not extract_files:
        typer.echo(f"No extracts in {extracted_dir}; run `plr reference extract` first.", err=True)
        raise typer.Exit(code=2)
    extracts = [
        ReferenceExtract.model_validate_json(p.read_text(encoding="utf-8")) for p in extract_files
    ]
    copied = [
        (path, source, phrase)
        for path in files
        for source, phrase in copied_phrases(path.read_text(encoding="utf-8"), extracts)
    ]
    for path, source, phrase in copied:
        typer.echo(f"{path}: shares '{phrase}' with {source}", err=True)
    if copied:
        raise typer.Exit(code=1)
    typer.echo(f"No copied phrases in {len(files)} file(s) against {len(extracts)} reports.")


@db_app.command("upgrade")
def db_upgrade(
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
    revision: Annotated[str, typer.Option(help="Target revision.")] = "head",
) -> None:
    """Apply database migrations up to REVISION (default: the latest)."""
    engine = create_db_engine(_load(config_file, env_file).database)
    try:
        migrate.upgrade(engine, revision)
        typer.echo(f"database at revision {migrate.current_revision(engine)}")
    finally:
        engine.dispose()


@db_app.command("current")
def db_current(
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Print the database's current migration revision ('none' if unmigrated)."""
    engine = create_db_engine(_load(config_file, env_file).database)
    try:
        typer.echo(migrate.current_revision(engine) or "none")
    finally:
        engine.dispose()


@models_app.command("health")
def models_health(
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Send a real request to every configured role. Exits 1 if any role fails."""
    settings = _load(config_file, env_file)
    engine = create_db_engine(settings.database)
    try:
        gateway = ModelGateway(
            settings.models,
            secrets=SecretResolver(env_file=env_file),
            store=PostgresCallStore(engine),
        )
        results = check_models(gateway, settings.models)
    finally:
        engine.dispose()
    healthy = all(r.ok for r in results)
    typer.echo(
        json.dumps(
            {
                "healthy": healthy,
                "roles": [r.model_dump() for r in results],
                "warnings": settings.models.warnings(),
            },
            indent=2,
        )
    )
    if not healthy:
        raise typer.Exit(code=1)


def _run_batch(
    settings: Settings, source_id: str, batch_id: uuid.UUID | None, keys: list[str]
) -> None:
    if source_id not in settings.data_sources:
        typer.echo(
            f"unknown data source '{source_id}' (configured: {sorted(settings.data_sources)})",
            err=True,
        )
        raise typer.Exit(code=2)
    source = build_source(source_id, settings.data_sources[source_id])
    engine = create_db_engine(settings.database)
    try:
        batch = batch_id or start_lookup_batch(engine, source, keys)
        report = run_lookup_batch(engine, RawStore(settings.object_storage), source, batch)
    except ReconciliationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.echo(report.model_dump_json(indent=2))
    if report.status != "complete":
        raise typer.Exit(code=1)


@ingest_app.command("lookup")
def ingest_lookup(
    numbers: Annotated[list[str] | None, typer.Argument(help="Publication numbers.")] = None,
    source: Annotated[str, typer.Option(help="Configured data source ID.")] = "google_patents",
    numbers_file: Annotated[
        Path | None, typer.Option(help="File with one number per line.")
    ] = None,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Fetch publications by number, store raw payloads, normalise, reconcile."""
    keys = list(numbers or [])
    if numbers_file is not None:
        keys += numbers_file.read_text(encoding="utf-8").splitlines()
    if not keys:
        typer.echo("give publication numbers as arguments or --numbers-file", err=True)
        raise typer.Exit(code=2)
    _run_batch(_load(config_file, env_file), source, None, keys)


@ingest_app.command("resume")
def ingest_resume(
    batch_id: Annotated[uuid.UUID, typer.Argument(help="Batch to resume.")],
    source: Annotated[str, typer.Option(help="Configured data source ID.")] = "google_patents",
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Retry only the items of a batch whose latest outcome is 'failed', then reconcile."""
    _run_batch(_load(config_file, env_file), source, batch_id, [])
