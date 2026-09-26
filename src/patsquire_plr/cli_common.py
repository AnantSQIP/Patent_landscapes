"""Options and helpers shared by the ``plr`` command modules."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from patsquire_plr.config import Settings, load_settings
from patsquire_plr.errors import ConfigError
from patsquire_plr.log import configure_logging

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


def load_cli_settings(config_file: Path, env_file: Path | None) -> Settings:
    try:
        settings = load_settings(config_file, env_file=env_file)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    configure_logging(settings.app.log_level)
    return settings
