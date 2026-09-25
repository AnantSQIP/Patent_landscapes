"""The data-source contract (ADR 0005).

A source turns requests into raw payloads (``fetch``) and raw payloads into canonical
documents (``normalize``). The two steps are separate so a stored raw payload can always be
re-normalised later without contacting the source again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from patsquire_plr.domain.patent import PatentDocument


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceInfo(_Frozen):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_type: str
    adapter_version: str = Field(min_length=1)
    source_api_version: str = Field(min_length=1, description="provider API/format version")


class Fetched(_Frozen):
    """The outcome of fetching one requested key."""

    requested_key: str
    status: Literal["ok", "not_found", "invalid_request", "failed"]
    content: bytes | None = None
    content_type: str | None = None
    retrieved_at: datetime | None = None
    detail: str | None = None


class Normalized(_Frozen):
    documents: tuple[tuple[PatentDocument, str], ...] = Field(
        description="(document, raw_pointer) pairs"
    )
    quarantine_reasons: tuple[str, ...] = ()


class LookupSource(Protocol):
    """A source that retrieves known publications by number."""

    @property
    def info(self) -> SourceInfo: ...

    def fetch(self, keys: Sequence[str]) -> Iterator[Fetched]: ...

    def normalize(
        self, content: bytes, *, raw_record_id: uuid.UUID, retrieved_at: datetime
    ) -> Normalized: ...


def build_source(source_id: str, settings: object) -> LookupSource:
    """Construct the adapter for a configured data source."""
    from patsquire_plr.config import GooglePatentsPageSettings  # noqa: PLC0415 - avoids a cycle
    from patsquire_plr.ingest.google_patents import GooglePatentsPageSource  # noqa: PLC0415

    if isinstance(settings, GooglePatentsPageSettings):
        return GooglePatentsPageSource(source_id, settings)
    raise TypeError(f"data source {source_id}: unsupported settings {type(settings).__name__}")
