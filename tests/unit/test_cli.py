from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from patsquire_plr.cli import app
from tests.support import TEST_DB_PASSWORD, TEST_S3_SECRET_KEY, WriteConfig, base_config

runner = CliRunner()

# Port 1 on loopback: nothing listens there, so connections are refused immediately.
UNREACHABLE_PORT = 1


@pytest.mark.usefixtures("secrets_env")
def test_config_show_masks_secrets(write_config: WriteConfig) -> None:
    result = runner.invoke(app, ["config", "show", "--config", str(write_config(base_config()))])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert shown["database"]["host"] == "localhost"
    assert TEST_DB_PASSWORD not in result.output
    assert TEST_S3_SECRET_KEY not in result.output


def test_config_errors_exit_2_with_message(write_config: WriteConfig) -> None:
    result = runner.invoke(app, ["config", "show", "--config", str(write_config(base_config()))])

    assert result.exit_code == 2
    assert "database.password" in result.stderr


def test_config_file_can_come_from_environment(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, secrets_env: dict[str, str]
) -> None:
    del secrets_env
    monkeypatch.setenv("PLR_CONFIG_FILE", str(write_config(base_config())))

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("secrets_env")
def test_health_reports_every_unreachable_service_and_exits_1(write_config: WriteConfig) -> None:
    data = base_config()
    data["database"]["host"] = "127.0.0.1"
    data["database"]["port"] = UNREACHABLE_PORT
    data["redis"]["host"] = "127.0.0.1"
    data["redis"]["port"] = UNREACHABLE_PORT
    data["object_storage"]["endpoint_url"] = f"http://127.0.0.1:{UNREACHABLE_PORT}"

    result = runner.invoke(app, ["health", "--config", str(write_config(data))])

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["healthy"] is False
    assert {c["name"]: c["status"] for c in report["checks"]} == {
        "postgres": "failed",
        "redis": "failed",
        "object_storage": "failed",
    }
    assert TEST_DB_PASSWORD not in result.output


def test_env_file_option_reads_secrets(write_config: WriteConfig, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_USER=ignored-compose-variable\n"
        "PLR__DATABASE__PASSWORD=from-file\n"
        "PLR__OBJECT_STORAGE__ACCESS_KEY_ID=a\n"
        "PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY=s\n",
        encoding="utf-8",
    )
    config = str(write_config(base_config()))

    by_option = runner.invoke(
        app, ["config", "show", "--config", config, "--env-file", str(env_file)]
    )
    by_envvar = runner.invoke(
        app, ["config", "show", "--config", config], env={"PLR_ENV_FILE": str(env_file)}
    )

    assert by_option.exit_code == 0, by_option.output
    assert by_envvar.exit_code == 0, by_envvar.output
    assert "from-file" not in by_option.output
