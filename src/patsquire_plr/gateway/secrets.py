"""Resolve API keys named by ``api_key_env`` from the process environment or a dotenv file.

Config files only ever contain the *name* of the variable. A referenced variable that is
missing or empty is an error, never an anonymous request.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from patsquire_plr.gateway.errors import MissingSecretError


class SecretResolver:
    def __init__(self, *, env_file: Path | None, environ: Mapping[str, str] | None = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)
        self._dotenv: dict[str, str | None] = (
            {} if env_file is None else dict(dotenv_values(env_file))
        )

    def resolve(self, name: str) -> SecretStr:
        value = self._environ.get(name)
        if value is None:
            value = self._dotenv.get(name)
        if not value:
            raise MissingSecretError(
                f"environment variable {name} is not set (or empty); set it in .env or the "
                "environment"
            )
        return SecretStr(value)
