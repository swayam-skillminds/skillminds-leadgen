"""Run config -> ``Config``: the runtime view the local_business sources read.

Sources and the pipeline were written against dotted paths ("sources.x.y",
"geo.center", "localities"). Rather than rewrite every source, a run config
compiles into that shape. The mapping is the exact inverse of the legacy
importer, so legacy file -> preset -> compiled view reproduces the original
file key for key (tests/test_config_schema.py proves it on both presets).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from ..paths import data_dir
from .legacy import Config
from .schema import RunConfig

_GEO_KEYS = {
    "center": "center", "radius_km": "radius_km", "maps_zoom": "maps_zoom",
    "keywords": "keywords", "exclude_keywords": "exclude_keywords",
    "default_area": "default_locality", "admin_area": "admin_area",
    "place_noise": "place_noise", "timezone": "timezone",
}
_HOSPITALITY_KEYS = ("platform_searches", "brands", "venue_tiers", "premium_platforms",
                     "hotel_chains", "premium_hotel_chains")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "project"


def _plain(value: Any) -> Any:
    """Tuples (pydantic's coordinate type) back to the lists YAML gave."""
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _dump(model) -> dict[str, Any]:
    return _plain(model.model_dump(mode="python", exclude_unset=True))


def compile_dict(rc: RunConfig) -> dict[str, Any]:
    from ..leadtypes.local_business.config import DEFAULT_SOURCES

    geo = rc.geography
    data: dict[str, Any] = {
        "name": rc.name,
        "project": rc.project,
        "city": geo.places[0],
        "places": list(geo.places),
        "country": geo.country,
    }

    geo_dump = _dump(geo)
    geo_out = {legacy: geo_dump[key] for key, legacy in _GEO_KEYS.items() if key in geo_dump}
    if geo_out:
        data["geo"] = geo_out

    if geo.areas:
        data["localities"] = [_dump(area) for area in geo.areas]
    else:
        # No sub-areas: each place is searched as a whole.
        data["localities"] = [{"name": place} for place in geo.places]

    lb = rc.lead_type("local_business")
    if lb is not None:
        data["verticals"] = list(lb.verticals)
        data["profile"] = "venues" if lb.is_venue_run else "vendors"
        categories = []
        for category in lb.categories:
            item = _dump(category)
            if "only_areas" in item:
                item["only_localities"] = item.pop("only_areas")
            if "queries" not in item:
                item["queries"] = [category.name]
            categories.append(item)
        data["categories"] = categories
        data["sources"] = (_dump(lb)["sources"] if "sources" in lb.model_fields_set
                           else copy.deepcopy(DEFAULT_SOURCES))
        if lb.scoring is not None:
            data["scoring"] = _dump(lb.scoring)
        if lb.dedup is not None:
            data["dedup"] = _dump(lb.dedup)
        if lb.hospitality is not None:
            hospitality = _dump(lb.hospitality)
            for key in _HOSPITALITY_KEYS:
                if key in hospitality:
                    data[key] = hospitality[key]
        for key, value in lb.legacy.items():
            data.setdefault(key, copy.deepcopy(value))

    data["score_weights"] = rc.scoring.weights.model_dump()

    runtime = _dump(rc.runtime)
    if "csv_export" not in runtime or "failure_log" not in runtime:
        # A new project writes into its own folder, never over a legacy run's
        # data/leads.csv.
        base = data_dir() / "projects" / slug(rc.project)
        runtime.setdefault("csv_export", str(base / "export.csv"))
        runtime.setdefault("failure_log", str(base / "logs" / "failures.jsonl"))
        runtime.setdefault("run_log", str(base / "logs" / "run.log"))
        runtime.setdefault("ai_cache", str(data_dir() / "cache" / "ai_cache.sqlite3"))
    data["runtime"] = runtime

    ai_out: dict[str, Any] = {}
    for key, value in _dump(rc.ai).items():
        ai_out[{"provider": "backend", "budget_usd": "max_cost_usd"}.get(key, key)] = value
    ai_out.setdefault("enabled", rc.ai.enabled)
    data["ai"] = ai_out

    sheets = _dump(rc.output.sheets) if rc.output.sheets is not None else {}
    data["sheets"] = {"enabled": rc.output.google_sheets, **sheets}
    data["output"] = rc.output.model_dump(mode="json")
    data["fields"] = rc.fields.model_dump(mode="json")
    data["filters"] = rc.filters.model_dump(mode="json")
    data["icp"] = rc.icp.model_dump(mode="json")
    data["depth"] = rc.depth
    data["time_budget_minutes"] = rc.time_budget_minutes
    return data


def compile_run_config(rc: RunConfig, path: Path | None = None) -> Config:
    config = Config(compile_dict(rc), path)
    config.run_config = rc
    return config
