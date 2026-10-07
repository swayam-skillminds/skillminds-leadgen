"""Legacy ``config.yaml`` files: their implicit defaults, and conversion to
run configs / presets.

Configs written before Phase 1 leaned on values the code supplied silently:
every run was in India, the city defaulted to Mumbai, the map centre to
Mumbai's, and the state name "Maharashtra" plus the words "mumbai" / "india"
were stripped from every business name. Those values now live here, applied
only to a file in the legacy format, so the code itself assumes no country
or city -- and a legacy run still behaves exactly as it did.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

#: What the pre-Phase-1 code assumed when a legacy config said nothing.
LEGACY_COUNTRY = "IN"
LEGACY_CITY = "Mumbai"
LEGACY_CENTER = [19.0760, 72.8777]          # Mumbai
LEGACY_ADMIN_AREA = "Maharashtra"
#: Place words the old code stripped from every name and SERP title, whatever
#: the run's city ("india" and "maharashtra" now come from country/admin area).
LEGACY_PLACE_NOISE = ["mumbai"]


def is_legacy(data: dict[str, Any]) -> bool:
    """A config without ``schema_version`` predates the run-config schema."""
    return "schema_version" not in (data or {})


def apply_legacy_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of a legacy config with every implicit default made explicit."""
    out = copy.deepcopy(data or {})
    out.setdefault("country", LEGACY_COUNTRY)
    if not out.get("city"):
        out["city"] = LEGACY_CITY
    geo = out.setdefault("geo", {}) if isinstance(out.get("geo", {}), dict) else {}
    out["geo"] = geo
    if not geo.get("center"):
        geo["center"] = list(LEGACY_CENTER)
    geo.setdefault("admin_area", LEGACY_ADMIN_AREA)
    geo.setdefault("place_noise", list(LEGACY_PLACE_NOISE))
    return out


# --- legacy file -> run config ---------------------------------------------------

#: Legacy top-level keys and where they live in a run config.
_GEO_KEYS = {
    "center": "center", "radius_km": "radius_km", "maps_zoom": "maps_zoom",
    "keywords": "keywords", "exclude_keywords": "exclude_keywords",
    "default_locality": "default_area", "admin_area": "admin_area",
    "place_noise": "place_noise", "timezone": "timezone",
}
_HOSPITALITY_KEYS = ("platform_searches", "brands", "venue_tiers", "premium_platforms",
                     "hotel_chains", "premium_hotel_chains")
_HANDLED = {"city", "country", "profile", "verticals", "geo", "localities", "areas",
            "categories", "sources", "scoring", "dedup", "runtime", "ai", "sheets",
            *_HOSPITALITY_KEYS}


def default_project_name(data: dict[str, Any]) -> str:
    """"Mumbai event vendors" / "Nashik venues": what the legacy profile meant."""
    city = str(data.get("city") or LEGACY_CITY)
    venues = str(data.get("profile") or "vendors").lower() == "venues"
    return f"{city} venues" if venues else f"{city} event vendors"


def legacy_to_document(raw: dict[str, Any], *, name: str | None = None,
                       project: str | None = None, source: str | None = None) -> dict[str, Any]:
    """A run-config document (a plain dict) equivalent to a legacy config.

    Lossless by construction: every key the legacy file set lands somewhere,
    and ``config.compile`` maps it straight back.
    """
    data = apply_legacy_defaults(raw)
    title = name or default_project_name(data)
    venues = str(data.get("profile") or "vendors").lower() == "venues"

    geo_in = data.get("geo") or {}
    geography: dict[str, Any] = {"country": str(data["country"]).upper(),
                                 "places": [str(data["city"])]}
    for legacy_key, key in _GEO_KEYS.items():
        if legacy_key in geo_in:
            geography[key] = copy.deepcopy(geo_in[legacy_key])
    unknown_geo = set(geo_in) - set(_GEO_KEYS)
    if unknown_geo:
        raise ValueError(f"legacy geo keys with no run-config home: {sorted(unknown_geo)}")
    areas = data.get("localities") or data.get("areas") or []
    if areas:
        geography["areas"] = copy.deepcopy(areas)

    categories = []
    for item in data.get("categories") or []:
        item = copy.deepcopy(item)
        if "only_localities" in item:
            item["only_areas"] = item.pop("only_localities")
        categories.append(item)

    local_business: dict[str, Any] = {
        "enabled": True,
        "verticals": list(data.get("verticals")
                          or (["hospitality", "events"] if venues else ["events"])),
        "categories": categories,
        "sources": copy.deepcopy(data.get("sources") or {}),
    }
    if "scoring" in data:
        local_business["scoring"] = copy.deepcopy(data["scoring"])
    if "dedup" in data:
        local_business["dedup"] = copy.deepcopy(data["dedup"])
    hospitality = {key: copy.deepcopy(data[key]) for key in _HOSPITALITY_KEYS if key in data}
    if hospitality:
        local_business["hospitality"] = hospitality
    legacy_rest = {key: copy.deepcopy(value) for key, value in data.items() if key not in _HANDLED}
    if legacy_rest:
        local_business["legacy"] = legacy_rest

    ai_in = copy.deepcopy(data.get("ai") or {})
    ai: dict[str, Any] = {}
    for key, value in ai_in.items():
        ai[{"backend": "provider", "max_cost_usd": "budget_usd"}.get(key, key)] = value

    sheets_in = copy.deepcopy(data.get("sheets") or {})
    output: dict[str, Any] = {
        # The legacy run always wrote its CSV; the Excel workbook is the
        # deliverable from Phase 4 on.
        "excel": True, "csv": True, "json": False,
        "google_sheets": bool(sheets_in.pop("enabled", False)),
    }
    if sheets_in:
        output["sheets"] = sheets_in

    document: dict[str, Any] = {
        "schema_version": 1,
        "name": title,
        "project": project or title,
        "description": (f"Imported from the legacy config {source}." if source
                        else "Imported from a legacy config."),
        "geography": geography,
        "lead_types": {"local_business": local_business},
        # The legacy score is the whole score: rankings stay exactly as they were.
        "scoring": {"weights": {"fit": 1.0, "intent": 0.0, "reach": 0.0}},
        "ai": ai,
        "output": output,
        "runtime": copy.deepcopy(data.get("runtime") or {}),
    }
    return document


def import_legacy_file(path: str | Path, **kwargs) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not is_legacy(raw):
        raise ValueError(f"{path} is already a run config (it has schema_version)")
    return legacy_to_document(raw, source=kwargs.pop("source", path.name), **kwargs)


def dump_document(document: dict[str, Any]) -> str:
    """YAML for a run-config document, in its documented key order."""
    header = ("# Run config (schema_version 1) -- see leadscraper/config/schema.py.\n"
              "# Validate: python -m leadscraper config validate <this file>\n")
    return header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100,
                                   default_flow_style=None)
