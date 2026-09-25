from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from patsquire_plr.config import Settings, load_settings, secret_field_paths
from patsquire_plr.errors import ConfigError
from tests.support import (
    TEST_DB_PASSWORD,
    WriteConfig,
    base_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.usefixtures("secrets_env")
def test_loads_complete_config(write_config: WriteConfig) -> None:
    settings = load_settings(write_config(base_config()), env_file=None)

    assert settings.database.port == 5432
    assert settings.database.password.get_secret_value() == TEST_DB_PASSWORD
    assert settings.redis.password is None
    assert settings.app.environment == "test"


@pytest.mark.usefixtures("secrets_env")
def test_committed_settings_file_is_valid() -> None:
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml", env_file=None)
    assert settings.object_storage.bucket == "plr-artifacts"


def test_env_example_covers_every_secret() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for path in secret_field_paths(Settings):
        if path == ("redis", "password"):
            continue  # optional: local Redis runs without auth
        assert f"PLR__{'__'.join(p.upper() for p in path)}=" in env_example


@pytest.mark.usefixtures("secrets_env")
def test_environment_overrides_yaml(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLR__DATABASE__PORT", "6543")
    monkeypatch.setenv("PLR__APP__LOG_LEVEL", "DEBUG")

    settings = load_settings(write_config(base_config()), env_file=None)

    assert settings.database.port == 6543
    assert settings.app.log_level == "DEBUG"


def test_environment_beats_dotenv(
    write_config: WriteConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PLR__DATABASE__PASSWORD=from-dotenv\n"
        "PLR__OBJECT_STORAGE__ACCESS_KEY_ID=dotenv-key\n"
        "PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY=dotenv-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLR__DATABASE__PASSWORD", "from-environment")

    settings = load_settings(write_config(base_config()), env_file=env_file)

    assert settings.database.password.get_secret_value() == "from-environment"
    assert settings.object_storage.access_key_id.get_secret_value() == "dotenv-key"


def test_missing_secret_fails_with_field_name(write_config: WriteConfig) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_settings(write_config(base_config()), env_file=None)

    message = str(exc_info.value)
    assert "database.password" in message
    assert "object_storage.secret_access_key" in message


@pytest.mark.usefixtures("secrets_env")
@pytest.mark.parametrize(
    ("section", "key"),
    [("database", "host"), ("redis", "socket_timeout_s"), ("object_storage", "bucket")],
)
def test_missing_non_secret_fails(write_config: WriteConfig, section: str, key: str) -> None:
    data = base_config()
    del data[section][key]

    with pytest.raises(ConfigError, match=rf"{section}\.{key}"):
        load_settings(write_config(data), env_file=None)


@pytest.mark.usefixtures("secrets_env")
def test_unknown_key_is_rejected(write_config: WriteConfig) -> None:
    data = base_config()
    data["database"]["hostname"] = "typo"

    with pytest.raises(ConfigError, match=r"database\.hostname"):
        load_settings(write_config(data), env_file=None)


@pytest.mark.usefixtures("secrets_env")
def test_unknown_section_is_rejected(write_config: WriteConfig) -> None:
    data = base_config()
    data["databse"] = {"host": "typo"}

    with pytest.raises(ConfigError, match="databse"):
        load_settings(write_config(data), env_file=None)


@pytest.mark.usefixtures("secrets_env")
@pytest.mark.parametrize("port", [0, 65536, -1])
def test_out_of_range_port_is_rejected(write_config: WriteConfig, port: int) -> None:
    data = base_config()
    data["database"]["port"] = port

    with pytest.raises(ConfigError, match=r"database\.port"):
        load_settings(write_config(data), env_file=None)


@pytest.mark.usefixtures("secrets_env")
def test_invalid_log_level_is_rejected(write_config: WriteConfig) -> None:
    data = base_config()
    data["app"]["log_level"] = "VERBOSE"

    with pytest.raises(ConfigError, match=r"app\.log_level"):
        load_settings(write_config(data), env_file=None)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("database", "password"),
        ("object_storage", "access_key_id"),
        ("object_storage", "secret_access_key"),
        ("redis", "password"),
    ],
)
def test_secret_in_yaml_is_rejected(write_config: WriteConfig, section: str, key: str) -> None:
    data = base_config()
    data[section][key] = "committed-by-mistake"

    with pytest.raises(ConfigError, match="must not be stored") as exc_info:
        load_settings(write_config(data), env_file=None)

    assert f"PLR__{section.upper()}__{key.upper()}" in str(exc_info.value)
    assert "committed-by-mistake" not in str(exc_info.value)


def test_validation_error_never_echoes_secret_values(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLR__DATABASE__PASSWORD", "super-secret-value")
    data = base_config()
    data["database"]["port"] = "not-a-port"

    with pytest.raises(ConfigError) as exc_info:
        load_settings(write_config(data), env_file=None)

    assert "super-secret-value" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


@pytest.mark.usefixtures("secrets_env")
def test_secrets_are_masked_in_repr_and_dump(write_config: WriteConfig) -> None:
    settings = load_settings(write_config(base_config()), env_file=None)

    assert TEST_DB_PASSWORD not in repr(settings)
    assert TEST_DB_PASSWORD not in settings.model_dump_json()
    assert TEST_DB_PASSWORD not in str(settings.database.sqlalchemy_url())


@pytest.mark.usefixtures("secrets_env")
def test_sqlalchemy_url_carries_the_real_password(write_config: WriteConfig) -> None:
    settings = load_settings(write_config(base_config()), env_file=None)
    url = settings.database.sqlalchemy_url()

    assert url.password == TEST_DB_PASSWORD
    assert url.drivername == "postgresql+psycopg"


@pytest.mark.usefixtures("secrets_env")
def test_settings_are_immutable(write_config: WriteConfig) -> None:
    settings = load_settings(write_config(base_config()), env_file=None)

    with pytest.raises(ValueError, match="frozen"):
        settings.database.port = 1  # type: ignore[misc]


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Config file not found"):
        load_settings(tmp_path / "absent.yaml", env_file=None)


def test_missing_env_file_is_an_error(write_config: WriteConfig, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Env file not found"):
        load_settings(write_config(base_config()), env_file=tmp_path / "absent.env")


@pytest.mark.parametrize("content", ["- just\n- a list\n", "plain string", "a: [unclosed"])
def test_malformed_yaml(tmp_path: Path, content: str) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=str(path.name)):
        load_settings(path, env_file=None)


def test_secret_field_paths_finds_every_secret() -> None:
    assert sorted(secret_field_paths(Settings)) == [
        ("database", "password"),
        ("object_storage", "access_key_id"),
        ("object_storage", "secret_access_key"),
        ("redis", "password"),
    ]


@given(port=st.integers(min_value=1, max_value=65535))
def test_any_valid_port_round_trips(port: int) -> None:
    # Property: every in-range port supplied via the environment is accepted unchanged.
    with pytest.MonkeyPatch.context() as mp:
        for name, value in {
            "PLR__DATABASE__PASSWORD": "p",
            "PLR__OBJECT_STORAGE__ACCESS_KEY_ID": "a",
            "PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY": "s",
            "PLR__DATABASE__PORT": str(port),
        }.items():
            mp.setenv(name, value)
        settings = load_settings(REPO_ROOT / "config" / "settings.yaml", env_file=None)
    assert settings.database.port == port


# ---------------------------------------------------------------- review findings (Phase 0)


def test_shipped_env_example_loads_as_dotenv_file() -> None:
    # .env.example also carries docker-compose variables; they must be ignored, not rejected.
    settings = load_settings(
        REPO_ROOT / "config" / "settings.yaml", env_file=REPO_ROOT / ".env.example"
    )
    assert settings.database.password.get_secret_value() == "change-me"


@pytest.mark.parametrize(
    "name",
    ["PLR__DATABSE__HOST", "PLR__OBJECTSTORAGE__BUCKET"],
)
def test_unknown_section_in_environment_is_rejected(
    write_config: WriteConfig, secrets_env: dict[str, str], name: str
) -> None:
    del secrets_env
    with pytest.raises(ConfigError, match="unknown section"):
        load_settings(write_config(base_config()), env_file=None, environ={name: "x"})


@pytest.mark.usefixtures("secrets_env")
def test_unknown_key_in_known_section_from_environment_is_rejected(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLR__DATABASE__HOSTNAME", "typo")

    with pytest.raises(ConfigError, match=r"database\.hostname"):
        load_settings(write_config(base_config()), env_file=None)


@pytest.mark.parametrize(
    "name",
    ["PLR__DATABASE", "PLR__DATABASE__HOST__EXTRA", "PLR__database__host", "PLR____HOST"],
)
def test_malformed_variable_names_are_rejected(write_config: WriteConfig, name: str) -> None:
    with pytest.raises(ConfigError, match="must have the form"):
        load_settings(write_config(base_config()), env_file=None, environ={name: "x"})


def test_unknown_section_in_dotenv_is_rejected(write_config: WriteConfig, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PLR__REDSI__PORT=1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"env file .*unknown section 'redsi'"):
        load_settings(write_config(base_config()), env_file=env_file, environ={})


def test_valueless_dotenv_entry_is_rejected(write_config: WriteConfig, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PLR__DATABASE__PASSWORD\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="has no value"):
        load_settings(write_config(base_config()), env_file=env_file, environ={})


@pytest.mark.parametrize(
    "name",
    [
        "PLR__DATABASE__PASSWORD",
        "PLR__OBJECT_STORAGE__ACCESS_KEY_ID",
        "PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY",
        "PLR__REDIS__PASSWORD",
    ],
)
def test_empty_secret_is_rejected(
    write_config: WriteConfig, secrets_env: dict[str, str], name: str
) -> None:
    environ = {**secrets_env, name: ""}

    with pytest.raises(
        ConfigError, match=r"password|access_key_id|secret_access_key: Value should have at least 1"
    ):
        load_settings(write_config(base_config()), env_file=None, environ=environ)


def test_redis_password_may_be_set_from_environment(
    write_config: WriteConfig, secrets_env: dict[str, str]
) -> None:
    environ = {**secrets_env, "PLR__REDIS__PASSWORD": "redis-secret"}

    settings = load_settings(write_config(base_config()), env_file=None, environ=environ)

    assert settings.redis.password is not None
    assert settings.redis.password.get_secret_value() == "redis-secret"


def test_explicit_environ_replaces_process_environment(
    write_config: WriteConfig, secrets_env: dict[str, str]
) -> None:
    # The process env holds valid secrets (fixture); an explicit empty environ must not see them.
    del secrets_env
    with pytest.raises(ConfigError, match=r"database\.password"):
        load_settings(write_config(base_config()), env_file=None, environ={})
