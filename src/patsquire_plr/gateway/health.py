"""``plr models health``: prove every configured role answers correctly before a run.

Chat roles must return schema-valid structured output; the embedding role must return a
vector. The cache is bypassed so the backend itself is exercised; calls are still logged.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from patsquire_plr.config import MODEL_ROLES, ModelsSettings
from patsquire_plr.gateway.gateway import ModelGateway
from patsquire_plr.gateway.prompts import PromptTemplate

HEALTH_PROMPT = PromptTemplate(
    id="system.health_check",
    version="1",
    system="You are a health check. Reply with JSON only.",
    user='Return exactly this JSON object: {{"status": "ok"}}',
)


class HealthPing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


class RoleHealth(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    role: str
    backend: str
    model: str
    ok: bool
    detail: str
    latency_ms: int


def check_models(gateway: ModelGateway, models: ModelsSettings) -> list[RoleHealth]:
    results = []
    for role in MODEL_ROLES:
        cfg = models.roles[role]
        started = time.monotonic()
        try:
            if role == "embedding":
                embedded = gateway.embed(["patent landscape health check"], use_cache=False)
                detail = f"{embedded.dimensions}-dimensional embedding"
            else:
                gateway.structured(role, HEALTH_PROMPT, {}, HealthPing, use_cache=False)
                detail = "schema-valid structured output"
            ok = True
        except Exception as exc:  # noqa: BLE001 - reported per role; the command exits non-zero
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(
            RoleHealth(
                role=role,
                backend=cfg.backend,
                model=cfg.model,
                ok=ok,
                detail=detail,
                latency_ms=round((time.monotonic() - started) * 1000),
            )
        )
    return results
