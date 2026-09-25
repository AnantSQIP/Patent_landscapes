"""Amazon Bedrock via boto3 ``bedrock-runtime``. Verified against botocore 1.43.102:

* Converse structured output: ``outputConfig.textFormat`` with a JSON schema passed as a
  *string*.
* Converse returns no model version, so ``model_reported`` is None and the call log keeps
  the exact model ID that was sent.
* Embeddings: Amazon Titan Text Embeddings V2 via InvokeModel, one text per request. Other
  embedding families use different body formats and are refused rather than guessed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ReadTimeoutError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from patsquire_plr.gateway.errors import PermanentProviderError, RetryableProviderError
from patsquire_plr.gateway.providers.base import (
    ChatResponse,
    EmbeddingResponse,
    GenerationParams,
    JsonSchemaSpec,
    Message,
)
from patsquire_plr.gateway.schema import for_bedrock

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
    from mypy_boto3_bedrock_runtime.type_defs import (
        InferenceConfigurationTypeDef,
        MessageTypeDef,
        OutputConfigTypeDef,
    )

_RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "ServiceQuotaExceededException",
    }
)
TITAN_EMBED_V2_PREFIX = "amazon.titan-embed-text-v2"


class BedrockAdapter:
    provider = "bedrock"

    def __init__(
        self, *, region: str, timeout_s: float, client: BedrockRuntimeClient | None = None
    ) -> None:
        self._client: BedrockRuntimeClient = client or boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=min(timeout_s, 10),
                read_timeout=timeout_s,
                retries={"total_max_attempts": 1},  # the gateway owns retries
            ),
        )

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            cls = RetryableProviderError if code in _RETRYABLE_CODES else PermanentProviderError
            return cls(f"bedrock: {code}: {exc}")
        if isinstance(exc, ReadTimeoutError | BotoConnectionError):
            return RetryableProviderError(f"bedrock: {type(exc).__name__}: {exc}")
        return PermanentProviderError(f"bedrock: {type(exc).__name__}: {exc}")

    def chat(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        params: GenerationParams,
        json_schema: JsonSchemaSpec | None,
    ) -> ChatResponse:
        if params.seed is not None:
            raise PermanentProviderError("bedrock: Converse has no seed parameter")
        inference: InferenceConfigurationTypeDef = {"maxTokens": params.max_output_tokens}
        if params.temperature is not None:
            inference["temperature"] = params.temperature
        output_config: OutputConfigTypeDef | None = None
        if json_schema is not None:
            output_config = {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "schema": json.dumps(for_bedrock(json_schema.schema_)),
                            "name": json_schema.name,
                        }
                    },
                }
            }
        chat_messages: list[MessageTypeDef] = [
            {"role": m.role, "content": [{"text": m.content}]} for m in messages
        ]
        try:
            if output_config is None:
                response = self._client.converse(
                    modelId=model,
                    system=[{"text": system}],
                    messages=chat_messages,
                    inferenceConfig=inference,
                )
            else:
                response = self._client.converse(
                    modelId=model,
                    system=[{"text": system}],
                    messages=chat_messages,
                    inferenceConfig=inference,
                    outputConfig=output_config,
                )
        except (ClientError, ReadTimeoutError, BotoConnectionError) as exc:
            raise self._translate(exc) from exc
        sent: dict[str, object] = {"inferenceConfig": dict(inference)}
        if output_config is not None:
            sent["outputConfig"] = dict(output_config)

        if response.get("stopReason") == "max_tokens":
            raise PermanentProviderError(
                f"bedrock: output truncated at {params.max_output_tokens} tokens"
            )
        blocks = response["output"]["message"]["content"]
        text = "".join(block["text"] for block in blocks if "text" in block)
        usage = response.get("usage", {})
        return ChatResponse(
            text=text,
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            model_reported=None,
            params_sent=sent,
        )

    def embed(self, *, model: str, texts: list[str]) -> EmbeddingResponse:
        if not model.startswith(TITAN_EMBED_V2_PREFIX):
            raise PermanentProviderError(
                f"bedrock: embedding model {model!r} is not supported; only "
                f"{TITAN_EMBED_V2_PREFIX}* request/response formats are implemented"
            )
        vectors: list[tuple[float, ...]] = []
        tokens = 0
        for text in texts:
            try:
                response = self._client.invoke_model(
                    modelId=model,
                    body=json.dumps({"inputText": text, "normalize": True}),
                    contentType="application/json",
                    accept="application/json",
                )
            except (ClientError, ReadTimeoutError, BotoConnectionError) as exc:
                raise self._translate(exc) from exc
            body = json.loads(response["body"].read())
            if "embedding" not in body:
                raise PermanentProviderError("bedrock: Titan response has no 'embedding' field")
            vectors.append(tuple(float(x) for x in body["embedding"]))
            tokens += int(body.get("inputTextTokenCount", 0))
        return EmbeddingResponse(vectors=tuple(vectors), input_tokens=tokens, model_reported=None)
