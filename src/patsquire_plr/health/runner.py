"""Runs health checks and aggregates the results.

A check signals success by returning a short detail string and failure by raising. The
runner records every failure (type and message) and logs it with its traceback; it never
hides one. The overall report is healthy only if every check passed.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from patsquire_plr.log import get_logger

_log = get_logger(__name__)


class HealthCheck(Protocol):
    @property
    def name(self) -> str: ...

    def run(self) -> str:
        """Return a short success detail, or raise on failure."""
        ...


class CheckStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    status: CheckStatus
    detail: str
    duration_ms: float


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    results: tuple[CheckResult, ...]

    @property
    def healthy(self) -> bool:
        return bool(self.results) and all(r.status is CheckStatus.OK for r in self.results)


def run_checks(checks: Sequence[HealthCheck]) -> HealthReport:
    if not checks:
        raise ValueError("run_checks needs at least one check; an empty run proves nothing")
    names = [check.name for check in checks]
    if len(set(names)) != len(names):
        raise ValueError(f"Duplicate health check names: {names}")

    results: list[CheckResult] = []
    for check in checks:
        started = time.perf_counter()
        try:
            detail = check.run()
            status = CheckStatus.OK
        except Exception as exc:  # noqa: BLE001 - recorded as a failed check and logged, not hidden
            detail = f"{type(exc).__name__}: {exc}"
            status = CheckStatus.FAILED
            _log.error("health_check_failed", check=check.name, exc_info=exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        results.append(
            CheckResult(name=check.name, status=status, detail=detail, duration_ms=duration_ms)
        )
    return HealthReport(results=tuple(results))
