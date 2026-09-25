"""Hash-chained audit log and dual-computation fact acceptance on real Postgres."""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from patsquire_plr.db.audit import GENESIS_HASH, append_event, verify_chain
from patsquire_plr.db.facts import (
    Computation,
    FactMismatchError,
    accept_fact,
    facts_for,
    input_set_sha256,
)
from patsquire_plr.db.models import AnalysisRun, AuditEvent, FactComputation

pytestmark = pytest.mark.integration


def _append(engine: Engine, n: int, **payload: object) -> None:
    for i in range(n):
        with Session(engine) as session, session.begin():
            append_event(
                session, step="test", event_type="tick", actor="system", payload={"i": i, **payload}
            )


# ---------------------------------------------------------------- audit chain


def test_chain_links_from_genesis_and_verifies(db_engine: Engine) -> None:
    _append(db_engine, 3, amount=Decimal("0.25"))
    with Session(db_engine) as session:
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.seq)))
        result = verify_chain(session)

    assert events[0].prev_hash == GENESIS_HASH
    assert [e.prev_hash for e in events[1:]] == [e.hash for e in events[:-1]]
    assert result.ok
    assert result.events_checked == 3


def test_empty_chain_verifies(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        assert verify_chain(session).ok


def _tamper(engine: Engine, statement: str) -> None:
    """Simulate an attacker with superuser rights who disables the append-only trigger."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_event DISABLE TRIGGER audit_event_append_only"))
        conn.execute(text(statement))
        conn.execute(text("ALTER TABLE audit_event ENABLE TRIGGER audit_event_append_only"))


def test_modified_event_is_detected(db_engine: Engine) -> None:
    _append(db_engine, 3)
    _tamper(db_engine, "UPDATE audit_event SET payload = '{\"i\": 99}' WHERE seq = 2")
    with Session(db_engine) as session:
        result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 2
    assert result.reason is not None
    assert "modified" in result.reason


def test_deleted_event_is_detected(db_engine: Engine) -> None:
    _append(db_engine, 3)
    _tamper(db_engine, "DELETE FROM audit_event WHERE seq = 2")
    with Session(db_engine) as session:
        result = verify_chain(session)
    assert not result.ok
    assert result.first_bad_seq == 3
    assert result.reason is not None
    assert "removed or reordered" in result.reason


def test_float_payloads_are_rejected_before_writing(db_engine: Engine) -> None:
    with (
        Session(db_engine) as session,
        session.begin(),
        pytest.raises(Exception, match="floats are not allowed"),
    ):
        append_event(session, step="t", event_type="e", actor="a", payload={"x": 0.1})
    with Session(db_engine) as session:
        assert verify_chain(session).events_checked == 0


def test_concurrent_writers_keep_one_valid_chain(db_engine: Engine) -> None:
    errors: list[BaseException] = []

    def writer(tag: str) -> None:
        try:
            _append(db_engine, 10, writer=tag)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with Session(db_engine) as session:
        result = verify_chain(session)
    assert result.ok
    assert result.events_checked == 40


# ---------------------------------------------------------------- fact store (Layer 3)


def _run(engine: Engine) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        run = AnalysisRun(
            template_version=1, config={}, data_snapshot_sha256=None, status="running"
        )
        session.add(run)
        session.flush()
        return run.id


INPUTS = input_set_sha256(["fam-2", "fam-1", "fam-3"])


def _accept(engine: Engine, run_id: uuid.UUID, *computations: Computation) -> uuid.UUID:
    return accept_fact(
        engine,
        run_id=run_id,
        metric_id="families_per_year",
        dimensions={"year": 2020, "scope": "all"},
        unit="families",
        definition_options={"time_basis": "earliest_priority_year"},
        query_spec={"sql": "SELECT count(*) ...", "snapshot": "s1"},
        computations=computations,
    )


def test_input_set_hash_ignores_order() -> None:
    assert input_set_sha256(["b", "a"]) == input_set_sha256(["a", "b"])


def test_agreeing_implementations_produce_one_fact(db_engine: Engine) -> None:
    run_id = _run(db_engine)
    fact_id = _accept(
        db_engine,
        run_id,
        Computation(implementation="duckdb", value=Decimal("0.50"), input_set_sha256=INPUTS),
        Computation(implementation="postgres", value=Decimal("0.5"), input_set_sha256=INPUTS),
    )
    with Session(db_engine) as session:
        [fact] = facts_for(session, run_id, "families_per_year")
        events = [e.event_type for e in session.scalars(select(AuditEvent))]
    assert fact.id == fact_id
    assert fact.value == {"$decimal": "0.5"}
    assert fact.implementations == ["duckdb", "postgres"]
    assert events == ["fact_accepted"]


def test_disagreement_stops_and_keeps_evidence(db_engine: Engine) -> None:
    run_id = _run(db_engine)
    with pytest.raises(FactMismatchError, match="implementations disagree"):
        _accept(
            db_engine,
            run_id,
            Computation(implementation="duckdb", value=41, input_set_sha256=INPUTS),
            Computation(implementation="postgres", value=42, input_set_sha256=INPUTS),
        )
    with Session(db_engine) as session:
        assert facts_for(session, run_id, "families_per_year") == []
        kept = session.scalars(select(FactComputation.value)).all()
        events = [e.event_type for e in session.scalars(select(AuditEvent))]
    assert sorted(kept) == [41, 42]  # type: ignore[type-var]
    assert events == ["fact_mismatch"]


def test_different_input_sets_are_a_mismatch(db_engine: Engine) -> None:
    run_id = _run(db_engine)
    with pytest.raises(FactMismatchError, match="different input sets"):
        _accept(
            db_engine,
            run_id,
            Computation(implementation="duckdb", value=3, input_set_sha256=INPUTS),
            Computation(
                implementation="postgres", value=3, input_set_sha256=input_set_sha256(["other"])
            ),
        )


@pytest.mark.parametrize("names", [["duckdb"], ["duckdb", "duckdb"]])
def test_single_or_duplicate_implementation_is_refused(db_engine: Engine, names: list[str]) -> None:
    run_id = _run(db_engine)
    with pytest.raises(ValueError, match="distinct implementations"):
        _accept(
            db_engine,
            run_id,
            *(Computation(implementation=n, value=1, input_set_sha256=INPUTS) for n in names),
        )


def test_float_values_are_refused(db_engine: Engine) -> None:
    run_id = _run(db_engine)
    with pytest.raises(Exception, match="floats are not allowed"):
        _accept(
            db_engine,
            run_id,
            Computation(implementation="duckdb", value=0.5, input_set_sha256=INPUTS),
            Computation(implementation="postgres", value=0.5, input_set_sha256=INPUTS),
        )


def test_same_fact_cannot_be_accepted_twice(db_engine: Engine) -> None:
    run_id = _run(db_engine)
    agree = (
        Computation(implementation="duckdb", value=7, input_set_sha256=INPUTS),
        Computation(implementation="postgres", value=7, input_set_sha256=INPUTS),
    )
    _accept(db_engine, run_id, *agree)
    with pytest.raises(Exception, match="uq_fact_run_id_metric_id_dimensions_key"):
        _accept(db_engine, run_id, *agree)
