"""OpenAI Chat Completions / Embeddings, for api.openai.com and any OpenAI-compatible
server (vLLM, SGLang, Ollama) via ``base_url``. Verified against openai 3.19.2."""

from __future__ import annotations

import httpx2
import openai
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONSchema
from pydantic import SecretStr

from patsquire_plr.gateway.errors import PermanentProviderError, RetryableProviderError
from patsquire_plr.gateway.providers.base import (
    ChatResponse,
    EmbeddingResponse,
    GenerationParams,
    JsonSchemaSpec,
    Message,
)
from patsquire_plr.gateway.schema import for_openai

# Servers without authentication (Ollama, local vLLM) ignore the key, but the SDK requires
# a non-empty string. "EMPTY" is the placeholder vLLM's documentation uses.
NO_AUTH_PLACEHOLDER = "EMPTY"

_RETRYABLE = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.ConflictError,
)


class OpenAICompatibleAdapter:
    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | None,
        timeout_s: float,
        max_tokens_field: str,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self._client = openai.OpenAI(
            api_key=NO_AUTH_PLACEHOLDER if api_key is None else api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,  # the gateway owns retries
            http_client=http_client,
        )
        self._max_tokens_field = max_tokens_field

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        params: GenerationParams,
        json_schema: JsonSchemaSpec | None,
    ) -> ChatResponse:
        response_format: ResponseFormatJSONSchema | None = None
        if json_schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.name,
                    "schema": for_openai(json_schema.schema_),
                    "strict": True,
                },
            }
        use_max_tokens = self._max_tokens_field == "max_tokens"
        chat_messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                chat_messages.append({"role": "user", "content": m.content})
            else:
                chat_messages.append({"role": "assistant", "content": m.content})
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=chat_messages,
                max_tokens=params.max_output_tokens if use_max_tokens else openai.omit,
                max_completion_tokens=openai.omit if use_max_tokens else params.max_output_tokens,
                temperature=openai.omit if params.temperature is None else params.temperature,
                seed=openai.omit if params.seed is None else params.seed,
                response_format=openai.omit if response_format is None else response_format,
            )
        except _RETRYABLE as exc:
            raise RetryableProviderError(f"openai_compatible: {type(exc).__name__}: {exc}") from exc
        except openai.OpenAIError as exc:
            raise PermanentProviderError(f"openai_compatible: {type(exc).__name__}: {exc}") from exc

        if not response.choices:
            raise PermanentProviderError("openai_compatible: response contained no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise PermanentProviderError(
                f"openai_compatible: output truncated at {params.max_output_tokens} tokens"
            )
        if choice.message.content is None:
            refusal = getattr(choice.message, "refusal", None)
            raise PermanentProviderError(f"openai_compatible: no content (refusal: {refusal!r})")
        usage = response.usage
        fingerprint = response.system_fingerprint
        return ChatResponse(
            text=choice.message.content,
            input_tokens=None if usage is None else usage.prompt_tokens,
            output_tokens=None if usage is None else usage.completion_tokens,
            model_reported=response.model + (f" ({fingerprint})" if fingerprint else ""),
            params_sent=_sent(
                {
                    self._max_tokens_field: params.max_output_tokens,
                    "temperature": params.temperature,
                    "seed": params.seed,
                    "response_format": response_format,
                }
            ),
        )

    def embed(self, *, model: str, texts: list[str]) -> EmbeddingResponse:
        try:
            response = self._client.embeddings.create(model=model, input=texts)
        except _RETRYABLE as exc:
            raise RetryableProviderError(f"openai_compatible: {type(exc).__name__}: {exc}") from exc
        except openai.OpenAIError as exc:
            raise PermanentProviderError(f"openai_compatible: {type(exc).__name__}: {exc}") from exc
        data = sorted(response.data, key=lambda item: item.index)
        if len(data) != len(texts):
            raise PermanentProviderError(
                f"openai_compatible: {len(texts)} texts sent but {len(data)} embeddings returned"
            )
        return EmbeddingResponse(
            vectors=tuple(tuple(float(x) for x in item.embedding) for item in data),
            input_tokens=None if response.usage is None else response.usage.prompt_tokens,
            model_reported=response.model,
        )


def _sent(params: dict[str, object]) -> dict[str, object]:
    """The parameters actually sent (None means omitted), for the call log."""
    return {key: value for key, value in params.items() if value is not None}
