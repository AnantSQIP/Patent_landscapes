# ADR 0002: Configuration and secrets

* Status: accepted (Phase 0)
* Date: 2026-09-25

## Decision
* **Precedence:** process environment, then the dotenv file, then `config/settings.yaml`.
  There is no other source. Only `PLR__<SECTION>__<KEY>` entries are read from the
  environment and the dotenv file; the dotenv file may also hold docker-compose variables,
  which the app ignores.
* **Typos fail everywhere:** unknown sections and keys are rejected whether they come from
  YAML, dotenv or the environment, and so are malformed names (wrong case, wrong depth).
* **Empty secrets are rejected** (`KEY=` in `.env` is an error, not an empty password).
* **No defaults in code.** Every non-secret value is written out in the committed YAML, so
  the effective configuration can always be read in one place.
* **Secrets:** any `SecretStr` field that has a non-null value in the YAML aborts loading
  with a message naming the environment variable to use instead. `null` is allowed for
  optional secrets (e.g. an unauthenticated local Redis).
* **Safe errors:** validation messages list the field and the problem but never the input
  value, and the original pydantic exception is not chained because its text contains
  input values.
* **Logging:** credential-like keys and `SecretStr` values are redacted recursively. The
  JSON config is applied on import so structlog's dev renderer (which shows traceback
  locals) is never active.

## Not yet done
* A secrets-manager source (e.g. AWS Secrets Manager) will be added as an extra settings
  source once there is a real AWS account to test against (build prompt §4 requirement).
  It is recorded as an open item rather than shipped untested.
