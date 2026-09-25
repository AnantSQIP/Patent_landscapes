"""Anthropic Messages API. Verified against anthropic 1.8.0 and the platform docs.

* Structured output uses the GA ``output_config.format`` (json_schema), with no beta header.
* The 1.x SDK removed ``temperature``/``top_p``/``top_k`` and there is no ``seed``, so for
  Anthropic backends reproducibility rests on the gateway's response cache (config validation
  enforces null temperature and seed).
* There is no embeddings API.
"""

from __future__ import annotations

import anthropic
import httpx2
from anthropic.types import MessageParam, OutputConfigParam
from pydantic import SecretStr

from patsquire_plr.gateway.errors import PermanentProviderError, RetryableProviderError
from patsquire_plr.gateway.providers.base import (
    ChatResponse,
    EmbeddingResponse,
    GenerationParams,
    JsonSchemaSpec,
    Message,
)
from patsquire_plr.gateway.schema import for_anthropic

_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.InternalServerError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)


# Only these stop reasons mean the answer is complete; anything else is a failure.
_COMPLETE = ("end_turn", "stop_sequence")


class AnthropicAdapter:
    provider = "anthropic"
    max_texts_per_request: int | None = None

    def __init__(
        self, *, api_key: SecretStr, timeout_s: float, http_client: httpx2.Client | None = None
    ) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            timeout=timeout_s,
            max_retries=0,
            http_client=http_client,
        )

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        params: GenerationParams,
        json_schema: JsonSchemaSpec | None,
    ) -> ChatResponse:
        if params.temperature is not None or params.seed is not None:
            raise PermanentProviderError("anthropic: temperature and seed are not supported")
        output_config: OutputConfigParam | None = None
        if json_schema is not None:
            output_config = {
                "format": {"type": "json_schema", "schema": for_anthropic(json_schema.schema_)}
            }
        sent: dict[str, object] = {"max_tokens": params.max_output_tokens}
        if output_config is not None:
            sent["output_config"] = output_config
        chat_messages: list[MessageParam] = [
            {"role": m.role, "content": m.content} for m in messages
        ]
        try:
            response = self._client.messages.create(
                model=model,
                system=system,
                messages=chat_messages,
                max_tokens=params.max_output_tokens,
                output_config=anthropic.omit if output_config is None else output_config,
            )
        except _RETRYABLE as exc:
            raise RetryableProviderError(f"anthropic: {type(exc).__name__}: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise PermanentProviderError(f"anthropic: {type(exc).__name__}: {exc}") from exc

        if response.stop_reason in ("max_tokens", "model_context_window_exceeded"):
            raise PermanentProviderError(
                f"anthropic: output truncated ({response.stop_reason}, "
                f"max_output_tokens={params.max_output_tokens})"
            )
        if response.stop_reason == "refusal":
            raise PermanentProviderError("anthropic: the model declined to answer (refusal)")
        if response.stop_reason not in _COMPLETE:
            raise PermanentProviderError(
                f"anthropic: generation ended with stop_reason={response.stop_reason!r}"
            )
        text = "".join(block.text for block in response.content if block.type == "text")
        return ChatResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model_reported=response.model,
            params_sent=sent,
        )

    def embed(self, *, model: str, texts: list[str]) -> EmbeddingResponse:
        raise PermanentProviderError("anthropic has no embeddings API; use another backend")
