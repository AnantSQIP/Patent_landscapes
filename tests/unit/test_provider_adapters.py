"""Provider adapters against each SDK's own offline transport (no network).

Responses here are TEST-ONLY fixtures shaped like the providers' documented formats
(docs/architecture/provider_apis.md); they check the request each SDK actually sends and
how the adapter translates responses and error codes.
"""

from __future__ import annotations

import io
import json

import boto3
import httpx
import httpx2
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from pydantic import SecretStr

from patsquire_plr.gateway.errors import PermanentProviderError, RetryableProviderError
from patsquire_plr.gateway.providers.anthropic_messages import AnthropicAdapter
from patsquire_plr.gateway.providers.base import GenerationParams, JsonSchemaSpec, Message
from patsquire_plr.gateway.providers.bedrock import BedrockAdapter
from patsquire_plr.gateway.providers.gemini import GeminiAdapter
from patsquire_plr.gateway.providers.openai_compatible import OpenAICompatibleAdapter

SCHEMA = JsonSchemaSpec(
    name="Verdict",
    schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "why": {"type": "string", "maxLength": 50}},
        "required": ["ok", "why"],
        "additionalProperties": False,
    },
)
PARAMS = GenerationParams(max_output_tokens=64, temperature=0, seed=7)
MESSAGES = [Message(role="user", content="Is this relevant?")]


class Recorder:
    """TEST-ONLY: records requests and replies with a fixed status and JSON body."""

    def __init__(
        self, status: int, body: dict[str, object], headers: dict[str, str] | None = None
    ) -> None:
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list[tuple[str, dict[str, object]]] = []

    def handle(self, url: str, content: bytes) -> tuple[int, dict[str, object], dict[str, str]]:
        self.requests.append((url, json.loads(content) if content else {}))
        return self.status, self.body, self.headers


def _httpx2_client(recorder: Recorder) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        status, body, headers = recorder.handle(str(request.url), request.content)
        return httpx2.Response(status, json=body, headers=headers)

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _httpx_client(recorder: Recorder) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body, headers = recorder.handle(str(request.url), request.content)
        return httpx.Response(status, json=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- OpenAI-compatible


def _openai_completion(content: str | None, finish: str = "stop") -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen2.5:0.5b",
        "system_fingerprint": "fp_test",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }


def _openai(recorder: Recorder, field: str = "max_tokens") -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        base_url="http://llm.test/v1", api_key=None, timeout_s=5, max_tokens_field=field,
        http_client=_httpx2_client(recorder),
    )  # fmt: skip


def test_openai_request_and_response() -> None:
    recorder = Recorder(200, _openai_completion('{"ok": true, "why": "x"}'))

    response = _openai(recorder).chat(
        model="qwen2.5:0.5b", system="S", messages=MESSAGES, params=PARAMS, json_schema=SCHEMA
    )

    url, body = recorder.requests[0]
    assert url == "http://llm.test/v1/chat/completions"
    assert body["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "Is this relevant?"},
    ]
    assert (body["max_tokens"], body["temperature"], body["seed"]) == (64, 0, 7)
    assert "max_completion_tokens" not in body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Verdict", "schema": SCHEMA.schema_, "strict": True},
    }
    assert response.text == '{"ok": true, "why": "x"}'
    assert (response.input_tokens, response.output_tokens) == (12, 5)
    assert response.model_reported == "qwen2.5:0.5b (fp_test)"
    assert response.params_sent["seed"] == 7


def test_openai_can_send_max_completion_tokens() -> None:
    recorder = Recorder(200, _openai_completion("hi"))
    _openai(recorder, "max_completion_tokens").chat(
        model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None
    )
    body = recorder.requests[0][1]
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body
    assert "response_format" not in body


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, RetryableProviderError),
        (503, RetryableProviderError),
        (401, PermanentProviderError),
        (400, PermanentProviderError),
    ],
)
def test_openai_error_classification(status: int, error: type[Exception]) -> None:
    recorder = Recorder(status, {"error": {"message": "boom", "type": "x"}})
    with pytest.raises(error):
        _openai(recorder).chat(
            model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None
        )


@pytest.mark.parametrize(
    ("content", "finish", "message"),
    [("partial", "length", "truncated"), (None, "stop", "no content")],
)
def test_openai_truncation_and_empty_content_fail(
    content: str | None, finish: str, message: str
) -> None:
    recorder = Recorder(200, _openai_completion(content, finish))
    with pytest.raises(PermanentProviderError, match=message):
        _openai(recorder).chat(
            model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None
        )


def test_openai_embeddings_keep_input_order() -> None:
    recorder = Recorder(
        200,
        {
            "object": "list",
            "model": "all-minilm:22m",
            "data": [
                {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
            ],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        },
    )
    response = _openai(recorder).embed(model="all-minilm:22m", texts=["a", "b"])
    url, body = recorder.requests[0]
    assert url == "http://llm.test/v1/embeddings"
    assert (body["model"], body["input"]) == ("all-minilm:22m", ["a", "b"])
    assert response.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert response.input_tokens == 4


# ---------------------------------------------------------------- Anthropic


def _anthropic_message(text: str, stop: str = "end_turn") -> dict[str, object]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "anthropic-test-model",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 30, "output_tokens": 9},
    }


def _anthropic(recorder: Recorder) -> AnthropicAdapter:
    return AnthropicAdapter(
        api_key=SecretStr("test-key"), timeout_s=5, http_client=_httpx2_client(recorder)
    )


NO_SAMPLING = GenerationParams(max_output_tokens=64, temperature=None, seed=None)


def test_anthropic_request_uses_output_config_and_no_sampling_params() -> None:
    recorder = Recorder(200, _anthropic_message('{"ok": true, "why": "x"}'))

    response = _anthropic(recorder).chat(
        model="anthropic-test-model", system="S", messages=MESSAGES, params=NO_SAMPLING, json_schema=SCHEMA
    )

    url, body = recorder.requests[0]
    assert url.endswith("/v1/messages")
    assert body["system"] == "S"
    assert body["max_tokens"] == 64
    assert "temperature" not in body
    output_format = body["output_config"]["format"]  # type: ignore[index]
    assert output_format["type"] == "json_schema"
    assert "maxLength" not in json.dumps(output_format["schema"])  # unsupported keyword stripped
    assert response.text == '{"ok": true, "why": "x"}'
    assert (response.input_tokens, response.output_tokens, response.model_reported) == (
        30,
        9,
        "anthropic-test-model",
    )


def test_anthropic_refuses_sampling_params_and_embeddings() -> None:
    adapter = _anthropic(Recorder(200, {}))
    with pytest.raises(PermanentProviderError, match="temperature and seed"):
        adapter.chat(model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None)
    with pytest.raises(PermanentProviderError, match="no embeddings API"):
        adapter.embed(model="m", texts=["a"])


@pytest.mark.parametrize(
    ("status", "error"),
    [(529, RetryableProviderError), (429, RetryableProviderError), (401, PermanentProviderError)],
)
def test_anthropic_error_classification(status: int, error: type[Exception]) -> None:
    kind = {529: "overloaded_error", 429: "rate_limit_error", 401: "authentication_error"}[status]
    recorder = Recorder(status, {"type": "error", "error": {"type": kind, "message": "x"}})
    with pytest.raises(error):
        _anthropic(recorder).chat(
            model="m", system="S", messages=MESSAGES, params=NO_SAMPLING, json_schema=None
        )


@pytest.mark.parametrize(
    ("stop", "message"), [("max_tokens", "truncated"), ("refusal", "declined")]
)
def test_anthropic_truncation_and_refusal_fail(stop: str, message: str) -> None:
    recorder = Recorder(200, _anthropic_message("partial", stop))
    with pytest.raises(PermanentProviderError, match=message):
        _anthropic(recorder).chat(
            model="m", system="S", messages=MESSAGES, params=NO_SAMPLING, json_schema=None
        )


# ---------------------------------------------------------------- Gemini


def _gemini_response(text: str, finish: str = "STOP") -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}
        ],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4, "totalTokenCount": 15},
        "modelVersion": "gemini-test-001",
    }


def _gemini(recorder: Recorder) -> GeminiAdapter:
    return GeminiAdapter(
        api_key=SecretStr("test-key"), timeout_s=5, http_client=_httpx_client(recorder)
    )


def test_gemini_request_and_response() -> None:
    recorder = Recorder(200, _gemini_response('{"ok": false, "why": "y"}'))

    response = _gemini(recorder).chat(
        model="gemini-test", system="S", messages=MESSAGES, params=PARAMS, json_schema=SCHEMA
    )

    url, body = recorder.requests[0]
    assert "models/gemini-test:generateContent" in url
    config = body["generationConfig"]
    assert isinstance(config, dict)
    assert (config["maxOutputTokens"], config["temperature"], config["seed"]) == (64, 0, 7)
    assert config["responseMimeType"] == "application/json"
    assert "maxLength" not in json.dumps(config["responseJsonSchema"])
    assert body["systemInstruction"]["parts"][0]["text"] == "S"  # type: ignore[index]
    assert response.text == '{"ok": false, "why": "y"}'
    assert (response.input_tokens, response.output_tokens, response.model_reported) == (
        11,
        4,
        "gemini-test-001",
    )


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, RetryableProviderError), (500, RetryableProviderError), (400, PermanentProviderError)],
)
def test_gemini_error_classification(status: int, error: type[Exception]) -> None:
    recorder = Recorder(status, {"error": {"code": status, "message": "x", "status": "SOMETHING"}})
    with pytest.raises(error):
        _gemini(recorder).chat(
            model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None
        )


def test_gemini_truncation_fails() -> None:
    recorder = Recorder(200, _gemini_response("partial", "MAX_TOKENS"))
    with pytest.raises(PermanentProviderError, match="truncated"):
        _gemini(recorder).chat(
            model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None
        )


# ---------------------------------------------------------------- Bedrock (botocore Stubber)


def _bedrock() -> tuple[BedrockAdapter, Stubber]:
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - TEST-ONLY dummy, Stubber makes no calls
    )
    return BedrockAdapter(region="us-east-1", timeout_s=5, client=client), Stubber(client)


BEDROCK_PARAMS = GenerationParams(max_output_tokens=64, temperature=0, seed=None)


def test_bedrock_converse_request_and_response() -> None:
    adapter, stubber = _bedrock()
    stubber.add_response(
        "converse",
        {
            "output": {
                "message": {"role": "assistant", "content": [{"text": '{"ok": true, "why": "z"}'}]}
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 21, "outputTokens": 6, "totalTokens": 27},
            "metrics": {"latencyMs": 100},
        },
        {
            "modelId": "anthropic.test-model",
            "system": [{"text": "S"}],
            "messages": [{"role": "user", "content": [{"text": "Is this relevant?"}]}],
            "inferenceConfig": {"maxTokens": 64, "temperature": 0},
            "outputConfig": {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "schema": json.dumps(
                                {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean"},
                                        "why": {"type": "string"},
                                    },
                                    "required": ["ok", "why"],
                                    "additionalProperties": False,
                                }
                            ),
                            "name": "Verdict",
                        }
                    },
                }
            },
        },
    )
    with stubber:
        response = adapter.chat(
            model="anthropic.test-model",
            system="S",
            messages=MESSAGES,
            params=BEDROCK_PARAMS,
            json_schema=SCHEMA,
        )
        stubber.assert_no_pending_responses()
    assert response.text == '{"ok": true, "why": "z"}'
    assert (response.input_tokens, response.output_tokens, response.model_reported) == (21, 6, None)


@pytest.mark.parametrize(
    ("code", "status", "error"),
    [
        ("ThrottlingException", 429, RetryableProviderError),
        ("ValidationException", 400, PermanentProviderError),
    ],
)
def test_bedrock_error_classification(code: str, status: int, error: type[Exception]) -> None:
    adapter, stubber = _bedrock()
    stubber.add_client_error("converse", service_error_code=code, http_status_code=status)
    with stubber, pytest.raises(error, match=code):
        adapter.chat(
            model="m", system="S", messages=MESSAGES, params=BEDROCK_PARAMS, json_schema=None
        )


def test_bedrock_titan_embeddings_and_unsupported_models() -> None:
    adapter, stubber = _bedrock()
    for vector in ([1.0, 0.0], [0.0, 1.0]):
        stubber.add_response(
            "invoke_model",
            {
                "body": _streaming(
                    json.dumps({"embedding": vector, "inputTextTokenCount": 2}).encode()
                ),
                "contentType": "application/json",
            },
        )
    with stubber:
        response = adapter.embed(model="amazon.titan-embed-text-v2:0", texts=["a", "b"])
    assert response.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert response.input_tokens == 4
    with pytest.raises(PermanentProviderError, match="not supported"):
        adapter.embed(model="cohere.embed-english-v3", texts=["a"])


def test_bedrock_refuses_seed() -> None:
    adapter, _ = _bedrock()
    with pytest.raises(PermanentProviderError, match="no seed"):
        adapter.chat(model="m", system="S", messages=MESSAGES, params=PARAMS, json_schema=None)


def _streaming(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))
