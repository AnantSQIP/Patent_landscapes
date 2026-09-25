"""The provider-adapter contract. One adapter per backend type; the gateway owns caching,
logging, retries, rate limits and validation, so adapters only translate requests,
responses and errors."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Message(_Frozen):
    role: Literal["user", "assistant"]
    content: str


class GenerationParams(_Frozen):
    max_output_tokens: int = Field(ge=1)
    temperature: float | None
    seed: int | None


class JsonSchemaSpec(_Frozen):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    schema_: dict[str, object] = Field(alias="schema")


class ChatResponse(_Frozen):
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model_reported: str | None = Field(description="model/version string the provider returned")
    params_sent: dict[str, object] = Field(description="what was actually sent, for the log")


class EmbeddingResponse(_Frozen):
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None
    model_reported: str | None


class ProviderAdapter(Protocol):
    provider: str
    # Largest number of texts one embedding request may carry (None = no stated limit). The
    # gateway splits batches accordingly so each request is rate-limited and retried alone.
    max_texts_per_request: int | None

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        params: GenerationParams,
        json_schema: JsonSchemaSpec | None,
    ) -> ChatResponse: ...

    def embed(self, *, model: str, texts: list[str]) -> EmbeddingResponse: ...
