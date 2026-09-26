"""Official classification files, kept byte-for-byte in object storage (ADR 0010).

A scheme version is loaded once. Its file is stored content-addressed and recorded with its
source URL and SHA-256, so every CPC check names the exact official file it was made
against. Loading the same version again with different bytes is an error.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from patsquire_plr.classification.cpc import CpcScheme, parse_title_list
from patsquire_plr.db.models import ClassificationScheme
from patsquire_plr.errors import PlrError
from patsquire_plr.ingest.rawstore import RawStore

RAW_SOURCE_ID = "classification_cpc"
CONTENT_TYPE = "application/zip"


class SchemeNotLoadedError(PlrError):
    """No matching classification scheme has been loaded."""


def load_cpc_title_list(
    engine: Engine, raw_store: RawStore, content: bytes, *, source_url: str
) -> tuple[ClassificationScheme, bool]:
    """Store and record a CPC title list. Returns the row and whether it was newly added."""
    scheme = parse_title_list(content)  # validates the whole file first
    sha256 = hashlib.sha256(content).hexdigest()
    with Session(engine, expire_on_commit=False) as session, session.begin():
        existing = session.scalar(
            select(ClassificationScheme).where(
                ClassificationScheme.scheme == "cpc",
                ClassificationScheme.version == scheme.version,
            )
        )
        if existing is not None:
            if existing.sha256 != sha256:
                raise PlrError(
                    f"CPC {scheme.version} is already loaded from a different file "
                    f"(sha256 {existing.sha256}); versions are never replaced"
                )
            return existing, False
        object_key, stored_sha = raw_store.put(RAW_SOURCE_ID, content, CONTENT_TYPE)
        row = ClassificationScheme(
            scheme="cpc",
            version=scheme.version,
            source_url=source_url,
            object_key=object_key,
            sha256=stored_sha,
            byte_size=len(content),
            entry_count=len(scheme),
        )
        session.add(row)
    return row, True


def open_cpc_scheme(
    engine: Engine,
    raw_store: RawStore,
    *,
    version: str | None = None,
    scheme_id: uuid.UUID | None = None,
) -> tuple[ClassificationScheme, CpcScheme]:
    """The requested CPC version (by version, by ID, or else the newest), hash-verified."""
    query = select(ClassificationScheme).where(ClassificationScheme.scheme == "cpc")
    if version is not None:
        query = query.where(ClassificationScheme.version == version)
    if scheme_id is not None:
        query = query.where(ClassificationScheme.id == scheme_id)
    with Session(engine, expire_on_commit=False) as session:
        row = session.scalar(query.order_by(ClassificationScheme.version.desc()).limit(1))
    if row is None:
        raise SchemeNotLoadedError(
            "no CPC title list loaded; run `plr cpc load CPCTitleList<YYYYMM>.zip` "
            f"(requested version: {version or 'newest'})"
        )
    scheme = parse_title_list(raw_store.get(row.object_key, row.sha256))
    if scheme.version != row.version or len(scheme) != row.entry_count:
        raise PlrError(f"stored CPC file does not match its record {row.id}")
    return row, scheme
