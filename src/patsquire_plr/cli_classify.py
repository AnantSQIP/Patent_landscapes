"""``plr classify`` and ``plr label``: relevance filtering, segment classification, the
human review queue, gold-set labelling and evaluation (Phase 6, ADR 0011)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from patsquire_plr.classify.evaluate import evaluate
from patsquire_plr.classify.gold import export_sample, import_labels
from patsquire_plr.classify.run import classify
from patsquire_plr.cli_common import DEFAULT_CONFIG_FILE, ConfigFileOption, EnvFileOption
from patsquire_plr.cli_landscape import session_of
from patsquire_plr.db.models import (
    ClassificationRun,
    FamilyText,
    RelevanceDecision,
    SegmentDecision,
)
from patsquire_plr.errors import PlrError
from patsquire_plr.gateway.gateway import ModelGateway
from patsquire_plr.gateway.secrets import SecretResolver
from patsquire_plr.gateway.store import PostgresCallStore

classify_app = typer.Typer(no_args_is_help=True, help="Relevance and segment classification.")
label_app = typer.Typer(no_args_is_help=True, help="Gold-set labelling by people (CSV/Excel).")

RunArgument = Annotated[uuid.UUID, typer.Argument(help="Classification run ID.")]


def register(app: typer.Typer) -> None:
    app.add_typer(classify_app, name="classify")
    app.add_typer(label_app, name="label")


@classify_app.command("run")
def classify_run(
    dataset: Annotated[uuid.UUID, typer.Option(help="Dataset whose families to classify.")],
    query_set: Annotated[uuid.UUID, typer.Option(help="Query set (gives scope and taxonomy).")],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Score every family, route borderline ones to the judge, assign segments."""
    with session_of(config_file, env_file) as (settings, engine):
        gateway = ModelGateway(
            settings.models,
            secrets=SecretResolver(env_file=env_file),
            store=PostgresCallStore(engine),
        )
        result = classify(
            engine,
            gateway,
            dataset_id=dataset,
            query_set_id=query_set,
            settings=settings.classification,
            embedding_model=settings.models.roles["embedding"].model,
            judge_model=settings.models.roles["bulk_classifier"].model,
            progress=lambda line: typer.echo(line, err=True),
        )
    typer.echo(json.dumps({"run_id": str(result.run_id), **result.summary}, indent=2))


@classify_app.command("report")
def classify_report(
    run_id: RunArgument,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Print a run's summary and configuration."""
    with session_of(config_file, env_file) as (_, engine), Session(engine) as session:
        run = session.get(ClassificationRun, run_id)
        if run is None:
            raise PlrError(f"no classification run {run_id}")
        typer.echo(
            json.dumps(
                {"run_id": str(run.id), "summary": run.summary, "config": run.config}, indent=2
            )
        )


@classify_app.command("review")
def classify_review(
    run_id: RunArgument,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """The human review queue: every uncertain decision, with its reason."""
    with session_of(config_file, env_file) as (_, engine), Session(engine) as session:
        if session.get(ClassificationRun, run_id) is None:
            raise PlrError(f"no classification run {run_id}")
        titles = dict(
            session.execute(
                select(FamilyText.family_key, FamilyText.title).where(FamilyText.run_id == run_id)
            ).all()
        )
        relevance = session.execute(
            select(RelevanceDecision.family_key, RelevanceDecision.reason)
            .where(RelevanceDecision.run_id == run_id, RelevanceDecision.final == "uncertain")
            .order_by(RelevanceDecision.family_key)
        ).all()
        segments = session.execute(
            select(SegmentDecision.family_key, SegmentDecision.segment_id, SegmentDecision.reason)
            .where(SegmentDecision.run_id == run_id, SegmentDecision.final == "uncertain")
            .order_by(SegmentDecision.family_key, SegmentDecision.segment_id)
        ).all()
    for family, reason in relevance:
        typer.echo(f"relevance\t{family}\t{titles.get(family) or ''}\t{reason}")
    for family, segment, reason in segments:
        typer.echo(f"segment:{segment}\t{family}\t{titles.get(family) or ''}\t{reason}")
    typer.echo(f"{len(relevance) + len(segments)} items to review", err=True)


@classify_app.command("evaluate")
def classify_evaluate(
    run_id: RunArgument,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Measure the run against the gold set. Exits 1 if the run is not publishable."""
    with session_of(config_file, env_file) as (settings, engine):
        row = evaluate(engine, run_id, settings.classification.evaluation)
    typer.echo(
        json.dumps(
            {"publishable": row.publishable, "problems": row.problems, "results": row.results},
            indent=2,
        )
    )
    if not row.publishable:
        raise typer.Exit(code=1)


@label_app.command("export")
def label_export(
    run_id: RunArgument,
    *,
    out: Annotated[Path, typer.Option(help="CSV file to write (opens in Excel).")],
    size: Annotated[int, typer.Option(min=1, max=2000)] = 200,
    seed: Annotated[int, typer.Option(help="Fixes which families are sampled.")] = 1,
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Write a random sample of unlabelled families for a person to label."""
    with session_of(config_file, env_file) as (_, engine):
        text = export_sample(engine, run_id, size=size, seed=seed)
    out.write_text(text, encoding="utf-8")
    typer.echo(
        f"wrote {text.count(chr(10)) - 1} families to {out}. Fill 'relevant' with y or n; for "
        "relevant ones fill 'segments' with segment ids separated by ';' (or 'none').",
        err=True,
    )


@label_app.command("import")
def label_import(
    run_id: RunArgument,
    file: Annotated[Path, typer.Argument(help="The labelled CSV.")],
    by: Annotated[str, typer.Option("--by", help="Name of the person who labelled.")],
    config_file: ConfigFileOption = DEFAULT_CONFIG_FILE,
    env_file: EnvFileOption = None,
) -> None:
    """Store a person's labels (the whole file is rejected if any row is invalid)."""
    with session_of(config_file, env_file) as (_, engine):
        result = import_labels(engine, run_id, file, labeller=by)
    typer.echo(json.dumps(result.__dict__, indent=2))
