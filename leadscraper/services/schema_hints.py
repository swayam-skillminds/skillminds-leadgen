"""``GET /schema/run-config``: the run-config JSON Schema plus what a form needs (Phase 3.1).

A UI generates its config editor from this, and its wizard from ``ui``:

* ``schema`` -- the JSON Schema (``config.schema.run_config_json_schema``),
  every registered lead type's section included;
* ``ui`` -- one entry per field path (``geography.country``): label, help
  text, default, enum, widget, the wizard step it belongs to and whether it is
  an advanced setting;
* ``example`` -- a minimal valid config (a country, a place, one category);
* ``applicability`` -- for the given country and verticals, which lead types
  and sources would run, and why the others would not;
* ``events`` -- the JSON Schema of the run event stream (``GET /runs/{id}/events``).
"""

from __future__ import annotations

from typing import Any

#: Labels and help that read better than the field names; everything else
#: comes from the schema's own descriptions.
LABELS: dict[str, tuple[str, str]] = {
    "name": ("Run name", "Shown in history and on the workbook's Overview."),
    "project": ("Project", "The lead database this run merges into. Same name, same project."),
    "geography.country": ("Country", "Drives phone parsing, currency and which packs apply."),
    "geography.places": ("Places", "Cities or regions, free text. Geocoded automatically."),
    "geography.areas": ("Areas", "Optional sub-areas (suburbs, districts); empty = the whole "
                                 "place."),
    "geography.radius_km": ("Radius (km)", "Listings farther from the centre are skipped."),
    "lead_types": ("Lead types", "What kind of leads to find."),
    "lead_types.local_business.categories": ("Categories", "What to search for, e.g. Dentists."),
    "lead_types.local_business.sources": ("Sources", "Leave empty for every applicable source "
                                                     "at polite default speeds."),
    "lead_types.local_business.verticals": ("Industry verticals", "events, hospitality: switch "
                                                                  "on their packs and layouts."),
    "fields.include": ("Fields", "Unchecked fields switch off the sources that only fill them."),
    "filters.suppression_lists": ("Suppression lists", "Names of lists whose organisations are "
                                                       "excluded or flagged."),
    "scoring.weights": ("Score weights", "How fit, intent and reach combine into the score."),
    "depth": ("Depth", "quick = a fast skim, balanced = the default, deep = thorough."),
    "time_budget_minutes": ("Time budget (minutes)", "Best units first; the run ends in time. "
                                                     "Empty = run to completion."),
    "ai": ("AI", "Optional and budgeted; off by default."),
    "ai.enabled": ("Use AI", ""),
    "ai.budget_usd": ("AI budget (USD)", "A hard cap per run."),
}
STEPS = {"name": "basics", "project": "basics", "description": "basics", "geography": "where",
         "lead_types": "what", "fields": "what", "filters": "filters", "icp": "filters",
         "depth": "time", "time_budget_minutes": "time", "ai": "ai", "output": "output"}
ADVANCED = ("runtime", "scoring", "schema_version", "icp", "geography.center",
            "geography.admin_area", "geography.timezone", "geography.maps_zoom",
            "geography.keywords", "geography.exclude_keywords", "geography.default_area",
            "geography.place_noise", "lead_types.local_business.scoring",
            "lead_types.local_business.dedup", "lead_types.local_business.hospitality",
            "lead_types.local_business.legacy", "output.sheets")


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow a $ref, and unwrap ``X | None`` to X."""
    ref = node.get("$ref")
    if ref and ref.startswith("#/$defs/"):
        target = schema.get("$defs", {}).get(ref.split("/")[-1], {})
        return _resolve(schema, {**target, **{k: v for k, v in node.items() if k != "$ref"}})
    for key in ("anyOf", "allOf"):
        options = [o for o in node.get(key, []) if o.get("type") != "null"]
        if len(options) == 1:
            rest = {k: v for k, v in node.items() if k != key}
            return {**_resolve(schema, options[0]), **rest}
    return node


def _widget(node: dict[str, Any]) -> str:
    if "enum" in node or "const" in node:
        return "select"
    kind = node.get("type")
    if kind == "boolean":
        return "checkbox"
    if kind in ("integer", "number"):
        return "number"
    if kind == "array":
        return "multiselect" if "enum" in (node.get("items") or {}) else "list"
    if kind == "object":
        return "group"
    return "text"


def _title(path: str) -> str:
    return path.rsplit(".", 1)[-1].replace("_", " ").capitalize()


def ui_hints(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], path: str, depth: int, required: bool) -> None:
        node = _resolve(schema, node)
        if path:
            label, help_text = LABELS.get(path, (None, None))
            items = _resolve(schema, node.get("items") or {}) \
                if node.get("type") == "array" else {}
            if items:
                node = {**node, "items": items}
            hints[path] = {
                "label": label or _title(path),
                "help": help_text or node.get("description", ""),
                "widget": _widget(node), "default": node.get("default"),
                "enum": node.get("enum") or items.get("enum"),
                "required": required, "step": STEPS.get(path.split(".")[0], "advanced"),
                "advanced": any(path == a or path.startswith(a + ".") for a in ADVANCED),
            }
        if depth > 4:
            return
        needed = set(node.get("required") or [])
        for name, child in (node.get("properties") or {}).items():
            walk(child, f"{path}.{name}" if path else name, depth + 1, name in needed)

    walk(schema, "", 0, True)
    return hints


EXAMPLE = {
    "schema_version": 1,
    "name": "Pune dentists",
    "project": "Pune dentists",
    "geography": {"country": "IN", "places": ["Pune"]},
    "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}},
}


def applicability(country: str | None, verticals: list[str] | None) -> dict[str, Any]:
    from . import catalog

    return {
        "country": (country or "").upper() or None, "verticals": verticals or [],
        "lead_types": [{"key": lt["key"], "applicable": lt["applicable"], "reason": lt["reason"]}
                       for lt in catalog.lead_types(country)],
        "sources": [{"name": s["name"], "kind": s["kind"], "pack": s["pack"],
                     "applicable": s["applicable"], "reason": s["reason"]}
                    for s in catalog.sources(country=country, verticals=verticals,
                                             with_health=False)],
    }


def run_config_schema(country: str | None = None,
                      verticals: list[str] | None = None) -> dict[str, Any]:
    from ..config.schema import run_config_json_schema
    from ..engine.events import event_json_schema

    schema = run_config_json_schema()
    return {"schema": schema, "ui": ui_hints(schema), "example": EXAMPLE,
            "applicability": applicability(country, verticals), "events": event_json_schema()}
