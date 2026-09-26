"""Persistence for landscapes, taxonomy versions and approvals (append-only, ADR 0010).

"Current" is always derived: the newest taxonomy version, and for any subject the newest
approval decision. Every write is also an audit event.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from patsquire_plr.canonical import canonical_sha256
from patsquire_plr.db.audit import append_event
from patsquire_plr.db.models import Approval, Landscape, QuerySet, TaxonomyVersion
from patsquire_plr.errors import PlrError
from patsquire_plr.landscape.scope import Scope
from patsquire_plr.landscape.taxonomy import Origin, TaxonomyContent

SubjectType = Literal["taxonomy_version", "query_set"]
Decision = Literal["approved", "rejected"]
Mode = Literal["human", "automatic"]


class NotFoundError(PlrError):
    """A referenced landscape, taxonomy version or query set does not exist."""


class StaleEditError(PlrError):
    """An edit was made from a version that is no longer the newest."""


class NotApprovedError(PlrError):
    """A step needs an approval that has not been given."""


def create_landscape(engine: Engine, *, name: str, scope: Scope) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        row = Landscape(name=name, scope=scope.model_dump(mode="json"), scope_sha256=scope.sha256)
        session.add(row)
        session.flush()
        append_event(
            session,
            step="scope",
            event_type="landscape_created",
            actor="user",
            payload={"landscape_id": str(row.id), "name": name, "scope_sha256": scope.sha256},
        )
        return row.id


def get_scope(engine: Engine, landscape_id: uuid.UUID) -> tuple[Landscape, Scope]:
    with Session(engine, expire_on_commit=False) as session:
        row = session.get(Landscape, landscape_id)
    if row is None:
        raise NotFoundError(f"no landscape {landscape_id}")
    scope = Scope.model_validate(row.scope, strict=False)
    if scope.sha256 != row.scope_sha256:
        raise PlrError(f"landscape {landscape_id}: stored scope does not match its hash")
    return row, scope


def add_taxonomy_version(
    engine: Engine,
    *,
    landscape_id: uuid.UUID,
    content: TaxonomyContent,
    origin: Origin,
    scheme_id: uuid.UUID,
    cache_keys: list[str],
    parent_id: uuid.UUID | None,
    actor: str,
    require_parent_is_newest: bool = False,
) -> TaxonomyVersion:
    """Add the next version. With ``require_parent_is_newest`` (a person's edit), the
    newest version is re-read under the landscape lock and must be ``parent_id`` (or there
    must be none when it is None), so two concurrent edits can never overwrite each other."""
    payload = content.model_dump(mode="json")
    with Session(engine, expire_on_commit=False) as session, session.begin():
        if session.get(Landscape, landscape_id) is None:
            raise NotFoundError(f"no landscape {landscape_id}")
        # Serialise version numbering per landscape (the unique constraint is the backstop).
        session.execute(select(func.pg_advisory_xact_lock(landscape_id.int % (2**63))))
        newest = session.scalar(
            select(TaxonomyVersion)
            .where(TaxonomyVersion.landscape_id == landscape_id)
            .order_by(TaxonomyVersion.version.desc())
            .limit(1)
        )
        if require_parent_is_newest and (newest.id if newest else None) != parent_id:
            raise StaleEditError(
                f"the edit was made from {parent_id or 'no version'}, but the newest version "
                f"is now {newest.id if newest else 'none'}; export it again so no edit is lost"
            )
        latest = newest.version if newest else None
        row = TaxonomyVersion(
            landscape_id=landscape_id,
            version=(latest or 0) + 1,
            parent_id=parent_id,
            origin=origin,
            content=payload,
            content_sha256=canonical_sha256(payload),
            classification_scheme_id=scheme_id,
            llm_cache_keys=list(cache_keys),
        )
        session.add(row)
        session.flush()
        append_event(
            session,
            step="taxonomy",
            event_type="taxonomy_version_added",
            actor=actor,
            payload={
                "landscape_id": str(landscape_id),
                "taxonomy_version_id": str(row.id),
                "version": row.version,
                "origin": origin,
                "content_sha256": row.content_sha256,
            },
        )
        return row


def get_taxonomy(
    engine: Engine, *, version_id: uuid.UUID | None = None, landscape_id: uuid.UUID | None = None
) -> tuple[TaxonomyVersion, TaxonomyContent]:
    """A taxonomy version by ID, or the newest version of a landscape."""
    query = select(TaxonomyVersion)
    if version_id is not None:
        query = query.where(TaxonomyVersion.id == version_id)
    elif landscape_id is not None:
        query = query.where(TaxonomyVersion.landscape_id == landscape_id)
    else:
        raise ValueError("give version_id or landscape_id")
    with Session(engine, expire_on_commit=False) as session:
        row = session.scalar(query.order_by(TaxonomyVersion.version.desc()).limit(1))
    if row is None:
        raise NotFoundError(f"no taxonomy version for {version_id or landscape_id}")
    content = TaxonomyContent.model_validate(row.content, strict=False)
    if canonical_sha256(content.model_dump(mode="json")) != row.content_sha256:
        raise PlrError(f"taxonomy version {row.id}: stored content does not match its hash")
    return row, content


def record_approval(
    engine: Engine,
    *,
    subject_type: SubjectType,
    subject_id: uuid.UUID,
    decision: Decision,
    mode: Mode,
    decided_by: str,
    note: str | None,
) -> uuid.UUID:
    if not decided_by.strip():
        raise PlrError("an approval must name who decided")
    subject_model = TaxonomyVersion if subject_type == "taxonomy_version" else QuerySet
    with Session(engine) as session, session.begin():
        if session.get(subject_model, subject_id) is None:
            raise NotFoundError(f"no {subject_type} {subject_id}")
        # One decision per subject at a time, stamped with the real clock (not the
        # transaction start), so "the latest decision" is well defined.
        session.execute(select(func.pg_advisory_xact_lock(subject_id.int % (2**63))))
        row = Approval(
            created_at=func.clock_timestamp(),
            subject_type=subject_type,
            subject_id=subject_id,
            decision=decision,
            mode=mode,
            decided_by=decided_by.strip(),
            note=note,
        )
        session.add(row)
        session.flush()
        append_event(
            session,
            step="approval",
            event_type=f"{subject_type}_{decision}",
            actor=decided_by.strip(),
            payload={"subject_id": str(subject_id), "mode": mode, "note": note},
        )
        return row.id


def latest_decision(
    engine: Engine, subject_type: SubjectType, subject_id: uuid.UUID
) -> Approval | None:
    with Session(engine, expire_on_commit=False) as session:
        return session.scalar(
            select(Approval)
            .where(Approval.subject_type == subject_type, Approval.subject_id == subject_id)
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )


def require_approved(engine: Engine, subject_type: SubjectType, subject_id: uuid.UUID) -> None:
    decision = latest_decision(engine, subject_type, subject_id)
    if decision is None or decision.decision != "approved":
        state = (
            "no decision" if decision is None else f"{decision.decision} by {decision.decided_by}"
        )
        raise NotApprovedError(f"{subject_type} {subject_id} is not approved ({state})")
