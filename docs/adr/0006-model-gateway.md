# ADR 0006: Model gateway

* Status: accepted (Phase 2)
* Date: 2026-09-25

## Decision
* **Roles, not models, in code.** Code asks for `embedding`, `bulk_classifier`, `reasoner`,
  `writer` or `critic`. `config/settings.yaml` maps each role to a backend and model.
  Switching between hosted APIs, on-prem GPUs and AWS GPUs is a config change only.
  vLLM and SGLang on-prem or on EC2 use the `openai_compatible` backend type.
* **Four adapters** (OpenAI-compatible, Anthropic, Gemini, Bedrock) only translate
  requests, responses and errors. Their contract is in `providers/base.py`, and the facts
  behind them are in `docs/architecture/provider_apis.md`.
* **The gateway owns everything else:**
  * the cache;
  * the append-only call log (every attempt, hit and failure);
  * retries with bounded exponential backoff, on retryable errors only;
  * a per-backend token-bucket rate limit;
  * strict local validation of structured output, with a bounded number of repair turns
    that quote the validation error.
* **Provider schema subsets.** Each provider receives a JSON Schema with its unsupported
  keywords removed. Our Pydantic model is always the final validator, so a constraint a
  provider ignores is still enforced.
* **Only a complete answer is accepted.** Each adapter allowlists its provider's
  normal-completion reasons (`stop`, `end_turn`/`stop_sequence`, `STOP`). Truncation,
  context overflow, refusals, content filters and guardrails are failures, so partial text
  can never enter the append-only cache.
* **Every attempt is logged.** An exception an adapter failed to classify is logged, then
  raised as a permanent failure.
* **Embedding batches follow the provider's request limit** (Bedrock Titan takes one text
  per request), so each request is rate-limited and retried on its own.
* **Config enforces reproducible sampling:** temperature 0 on every chat role that accepts
  a temperature, and a fixed seed on OpenAI-compatible and Gemini backends.
* **Reproducibility (principle 6):**
  * temperature 0 and a fixed seed wherever the provider accepts them;
  * a persistent cache keyed by backend, model, parameters, prompt ID, version and content
    hash, rendered input and schema.

  Anthropic accepts neither temperature nor seed, so for Anthropic backends the cache is the
  reproducibility mechanism, and config validation enforces null values. Seeded
  temperature-0 reproducibility is verified live on Ollama.
* **Secrets:** a backend names the environment variable that holds its key (`api_key_env`).
  A missing or empty key fails before any request is sent. Bedrock uses the AWS credential
  chain.
* **Health:** `plr models health` makes a real schema-constrained call per chat role and an
  embedding call, bypassing the cache but still logging.

## Consequences
* The call log grows with every call, including cache hits, by design (audit). Retention is
  a Phase 12 concern.
* Rate limits are per process. Multi-worker limits need Redis, which is added when the
  workflow engine lands (Phase 7/10).
* Hosted adapters are verified offline only until API keys are provided.
