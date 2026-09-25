"""Application configuration.

Sources, highest priority first:

1. Process environment variables named ``PLR__<SECTION>__<KEY>`` (e.g. ``PLR__DATABASE__PORT``).
2. A dotenv file, if one is given (never committed). Only its ``PLR__*`` entries are read;
   other entries (e.g. ``POSTGRES_USER`` for docker-compose) are intentionally ignored.
3. The YAML config file (committed, non-secret values only).

Rules enforced here:

* Every setting is required. There are no code defaults that could hide a missing value;
  the committed YAML file states every non-secret value explicitly.
* Unknown sections and keys are rejected from every source, so typos fail loudly.
* Secret fields may not carry a value in the YAML file, and may not be empty strings.
  An explicit ``null`` in YAML is allowed where the field is optional (e.g. Redis without
  a password).
* Validation errors never echo input values, so a malformed secret cannot leak into logs.

The loader is deliberately small and explicit (no pydantic-settings) so each of these rules
is visible in this file and covered by tests.
"""

from __future__ import annotations

import os
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Union, get_args, get_origin

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy.engine import URL

from patsquire_plr.errors import ConfigError

ENV_PREFIX = "PLR__"
ENV_NESTED_DELIMITER = "__"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
Environment = Literal["dev", "test", "prod"]
Secret = Annotated[SecretStr, Field(min_length=1)]


class _Section(BaseModel):
    # Not strict: environment values are strings ("5432") and must parse to their types.
    # Every other guarantee (no extras, immutability, no defaults) still holds.
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSettings(_Section):
    environment: Environment
    log_level: LogLevel


class DatabaseSettings(_Section):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: Secret
    connect_timeout_s: int = Field(ge=1, le=300)

    def sqlalchemy_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.name,
        )


class RedisSettings(_Section):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    db: int = Field(ge=0)
    password: Secret | None
    socket_timeout_s: float = Field(gt=0, le=300)


class ObjectStorageSettings(_Section):
    endpoint_url: str = Field(pattern=r"^https?://")
    region: str = Field(min_length=1)
    bucket: str = Field(min_length=3, max_length=63)
    access_key_id: Secret
    secret_access_key: Secret
    connect_timeout_s: float = Field(gt=0, le=300)
    read_timeout_s: float = Field(gt=0, le=3600)


class Settings(_Section):
    app: AppSettings
    database: DatabaseSettings
    redis: RedisSettings
    object_storage: ObjectStorageSettings


def secret_field_paths(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Return the paths of every ``SecretStr`` field in ``model``, recursively."""
    paths: list[tuple[str, ...]] = []
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        for candidate in _flatten_annotation(field.annotation):
            if candidate is SecretStr:
                paths.append(path)
            elif isinstance(candidate, type) and issubclass(candidate, BaseModel):
                paths.extend(secret_field_paths(candidate, path))
    return paths


def _flatten_annotation(annotation: object) -> list[object]:
    """Unwrap ``X | None`` and ``Annotated[X, ...]`` into the plain member types."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return [flat for member in get_args(annotation) for flat in _flatten_annotation(member)]
    if origin is Annotated:
        return _flatten_annotation(get_args(annotation)[0])
    return [annotation]


def env_var_name(path: tuple[str, ...]) -> str:
    return ENV_PREFIX + ENV_NESTED_DELIMITER.join(part.upper() for part in path)


def _read_yaml_mapping(config_file: Path) -> dict[str, object]:
    if not config_file.is_file():
        raise ConfigError(f"Config file not found: {config_file}")
    try:
        loaded: object = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file is not valid YAML: {config_file}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config file must contain a mapping at the top level: {config_file}")
    return {str(key): value for key, value in loaded.items()}


def _reject_secrets_in_yaml(data: Mapping[str, object], config_file: Path) -> None:
    offenders: list[tuple[str, ...]] = []
    for path in secret_field_paths(Settings):
        node: object = data
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None:
            offenders.append(path)
    if offenders:
        dotted = ", ".join(".".join(p) for p in offenders)
        env_names = ", ".join(env_var_name(p) for p in offenders)
        raise ConfigError(
            f"Secrets must not be stored in {config_file}: {dotted}. "
            f"Remove them from the file and set {env_names} in the environment or .env instead."
        )


def _prefixed_to_nested(
    variables: Mapping[str, str | None], origin: str
) -> dict[str, dict[str, object]]:
    """Turn ``PLR__SECTION__KEY=value`` entries into ``{"section": {"key": value}}``.

    Entries without the prefix are ignored. Malformed names, unknown sections and valueless
    entries raise ``ConfigError``. Unknown keys inside a known section are left for model
    validation, which rejects them.
    """
    nested: dict[str, dict[str, object]] = {}
    for name, value in variables.items():
        if not name.startswith(ENV_PREFIX):
            continue
        parts = name[len(ENV_PREFIX) :].split(ENV_NESTED_DELIMITER)
        if len(parts) != 2 or not all(parts) or name != name.upper():  # noqa: PLR2004 - section + key
            raise ConfigError(
                f"{origin}: '{name}' must have the form {ENV_PREFIX}<SECTION>{ENV_NESTED_DELIMITER}"
                "<KEY> in upper case"
            )
        section, key = parts[0].lower(), parts[1].lower()
        if section not in Settings.model_fields:
            known = ", ".join(sorted(Settings.model_fields))
            raise ConfigError(
                f"{origin}: '{name}' names unknown section '{section}' (known: {known})"
            )
        if value is None:
            raise ConfigError(f"{origin}: '{name}' has no value (expected {name}=<value>)")
        nested.setdefault(section, {})[key] = value
    return nested


def _deep_merge(base: Mapping[str, object], override: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"  - {location}: {error['msg']}")
    return "Invalid configuration:\n" + "\n".join(lines)


def load_settings(
    config_file: Path,
    *,
    env_file: Path | None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load and validate settings. Raises ``ConfigError`` with a readable, secret-free message.

    ``env_file``: dotenv file to read, or ``None`` for none. A missing file is an error.
    ``environ``: the environment to read; ``None`` means ``os.environ``.
    """
    data = _read_yaml_mapping(config_file)
    _reject_secrets_in_yaml(data, config_file)

    if env_file is not None:
        if not env_file.is_file():
            raise ConfigError(f"Env file not found: {env_file}")
        dotenv = _prefixed_to_nested(dotenv_values(env_file), f"env file {env_file}")
        data = _deep_merge(data, dotenv)

    env = _prefixed_to_nested(os.environ if environ is None else environ, "environment")
    data = _deep_merge(data, env)

    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        # ``from None``: the original error's string form includes input values (secrets).
        raise ConfigError(_format_validation_error(exc)) from None
