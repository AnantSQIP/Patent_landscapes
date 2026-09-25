from __future__ import annotations

import importlib
import io
import json

import pytest
import structlog
from pydantic import SecretStr

from patsquire_plr import log as log_module
from patsquire_plr.log import REDACTED, configure_logging, get_logger


def _capture(level: str = "INFO") -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level, stream=stream)  # type: ignore[arg-type]
    return stream


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_emits_one_json_object_per_line_with_standard_fields() -> None:
    stream = _capture()

    get_logger("unit").info("step_done", step="ingest", kept=10)

    [record] = _records(stream)
    assert record["event"] == "step_done"
    assert record["logger_name"] == "unit"
    assert record["level"] == "info"
    assert record["kept"] == 10
    assert str(record["timestamp"]).endswith("Z")


def test_level_filtering() -> None:
    stream = _capture("WARNING")
    log = get_logger("unit")

    log.info("hidden")
    log.warning("shown")

    assert [r["event"] for r in _records(stream)] == ["shown"]


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "db_password",
        "api_key",
        "API-KEY",
        "secret_access_key",
        "token",
        "Authorization",
    ],
)
def test_credential_like_keys_are_redacted(key: str) -> None:
    stream = _capture()

    get_logger("unit").info("connecting", **{key: "hunter2"})

    output = stream.getvalue()
    assert "hunter2" not in output
    assert _records(stream)[0][key] == REDACTED


def test_nested_and_secretstr_values_are_redacted() -> None:
    stream = _capture()

    get_logger("unit").info(
        "config",
        backend={"model": "m", "api_key": "sk-nested"},
        items=[{"password": "in-list"}],
        credential_holder=SecretStr("wrapped"),
        plain=SecretStr("also-wrapped"),
    )

    output = stream.getvalue()
    for leaked in ("sk-nested", "in-list", "wrapped", "also-wrapped"):
        assert leaked not in output
    record = _records(stream)[0]
    assert record["backend"] == {"model": "m", "api_key": REDACTED}
    assert record["plain"] == REDACTED


def test_exceptions_are_rendered_not_dropped() -> None:
    stream = _capture()

    try:
        raise RuntimeError("boom")  # noqa: TRY301 - raising to produce a real traceback
    except RuntimeError as exc:
        get_logger("unit").error("failed", exc_info=exc)

    record = _records(stream)[0]
    assert "RuntimeError: boom" in str(record["exception"])


def test_import_installs_safe_json_config_without_locals() -> None:
    # Re-importing log.py must leave JSON rendering (with plain tracebacks) in place.
    stream = io.StringIO()
    importlib.reload(log_module)
    processors = structlog.get_config()["processors"]
    assert isinstance(processors[-1], structlog.processors.JSONRenderer)
    configure_logging("INFO", stream=stream)
    try:
        held_in_a_local = "local-secret-value"
        _fail(len(held_in_a_local))
    except ValueError as exc:
        get_logger("unit").error("failed", exc_info=exc)
    output = stream.getvalue()
    assert "ValueError: boom" in output
    assert "local-secret-value" not in output


def _fail(_n: int) -> None:
    raise ValueError("boom")
