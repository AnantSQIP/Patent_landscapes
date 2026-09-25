"""Google Gemini via the ``google-genai`` SDK (not the deprecated google-generativeai).
Verified against google-genai 2.25.0: ``response_json_schema`` takes JSON Schema, the HTTP
timeout is in **milliseconds**, and the SDK does not retry unless told to."""

from __future__ import annotations

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr

from patsquire_plr.gateway.errors import PermanentProviderError, RetryableProviderError
from patsquire_plr.gateway.providers.base import (
    ChatResponse,
    EmbeddingResponse,
    GenerationParams,
    JsonSchemaSpec,
    Message,
)
from patsquire_plr.gateway.schema import for_gemini

HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500


class GeminiAdapter:
    provider = "gemini"

    def __init__(
        self, *, api_key: SecretStr, timeout_s: float, http_client: httpx.Client | None = None
    ) -> None:
        self._client = genai.Client(
            api_key=api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000), httpx_client=http_client),
        )

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        if isinstance(exc, genai_errors.APIError):
            retryable = exc.code == HTTP_TOO_MANY_REQUESTS or exc.code >= HTTP_SERVER_ERROR
            cls = RetryableProviderError if retryable else PermanentProviderError
            return cls(f"gemini: HTTP {exc.code}: {exc.message}")
        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return RetryableProviderError(f"gemini: {type(exc).__name__}: {exc}")
        return PermanentProviderError(f"gemini: {type(exc).__name__}: {exc}")

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        params: GenerationParams,
        json_schema: JsonSchemaSpec | None,
    ) -> ChatResponse:
        sent: dict[str, object] = {"max_output_tokens": params.max_output_tokens}
        if params.temperature is not None:
            sent["temperature"] = params.temperature
        if params.seed is not None:
            sent["seed"] = params.seed
        if json_schema is not None:
            sent["response_mime_type"] = "application/json"
            sent["response_json_schema"] = for_gemini(json_schema.schema_)
        config = types.GenerateContentConfig(system_instruction=system, **sent)  # type: ignore[arg-type]
        contents: list[types.ContentUnionDict] = [
            types.Content(
                role="model" if m.role == "assistant" else "user",
                parts=[types.Part(text=m.content)],
            )
            for m in messages
        ]
        try:
            response = self._client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except (genai_errors.APIError, httpx.HTTPError) as exc:
            raise self._translate(exc) from exc

        candidates = response.candidates or []
        if candidates and candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
            raise PermanentProviderError(
                f"gemini: output truncated at {params.max_output_tokens} tokens"
            )
        if response.text is None:
            raise PermanentProviderError(
                f"gemini: no text in response (candidates: {len(candidates)})"
            )
        usage = response.usage_metadata
        return ChatResponse(
            text=response.text,
            input_tokens=None if usage is None else usage.prompt_token_count,
            output_tokens=None if usage is None else usage.candidates_token_count,
            model_reported=response.model_version,
            params_sent=sent,
        )

    def embed(self, *, model: str, texts: list[str]) -> EmbeddingResponse:
        try:
            response = self._client.models.embed_content(model=model, contents=texts)  # type: ignore[arg-type]
        except (genai_errors.APIError, httpx.HTTPError) as exc:
            raise self._translate(exc) from exc
        embeddings = response.embeddings or []
        if len(embeddings) != len(texts) or any(e.values is None for e in embeddings):
            raise PermanentProviderError(
                f"gemini: {len(texts)} texts sent but {len(embeddings)} complete embeddings "
                "returned"
            )
        return EmbeddingResponse(
            vectors=tuple(tuple(float(x) for x in e.values or ()) for e in embeddings),
            input_tokens=None,
            model_reported=model,
        )
