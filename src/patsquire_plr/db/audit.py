"""Hash-chained, append-only audit log (build prompt §8 Layer 8).

Each event's ``hash`` is SHA-256 over the previous event's hash and the event's canonical
content. The table rejects UPDATE/DELETE/TRUNCATE (trigger), and ``verify_chain`` detects
any change made by someone bypassing the trigger (e.g. a superuser disabling it).
Appends are serialised with a transaction-level advisory lock, so concurrent writers
cannot fork the chain.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from patsquire_plr.canonical import canonical_json, to_canonical
from patsquire_plr.db.models import AuditEvent

GENESIS_HASH = "0" * 64
# Arbitrary constant identifying the audit-chain lock among advisory locks.
AUDIT_LOCK_KEY = 0x504C5241  # "PLRA"


def event_hash(
    *,
    prev_hash: str,
    run_id: uuid.UUID | None,
    occurred_at: datetime,
    step: str,
    event_type: str,
    actor: str,
    payload: dict[str, object],
) -> str:
    content = canonical_json(
        {
            "prev_hash": prev_hash,
            "run_id": None if run_id is None else str(run_id),
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
            "step": step,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        }
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def append_event(
    session: Session,
    *,
    step: str,
    event_type: str,
    actor: str,
    payload: dict[str, object],
    run_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    """Append one event in the caller's transaction; the chain lock is held until it ends."""
    moment = (occurred_at or datetime.now(UTC)).astimezone(UTC)
    # Store exactly what is hashed; this also rejects floats before anything is written.
    stored_payload = to_canonical(payload)
    if not isinstance(stored_payload, dict):  # pragma: no cover - payload is typed as a dict
        raise TypeError("audit payload must be a JSON object")
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIT_LOCK_KEY})
    last_hash = session.scalar(select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1))
    prev_hash = last_hash or GENESIS_HASH
    event = AuditEvent(
        run_id=run_id,
        occurred_at=moment,
        step=step,
        event_type=event_type,
        actor=actor,
        payload=stored_payload,
        prev_hash=prev_hash,
        hash=event_hash(
            prev_hash=prev_hash,
            run_id=run_id,
            occurred_at=moment,
            step=step,
            event_type=event_type,
            actor=actor,
            payload=stored_payload,
        ),
    )
    session.add(event)
    session.flush()
    return event


class ChainVerification(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    ok: bool
    events_checked: int
    first_bad_seq: int | None
    reason: str | None


def verify_chain(session: Session) -> ChainVerification:
    """Recompute every hash in order. The first inconsistency is reported, never skipped."""
    expected_prev = GENESIS_HASH
    checked = 0
    for event in session.scalars(select(AuditEvent).order_by(AuditEvent.seq)):
        checked += 1
        if event.prev_hash != expected_prev:
            return ChainVerification(
                ok=False,
                events_checked=checked,
                first_bad_seq=event.seq,
                reason="prev_hash does not match the preceding event (event removed or reordered)",
            )
        recomputed = event_hash(
            prev_hash=event.prev_hash,
            run_id=event.run_id,
            occurred_at=event.occurred_at,
            step=event.step,
            event_type=event.event_type,
            actor=event.actor,
            payload=event.payload,
        )
        if recomputed != event.hash:
            return ChainVerification(
                ok=False,
                events_checked=checked,
                first_bad_seq=event.seq,
                reason="content does not match its hash (event modified)",
            )
        expected_prev = event.hash
    return ChainVerification(ok=True, events_checked=checked, first_bad_seq=None, reason=None)
