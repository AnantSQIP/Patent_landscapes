"""Canonical JSON: one byte-exact encoding for hashing and exact comparison.

Allowed values are ``None``, ``bool``, ``int``, ``str``, ``Decimal``, ``list``/``tuple`` and
``dict`` with ``str`` keys. **Floats are rejected**: binary floating point makes "exactly
equal" depend on the implementation, which would defeat dual computation (Layer 3). Exact
fractional values must be ``Decimal``s. A Decimal encodes as ``{"$decimal": "<normalised>"}``,
so ``Decimal("0.50")`` and ``Decimal("0.5")`` encode identically.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from patsquire_plr.errors import PlrError


class CanonicalValueError(PlrError):
    """A value cannot be canonically encoded (e.g. a float, NaN or a non-string key)."""


def to_canonical(value: object, path: str = "$") -> object:
    """Convert ``value`` to plain JSON types, applying the rules in the module docstring."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalValueError(f"{path}: floats are not allowed; use Decimal ({value!r})")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalValueError(f"{path}: non-finite Decimal {value!r}")
        normalized = value.normalize()
        text = format(normalized, "f")
        return {"$decimal": "0" if text in ("-0", "0") else text}
    if isinstance(value, list | tuple):
        return [to_canonical(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalValueError(f"{path}: dict keys must be str, got {key!r}")
            out[key] = to_canonical(item, f"{path}.{key}")
        return out
    raise CanonicalValueError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        to_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
