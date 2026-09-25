# Model provider API reference (verified 2026-09-25)

Build prompt principle 8 says we must not invent APIs. Each adapter in
`src/patsquire_plr/gateway/providers/` follows this reference. Every item was checked against
official documentation or the installed SDK source, and the offline adapter tests
(`tests/unit/test_provider_adapters.py`) assert the resulting request shapes. **Re-verify
before upgrading an SDK.**

Installed versions: openai 3.19.2, anthropic 1.8.0, google-genai 2.25.0, botocore 1.43.102.

| | OpenAI-compatible | Anthropic | Gemini | Bedrock |
|---|---|---|---|---|
| SDK call | `chat.completions.create` | `messages.create` | `models.generate_content` | `converse` |
| Structured output | `response_format={"type":"json_schema","json_schema":{name,schema,strict:true}}` | `output_config={"format":{"type":"json_schema","schema":…}}` (GA, no beta header) | `response_mime_type="application/json"` + `response_json_schema` | `outputConfig.textFormat.structure.jsonSchema.schema` (a **JSON string**) |
| Unsupported schema keywords (stripped by `gateway/schema.py`, still enforced locally) | none in strict mode | min/max, multipleOf, min/maxLength, pattern, format, maxItems | anything outside the documented keyword list (e.g. pattern, maxLength) | same as Anthropic |
| temperature / seed | both (seed is best-effort) | **neither**: the 1.x SDK removed temperature; there is no seed | both (seed is best-effort) | temperature only; no seed |
| Embeddings | `embeddings.create` | **none** (Anthropic points to Voyage AI) | `models.embed_content` | Titan Text Embeddings V2 via `invoke_model` |
| Model version returned | `model` + `system_fingerprint` | `model` | `model_version` | **none**: we log the model ID we sent |
| Retryable | RateLimit, InternalServer, APITimeout, APIConnection, Conflict | RateLimit, **Overloaded (529)**, InternalServer, ServiceUnavailable, DeadlineExceeded, APITimeout, APIConnection | `APIError` with code 429 or ≥500; httpx timeouts and transport errors | ThrottlingException, ServiceUnavailable, InternalServer, ModelNotReady, ModelTimeout, ServiceQuotaExceeded; read timeouts |
| SDK retries disabled via | `max_retries=0` | `max_retries=0` | default is no retries | `retries={"total_max_attempts": 1}` |
| Timeout unit | seconds | seconds | **milliseconds** | seconds |
| Offline test transport | `httpx2.MockTransport` (the SDK uses **httpx2**; respx does not see it) | `httpx2.MockTransport` | `httpx.MockTransport` via `HttpOptions(httpx_client=…)` | `botocore.stub.Stubber` |

## OpenAI-compatible servers
* **Ollama:** `response_format` json_schema and `seed` are supported
  (docs.ollama.com/api/openai-compatibility). It accepts `max_tokens`. Embeddings are at
  `/v1/embeddings`. Verified live in `tests/integration/test_gateway_live.py`: `all-minilm:22m`
  returns 384 dimensions.
* **vLLM:** `response_format` json_schema is supported; `guided_json` was removed in v0.12.0.
  `seed` is accepted, but online serving is reproducible only with batch invariance enabled
  (docs.vllm.ai).
* **SGLang:** `response_format` json_schema is supported. **Whether it honours `seed` is not
  verified.**
* The token-limit parameter differs between servers, so each `openai_compatible` backend
  states it explicitly in config (`max_tokens_field`).

## Sources
* platform.openai.com / developers.openai.com structured-outputs guide; openai SDK
  `types/chat/completion_create_params.py`, `_exceptions.py`, `_constants.py`.
* Anthropic platform documentation (structured outputs); anthropic SDK
  `types/output_config_param.py`, `_exceptions.py`.
* ai.google.dev/gemini-api/docs (structured output, embeddings); google-genai `types.py`,
  `errors.py`, `_api_client.py`.
* docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html and
  model-parameters-titan-embed-text.html; botocore bedrock-runtime service model.

## Not verified yet
* Live calls to the hosted APIs: no API keys were available on 2026-09-25. The adapters are
  tested offline against the documented formats only.
* Current Gemini model IDs. Check the models page before configuring one.
* Which exception google-genai raises on timeout. The adapter treats httpx
  timeout/transport errors as retryable.
