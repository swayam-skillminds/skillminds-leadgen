"""Runs: create, list, inspect, control, re-run, compare (Phase 3.1, 3.3).

The API never runs anything itself (A.3: the API process never scrapes). A
run created here is a ``queued`` row with its immutable config snapshot and
its request (areas, categories, sources, depth, budget); a worker
(``engine/worker.py``) claims it and executes it through the same
``engine.runner.execute`` the CLI uses.

History is read back from the run row and its children: the snapshot,
timings, estimate vs. actual, counts per lead type and per source, failures,
AI usage, generated files and the event stream.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.engine import Engine

from ..db.models import (
    AIUsage, FailureRow, Organization, Project, Run, RunOrganization, WorkUnitRow,
)
from ..db.runs import TERMINAL, TransitionError, request_control, start_run, update_run
from .configs import validate_document
from .errors import Conflict, Invalid, NotFound
from .query import Page, aware, clamp, fetch_page, order_by
from .workspace import Workspace

RUNS = Run.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__
AI = AIUsage.__table__
PROJECTS = Project.__table__
ORGS = Organization.__table__
MEMBERS = RunOrganization.__table__

#: Run-level options a request may set on top of the config.
OVERRIDE_KEYS = ("depth", "time_budget_minutes", "areas", "categories", "sources", "enrichers",
                 "lead_types", "force_enrich", "discovery_workers")
RUN_SORTABLE = {"id": RUNS.c.id, "created_at": RUNS.c.created_at,
                "finished_at": RUNS.c.finished_at, "status": RUNS.c.status}


# --- building a run --------------------------------------------------------------------

def build_request(document: dict[str, Any], overrides: dict[str, Any] | None = None, *,
                  kind: str = "run"):
    """A validated RunRequest for `document` with `overrides` (no network, no DB)."""
    from ..config.compile import compile_run_config
    from ..engine.runner import RunRequest
    from ..sources.registry import DISCOVERY_SOURCES, ENRICHMENT_SOURCES

    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    unknown = set(overrides) - set(OVERRIDE_KEYS)
    if unknown:
        raise Invalid(f"unknown run option(s): {', '.join(sorted(unknown))}")
    rc = validate_document(document)
    config = compile_run_config(rc)
    try:
        localities = (config.select_localities(overrides["areas"])
                      if overrides.get("areas") else None)
        categories = (config.select_categories(overrides["categories"])
                      if overrides.get("categories") else None)
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    for key, registry in (("sources", DISCOVERY_SOURCES), ("enrichers", ENRICHMENT_SOURCES)):
        names = overrides.get(key)
        if names:
            unknown_sources = {n.strip().lower() for n in names} - set(registry) - {"all"}
            if unknown_sources:
                raise Invalid(f"unknown {key}: {sorted(unknown_sources)}; available: "
                              f"{sorted(registry)}")
    lead_types = overrides.get("lead_types")
    if lead_types:
        missing = set(lead_types) - set(rc.enabled_lead_types())
        if missing:
            raise Invalid(f"lead type(s) not enabled in the config: {sorted(missing)}")
    depth = overrides.get("depth")
    if depth is not None and depth not in ("quick", "balanced", "deep"):
        raise Invalid("depth must be quick, balanced or deep")
    budget = overrides.get("time_budget_minutes")
    if budget is not None and float(budget) <= 0:
        raise Invalid("time_budget_minutes must be positive")
    return RunRequest(config=config, localities=localities, categories=categories,
                      source_names=overrides.get("sources"),
                      enricher_names=overrides.get("enrichers"),
                      lead_types=lead_types, depth=depth,
                      time_budget_minutes=float(budget) if budget is not None else None,
                      force_enrich=bool(overrides.get("force_enrich")),
                      discovery_workers=overrides.get("discovery_workers"),
                      install_signals=False, kind=kind)


def _project_row(engine: Engine, ws: Workspace, project_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(PROJECTS).where(
            PROJECTS.c.id == project_id, PROJECTS.c.workspace_id == ws.id)).mappings().first()
    if row is None:
        raise NotFound(f"no project {project_id}")
    return dict(row)


def resolve_document(engine: Engine, ws: Workspace, *, document: dict[str, Any] | None = None,
                     saved_config_id: int | None = None, version: int | None = None,
                     project_id: int | None = None) -> tuple[dict[str, Any], int | None]:
    """(document, saved-config version row id) for a run request."""
    from . import configs

    if (document is None) == (saved_config_id is None):
        raise Invalid("give either a config document or a saved_config_id")
    version_id = None
    if saved_config_id is not None:
        saved = configs.get_config(engine, ws, saved_config_id, version)
        document, version_id = saved["document"], saved["version_id"]
    document = copy.deepcopy(document)
    if project_id is not None:
        document["project"] = _project_row(engine, ws, project_id)["name"]
    return document, version_id


def queue_request(engine: Engine, ws: Workspace, request, *, saved_config_id: int | None = None,
                  extra: dict[str, Any] | None = None) -> int:
    """Insert a queued run for `request` (the worker picks it up); returns its id."""
    from ..engine.runner import _snapshot, request_document
    from .projects import ensure_project

    project = ensure_project(engine, ws, str(request.config.get("project")))
    document = request_document(request)
    document.update(extra or {})
    run_id = start_run(engine, ws.id, project["id"], kind=request.kind,
                       config_snapshot=_snapshot(request.config), status="queued",
                       request=document)
    if saved_config_id is not None:
        update_run(engine, run_id, saved_config_id=saved_config_id)
    return run_id


def create_run(engine: Engine, ws: Workspace, *, document: dict[str, Any] | None = None,
               saved_config_id: int | None = None, version: int | None = None,
               project_id: int | None = None, overrides: dict[str, Any] | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue a run of a config (a document, or a saved config's version)."""
    document, version_id = resolve_document(engine, ws, document=document,
                                            saved_config_id=saved_config_id, version=version,
                                            project_id=project_id)
    request = build_request(document, overrides)
    run_id = queue_request(engine, ws, request, saved_config_id=version_id, extra=extra)
    return get_run(engine, ws, run_id)


# --- reading -----------------------------------------------------------------------------

def run_row(engine: Engine, ws: Workspace, run_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(RUNS).where(RUNS.c.id == run_id,
                                              RUNS.c.workspace_id == ws.id)).mappings().first()
    if row is None:
        raise NotFound(f"no run {run_id}")
    return dict(row)


def _summary(row: dict[str, Any], project: str | None) -> dict[str, Any]:
    stats = row.get("stats") or {}
    engine_stats = stats.get("engine") or {}
    estimate = row.get("estimate") or {}
    basis = estimate.get("budget") or estimate
    snapshot = row.get("config_snapshot") or {}
    return {
        "id": int(row["id"]), "project_id": int(row["project_id"]), "project": project,
        "saved_config_id": row.get("saved_config_id"), "config_name": snapshot.get("name"),
        "kind": row["kind"], "status": row["status"], "error": row.get("error") or "",
        "created_at": aware(row["created_at"]), "started_at": aware(row.get("started_at")),
        "finished_at": aware(row.get("finished_at")),
        "heartbeat_at": aware(row.get("heartbeat_at")), "worker": row.get("worker_id"),
        "attempts": int(row.get("attempts") or 0),
        "active_seconds": round(float(row.get("active_seconds") or 0.0), 1),
        "new": int(engine_stats.get("new", 0) or 0),
        "updated": int(engine_stats.get("merged", 0) or 0),
        "units_done": engine_stats.get("units_done"),
        "units_total": engine_stats.get("units_total"),
        "estimate_p50_seconds": basis.get("p50_seconds"),
        "estimate_p90_seconds": basis.get("p90_seconds"),
    }


def list_runs(engine: Engine, ws: Workspace, *, project_id: int | None = None,
              status: list[str] | None = None, kind: str | None = None,
              saved_config_id: int | None = None, sort: str | None = None,
              limit: int | None = None, offset: int | None = None) -> Page[dict[str, Any]]:
    limit, offset = clamp(limit, offset)
    query = (select(RUNS, PROJECTS.c.name.label("project_name"))
             .join(PROJECTS, PROJECTS.c.id == RUNS.c.project_id)
             .where(RUNS.c.workspace_id == ws.id))
    if project_id is not None:
        query = query.where(RUNS.c.project_id == project_id)
    if status:
        query = query.where(RUNS.c.status.in_(list(status)))
    if kind:
        query = query.where(RUNS.c.kind == kind)
    if saved_config_id is not None:
        query = query.where(RUNS.c.saved_config_id == saved_config_id)
    query = query.order_by(*order_by(sort, RUN_SORTABLE, "-id"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    return Page([_summary(r, r["project_name"]) for r in rows], total, limit, offset)


def _group(rows) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for source, reason, count in rows:
        out.setdefault(source, {})[reason] = int(count)
    return out


def get_run(engine: Engine, ws: Workspace, run_id: int) -> dict[str, Any]:
    """Everything history keeps about a run (Phase 3.3)."""
    row = run_row(engine, ws, run_id)
    with engine.connect() as conn:
        project = conn.execute(select(PROJECTS.c.name).where(
            PROJECTS.c.id == row["project_id"])).scalar()
        units = conn.execute(select(UNITS.c.status, func.count()).where(
            UNITS.c.run_id == run_id).group_by(UNITS.c.status)).all()
        per_source = conn.execute(
            select(UNITS.c.lead_type, UNITS.c.source, func.count().label("units"),
                   func.sum(case((UNITS.c.status == "done", 1), else_=0)).label("done"),
                   func.sum(case((UNITS.c.status == "failed", 1), else_=0)).label("failed"),
                   func.sum(case((UNITS.c.status == "skipped", 1), else_=0)).label("skipped"),
                   func.sum(UNITS.c.found).label("found"), func.sum(UNITS.c.new).label("new"),
                   func.sum(UNITS.c.merged).label("merged"),
                   func.sum(UNITS.c.seconds).label("seconds"),
                   func.sum(UNITS.c.expected_seconds).label("expected_seconds"))
            .where(UNITS.c.run_id == run_id)
            .group_by(UNITS.c.lead_type, UNITS.c.source)
            .order_by(UNITS.c.lead_type, UNITS.c.source)).mappings().all()
        failures = conn.execute(select(FAILURES.c.source, FAILURES.c.reason, func.count())
                                .where(FAILURES.c.run_id == run_id)
                                .group_by(FAILURES.c.source, FAILURES.c.reason)).all()
        ai_rows = conn.execute(select(AI).where(AI.c.run_id == run_id)).mappings().all()
        members = conn.execute(select(MEMBERS.c.action, func.count()).where(
            MEMBERS.c.run_id == run_id).group_by(MEMBERS.c.action)).all()
    out = _summary(row, project)
    stats = row.get("stats") or {}
    engine_stats = stats.get("engine") or {}
    by_reason: dict[str, int] = {}
    for _, reason, count in failures:
        by_reason[reason] = by_reason.get(reason, 0) + int(count)
    out.update({
        "config": row.get("config_snapshot") or {},
        "request": row.get("request") or {},
        "checkpoint": row.get("checkpoint") or {},
        "units": {status: int(n) for status, n in units},
        "lead_types": {k: v for k, v in stats.items() if k not in ("engine", "preview")},
        "per_source": [{
            "lead_type": r["lead_type"], "source": r["source"], "units": int(r["units"]),
            "done": int(r["done"] or 0), "failed": int(r["failed"] or 0),
            "skipped": int(r["skipped"] or 0), "found": int(r["found"] or 0),
            "new": int(r["new"] or 0), "merged": int(r["merged"] or 0),
            "seconds": round(float(r["seconds"] or 0), 1),
            "expected_seconds": round(float(r["expected_seconds"] or 0), 1)}
            for r in per_source],
        "failures": {"total": sum(by_reason.values()), "by_reason": by_reason,
                     "by_source": _group(failures)},
        "organizations": {action: int(n) for action, n in members},
        "estimate": row.get("estimate") or {},
        "actual": {"elapsed_seconds": engine_stats.get("elapsed_seconds"),
                   "new": engine_stats.get("new"), "merged": engine_stats.get("merged"),
                   "units_done": engine_stats.get("units_done"),
                   "units_skipped": engine_stats.get("units_skipped"),
                   "skipped_units": engine_stats.get("skipped_units") or [],
                   "deadline_reached": engine_stats.get("deadline_reached")},
        "ai_usage": {"cost_usd": round(sum(float(r["cost_usd"] or 0) for r in ai_rows), 4),
                     "calls": sum(int(r["calls"] or 0) for r in ai_rows),
                     "rows": [{"provider": r["provider"], "model": r["model"],
                               "feature": r["feature"], "calls": int(r["calls"] or 0),
                               "cost_usd": float(r["cost_usd"] or 0)} for r in ai_rows]},
        "files": list(row.get("files") or []),
        "preview": stats.get("preview"),
    })
    return out


# --- control -----------------------------------------------------------------------------

def control_run(engine: Engine, ws: Workspace, run_id: int, action: str) -> dict[str, Any]:
    """pause / resume / cancel -- requests the runner honours on its next heartbeat."""
    run_row(engine, ws, run_id)
    try:
        request_control(engine, run_id, action)
    except TransitionError as exc:
        raise Conflict(str(exc)) from None
    return get_run(engine, ws, run_id)


def rerun(engine: Engine, ws: Workspace, run_id: int,
          overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue a new run with the same config snapshot and request (plus overrides)."""
    row = run_row(engine, ws, run_id)
    if row["kind"] != "run":
        raise Invalid(f"run {run_id} is a {row['kind']} run; only ordinary runs can be re-run")
    snapshot = row.get("config_snapshot") or {}
    if "schema_version" not in snapshot:
        raise Invalid(f"run {run_id} ran a legacy config; re-run it from the CLI")
    document = {k: v for k, v in snapshot.items() if k != "cli_overrides"}
    stored = row.get("request") or {}
    merged = {
        "areas": stored.get("localities"), "categories": stored.get("categories"),
        "sources": stored.get("source_names"), "enrichers": stored.get("enricher_names"),
        "lead_types": stored.get("lead_types"), "depth": stored.get("depth"),
        "time_budget_minutes": stored.get("time_budget_minutes"),
        "force_enrich": stored.get("force_enrich") or None,
        "discovery_workers": stored.get("discovery_workers"),
    }
    merged.update({k: v for k, v in (overrides or {}).items() if v is not None})
    request = build_request(document, merged)
    for dotted, value in (snapshot.get("cli_overrides") or {}).items():
        request.config.set(dotted, value)
    new_id = queue_request(engine, ws, request, saved_config_id=row.get("saved_config_id"),
                           extra={"rerun_of": run_id})
    return get_run(engine, ws, new_id)


# --- failures, units, events --------------------------------------------------------------

def list_failures(engine: Engine, ws: Workspace, run_id: int, *, source: str | None = None,
                  reason: str | None = None, sort: str | None = None, limit: int | None = None,
                  offset: int | None = None) -> Page[dict[str, Any]]:
    run_row(engine, ws, run_id)
    limit, offset = clamp(limit, offset)
    query = select(FAILURES).where(FAILURES.c.run_id == run_id)
    if source:
        query = query.where(FAILURES.c.source == source)
    if reason:
        query = query.where(FAILURES.c.reason == reason)
    query = query.order_by(*order_by(sort, {"id": FAILURES.c.id, "source": FAILURES.c.source,
                                            "reason": FAILURES.c.reason,
                                            "created_at": FAILURES.c.created_at}, "id"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    items = [{"id": int(r["id"]), "source": r["source"], "reason": r["reason"], "url": r["url"],
              "query": r["query"], "locality": r["locality"], "category": r["category"],
              "business": r["business"], "detail": r["detail"] or "",
              "created_at": aware(r["created_at"])} for r in rows]
    return Page(items, total, limit, offset)


def list_units(engine: Engine, ws: Workspace, run_id: int, *, status: str | None = None,
               source: str | None = None, limit: int | None = None,
               offset: int | None = None) -> Page[dict[str, Any]]:
    run_row(engine, ws, run_id)
    limit, offset = clamp(limit, offset)
    query = select(UNITS).where(UNITS.c.run_id == run_id)
    if status:
        query = query.where(UNITS.c.status == status)
    if source:
        query = query.where(UNITS.c.source == source)
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query.order_by(UNITS.c.id), limit=limit, offset=offset)
    items = [{"key": r["unit_key"], "lead_type": r["lead_type"], "source": r["source"],
              "area": r["area"], "category": r["category"], "scope": r["scope"],
              "status": r["status"], "found": r["found"], "new": r["new"],
              "merged": r["merged"], "seconds": r["seconds"],
              "expected_seconds": r["expected_seconds"], "priority": r["priority"],
              "skip_reason": r["skip_reason"] or "", "error": r["error"] or "",
              "started_at": aware(r["started_at"]), "finished_at": aware(r["finished_at"])}
             for r in rows]
    return Page(items, total, limit, offset)


def read_events(engine: Engine, ws: Workspace, run_id: int, *, after: int = 0,
                limit: int | None = None) -> list[dict[str, Any]]:
    from ..engine.events import read_events as _read

    run_row(engine, ws, run_id)
    return [e.model_dump(mode="json") for e in _read(engine, run_id, after=after, limit=limit)]


# --- diff --------------------------------------------------------------------------------

CHANGES = ("new", "updated", "unchanged", "only_in_against")


def previous_run(engine: Engine, ws: Workspace, run_id: int) -> int | None:
    """The run before `run_id` in its project that finished (the default comparison)."""
    row = run_row(engine, ws, run_id)
    with engine.connect() as conn:
        found = conn.execute(select(RUNS.c.id).where(
            RUNS.c.project_id == row["project_id"], RUNS.c.id < run_id,
            RUNS.c.kind.in_(["run", "import"]), RUNS.c.status.in_(list(TERMINAL)))
            .order_by(RUNS.c.id.desc()).limit(1)).first()
    return int(found[0]) if found else None


def _end_of(row: dict[str, Any]) -> datetime | None:
    return aware(row.get("finished_at") or row.get("started_at") or row.get("created_at"))


def diff_items(engine: Engine, ws: Workspace, run_id: int,
               against: int | None = None) -> tuple[int | None, list[dict[str, Any]]]:
    """(against, every organisation with its change) -- see :func:`diff_runs`."""
    row = run_row(engine, ws, run_id)
    if against is None:
        against = previous_run(engine, ws, run_id)
    other = run_row(engine, ws, against) if against is not None else None
    if other is not None and other["project_id"] != row["project_id"]:
        raise Invalid("both runs must belong to the same project")
    cutoff = _end_of(other) if other is not None else None
    columns = (MEMBERS.c.organization_id, MEMBERS.c.action, ORGS.c.created_at, ORGS.c.name,
               ORGS.c.locality, ORGS.c.category, ORGS.c.score)
    with engine.connect() as conn:
        mine = conn.execute(select(*columns).join(ORGS, ORGS.c.id == MEMBERS.c.organization_id)
                            .where(MEMBERS.c.run_id == run_id)).mappings().all()
        theirs = conn.execute(select(*columns).join(
            ORGS, ORGS.c.id == MEMBERS.c.organization_id)
            .where(MEMBERS.c.run_id == against)).mappings().all() if against is not None else []
    items: list[dict[str, Any]] = []
    seen: set[int] = set()

    def item(m, change: str) -> dict[str, Any]:
        return {"organization_id": int(m["organization_id"]), "name": m["name"],
                "locality": m["locality"], "category": m["category"],
                "score": m["score"], "change": change}

    for m in mine:
        seen.add(int(m["organization_id"]))
        created = aware(m["created_at"])
        if m["action"] == "new" or (cutoff is not None and created is not None
                                    and created > cutoff):
            change = "new"
        else:
            change = "updated" if m["action"] == "updated" else "unchanged"
        items.append(item(m, change))
    for t in theirs:
        if int(t["organization_id"]) not in seen:
            seen.add(int(t["organization_id"]))
            items.append(item(t, "only_in_against"))
    order = {c: n for n, c in enumerate(CHANGES)}
    items.sort(key=lambda i: (order[i["change"]], -(i["score"] or 0), i["organization_id"]))
    return against, items


def diff_runs(engine: Engine, ws: Workspace, run_id: int, *, against: int | None = None,
              change: str | None = None, limit: int | None = None,
              offset: int | None = None) -> dict[str, Any]:
    """What run `run_id` found, against an earlier run (default: the previous one).

    * ``new`` -- organisations that did not exist when `against` ended (created
      by this run, or by anything after `against`);
    * ``updated`` -- existed already, and this run added to them;
    * ``unchanged`` -- existed already, and this run found them again with
      nothing to add;
    * ``only_in_against`` -- `against` had them, this run did not find them.

    Organisations merged away since are reported under their survivor.
    """
    limit, offset = clamp(limit, offset)
    if change is not None and change not in CHANGES:
        raise Invalid(f"change must be one of {', '.join(CHANGES)}")
    against, items = diff_items(engine, ws, run_id, against)
    counts = {c: sum(1 for i in items if i["change"] == c) for c in CHANGES}
    if change:
        items = [i for i in items if i["change"] == change]
    return {"run_id": run_id, "against": against, "counts": counts, "total": len(items),
            "limit": limit, "offset": offset, "items": items[offset:offset + limit]}
