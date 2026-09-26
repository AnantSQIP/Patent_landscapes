"""Gateway failures. Adapters translate every SDK exception into one of these two classes."""

from __future__ import annotations

from patsquire_plr.errors import PlrError


class GatewayError(PlrError):
    """Base class for model gateway failures."""


class RetryableProviderError(GatewayError):
    """Rate limit, overload, server error or timeout: worth retrying after a backoff."""


class PermanentProviderError(GatewayError):
    """Authentication, bad request, unknown model, ...: retrying cannot help."""


class StructuredOutputError(GatewayError):
    """The model's output never validated against the schema within the allowed retries.

    ``cache_key`` identifies the call, so its logged attempts can be found in ``llm_call``.
    """

    def __init__(self, message: str, *, cache_key: str | None = None) -> None:
        super().__init__(message)
        self.cache_key = cache_key


class MissingSecretError(GatewayError):
    """A backend references an API-key environment variable that is not set."""
