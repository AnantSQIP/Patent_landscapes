"""JSON Schemas for structured output.

``strict_json_schema`` derives the schema from a Pydantic output model and checks that the
model is strict-compatible: extra fields forbidden and no defaults, so every property is
required (OpenAI strict mode needs this).

Providers support different JSON Schema subsets (docs/architecture/provider_apis.md), so
each adapter sends a *sanitised* copy with unsupported keywords removed. The full schema is
still enforced locally: every response is validated against the Pydantic model, so a
stripped constraint (e.g. ``maxLength``) can never be skipped.
"""

from __future__ import annotations

import copy

from pydantic import BaseModel

from patsquire_plr.gateway.errors import GatewayError

# Keywords each provider's structured-output mode does not accept (verified 2026-09-25).
_ANTHROPIC_BEDROCK_UNSUPPORTED = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "maxItems",
    }
)
_GEMINI_SUPPORTED = frozenset(
    {
        "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description", "enum",
        "items", "prefixItems", "minItems", "maxItems", "minimum", "maximum", "anyOf", "oneOf",
        "properties", "additionalProperties", "required", "propertyOrdering",
    }
)  # fmt: skip


def strict_json_schema(model: type[BaseModel]) -> dict[str, object]:
    config = model.model_config
    if config.get("extra") != "forbid":
        raise GatewayError(f"{model.__name__}: structured-output models must set extra='forbid'")
    for name, field in model.model_fields.items():
        if not field.is_required():
            raise GatewayError(
                f"{model.__name__}.{name}: structured-output fields must be required (no "
                "defaults); express 'optional' as a nullable type"
            )
    schema = model.model_json_schema()
    _require_all_properties(schema)
    return schema


def _require_all_properties(node: object) -> None:
    """Every object: additionalProperties false and all properties required (recursively)."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            properties = node["properties"]
            if isinstance(properties, dict):
                node["required"] = list(properties)
        for value in node.values():
            _require_all_properties(value)
    elif isinstance(node, list):
        for item in node:
            _require_all_properties(item)


def _strip(
    node: object, *, drop: frozenset[str] = frozenset(), keep: frozenset[str] | None = None
) -> object:
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                out[key] = {k: _strip(v, drop=drop, keep=keep) for k, v in value.items()}
            elif key in drop or (keep is not None and key not in keep):
                continue
            else:
                out[key] = _strip(value, drop=drop, keep=keep)
        return out
    if isinstance(node, list):
        return [_strip(item, drop=drop, keep=keep) for item in node]
    return node


def _as_object(node: object) -> dict[str, object]:
    if not isinstance(node, dict):  # pragma: no cover - schemas are objects at the root
        raise GatewayError("JSON schema root must be an object")
    return node


def for_openai(schema: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy(schema)


def for_anthropic(schema: dict[str, object]) -> dict[str, object]:
    return _as_object(_strip(copy.deepcopy(schema), drop=_ANTHROPIC_BEDROCK_UNSUPPORTED))


def for_bedrock(schema: dict[str, object]) -> dict[str, object]:
    return _as_object(_strip(copy.deepcopy(schema), drop=_ANTHROPIC_BEDROCK_UNSUPPORTED))


def for_gemini(schema: dict[str, object]) -> dict[str, object]:
    return _as_object(_strip(copy.deepcopy(schema), keep=_GEMINI_SUPPORTED))
