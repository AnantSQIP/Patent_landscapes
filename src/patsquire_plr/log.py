"""Structured JSON logging.

All log output is one JSON object per line on stderr, with an ISO-8601 UTC timestamp.
Values under keys that look like credentials are redacted before rendering, and pydantic
``SecretStr`` values render masked, so secrets cannot reach logs by accident.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from collections.abc import MutableMapping
from typing import TextIO, cast

import structlog
from pydantic import SecretStr

from patsquire_plr.config import LogLevel

REDACTED = "***REDACTED***"

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|authorization|credential)",
    re.IGNORECASE,
)


def _redact_value(value: object) -> object:
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_KEY.search(str(key)) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def redact_secrets(
    _logger: object, _method_name: str, event_dict: MutableMapping[str, object]
) -> MutableMapping[str, object]:
    """structlog processor: mask credential-like keys and ``SecretStr`` values, recursively."""
    for key in list(event_dict):
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


class _CurrentStderr(io.TextIOBase):
    """Writes to whatever ``sys.stderr`` is *now*, so later redirection is honoured."""

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: LogLevel, *, stream: TextIO | None = None) -> None:
    """Configure structlog for JSON output. Safe to call more than once (last call wins).

    ``stream`` defaults to the process's current stderr at write time.
    """
    target = stream if stream is not None else cast("TextIO", _CurrentStderr())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=target),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a logger whose records carry ``logger_name=<name>``.

    The logger is a lazy proxy, so module-level loggers pick up whatever configuration is
    active when they first log, not the one active at import time.
    """
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(logger_name=name)
    return logger


# Applied on import so nothing can ever log through structlog's development defaults, which
# render tracebacks with local variables (and therefore could print secrets).
configure_logging("INFO")
