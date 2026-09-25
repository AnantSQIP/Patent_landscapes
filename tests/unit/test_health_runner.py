"""Runner behaviour, using TEST-ONLY stub checks (real service checks are in tests/integration)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patsquire_plr.health import CheckStatus, run_checks
from patsquire_plr.log import configure_logging


class StubCheck:
    """TEST-ONLY check that succeeds or raises on demand."""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self._error = error
        self.calls = 0

    def run(self) -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return f"{self.name} fine"


def test_all_passing_is_healthy() -> None:
    report = run_checks([StubCheck("a"), StubCheck("b")])

    assert report.healthy
    assert [r.status for r in report.results] == [CheckStatus.OK, CheckStatus.OK]
    assert report.results[0].detail == "a fine"
    assert all(r.duration_ms >= 0 for r in report.results)


def test_failure_is_recorded_with_type_and_message_and_other_checks_still_run() -> None:
    later = StubCheck("later")

    report = run_checks([StubCheck("bad", ConnectionError("refused")), later])

    assert not report.healthy
    bad, ok = report.results
    assert bad.status is CheckStatus.FAILED
    assert bad.detail == "ConnectionError: refused"
    assert ok.status is CheckStatus.OK
    assert later.calls == 1


def test_failure_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    run_checks([StubCheck("bad", RuntimeError("kaput"))])

    err = capsys.readouterr().err
    assert "health_check_failed" in err
    assert "kaput" in err


def test_empty_check_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one check"):
        run_checks([])


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        run_checks([StubCheck("x"), StubCheck("x")])


@given(st.lists(st.booleans(), min_size=1, max_size=8))
def test_healthy_iff_every_check_passes(outcomes: list[bool]) -> None:
    checks = [
        StubCheck(f"c{i}", None if ok else RuntimeError("no")) for i, ok in enumerate(outcomes)
    ]

    report = run_checks(checks)

    assert report.healthy == all(outcomes)
    assert len(report.results) == len(outcomes)
    assert [r.status is CheckStatus.OK for r in report.results] == outcomes
