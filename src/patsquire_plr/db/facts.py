"""Fact Store with dual independent computation (build prompt §8 Layer 3).

A fact is accepted only when at least two differently-named implementations produced the
**exactly** equal canonical value from the **same** input set. Every computation is
recorded, including rejected ones. A mismatch records its evidence (computations plus an
audit event) in its own committed transaction, then raises ``FactMismatchError`` so the
pipeline stops.

Lineage is the ``query_spec`` (a reproducible description of the computation, re-runnable
against the stored snapshot for drill-down) plus the SHA-256 of the sorted input IDs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.canonical import canonical_json, canonical_sha256, to_canonical
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import Fact, FactComputation
from patsquire_plr.errors import PlrError

MIN_IMPLEMENTATIONS = 2


class FactMismatchError(PlrError):
    """Independent implementations disagreed, or used different inputs. The pipeline stops."""


class Computation(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    implementation: str = Field(min_length=1, description="e.g. 'duckdb' or 'postgres'")
    value: object
    input_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def input_set_sha256(ids: Iterable[str | uuid.UUID]) -> str:
    """Order-independent fingerprint of the records a computation used."""
    return canonical_sha256(sorted(str(i) for i in ids))


def accept_fact(
    engine: Engine,
    *,
    run_id: uuid.UUID,
    metric_id: str,
    dimensions: dict[str, object],
    unit: str,
    definition_options: dict[str, object],
    query_spec: dict[str, object],
    computations: Sequence[Computation],
) -> uuid.UUID:
    """Record the computations, then accept the fact or raise ``FactMismatchError``."""
    names = [c.implementation for c in computations]
    if len(set(names)) < MIN_IMPLEMENTATIONS or len(set(names)) != len(names):
        raise ValueError(
            f"{metric_id}: need at least {MIN_IMPLEMENTATIONS} computations from distinct "
            f"implementations, got {names}"
        )
    dimensions_key = canonical_sha256(dimensions)
    canonical_values = {c.implementation: canonical_json(c.value) for c in computations}
    inputs = {c.implementation: c.input_set_sha256 for c in computations}

    with Session(engine) as session, session.begin():
        for c in computations:
            session.add(
                FactComputation(
                    run_id=run_id,
                    metric_id=metric_id,
                    dimensions_key=dimensions_key,
                    implementation=c.implementation,
                    value=to_canonical(c.value),
                    input_set_sha256=c.input_set_sha256,
                )
            )
        problem = None
        if len(set(inputs.values())) != 1:
            problem = f"implementations used different input sets: {inputs}"
        elif len(set(canonical_values.values())) != 1:
            problem = f"implementations disagree: {canonical_values}"
        if problem is not None:
            append_event(
                session,
                run_id=run_id,
                step="analytics",
                event_type="fact_mismatch",
                actor="system",
                payload={"metric_id": metric_id, "dimensions": dimensions, "problem": problem},
            )
    if problem is not None:
        raise FactMismatchError(f"{metric_id} {canonical_json(dimensions)}: {problem}")

    with Session(engine) as session, session.begin():
        fact = Fact(
            run_id=run_id,
            metric_id=metric_id,
            dimensions=to_canonical(dimensions),
            dimensions_key=dimensions_key,
            value=to_canonical(computations[0].value),
            unit=unit,
            definition_options=to_canonical(definition_options),
            query_spec=to_canonical(query_spec),
            input_set_sha256=computations[0].input_set_sha256,
            implementations=sorted(names),
        )
        session.add(fact)
        session.flush()
        append_event(
            session,
            run_id=run_id,
            step="analytics",
            event_type="fact_accepted",
            actor="system",
            payload={
                "fact_id": str(fact.id),
                "metric_id": metric_id,
                "implementations": sorted(names),
            },
        )
        return fact.id


def facts_for(session: Session, run_id: uuid.UUID, metric_id: str) -> list[Fact]:
    return list(
        session.scalars(
            select(Fact)
            .where(Fact.run_id == run_id, Fact.metric_id == metric_id)
            .order_by(Fact.dimensions_key)
        )
    )
