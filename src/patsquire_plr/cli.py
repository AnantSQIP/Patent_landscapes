"""Command-line entry point: ``plr``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from patsquire_plr.config import Settings, load_settings
from patsquire_plr.errors import ConfigError
from patsquire_plr.health import default_checks, run_checks
from patsquire_plr.log import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False)
config_app = typer.Typer(no_args_is_help=True, help="Inspect configuration.")
app.add_typer(config_app, name="config")

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
