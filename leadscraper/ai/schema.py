"""Schema-driven output: every AI call names a pydantic model (Phase 5.6).

* :func:`strict_schema` -- the model's JSON Schema in the form native
  structured-output modes accept (OpenAI strict, Anthropic ``output_config``):
  references inlined, every property required, optional ones nullable,
  ``additionalProperties: false``.
* :func:`gemini_schema` -- the OpenAPI subset Gemini's ``responseSchema`` takes.
* :func:`validate` -- parse a model's text into the pydantic model, or None.
  Native modes only constrain the shape; the pydantic model is what we trust.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = copy.deepcopy(defs[ref.split("/")[-1]])
            extra = {k: v for k, v in node.items() if k != "$ref"}
            return _inline({**target, **extra}, defs)
        return {k: _inline(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


_DROP = ("title", "default", "examples")


def _strict(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strict(v) for k, v in node.items() if k not in _DROP}
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties", {})
        required = set(out.get("required", []))
        for name, prop in list(props.items()):
            if name not in required:
                props[name] = _nullable(prop)
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
        out.setdefault("type", "object")
    return out


def _nullable(prop: dict[str, Any]) -> dict[str, Any]:
    if "anyOf" in prop:
        if not any(p.get("type") == "null" for p in prop["anyOf"]):
            prop = {**prop, "anyOf": [*prop["anyOf"], {"type": "null"}]}
        return prop
    if prop.get("type") == "null":
        return prop
    return {"anyOf": [prop, {"type": "null"}], **({"description": prop["description"]}
                                                     if "description" in prop else {})}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    return _strict(_inline(raw, raw.get("$defs", {})))


_GEMINI_KEYS = {"type", "properties", "required", "items", "enum", "description", "nullable",
                "minItems", "maxItems", "minimum", "maximum"}


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's schema subset: no additionalProperties, nullable instead of anyOf-null."""

    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            nullable = len(options) != len(node["anyOf"])
            base = convert(options[0]) if options else {"type": "string"}
            if nullable:
                base["nullable"] = True
            if "description" in node:
                base["description"] = node["description"]
            return base
        out = {}
        for key, value in node.items():
            if key not in _GEMINI_KEYS:
                continue
            if key == "properties":
                out[key] = {name: convert(prop) for name, prop in value.items()}
            elif key == "items":
                out[key] = convert(value)
            else:
                out[key] = value
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].upper()
        return out

    return convert(schema)


def parse_json(text: str) -> Any | None:
    """Best-effort JSON from a model's text: fences stripped, outermost span tried."""
    cleaned = _FENCE.sub("", text or "").strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    pairs = sorted((("{", "}"), ("[", "]")),
                   key=lambda pair: (cleaned.find(pair[0]) == -1, cleaned.find(pair[0])))
    for opener, closer in pairs:                 # the outermost span opens first
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def validate(model: type[M], text: str) -> tuple[M | None, str]:
    """(the parsed model, "") or (None, why it isn't valid)."""
    data = parse_json(text)
    if data is None:
        return None, "not JSON"
    if "items" in model.model_fields:
        if isinstance(data, list):
            data = {"items": data}         # a bare array where an envelope was asked for
        elif isinstance(data, dict) and "items" not in data:
            lists = [v for v in data.values() if isinstance(v, list)]
            if len(lists) == 1:            # {"results": [...]}: another envelope name
                data = {"items": lists[0]}
            elif not lists and len(model.model_fields) == 1:
                data = {"items": [data]}   # one record where a list was asked for
    try:
        return model.model_validate(data), ""
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return None, f"schema: {where} {first.get('msg', '')}".strip()[:200]
