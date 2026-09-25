"""Versioned prompt templates.

A template has an ID, an explicit version and fixed system/user text with ``{name}``
placeholders. Its SHA-256 is recorded with every call, so an edit made without bumping the
version is still visible in the call log and changes the cache key.
"""

from __future__ import annotations

import hashlib
import string
from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patsquire_plr.gateway.errors import GatewayError


class PromptTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    version: str = Field(pattern=r"^\d+(\.\d+)*$")
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)

    @model_validator(mode="after")
    def _placeholders_are_plain_names(self) -> Self:
        for field in (self.system, self.user):
            for _, name, spec, conversion in string.Formatter().parse(field):
                if name is not None and (not name.isidentifier() or spec or conversion):
                    raise ValueError(f"placeholder {{{name}}} must be a plain identifier")
        return self

    @property
    def sha256(self) -> str:
        content = f"{self.id}\n{self.version}\n{self.system}\n\x00\n{self.user}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def placeholders(self) -> set[str]:
        return {
            name
            for field in (self.system, self.user)
            for _, name, _, _ in string.Formatter().parse(field)
            if name is not None
        }

    def render(self, variables: Mapping[str, str]) -> tuple[str, str]:
        """Return (system, user). Missing or unexpected variables are errors."""
        expected = self.placeholders()
        if missing := sorted(expected - set(variables)):
            raise GatewayError(f"prompt {self.id}@{self.version}: missing variables {missing}")
        if extra := sorted(set(variables) - expected):
            raise GatewayError(f"prompt {self.id}@{self.version}: unexpected variables {extra}")
        return self.system.format_map(variables), self.user.format_map(variables)
