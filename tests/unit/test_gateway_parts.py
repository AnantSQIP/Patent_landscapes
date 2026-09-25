"""Prompts, schemas and model configuration rules."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patsquire_plr.config import ModelsSettings
from patsquire_plr.gateway.errors import GatewayError
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.gateway.schema import (
    for_anthropic,
    for_bedrock,
    for_gemini,
    for_openai,
    strict_json_schema,
)
from tests.support import base_models_config

# ---------------------------------------------------------------- prompts


def _prompt(**overrides: str) -> PromptTemplate:
    values = {"id": "p.one", "version": "2", "system": "Sys {a}", "user": "User {b}"} | overrides
    return PromptTemplate(**values)


def test_render_and_placeholders() -> None:
    prompt = _prompt()
    assert prompt.placeholders() == {"a", "b"}
    assert prompt.render({"a": "1", "b": "2"}) == ("Sys 1", "User 2")


def test_render_rejects_missing_and_unexpected_variables() -> None:
    with pytest.raises(GatewayError, match=r"missing variables \['b'\]"):
        _prompt().render({"a": "1"})
    with pytest.raises(GatewayError, match=r"unexpected variables \['c'\]"):
        _prompt().render({"a": "1", "b": "2", "c": "3"})


@pytest.mark.parametrize("bad", ["Sys {a.b}", "Sys {a!r}", "Sys {a:>5}", "Sys {0}"])
def test_placeholders_must_be_plain_names(bad: str) -> None:
    with pytest.raises(ValidationError, match="plain identifier"):
        _prompt(system=bad)


def test_hash_changes_with_any_content_change() -> None:
    base = _prompt()
    assert base.sha256 == _prompt().sha256
    assert base.sha256 != _prompt(user="User {b}!").sha256
    assert base.sha256 != _prompt(version="3").sha256


# ---------------------------------------------------------------- schemas


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-H]", max_length=20)


class Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int = Field(ge=0, le=10)
    items: list[Inner] = Field(max_length=3)
    note: str | None


def test_strict_schema_requires_every_property_and_forbids_extras() -> None:
    schema = strict_json_schema(Outer)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["score", "items", "note"]
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    assert defs["Inner"]["additionalProperties"] is False


def test_models_with_defaults_or_extras_are_refused() -> None:
    class WithDefault(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int = 1

    class AllowsExtra(BaseModel):
        x: int

    with pytest.raises(GatewayError, match="must be required"):
        strict_json_schema(WithDefault)
    with pytest.raises(GatewayError, match="extra='forbid'"):
        strict_json_schema(AllowsExtra)


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return {k for k in node if isinstance(k, str)} | {
            k for v in node.values() for k in _keys(v)
        }
    if isinstance(node, list):
        return {k for item in node for k in _keys(item)}
    return set()


def test_provider_sanitisers_drop_unsupported_keywords_without_mutating_the_original() -> None:
    schema = strict_json_schema(Outer)
    original = repr(schema)

    assert {"minimum", "maximum", "pattern", "maxLength"} <= _keys(for_openai(schema))
    for sanitised in (for_anthropic(schema), for_bedrock(schema)):
        assert not {"minimum", "maximum", "pattern", "maxLength", "maxItems"} & _keys(sanitised)
        assert {"properties", "required", "additionalProperties", "$defs"} <= _keys(sanitised)
    gemini = for_gemini(schema)
    assert "pattern" not in _keys(gemini)
    assert "maxLength" not in _keys(gemini)
    assert {"minimum", "maximum", "maxItems"} <= _keys(gemini)
    assert repr(schema) == original


def test_property_named_like_a_keyword_is_kept() -> None:
    class Tricky(BaseModel):
        model_config = ConfigDict(extra="forbid")
        pattern: str
        minimum: int

    sanitised = for_anthropic(strict_json_schema(Tricky))
    properties = sanitised["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"pattern", "minimum"}


# ---------------------------------------------------------------- model configuration rules


def _models_with(**changes: object) -> dict[str, object]:
    data = base_models_config()
    for path, value in changes.items():
        node: object = data
        *parents, last = path.split("__")
        for part in parents:
            assert isinstance(node, dict)
            node = node[part]
        assert isinstance(node, dict)
        node[last] = value
    return data


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"roles__writer__backend": "nowhere"}, "unknown backend 'nowhere'"),
        (
            {"roles__embedding__max_output_tokens": 10},
            "embedding must set max_output_tokens to null",
        ),
        ({"roles__writer__max_output_tokens": None}, "writer needs max_output_tokens"),
        ({"backends__local__base_url": None}, "need base_url"),
        ({"backends__local__max_tokens_field": None}, "max_tokens_field is required"),
    ],
)
def test_invalid_model_configuration_is_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelsSettings.model_validate(_models_with(**changes))


def test_missing_role_is_rejected() -> None:
    data = base_models_config()
    roles = data["roles"]
    assert isinstance(roles, dict)
    del roles["critic"]
    with pytest.raises(ValidationError, match=r"roles not configured: \['critic'\]"):
        ModelsSettings.model_validate(data)


def _hosted(backend_type: str) -> dict[str, object]:
    return {
        "type": backend_type,
        "base_url": None,
        "api_key_env": "K" if backend_type != "bedrock" else None,
        "region": "us-east-1" if backend_type == "bedrock" else None,
        "max_tokens_field": None,
        "timeout_s": 5,
        "max_retries": 0,
        "requests_per_minute": 1,
    }


def test_anthropic_roles_must_not_set_temperature_or_seed() -> None:
    data = base_models_config()
    backends, roles = data["backends"], data["roles"]
    assert isinstance(backends, dict)
    assert isinstance(roles, dict)
    backends["anthropic_hosted"] = _hosted("anthropic")
    roles["writer"]["backend"] = "anthropic_hosted"
    with pytest.raises(ValidationError, match="no longer accepts temperature or seed"):
        ModelsSettings.model_validate(data)
    roles["writer"].update(temperature=None, seed=None)
    assert ModelsSettings.model_validate(data).roles["writer"].backend == "anthropic_hosted"


def test_bedrock_seed_and_embedding_on_anthropic_are_rejected() -> None:
    data = base_models_config()
    backends, roles = data["backends"], data["roles"]
    assert isinstance(backends, dict)
    assert isinstance(roles, dict)
    backends["aws"] = _hosted("bedrock")
    backends["anthropic_hosted"] = _hosted("anthropic")
    roles["reasoner"]["backend"] = "aws"
    roles["embedding"]["backend"] = "anthropic_hosted"
    with pytest.raises(ValidationError) as exc_info:
        ModelsSettings.model_validate(data)
    assert "Bedrock Converse has no seed" in str(exc_info.value)
    assert "embedding cannot use an anthropic backend" in str(exc_info.value)


def test_same_writer_and_critic_model_is_a_warning_not_an_error() -> None:
    data = base_models_config()
    roles = data["roles"]
    assert isinstance(roles, dict)
    assert ModelsSettings.model_validate(data).warnings() == []
    roles["critic"]["model"] = "chat-model"
    assert "independent model is recommended" in ModelsSettings.model_validate(data).warnings()[0]


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        ({"type": "anthropic", "api_key_env": None}, "anthropic backends need api_key_env"),
        ({"type": "gemini", "api_key_env": None}, "gemini backends need api_key_env"),
        ({"type": "bedrock", "region": None}, "bedrock backends need region"),
        ({"type": "bedrock", "region": "us-east-1", "api_key_env": "K"}, "AWS credential chain"),
        (
            {"type": "anthropic", "api_key_env": "K", "base_url": "http://x"},
            "base_url is only used",
        ),
    ],
)
def test_backend_type_rules(backend: dict[str, object], message: str) -> None:
    from patsquire_plr.config import BackendSettings  # noqa: PLC0415 - kept next to its tests

    values: dict[str, object] = {
        "base_url": None,
        "api_key_env": None,
        "region": None,
        "max_tokens_field": None,
        "timeout_s": 5,
        "max_retries": 0,
        "requests_per_minute": 1,
    } | backend
    with pytest.raises(ValidationError, match=message):
        BackendSettings.model_validate(values)
