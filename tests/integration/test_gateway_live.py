"""Real model calls through the gateway (build prompt Phase 2 acceptance).

A small CPU model served by Ollama's OpenAI-compatible API stands in for on-prem and AWS GPU
servers (vLLM/SGLang expose the same API). Models are cached in a named Docker volume, so
they download once. These tests check behaviour (schema validity, logging, caching,
determinism, config-only switching), not the small model's judgement.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy
from typer.testing import CliRunner

from patsquire_plr.cli import app
from patsquire_plr.config import ModelsSettings
from patsquire_plr.db.models import LlmCache, LlmCall
from patsquire_plr.gateway.errors import MissingSecretError
from patsquire_plr.gateway.gateway import ModelGateway
from patsquire_plr.gateway.health import check_models
from patsquire_plr.gateway.prompts import PromptTemplate
from patsquire_plr.gateway.secrets import SecretResolver
from patsquire_plr.gateway.store import PostgresCallStore
from tests.support import TEST_DB_PASSWORD, base_config, base_secrets

pytestmark = pytest.mark.integration

OLLAMA_IMAGE = "ollama/ollama:0.34.4"
CHAT_MODEL = "qwen2.5:0.5b"
EMBED_MODEL = "all-minilm:22m"
MODEL_VOLUME = "plr-test-ollama-models"


@pytest.fixture(scope="module")
def ollama_url() -> Iterator[str]:
    container = (
        DockerContainer(OLLAMA_IMAGE)
        .with_exposed_ports(11434)
        .with_volume_mapping(MODEL_VOLUME, "/root/.ollama", mode="rw")
        .waiting_for(HttpWaitStrategy(11434, "/").for_status_code(200))
    )
    with container:
        for model in (CHAT_MODEL, EMBED_MODEL):
            _pull(container, model)
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(11434)}/v1"


def _pull(container: DockerContainer, model: str, attempts: int = 3) -> None:
    """Download a model; network setup gets a few tries, then fails with the output."""
    output = b""
    for attempt in range(1, attempts + 1):
        exit_code, output = container.exec(["ollama", "pull", model])
        if exit_code == 0:
            return
        time.sleep(2 * attempt)
    tail = output.decode(errors="replace")[-500:]
    pytest.fail(f"could not pull {model} after {attempts} attempts: {tail}")


def _backend(url: str) -> dict[str, object]:
    return {
        "type": "openai_compatible",
        "base_url": url,
        "api_key_env": None,
        "region": None,
        "max_tokens_field": "max_tokens",
        "timeout_s": 120,
        "max_retries": 1,
        "requests_per_minute": 600,
    }


def _role(
    backend: str, model: str, *, embedding: bool = False, sampling: bool = True
) -> dict[str, object]:
    fixed = sampling and not embedding
    return {
        "backend": backend,
        "model": model,
        "max_output_tokens": None if embedding else 256,
        "temperature": 0 if fixed else None,
        "seed": 7 if fixed else None,
        "max_schema_retries": 2,
        "input_price_per_mtok_usd": None,
        "output_price_per_mtok_usd": None,
    }


def _models(backends: dict[str, dict[str, object]], chat_backend: str) -> ModelsSettings:
    embed_backend = next(n for n, b in backends.items() if b["type"] == "openai_compatible")
    return ModelsSettings.model_validate(
        {
            "backends": backends,
            "roles": {
                "embedding": _role(embed_backend, EMBED_MODEL, embedding=True),
                **{
                    r: _role(
                        chat_backend,
                        CHAT_MODEL,
                        sampling=backends[chat_backend]["type"] == "openai_compatible",
                    )
                    for r in ("bulk_classifier", "reasoner", "writer", "critic")
                },
            },
        }
    )


def _gateway(models: ModelsSettings, engine: Engine) -> ModelGateway:
    return ModelGateway(
        models, secrets=SecretResolver(env_file=None, environ={}), store=PostgresCallStore(engine)
    )


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: Literal["batteries", "software", "medicine"]
    is_patent_related: bool


PROMPT = PromptTemplate(
    id="test.classify_topic",
    version="1",
    system="You classify short texts. Reply with JSON only.",
    user="Text: {text}\nChoose the topic and say whether it concerns patents.",
)
TEXT = {"text": "A patent application for a lithium-ion battery cathode."}


def test_structured_output_is_valid_logged_and_cached(ollama_url: str, db_engine: Engine) -> None:
    gateway = _gateway(_models({"on_prem": _backend(ollama_url)}, "on_prem"), db_engine)

    first = gateway.structured("bulk_classifier", PROMPT, TEXT, Classification)
    second = gateway.structured("bulk_classifier", PROMPT, TEXT, Classification)

    assert isinstance(first.value, Classification)
    assert not first.cached
    assert second.cached
    assert second.value == first.value
    with Session(db_engine) as session:
        calls = session.scalars(select(LlmCall).order_by(LlmCall.occurred_at)).all()
        cached_rows = session.scalar(select(func.count()).select_from(LlmCache))
    assert [c.cache_hit for c in calls][-1] is True
    fresh = [c for c in calls if not c.cache_hit and c.error is None]
    assert fresh
    assert fresh[0].model_version_reported is not None
    assert fresh[0].model_version_reported.startswith(CHAT_MODEL)
    assert fresh[0].input_tokens is not None
    assert fresh[0].input_tokens > 0
    assert fresh[0].params == {
        "max_tokens": 256,
        "temperature": 0,
        "seed": 7,
        "response_format": fresh[0].params["response_format"],
    }
    assert cached_rows == 1


def test_seeded_temperature_zero_calls_are_reproducible(ollama_url: str, db_engine: Engine) -> None:
    gateway = _gateway(_models({"on_prem": _backend(ollama_url)}, "on_prem"), db_engine)

    runs = [
        gateway.structured("reasoner", PROMPT, TEXT, Classification, use_cache=False).value
        for _ in range(3)
    ]

    assert runs[0] == runs[1] == runs[2]


def test_real_embeddings_are_cached_per_text(ollama_url: str, db_engine: Engine) -> None:
    gateway = _gateway(_models({"on_prem": _backend(ollama_url)}, "on_prem"), db_engine)

    first = gateway.embed(["solid-state battery electrolyte"])
    both = gateway.embed(["solid-state battery electrolyte", "neural network training"])

    assert first.dimensions == 384
    assert both.cached_count == 1
    assert both.vectors[0] == first.vectors[0]
    assert both.vectors[0] != both.vectors[1]


def test_switching_backends_is_configuration_only(ollama_url: str, db_engine: Engine) -> None:
    """The same gateway code serves whichever backend the configuration names."""
    backends: dict[str, dict[str, object]] = {
        "on_prem_gpu": _backend(ollama_url),
        # Stands in for an AWS GPU host running vLLM; same API, different endpoint entry.
        "aws_gpu": _backend(ollama_url.replace("localhost", "127.0.0.1")),
        "hosted_api": {
            "type": "anthropic",
            "base_url": None,
            "api_key_env": "PLR_TEST_UNSET_ANTHROPIC_KEY",
            "region": None,
            "max_tokens_field": None,
            "timeout_s": 30,
            "max_retries": 0,
            "requests_per_minute": 10,
        },
    }
    for backend in ("on_prem_gpu", "aws_gpu"):
        models = _models(backends, backend)
        results = check_models(_gateway(models, db_engine), models)
        assert all(r.ok for r in results), results
        assert {r.backend for r in results if r.role != "embedding"} == {backend}

    gateway = _gateway(_models(backends, "hosted_api"), db_engine)
    with pytest.raises(MissingSecretError, match="PLR_TEST_UNSET_ANTHROPIC_KEY"):
        gateway.structured("writer", PROMPT, TEXT, Classification)


def test_cli_models_health_reports_every_role(
    ollama_url: str, db_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = db_engine.url
    data = base_config()
    data["database"].update(host=url.host, port=url.port, name=url.database)
    data["models"] = _models({"local": _backend(ollama_url)}, "local").model_dump(mode="json")
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    for name, value in {**base_secrets(), "PLR__DATABASE__PASSWORD": TEST_DB_PASSWORD}.items():
        monkeypatch.setenv(name, value)

    result = CliRunner().invoke(app, ["models", "health", "--config", str(config)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["healthy"] is True
    assert [r["role"] for r in report["roles"]] == [
        "embedding",
        "bulk_classifier",
        "reasoner",
        "writer",
        "critic",
    ]
    assert report["warnings"] == [
        "critic uses the same backend and model as writer; an independent model is recommended"
    ]

    backends = data["models"]["backends"]
    assert isinstance(backends, dict)
    backends["local"]["base_url"] = "http://127.0.0.1:1/v1"  # nothing listens here
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    failing = CliRunner().invoke(app, ["models", "health", "--config", str(config)])
    assert failing.exit_code == 1
    assert all(not r["ok"] for r in json.loads(failing.stdout)["roles"])
