"""Consistency checks between docker-compose.yml and the rest of the repo."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.support import MINIO_IMAGE, PGVECTOR_IMAGE, REDIS_IMAGE

REPO_ROOT = Path(__file__).resolve().parents[2]


def _services() -> dict[str, dict[str, object]]:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services: dict[str, dict[str, object]] = compose["services"]
    return services


def test_compose_uses_the_same_images_as_the_integration_tests() -> None:
    services = _services()

    assert services["postgres"]["image"] == PGVECTOR_IMAGE
    assert services["redis"]["image"] == REDIS_IMAGE
    assert services["minio"]["image"] == MINIO_IMAGE


def test_no_floating_latest_tags() -> None:
    for name, service in _services().items():
        image = service.get("image")
        if image is not None:
            assert not str(image).endswith(":latest"), f"{name} uses a floating :latest tag"


def test_bucket_name_matches_between_compose_env_and_app_config() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    settings = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))

    assert f"PLR_BUCKET={settings['object_storage']['bucket']}\n" in env_example


def _env_example() -> dict[str, str]:
    pairs = {}
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


def test_host_ports_match_between_compose_env_and_app_config() -> None:
    env = _env_example()
    settings = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))

    assert int(env["PLR_POSTGRES_HOST_PORT"]) == settings["database"]["port"]
    assert int(env["PLR_REDIS_HOST_PORT"]) == settings["redis"]["port"]
    assert settings["object_storage"]["endpoint_url"].endswith(f":{env['PLR_MINIO_HOST_PORT']}")


def test_compose_credentials_match_app_secrets_in_env_example() -> None:
    env = _env_example()

    assert env["POSTGRES_PASSWORD"] == env["PLR__DATABASE__PASSWORD"]
    assert env["MINIO_ROOT_USER"] == env["PLR__OBJECT_STORAGE__ACCESS_KEY_ID"]
    assert env["MINIO_ROOT_PASSWORD"] == env["PLR__OBJECT_STORAGE__SECRET_ACCESS_KEY"]
