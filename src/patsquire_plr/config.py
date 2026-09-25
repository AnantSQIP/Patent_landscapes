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
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self, Union, get_args, get_origin

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
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


BackendType = Literal["openai_compatible", "anthropic", "gemini", "bedrock"]
ModelRole = Literal["embedding", "bulk_classifier", "reasoner", "writer", "critic"]
MODEL_ROLES: tuple[ModelRole, ...] = (
    "embedding",
    "bulk_classifier",
    "reasoner",
    "writer",
    "critic",
)
_ENV_NAME = r"^[A-Z][A-Z0-9_]*$"


class BackendSettings(_Section):
    """Where models run. Credentials are referenced by environment-variable *name* only."""

    type: BackendType
    base_url: str | None = Field(
        pattern=r"^https?://", description="required for openai_compatible (vLLM, SGLang, Ollama)"
    )
    api_key_env: str | None = Field(pattern=_ENV_NAME, description="env var holding the API key")
    region: str | None = Field(description="AWS region (bedrock)")
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] | None = Field(
        description="openai_compatible only: the token-limit parameter this server accepts"
    )
    timeout_s: float = Field(gt=0, le=3600)
    max_retries: int = Field(ge=0, le=10)
    requests_per_minute: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def _type_specific(self) -> Self:
        problems = []
        if self.type == "openai_compatible" and self.base_url is None:
            problems.append("openai_compatible backends need base_url")
        if (self.type == "openai_compatible") != (self.max_tokens_field is not None):
            problems.append("max_tokens_field is required for, and only for, openai_compatible")
        if self.type == "bedrock" and self.api_key_env is not None:
            problems.append("bedrock uses the AWS credential chain; api_key_env must be null")
        if self.type in ("anthropic", "gemini") and self.api_key_env is None:
            problems.append(f"{self.type} backends need api_key_env")
        if self.type == "bedrock" and self.region is None:
            problems.append("bedrock backends need region")
        if self.type != "openai_compatible" and self.base_url is not None:
            problems.append(f"base_url is only used by openai_compatible, not {self.type}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RoleSettings(_Section):
    backend: str = Field(min_length=1)
    model: str = Field(min_length=1)
    max_output_tokens: int | None = Field(ge=1, le=200_000, description="None for embedding")
    temperature: float | None = Field(ge=0, le=2, description="None = provider does not accept it")
    seed: int | None = Field(description="None = provider does not accept it")
    max_schema_retries: int = Field(ge=0, le=5)
    input_price_per_mtok_usd: Decimal | None = Field(ge=0)
    output_price_per_mtok_usd: Decimal | None = Field(ge=0)

    @model_validator(mode="after")
    def _prices_come_in_pairs(self) -> Self:
        if (self.input_price_per_mtok_usd is None) != (self.output_price_per_mtok_usd is None):
            raise ValueError(
                "set both input_price_per_mtok_usd and output_price_per_mtok_usd, or neither "
                "(a single price cannot produce a cost estimate)"
            )
        return self


def _reproducibility_problems(
    role: str, backend_type: str | None, config: RoleSettings
) -> list[str]:
    """Build prompt principle 6: temperature 0 and a fixed seed wherever the provider allows."""
    if role == "embedding" or backend_type is None or backend_type == "anthropic":
        return []  # Anthropic accepts neither (checked separately); embeddings take neither
    problems = []
    if config.temperature != 0:
        problems.append(f"role {role}: temperature must be 0 for reproducible output")
    if backend_type in ("openai_compatible", "gemini") and config.seed is None:
        problems.append(f"role {role}: set a fixed seed; {backend_type} supports one")
    return problems


class ModelsSettings(_Section):
    backends: dict[str, BackendSettings] = Field(min_length=1)
    roles: dict[ModelRole, RoleSettings]

    @model_validator(mode="after")
    def _roles_complete_and_resolvable(self) -> Self:
        problems = []
        if missing := [r for r in MODEL_ROLES if r not in self.roles]:
            problems.append(f"roles not configured: {missing}")
        for role, config in self.roles.items():
            if config.backend not in self.backends:
                problems.append(f"role {role} uses unknown backend '{config.backend}'")
            elif role == "embedding" and self.backends[config.backend].type == "anthropic":
                problems.append(
                    "role embedding cannot use an anthropic backend (no embeddings API)"
                )
            backend_type = (
                self.backends[config.backend].type if config.backend in self.backends else None
            )
            if backend_type == "anthropic" and (config.temperature, config.seed) != (None, None):
                problems.append(
                    f"role {role}: the Anthropic API no longer accepts temperature or seed; "
                    "set both to null (reproducibility comes from the response cache)"
                )
            if backend_type == "bedrock" and config.seed is not None:
                problems.append(f"role {role}: Bedrock Converse has no seed; set seed to null")
            problems += _reproducibility_problems(role, backend_type, config)
            if role == "embedding" and config.max_output_tokens is not None:
                problems.append("role embedding must set max_output_tokens to null")
            if role != "embedding" and config.max_output_tokens is None:
                problems.append(f"role {role} needs max_output_tokens")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def warnings(self) -> list[str]:
        """Non-blocking advice, surfaced by `plr models health` (build prompt §4)."""
        writer, critic = self.roles.get("writer"), self.roles.get("critic")
        if writer and critic and (writer.backend, writer.model) == (critic.backend, critic.model):
            return [
                "critic uses the same backend and model as writer; "
                "an independent model is recommended"
            ]
        return []


class GooglePatentsPageSettings(_Section):
    """Lookup of individual patents by number on Google Patents pages.

    Only ``/patent/<number>/`` pages are requested, which the site's robots.txt allows.
    Search pages are disallowed there and never used.
    """

    type: Literal["google_patents_page"]
    base_url: str = Field(pattern=r"^https://")
    user_agent: str = Field(min_length=1)
    requests_per_minute: int = Field(ge=1, le=60, description="politeness ceiling")
    timeout_s: float = Field(gt=0, le=300)
    max_retries: int = Field(ge=0, le=5)


# Grows into a discriminated union as more source types are added (ADR 0005).
DataSourceSettings = GooglePatentsPageSettings
_SOURCE_ID = r"^[a-z][a-z0-9_]*$"


class Settings(_Section):
    app: AppSettings
    database: DatabaseSettings
    redis: RedisSettings
    object_storage: ObjectStorageSettings
    models: ModelsSettings
    data_sources: dict[Annotated[str, Field(pattern=_SOURCE_ID)], DataSourceSettings]


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
