"""Gateway behaviour with TEST-ONLY doubles: a scripted adapter and an in-memory call store."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from patsquire_plr.config import BackendSettings, ModelsSettings
from patsquire_plr.gateway.errors import (
    GatewayError,
    MissingSecretError,
    PermanentProviderError,
    RetryableProviderError,
    StructuredOutputError,
)
from patsquire_plr.gateway.gateway import ModelGateway, build_adapter
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.gateway.providers.base import (
    ChatResponse,
    EmbeddingResponse,
    GenerationParams,
    JsonSchemaSpec,
    Message,
    ProviderAdapter,
)
from patsquire_plr.gateway.ratelimit import RateLimiter
from patsquire_plr.gateway.secrets import SecretResolver
from patsquire_plr.gateway.store import CallRecord
from tests.support import base_models_config

# ---------------------------------------------------------------- test doubles (TEST-ONLY)


class MemoryStore:
    """TEST-ONLY in-memory CallStore."""

    def __init__(self) -> None:
        self.cache: dict[str, dict[str, object]] = {}
        self.calls: list[CallRecord] = []

    def get_cached(self, cache_key: str) -> dict[str, object] | None:
        return self.cache.get(cache_key)

    def put_cached(
        self,
        cache_key: str,
        *,
        backend: str,
        model: str,
        operation: str,
        response: dict[str, object],
    ) -> None:
        self.cache.setdefault(cache_key, response)

    def log_call(self, record: CallRecord) -> None:
        self.calls.append(record)


Step = str | Exception | tuple[tuple[float, ...], ...]


class ScriptedAdapter:
    """TEST-ONLY adapter replaying a script of replies (text or vectors) or exceptions."""

    provider = "scripted"

    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self.chat_calls: list[dict[str, object]] = []
        self.embed_calls: list[list[str]] = []

    def _next(self) -> Step:
        if not self.script:
            raise AssertionError("adapter called more often than scripted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        params: GenerationParams,
        json_schema: JsonSchemaSpec | None,
    ) -> ChatResponse:
        self.chat_calls.append(
            {
                "model": model,
                "system": system,
                "messages": messages,
                "params": params,
                "schema": json_schema,
            }
        )
        text = self._next()
        assert isinstance(text, str)
        return ChatResponse(
            text=text,
            input_tokens=100,
            output_tokens=20,
            model_reported=f"{model}-v1",
            params_sent={"seed": 7},
        )

    def embed(self, *, model: str, texts: list[str]) -> EmbeddingResponse:
        self.embed_calls.append(texts)
        vectors = self._next()
        assert isinstance(vectors, tuple)
        return EmbeddingResponse(vectors=vectors, input_tokens=len(texts), model_reported=model)


PROMPT = PromptTemplate(
    id="test.classify", version="1", system="You classify {topic}.", user="Text: {text}"
)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant: bool
    confidence: int = Field(ge=0, le=100)
    label: Literal["a", "b"]


def _models(**role_overrides: object) -> ModelsSettings:
    data = base_models_config()
    roles = data["roles"]
    assert isinstance(roles, dict)
    for role in roles.values():
        role.update(role_overrides)
    roles["embedding"].update(max_output_tokens=None)
    return ModelsSettings.model_validate(data)


def _gateway(
    script: list[Step], models: ModelsSettings | None = None
) -> tuple[ModelGateway, ScriptedAdapter, MemoryStore, list[float]]:
    adapter = ScriptedAdapter(script)
    store = MemoryStore()
    sleeps: list[float] = []
    factory: Callable[[str, BackendSettings, SecretResolver], ProviderAdapter] = lambda *_: adapter  # noqa: E731
    gateway = ModelGateway(
        models or _models(),
        secrets=SecretResolver(env_file=None, environ={}),
        store=store,
        adapter_factory=factory,
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    return gateway, adapter, store, sleeps


VARS = {"topic": "batteries", "text": "A lithium cell."}

# ---------------------------------------------------------------- text + cache


def test_text_call_is_logged_then_served_from_cache() -> None:
    gateway, adapter, store, _ = _gateway(["first answer"])

    first = gateway.text("writer", PROMPT, VARS)
    second = gateway.text("writer", PROMPT, VARS)

    assert (first.text, first.cached) == ("first answer", False)
    assert (second.text, second.cached) == ("first answer", True)
    assert len(adapter.chat_calls) == 1
    assert [c.cache_hit for c in store.calls] == [False, True]
    call = store.calls[0]
    assert (call.role, call.model, call.prompt_id, call.prompt_version) == (
        "writer",
        "chat-model",
        "test.classify",
        "1",
    )
    assert call.model_version_reported == "chat-model-v1"
    assert (call.input_tokens, call.output_tokens) == (100, 20)
    assert call.cost_estimate_usd is None  # no prices configured: unknown, not zero


def test_rendered_prompt_reaches_the_adapter() -> None:
    gateway, adapter, _, _ = _gateway(["ok"])
    gateway.text("writer", PROMPT, VARS)
    call = adapter.chat_calls[0]
    assert call["system"] == "You classify batteries."
    assert call["messages"] == [Message(role="user", content="Text: A lithium cell.")]
    assert call["params"] == GenerationParams(max_output_tokens=256, temperature=0, seed=7)


def test_prompt_edit_without_version_bump_misses_the_cache() -> None:
    gateway, adapter, _, _ = _gateway(["one", "two"])
    edited = PROMPT.model_copy(update={"system": "You strictly classify {topic}."})

    gateway.text("writer", PROMPT, VARS)
    result = gateway.text("writer", edited, VARS)

    assert result.text == "two"
    assert len(adapter.chat_calls) == 2


def test_different_roles_models_do_not_share_cache_entries() -> None:
    gateway, adapter, _, _ = _gateway(["writer text", "critic text"])
    assert gateway.text("writer", PROMPT, VARS).text == "writer text"
    assert gateway.text("critic", PROMPT, VARS).text == "critic text"  # critic-model differs
    assert len(adapter.chat_calls) == 2


def test_cost_is_computed_only_when_prices_are_configured() -> None:
    models = _models(input_price_per_mtok_usd="3", output_price_per_mtok_usd="15")
    gateway, _, store, _ = _gateway(["x"], models)
    gateway.text("writer", PROMPT, VARS)
    assert store.calls[0].cost_estimate_usd == Decimal("0.0006")  # (100*3 + 20*15) / 1e6


# ---------------------------------------------------------------- structured output


def test_structured_output_is_validated_and_cached() -> None:
    gateway, adapter, store, _ = _gateway(['{"relevant": true, "confidence": 90, "label": "a"}'])

    first = gateway.structured("bulk_classifier", PROMPT, VARS, Verdict)
    second = gateway.structured("bulk_classifier", PROMPT, VARS, Verdict)

    assert first.value == Verdict(relevant=True, confidence=90, label="a")
    assert second.cached
    assert second.value == first.value
    schema = adapter.chat_calls[0]["schema"]
    assert isinstance(schema, JsonSchemaSpec)
    assert schema.name == "Verdict"
    assert schema.schema_["additionalProperties"] is False
    assert [c.cache_hit for c in store.calls] == [False, True]


def test_invalid_output_gets_a_repair_turn_quoting_the_error() -> None:
    gateway, adapter, store, _ = _gateway(
        [
            '{"relevant": "yes", "confidence": 90, "label": "a"}',
            '{"relevant": false, "confidence": 5, "label": "b"}',
        ]
    )

    result = gateway.structured("bulk_classifier", PROMPT, VARS, Verdict)

    assert result.value.relevant is False
    repair = adapter.chat_calls[1]["messages"]
    assert isinstance(repair, list)
    assert repair[1] == Message(
        role="assistant", content='{"relevant": "yes", "confidence": 90, "label": "a"}'
    )
    assert "relevant" in repair[2].content
    errors = [c.error for c in store.calls if c.error]
    assert len(errors) == 1
    assert errors[0].startswith("schema validation failed: relevant")


def test_constraint_violations_are_caught_locally_even_if_the_provider_ignored_them() -> None:
    gateway, _, _, _ = _gateway(['{"relevant": true, "confidence": 150, "label": "a"}'] * 2)
    with pytest.raises(StructuredOutputError, match="confidence"):
        gateway.structured("bulk_classifier", PROMPT, VARS, Verdict)


def test_exhausted_repairs_fail_loudly_and_cache_nothing() -> None:
    gateway, adapter, store, _ = _gateway(["not json", "still not json"])

    with pytest.raises(StructuredOutputError, match="failed schema validation 2 time"):
        gateway.structured("bulk_classifier", PROMPT, VARS, Verdict)

    assert len(adapter.chat_calls) == 2  # max_schema_retries=1 -> two attempts
    assert store.cache == {}


def test_use_cache_false_always_calls_the_backend() -> None:
    reply = '{"relevant": true, "confidence": 1, "label": "a"}'
    gateway, adapter, _, _ = _gateway([reply, reply])
    gateway.structured("reasoner", PROMPT, VARS, Verdict)
    gateway.structured("reasoner", PROMPT, VARS, Verdict, use_cache=False)
    assert len(adapter.chat_calls) == 2


# ---------------------------------------------------------------- retries


def test_retryable_errors_back_off_then_succeed() -> None:
    models = _models()
    models.backends["local"] = models.backends["local"].model_copy(update={"max_retries": 2})
    gateway, _adapter, store, sleeps = _gateway(
        [RetryableProviderError("429"), RetryableProviderError("503"), "finally"], models
    )

    assert gateway.text("writer", PROMPT, VARS).text == "finally"
    assert sleeps == [1.0, 2.0]
    assert [(c.attempt, c.error) for c in store.calls] == [(1, "429"), (2, "503"), (3, None)]


def test_retries_are_bounded() -> None:
    gateway, adapter, store, _ = _gateway([RetryableProviderError("429")] * 2)  # max_retries=1
    with pytest.raises(RetryableProviderError):
        gateway.text("writer", PROMPT, VARS)
    assert len(adapter.chat_calls) == 2
    assert len(store.calls) == 2


def test_permanent_errors_are_not_retried() -> None:
    gateway, adapter, store, sleeps = _gateway([PermanentProviderError("401 unauthorized")])
    with pytest.raises(PermanentProviderError):
        gateway.text("writer", PROMPT, VARS)
    assert len(adapter.chat_calls) == 1
    assert sleeps == []
    assert store.calls[0].error == "401 unauthorized"


# ---------------------------------------------------------------- embeddings


def test_embeddings_are_cached_per_text() -> None:
    gateway, adapter, _, _ = _gateway([((1.0, 0.0),), ((0.0, 1.0),)])

    first = gateway.embed(["alpha"])
    mixed = gateway.embed(["alpha", "beta"])

    assert first.vectors == ((1.0, 0.0),)
    assert mixed.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert mixed.cached_count == 1
    assert mixed.dimensions == 2
    assert adapter.embed_calls == [["alpha"], ["beta"]]  # only the uncached text was sent


@pytest.mark.parametrize(
    ("reply", "message"),
    [(((1.0,),), "2 texts sent, 1 vectors"), (((1.0,), (1.0, 2.0)), "dimensions differ")],
)
def test_embedding_shape_problems_fail(reply: tuple[tuple[float, ...], ...], message: str) -> None:
    gateway, _, _, _ = _gateway([reply])
    with pytest.raises((GatewayError, PermanentProviderError), match=message):
        gateway.embed(["a", "b"])


def test_embedding_needs_input() -> None:
    gateway, _, _, _ = _gateway([])
    with pytest.raises(GatewayError, match="at least one text"):
        gateway.embed([])


# ---------------------------------------------------------------- secrets, adapters, rate limit


def test_secret_resolution_prefers_environment_and_fails_when_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEY_A=from-file\nKEY_B=file-only\nKEY_EMPTY=\n", encoding="utf-8")
    resolver = SecretResolver(env_file=env_file, environ={"KEY_A": "from-env"})

    assert resolver.resolve("KEY_A").get_secret_value() == "from-env"
    assert resolver.resolve("KEY_B").get_secret_value() == "file-only"
    for name in ("KEY_EMPTY", "KEY_MISSING"):
        with pytest.raises(MissingSecretError, match=name):
            resolver.resolve(name)


def test_hosted_backend_without_its_key_fails_before_any_request() -> None:
    backend = BackendSettings(
        type="anthropic", base_url=None, api_key_env="PLR_TEST_ANTHROPIC_KEY", region=None,
        max_tokens_field=None, timeout_s=10, max_retries=0, requests_per_minute=10,
    )  # fmt: skip
    with pytest.raises(MissingSecretError, match="PLR_TEST_ANTHROPIC_KEY"):
        build_adapter("hosted", backend, SecretResolver(env_file=None, environ={}))


@pytest.mark.parametrize("backend_type", ["openai_compatible", "anthropic", "gemini", "bedrock"])
def test_build_adapter_constructs_each_provider(backend_type: str) -> None:
    backend = BackendSettings.model_validate(
        {
            "type": backend_type,
            "base_url": "http://localhost:1/v1" if backend_type == "openai_compatible" else None,
            "api_key_env": "K" if backend_type in ("anthropic", "gemini") else None,
            "region": "us-east-1" if backend_type == "bedrock" else None,
            "max_tokens_field": "max_tokens" if backend_type == "openai_compatible" else None,
            "timeout_s": 5,
            "max_retries": 0,
            "requests_per_minute": 1,
        }
    )
    adapter = build_adapter("b", backend, SecretResolver(env_file=None, environ={"K": "test-key"}))
    assert adapter.provider == backend_type


def test_rate_limiter_allows_a_burst_then_paces_requests() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(60, clock=lambda: now[0], sleep=sleep)  # one per second
    for _ in range(60):
        assert limiter.acquire() == 0.0
    assert limiter.acquire() == pytest.approx(1.0)
    assert sum(sleeps) == pytest.approx(1.0)
    with pytest.raises(ValueError, match=">= 1"):
        RateLimiter(0, clock=lambda: 0.0, sleep=lambda _: None)
