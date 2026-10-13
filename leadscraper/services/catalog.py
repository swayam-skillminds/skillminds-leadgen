"""The catalog: which lead types and sources exist, where they apply, how healthy they are.

``lead_types(country)`` and ``sources(country, verticals)`` answer what a UI
needs to offer a user: each entry's description, where it applies (and if
not, why not -- the same reason a run logs as "not built"), its default
settings, its terms note and last live check (``sources/notes.py``), and its
recent health.

**Source health** (Phase 3.4) rolls up the most recent finished runs of the
workspace (default 20):

* ``units`` / ``productive`` / ``empty`` / ``failed`` -- work units the
  source ran, those that found something, found nothing, or crashed;
* ``failures`` by reason, and ``blocks`` (blocked, login wall, CAPTCHA);
* ``success_rate`` = productive / units, ``failure_rate`` = failed / units,
  ``block_rate`` = blocks / (units + blocks) -- enrichers, which have no
  units, report failures and blocks only;
* ``status``: ``likely_broken`` when, in each of the source's last three
  runs, it found nothing and recorded a "nothing parsed" reason
  (``no_listings``, ``no_results``, ``no_businesses_parsed``, ...) -- the
  page shape probably changed; ``degraded`` when its block or failure rate
  is 30 % or more; ``ok``; or ``unknown`` without data.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from ..db.models import FailureRow, Run, WorkUnitRow
from ..db.runs import TERMINAL
from .workspace import Workspace

RUNS = Run.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__

BLOCK_REASONS = frozenset({"blocked", "login_wall", "captcha", "rate_limited"})
EMPTY_REASONS = frozenset({"no_listings", "no_results", "no_businesses_parsed", "no_properties",
                           "no_property_data", "no_maps_result"})
HEALTH_WINDOW = 20
#: A source that needs an API key nobody configured (Phase 7): it is skipped, never tried.
KEY_NOT_CONFIGURED = "key_not_configured"


# --- health ---------------------------------------------------------------------------------

def source_health(engine: Engine, ws: Workspace, *, window: int = HEALTH_WINDOW
                  ) -> dict[str, dict[str, Any]]:
    """Rolling health per source over the workspace's last `window` finished runs."""
    with engine.connect() as conn:
        run_ids = [int(r[0]) for r in conn.execute(
            select(RUNS.c.id).where(RUNS.c.workspace_id == ws.id,
                                    RUNS.c.kind.in_(["run", "preview"]),
                                    RUNS.c.status.in_(list(TERMINAL)))
            .order_by(RUNS.c.id.desc()).limit(window))]
        if not run_ids:
            return {}
        units = conn.execute(select(UNITS.c.run_id, UNITS.c.source, UNITS.c.status,
                                    UNITS.c.found).where(
            UNITS.c.run_id.in_(run_ids), UNITS.c.status.not_in(["skipped", "pending"]))).all()
        failures = conn.execute(select(FAILURES.c.run_id, FAILURES.c.source, FAILURES.c.reason,
                                       func.count()).where(FAILURES.c.run_id.in_(run_ids))
                                .group_by(FAILURES.c.run_id, FAILURES.c.source,
                                          FAILURES.c.reason)).all()
    tally: dict[str, dict[str, Any]] = {}

    def entry(source: str) -> dict[str, Any]:
        return tally.setdefault(source, {"runs": set(), "units": 0, "productive": 0,
                                         "empty": 0, "failed": 0, "failures": {},
                                         "blocks": 0, "per_run": {}})

    def run_of(e: dict[str, Any], run_id: int) -> dict[str, Any]:
        return e["per_run"].setdefault(run_id, {"found": 0, "nothing_parsed": False})

    for run_id, source, status, found in units:
        e = entry(source)
        e["runs"].add(int(run_id))
        e["units"] += 1
        run_of(e, int(run_id))["found"] += int(found or 0)
        if status == "failed":
            e["failed"] += 1
        elif int(found or 0) > 0:
            e["productive"] += 1
        else:
            e["empty"] += 1
    for run_id, source, reason, count in failures:
        e = entry(source)
        e["runs"].add(int(run_id))
        e["failures"][reason] = e["failures"].get(reason, 0) + int(count)
        if reason in BLOCK_REASONS:
            e["blocks"] += int(count)
        if reason in EMPTY_REASONS:
            run_of(e, int(run_id))["nothing_parsed"] = True
    out = {}
    for source, e in tally.items():
        units_n = e["units"]
        success = e["productive"] / units_n if units_n else None
        failure = e["failed"] / units_n if units_n else None
        block = e["blocks"] / (units_n + e["blocks"]) if (units_n + e["blocks"]) else None
        recent = sorted(e["per_run"].items(), reverse=True)[:3]
        broken = len(recent) == 3 and all(r["found"] == 0 and r["nothing_parsed"]
                                          for _, r in recent)
        if broken:
            status = "likely_broken"
        elif (block or 0) >= 0.3 or (failure or 0) >= 0.3:
            status = "degraded"
        else:
            status = "ok"
        out[source] = {
            "status": status, "runs": len(e["runs"]), "units": units_n,
            "productive": e["productive"], "empty": e["empty"], "failed": e["failed"],
            "failures": dict(sorted(e["failures"].items())), "blocks": e["blocks"],
            "success_rate": round(success, 3) if success is not None else None,
            "failure_rate": round(failure, 3) if failure is not None else None,
            "block_rate": round(block, 3) if block is not None else None,
        }
    return out


# --- catalog --------------------------------------------------------------------------------

def _columns_cfg(country: str | None):
    from ..core.country import country as country_info, is_known_region

    currency = "INR"
    if country and is_known_region(country):
        try:
            currency = country_info(country).currency
        except Exception:
            pass
    return SimpleNamespace(profile="vendors", currency=currency)


def lead_types(country: str | None = None) -> list[dict[str, Any]]:
    from ..leadtypes import registry

    code = (country or "").upper() or None
    out = []
    for lt in registry.all_lead_types():
        countries = sorted(lt.applicable_countries) if lt.applicable_countries else None
        applicable = code is None or countries is None or code in countries
        try:
            columns = [{"key": c.key, "header": c.header, "kind": c.kind, "field": c.field}
                       for c in lt.columns(_columns_cfg(code))]
        except Exception:
            columns = []
        out.append({
            "key": lt.key, "title": lt.title, "description": getattr(lt, "description", ""),
            "applicable_countries": countries, "applicable": applicable,
            "reason": None if applicable else f"{lt.key} covers {', '.join(countries or [])}",
            "signals": [{"type": s.type, "title": s.title, "default_strength": s.default_strength,
                         "half_life_days": s.half_life_days, "description": s.description}
                        for s in lt.signal_specs()],
            "columns": columns,
            "config_schema": lt.config_model.model_json_schema(),
        })
    return out


def sources(engine: Engine | None = None, ws: Workspace | None = None, *,
            country: str | None = None, verticals: list[str] | None = None,
            with_health: bool = True) -> list[dict[str, Any]]:
    from ..leadtypes.local_business.config import DEFAULT_SOURCES
    from ..packs import inactive_reason, pack
    from ..sources.notes import note
    from ..sources.registry import DISCOVERY_SOURCES, ENRICHMENT_SOURCES

    health = source_health(engine, ws) if (with_health and engine is not None
                                           and ws is not None) else {}
    code = (country or "").upper() or None
    out = []
    for kind, registry in (("discovery", DISCOVERY_SOURCES), ("enrichment", ENRICHMENT_SOURCES)):
        for name, cls in registry.items():
            reason = inactive_reason(cls, code, verticals or []) if code is not None else None
            owner = pack(getattr(cls, "pack", None) or "")
            countries = getattr(cls, "applicable_countries", None)
            if countries is None and owner is not None and owner.applicable_countries:
                countries = owner.applicable_countries
            out.append({
                "name": name, "kind": kind, "pack": getattr(cls, "pack", None) or "core",
                "lead_type": "local_business",
                "applicable": reason is None, "reason": reason,
                "applicable_countries": sorted(countries) if countries else None,
                "applicable_verticals": sorted(owner.applicable_verticals)
                if owner is not None and owner.applicable_verticals else None,
                "scope": getattr(cls, "scope", "combo"),
                "priority": getattr(cls, "priority", 100),
                "uses_browser": bool(getattr(cls, "uses_browser", False)),
                "fills": sorted(cls.fills) if getattr(cls, "fills", None) else None,
                "defaults": DEFAULT_SOURCES.get(name, {}),
                **note(name),
                "health": health.get(name, {"status": "unknown"}),
            })
    out.extend(_lead_type_sources(health, code))
    return out


def _lead_type_sources(health: dict[str, Any], country: str | None) -> list[dict[str, Any]]:
    """Sources a lead type runs itself (Phase 6: job boards, the site audit), from its
    ``catalog_sources()``, in the same shape as the shared registry's."""
    from ..leadtypes import registry
    from ..sources.notes import note

    out = []
    for lt in registry.all_lead_types():
        try:
            entries = lt.catalog_sources()
        except Exception:
            continue
        for entry in entries:
            countries = entry.get("applicable_countries") or lt.applicable_countries
            applicable = country is None or not countries or country in countries
            row = {
                "name": entry["name"], "kind": entry.get("kind", "discovery"),
                "pack": entry.get("pack") or "core", "lead_type": lt.key,
                "applicable": applicable,
                "reason": None if applicable else f"{entry['name']} covers "
                                                  f"{', '.join(sorted(countries))}",
                "applicable_countries": sorted(countries) if countries else None,
                "applicable_verticals": None, "scope": entry.get("scope", "run"),
                "priority": 100, "uses_browser": False, "fills": None,
                "defaults": entry.get("defaults") or {},
                **note(entry["name"]),
                "health": health.get(entry["name"], {"status": "unknown"}),
            }
            key = entry.get("key")
            if key:
                # Phase 7: a source that only answers with an API key says whether one is
                # configured (never the key) and where to get one.
                row["key"] = dict(key)
                if not key.get("configured") and row["health"].get("status") == "unknown":
                    row["health"] = {"status": KEY_NOT_CONFIGURED}
            out.append(row)
    return out
