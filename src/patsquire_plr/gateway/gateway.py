"""The Model Gateway (build prompt §4).

One entry point for text, structured output and embeddings, addressed by *role*. Which
backend and model serve a role comes only from configuration, so switching from a hosted
API to on-prem or AWS GPUs changes no code.

Every call:

1. derives a cache key from (backend, model, operation, parameters, prompt ID/version/hash,
   rendered input, schema) and returns a cached, previously validated response if one
   exists;
2. otherwise waits for the backend's rate limiter, then calls the provider with bounded
   exponential-backoff retries on retryable errors only;
3. for structured output, validates the response strictly against the Pydantic model. On
   failure it sends a bounded number of repair turns quoting the validation error, then
   raises ``StructuredOutputError``;
4. logs every attempt (hits, failures, retries) to the append-only call log, and caches
   only validated responses.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import partial

from pydantic import BaseModel, ValidationError

from patsquire_plr.canonical import canonical_sha256
from patsquire_plr.config import BackendSettings, ModelRole, ModelsSettings, RoleSettings
from patsquire_plr.gateway.errors import (
    GatewayError,
    PermanentProviderError,
    RetryableProviderError,
    StructuredOutputError,
)
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.gateway.providers.anthropic_messages import AnthropicAdapter
from patsquire_plr.gateway.providers.base import (
    ChatResponse,
    EmbeddingResponse,
    GenerationParams,
    JsonSchemaSpec,
    Message,
    ProviderAdapter,
)
from patsquire_plr.gateway.providers.bedrock import BedrockAdapter
from patsquire_plr.gateway.providers.gemini import GeminiAdapter
from patsquire_plr.gateway.providers.openai_compatible import OpenAICompatibleAdapter
from patsquire_plr.gateway.schema import strict_json_schema
from patsquire_plr.gateway.secrets import SecretResolver
from patsquire_plr.gateway.store import CallRecord, CallStore
from patsquire_plr.log import get_logger
from patsquire_plr.ratelimit import RateLimiter

_log = get_logger(__name__)

MAX_BACKOFF_S = 30.0
MICRO = Decimal(1_000_000)


@dataclass(frozen=True)
class _CallContext:
    """Everything that identifies one logical request, for logging each attempt."""

    role: ModelRole
    role_cfg: RoleSettings
    prompt: PromptTemplate | None
    key: str
    operation: str
    batch_keys: tuple[str, ...] = ()  # embedding batches: the cache key of every text sent


@dataclass(frozen=True)
class TextResult:
    text: str
    cached: bool
    backend: str
    model: str


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    cached: bool
    backend: str
    model: str


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    dimensions: int
    cached_count: int
    backend: str
    model: str


AdapterFactory = Callable[[str, BackendSettings, SecretResolver], ProviderAdapter]


def build_adapter(name: str, backend: BackendSettings, secrets: SecretResolver) -> ProviderAdapter:
    """Construct the SDK adapter for one configured backend (config is already validated)."""
    api_key = None if backend.api_key_env is None else secrets.resolve(backend.api_key_env)
    match backend.type:
        case "openai_compatible" if backend.base_url and backend.max_tokens_field:
            return OpenAICompatibleAdapter(
                base_url=backend.base_url,
                api_key=api_key,
                timeout_s=backend.timeout_s,
                max_tokens_field=backend.max_tokens_field,
            )
        case "anthropic" if api_key is not None:
            return AnthropicAdapter(api_key=api_key, timeout_s=backend.timeout_s)
        case "gemini" if api_key is not None:
            return GeminiAdapter(api_key=api_key, timeout_s=backend.timeout_s)
        case "bedrock" if backend.region is not None:
            return BedrockAdapter(region=backend.region, timeout_s=backend.timeout_s)
    raise GatewayError(
        f"backend {name}: incomplete {backend.type} configuration"
    )  # pragma: no cover


class ModelGateway:
    def __init__(
        self,
        models: ModelsSettings,
        *,
        secrets: SecretResolver,
        store: CallStore,
        adapter_factory: AdapterFactory = build_adapter,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._models = models
        self._secrets = secrets
        self._store = store
        self._factory = adapter_factory
        self._clock = clock
        self._sleep = sleep
        self._adapters: dict[str, ProviderAdapter] = {}
        self._limiters = {
            name: RateLimiter(b.requests_per_minute, clock=clock, sleep=sleep)
            for name, b in models.backends.items()
        }

    # ------------------------------------------------------------------ public API

    def text(
        self, role: ModelRole, prompt: PromptTemplate, variables: Mapping[str, str]
    ) -> TextResult:
        role_cfg = self._role(role, chat=True)
        system, user = prompt.render(variables)
        key = self._chat_key(role_cfg, prompt, system, user, schema=None)
        ctx = _CallContext(role=role, role_cfg=role_cfg, prompt=prompt, key=key, operation="text")
        cached = self._cached(ctx)
        if cached is not None:
            return TextResult(
                text=str(cached["text"]),
                cached=True,
                backend=role_cfg.backend,
                model=role_cfg.model,
            )
        response = self._chat(ctx, system, [Message(role="user", content=user)], None)
        self._store.put_cached(
            key,
            backend=role_cfg.backend,
            model=role_cfg.model,
            operation="text",
            response={"text": response.text},
        )
        return TextResult(
            text=response.text, cached=False, backend=role_cfg.backend, model=role_cfg.model
        )

    def structured[T: BaseModel](
        self,
        role: ModelRole,
        prompt: PromptTemplate,
        variables: Mapping[str, str],
        output_model: type[T],
        *,
        use_cache: bool = True,
    ) -> StructuredResult[T]:
        role_cfg = self._role(role, chat=True)
        schema = strict_json_schema(output_model)
        spec = JsonSchemaSpec(name=output_model.__name__[:64], schema=schema)
        system, user = prompt.render(variables)
        key = self._chat_key(role_cfg, prompt, system, user, schema=schema)
        ctx = _CallContext(
            role=role, role_cfg=role_cfg, prompt=prompt, key=key, operation="structured"
        )
        if use_cache:
            cached = self._cached(ctx)
            if cached is not None:
                # The cached text is exactly the response that passed validation before.
                value = output_model.model_validate_json(str(cached["json"]), strict=True)
                return StructuredResult(
                    value=value, cached=True, backend=role_cfg.backend, model=role_cfg.model
                )

        messages = [Message(role="user", content=user)]
        last_error = ""
        for _ in range(role_cfg.max_schema_retries + 1):
            response = self._chat(ctx, system, messages, spec)
            try:
                value = output_model.model_validate_json(response.text, strict=True)
            except ValidationError as exc:
                last_error = _summarise(exc)
                # attempt=0 marks a validation verdict on the response logged just before.
                self._log_attempt(
                    ctx,
                    attempt=0,
                    response=response,
                    latency_ms=0,
                    error=f"schema validation failed: {last_error}",
                )
                messages = [
                    *messages,
                    Message(role="assistant", content=response.text),
                    Message(
                        role="user",
                        content=(
                            "Your previous reply did not match the required JSON schema: "
                            f"{last_error}. Reply again with only JSON that matches the schema."
                        ),
                    ),
                ]
                continue
            self._store.put_cached(
                key,
                backend=role_cfg.backend,
                model=role_cfg.model,
                operation="structured",
                response={"json": response.text},
            )
            return StructuredResult(
                value=value, cached=False, backend=role_cfg.backend, model=role_cfg.model
            )
        raise StructuredOutputError(
            f"role {role} ({role_cfg.backend}/{role_cfg.model}): output failed schema validation "
            f"{role_cfg.max_schema_retries + 1} time(s); last error: {last_error}"
        )

    def embed(self, texts: list[str], *, use_cache: bool = True) -> EmbeddingResult:
        role: ModelRole = "embedding"
        role_cfg = self._role(role, chat=False)
        if not texts:
            raise GatewayError("embed() needs at least one text")
        keys = [self._embed_key(role_cfg, text) for text in texts]
        vectors: dict[int, tuple[float, ...]] = {}
        if use_cache:
            for i, key in enumerate(keys):
                hit = self._cached(
                    _CallContext(
                        role=role, role_cfg=role_cfg, prompt=None, key=key, operation="embed"
                    )
                )
                if hit is not None:
                    vectors[i] = _vector(hit)
        missing = [i for i in range(len(texts)) if i not in vectors]
        adapter = self._adapter(role_cfg.backend)
        size = adapter.max_texts_per_request or len(missing) or 1
        for start in range(0, len(missing), size):
            chunk = missing[start : start + size]
            batch = [texts[i] for i in chunk]
            batch_keys = tuple(keys[i] for i in chunk)
            ctx = _CallContext(
                role=role,
                role_cfg=role_cfg,
                prompt=None,
                key=canonical_sha256(list(batch_keys)),
                operation="embed",
                batch_keys=batch_keys,
            )
            response = self._with_retries(
                ctx, partial(adapter.embed, model=role_cfg.model, texts=batch)
            )
            if len(response.vectors) != len(batch):
                raise PermanentProviderError(
                    f"{role_cfg.backend}: {len(batch)} texts sent, "
                    f"{len(response.vectors)} vectors returned"
                )
            for i, vector in zip(chunk, response.vectors, strict=True):
                vectors[i] = vector
                self._store.put_cached(
                    keys[i],
                    backend=role_cfg.backend,
                    model=role_cfg.model,
                    operation="embed",
                    response={"vector": list(vector)},
                )
        dims = {len(v) for v in vectors.values()}
        if len(dims) != 1:
            raise GatewayError(f"embedding dimensions differ within one result: {sorted(dims)}")
        ordered = tuple(vectors[i] for i in range(len(texts)))
        return EmbeddingResult(
            vectors=ordered,
            dimensions=dims.pop(),
            cached_count=len(texts) - len(missing),
            backend=role_cfg.backend,
            model=role_cfg.model,
        )

    # ------------------------------------------------------------------ internals

    def _role(self, role: ModelRole, *, chat: bool) -> RoleSettings:
        role_cfg = self._models.roles[role]
        if chat and role_cfg.max_output_tokens is None:  # pragma: no cover - config validation
            raise GatewayError(f"role {role} has no max_output_tokens")
        return role_cfg

    def _adapter(self, backend: str) -> ProviderAdapter:
        if backend not in self._adapters:
            self._adapters[backend] = self._factory(
                backend, self._models.backends[backend], self._secrets
            )
        return self._adapters[backend]

    def _params(self, role_cfg: RoleSettings) -> GenerationParams:
        if role_cfg.max_output_tokens is None:  # pragma: no cover - guarded by _role
            raise GatewayError("chat role without max_output_tokens")
        return GenerationParams(
            max_output_tokens=role_cfg.max_output_tokens,
            temperature=role_cfg.temperature,
            seed=role_cfg.seed,
        )

    def _chat_key(
        self,
        role_cfg: RoleSettings,
        prompt: PromptTemplate,
        system: str,
        user: str,
        *,
        schema: dict[str, object] | None,
    ) -> str:
        return canonical_sha256(
            {
                "operation": "structured" if schema is not None else "text",
                "backend": role_cfg.backend,
                "server": self._server_identity(role_cfg.backend),
                "model": role_cfg.model,
                "params": {
                    "max_output_tokens": role_cfg.max_output_tokens,
                    "temperature": None
                    if role_cfg.temperature is None
                    else Decimal(str(role_cfg.temperature)),
                    "seed": role_cfg.seed,
                },
                "prompt": {"id": prompt.id, "version": prompt.version, "sha256": prompt.sha256},
                "system": system,
                "user": user,
                "schema": schema,
            }
        )

    def _server_identity(self, backend_name: str) -> dict[str, object]:
        """Which server answers: pointing a backend elsewhere must not reuse old answers."""
        backend = self._models.backends[backend_name]
        return {"type": backend.type, "base_url": backend.base_url, "region": backend.region}

    def _embed_key(self, role_cfg: RoleSettings, text: str) -> str:
        return canonical_sha256(
            {
                "operation": "embed",
                "backend": role_cfg.backend,
                "server": self._server_identity(role_cfg.backend),
                "model": role_cfg.model,
                "text": text,
            }
        )

    def _cached(self, ctx: _CallContext) -> dict[str, object] | None:
        cached = self._store.get_cached(ctx.key)
        if cached is not None:
            self._store.log_call(
                CallRecord(
                    role=ctx.role,
                    backend=ctx.role_cfg.backend,
                    provider=self._models.backends[ctx.role_cfg.backend].type,
                    model=ctx.role_cfg.model,
                    model_version_reported=None,
                    operation=ctx.operation,
                    prompt_id=None if ctx.prompt is None else ctx.prompt.id,
                    prompt_version=None if ctx.prompt is None else ctx.prompt.version,
                    params={},
                    request_sha256=ctx.key,
                    cache_key=ctx.key,
                    cache_hit=True,
                    attempt=0,
                    output=cached,
                    input_tokens=None,
                    output_tokens=None,
                    latency_ms=0,
                    cost_estimate_usd=None,
                    error=None,
                )
            )
        return cached

    def _chat(
        self,
        ctx: _CallContext,
        system: str,
        messages: list[Message],
        spec: JsonSchemaSpec | None,
    ) -> ChatResponse:
        params = self._params(ctx.role_cfg)
        adapter = self._adapter(ctx.role_cfg.backend)
        return self._with_retries(
            ctx,
            lambda: adapter.chat(
                model=ctx.role_cfg.model,
                system=system,
                messages=messages,
                params=params,
                json_schema=spec,
            ),
        )

    def _cost(
        self, role_cfg: RoleSettings, input_tokens: int | None, output_tokens: int | None
    ) -> Decimal | None:
        in_price, out_price = role_cfg.input_price_per_mtok_usd, role_cfg.output_price_per_mtok_usd
        if in_price is None or out_price is None or input_tokens is None or output_tokens is None:
            return None  # unknown is reported as unknown, never estimated from nothing
        return (Decimal(input_tokens) * in_price + Decimal(output_tokens) * out_price) / MICRO

    def _log_attempt(
        self,
        ctx: _CallContext,
        *,
        attempt: int,
        response: ChatResponse | EmbeddingResponse | None,
        latency_ms: int,
        error: str | None,
    ) -> None:
        output: object = None
        input_tokens = output_tokens = None
        model_reported = None
        params: dict[str, object] = {}
        if isinstance(response, ChatResponse):
            output, input_tokens, output_tokens = (
                response.text,
                response.input_tokens,
                response.output_tokens,
            )
            model_reported, params = response.model_reported, response.params_sent
        elif isinstance(response, EmbeddingResponse):
            output = {
                "count": len(response.vectors),
                "dimensions": len(response.vectors[0]) if response.vectors else 0,
                "cache_keys": list(ctx.batch_keys),
            }
            input_tokens, model_reported = response.input_tokens, response.model_reported
        role_cfg = ctx.role_cfg
        self._store.log_call(
            CallRecord(
                role=ctx.role,
                backend=role_cfg.backend,
                provider=self._models.backends[role_cfg.backend].type,
                model=role_cfg.model,
                model_version_reported=model_reported,
                operation=ctx.operation,
                prompt_id=None if ctx.prompt is None else ctx.prompt.id,
                prompt_version=None if ctx.prompt is None else ctx.prompt.version,
                params=_json_safe(params),
                request_sha256=ctx.key,
                cache_key=ctx.key,
                cache_hit=False,
                attempt=attempt,
                output=output,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_estimate_usd=self._cost(role_cfg, input_tokens, output_tokens),
                error=error,
            )
        )

    def _with_retries[R](self, ctx: _CallContext, call: Callable[[], R]) -> R:
        """Run ``call`` under the backend's rate limit, retrying retryable errors only."""
        backend = self._models.backends[ctx.role_cfg.backend]
        for attempt in range(1, backend.max_retries + 2):
            self._limiters[ctx.role_cfg.backend].acquire()
            started = self._clock()
            try:
                response = call()
            except RetryableProviderError as exc:
                self._log_attempt(
                    ctx,
                    attempt=attempt,
                    response=None,
                    latency_ms=self._ms_since(started),
                    error=str(exc),
                )
                if attempt > backend.max_retries:
                    raise
                delay = min(MAX_BACKOFF_S, 2.0 ** (attempt - 1))
                _log.warning(
                    "model_call_retry",
                    role=ctx.role,
                    backend=ctx.role_cfg.backend,
                    attempt=attempt,
                    delay_s=delay,
                )
                self._sleep(delay)
                continue
            except PermanentProviderError as exc:
                self._log_attempt(
                    ctx,
                    attempt=attempt,
                    response=None,
                    latency_ms=self._ms_since(started),
                    error=str(exc),
                )
                raise
            except Exception as exc:
                # Anything an adapter failed to classify is still logged, then surfaces as a
                # permanent failure (never retried blindly, never swallowed).
                message = f"unclassified {type(exc).__name__}: {exc}"
                self._log_attempt(
                    ctx,
                    attempt=attempt,
                    response=None,
                    latency_ms=self._ms_since(started),
                    error=message,
                )
                raise PermanentProviderError(message) from exc
            if isinstance(response, ChatResponse | EmbeddingResponse):
                self._log_attempt(
                    ctx,
                    attempt=attempt,
                    response=response,
                    latency_ms=self._ms_since(started),
                    error=None,
                )
            return response
        raise GatewayError("unreachable: retry loop exited without result")  # pragma: no cover

    def _ms_since(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))


def _summarise(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    )[:1000]


def _json_safe(value: object) -> dict[str, object]:
    """Parameters for the log: JSON-serialisable (floats are fine here; they are not facts)."""
    if not isinstance(value, dict):  # pragma: no cover
        return {}
    return {str(k): v for k, v in value.items()}


def _vector(cached: dict[str, object]) -> tuple[float, ...]:
    values = cached.get("vector")
    if not isinstance(values, list):
        raise GatewayError("corrupt embedding cache entry: 'vector' is not a list")
    return tuple(float(x) for x in values)
