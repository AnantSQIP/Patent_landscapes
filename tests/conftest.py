"""Shared pytest fixtures. Plain helpers live in ``tests.support``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from hypothesis import settings

from patsquire_plr.config import ENV_PREFIX
from patsquire_plr.log import configure_logging
from tests.support import WriteConfig, base_secrets

# Per-example deadlines are timing-dependent (slow on WSL's /mnt/c) and would make runs flaky.
settings.register_profile("plr", deadline=None)
settings.load_profile("plr")


@pytest.fixture(autouse=True)
def _json_logging() -> None:
    """Every test logs through the production JSON pipeline, never structlog's dev defaults."""
    configure_logging("INFO")


@pytest.fixture(autouse=True)
def _isolate_plr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the developer's real PLR__* environment from leaking into tests."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX) or name in {"PLR_CONFIG_FILE", "PLR_ENV_FILE"}:
            monkeypatch.delenv(name)


@pytest.fixture
def write_config(tmp_path: Path) -> WriteConfig:
    def _write(data: dict[str, dict[str, object]]) -> Path:
        path = tmp_path / "settings.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def secrets_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    secrets = base_secrets()
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    return secrets
