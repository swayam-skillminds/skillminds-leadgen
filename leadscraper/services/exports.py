"""Exports, regenerated from the database at any time (Phase 3.1, Phase 4).

Re-exporting never needs a re-scrape: every format is built from the stored
organisations, rescored with the run's own config. Scopes:

* ``run`` -- the organisations this run created, updated or found again;
* ``new`` -- only those new against an earlier run (default: the previous
  one; a scheduled "only new" run compares with its schedule's last run);
* ``project`` -- the project's whole table.

Suppression lists attached to the config (``filters.suppression_lists``),
plus the workspace's "Erasure requests" list, apply to every export:
``exclude`` lists drop rows, ``flag`` lists fill a ``Suppressed`` column.

Formats (Phase 4): the **workbook** (``export/excel.py``: Overview + Records),
**CSV** and **JSON** all render one ``ExportTable`` through one column model
(``export/columns.py``). This module only reads the database into it: the
records with their contacts, people, signals and run membership, and the
run's facts for the Overview (sources, units, failures, AI spend).

The run's own CSV (``runtime.csv_export``) and its Google Sheet keep the
legacy column layout (``Lead.row_for``) that the Mumbai and Nashik tabs use.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from ..core.lead import Lead
from ..db.models import (
    AIUsage, Contact, FailureRow, Organization, Person, Project, Run, RunOrganization, Signal,
    WorkUnitRow,
)
from ..export.columns import assemble
from ..export.excel import to_xlsx
from ..export.records import SORTS, ExportRecord, place_of, score_records, sort_records
from ..export.table import ExportTable, safe_cell, to_csv, to_json
from .errors import Conflict, Invalid, NotFound
from .query import aware
from .workspace import Workspace

log = logging.getLogger(__name__)

__all__ = ["ExportTable", "build_table", "export", "to_google_sheets", "safe_cell",
           "FORMATS", "SCOPES", "MEDIA_TYPES", "RENDERERS"]

ORGS = Organization.__table__
RUNS = Run.__table__
PROJECTS = Project.__table__
MEMBERS = RunOrganization.__table__
SIGNALS = Signal.__table__
PEOPLE = Person.__table__
CONTACTS = Contact.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__
AI_USAGE = AIUsage.__table__

SCOPES = ("run", "new", "project")
FORMATS = ("csv", "json", "xlsx")
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# --- assembling the table -------------------------------------------------------------------

def config_for(snapshot: dict[str, Any] | None):
    """The compiled Config a run ran with, or None."""
    from ..engine.runner import config_from_snapshot

    if not snapshot:
        return None
    try:
        return config_from_snapshot(snapshot)
    except Exception:
        return None


def _latest_snapshot(engine: Engine, project_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(RUNS.c.config_snapshot).where(
            RUNS.c.project_id == project_id, RUNS.c.kind.in_(["run", "import"]))
            .order_by(RUNS.c.id.desc()).limit(1)).first()
    return dict(row[0]) if row and row[0] else None


def _epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _lead_type_keys(config: Any, records: list[ExportRecord]) -> list[str]:
    """The run's enabled lead types, then any other the records carry; registered ones only."""
    from ..engine.runner import enabled_lead_types
    from ..leadtypes import registry

    keys = list(enabled_lead_types(config)) if config is not None else ["local_business"]
    extra = sorted({k for r in records for k in r.lead_types} - set(keys))
    return [k for k in [*keys, *extra] if registry.get(k) is not None]


def _places(config: Any) -> tuple[list[str], list[str]]:
    """(places, areas) the run covered."""
    if config is None:
        return [], []
    places = [str(p) for p in (config.get("places") or []) if p] or \
        ([config.city] if config.city else [])
    areas = [str(item.get("name") if isinstance(item, dict) else getattr(item, "name", item))
             for item in (config.get("localities") or [])]
    return places, [a for a in areas if a and a not in places]


def _money_prefix(config: Any, currency: str) -> str:
    from ..core.country import country as country_info, is_known_region

    code = config.country if config is not None else ""
    if code and is_known_region(code):
        info = country_info(code)
        if info.currency == currency:
            return info.money_prefix
    return f"{currency} " if currency else ""


def _timezone(config: Any) -> str:
    if config is None:
        return "UTC"
    from zoneinfo import ZoneInfo

    zone = config.get("geo.timezone")
    if not zone:
        try:
            zone = config.country_info.timezone
        except Exception:
            zone = None
    try:
        ZoneInfo(str(zone or "UTC"))
    except Exception:      # an unknown name: say UTC, since that is what is shown
        return "UTC"
    return str(zone or "UTC")


_MUST_HAVE = {"phone": lambda lead: bool(lead.phones), "email": lambda lead: bool(lead.emails),
              "website": lambda lead: bool(lead.website)}


def _filter(config: Any):
    """``filters.min_rating`` / ``must_have`` as a test a lead must pass to be exported."""
    from ..core.ratings import best_rating

    min_rating = config.get("filters.min_rating") if config is not None else None
    must_have = [_MUST_HAVE[k] for k in ((config.get("filters.must_have") if config is not None
                                          else None) or []) if k in _MUST_HAVE]

    def keep(lead: Lead) -> bool:
        if not all(test(lead) for test in must_have):
            return False
        if min_rating is not None:
            rating, _ = best_rating(lead)
            if rating is not None and rating < float(min_rating):
                return False
        return True
    return keep


def _filter_text(config: Any) -> str:
    if config is None:
        return ""
    parts = []
    if config.get("filters.min_rating") is not None:
        parts.append(f"rating at least {float(config.get('filters.min_rating')):g}")
    for item in config.get("filters.must_have") or []:
        parts.append(f"has a {item}")
    return ", ".join(parts)


#: Ids per IN (...) list: far below every backend's bind-parameter limit.
CHUNK = 500


def _chunks(ids: list[int]):
    for start in range(0, len(ids), CHUNK):
        yield ids[start:start + CHUNK]


def _rows(conn, query_for, ids: list[int]):
    """Rows of `query_for(chunk)` for every chunk of ids, in (chunk, row) order."""
    for chunk in _chunks(ids):
        yield from conn.execute(query_for(chunk)).mappings()


def _facts(conn, ws: Workspace, ids: list[int]) -> dict[str, Any]:
    """Signals, people, inferred contacts and last-seen times, for these organisations only."""
    def by_id(table, *where):
        rows = _rows(conn, lambda chunk: select(table).where(
            table.c.organization_id.in_(chunk), *where), ids)
        return sorted(rows, key=lambda r: r["id"])

    signals: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in by_id(SIGNALS):
        signals[row["organization_id"]].append({
            "type": row["type"], "lead_type": row["lead_type"], "title": row["title"],
            "payload": dict(row["payload"] or {}), "observed_at": aware(row["observed_at"]),
            "published_at": aware(row["published_at"]), "expires_at": aware(row["expires_at"]),
            "strength": row["strength"], "source_url": row["source_url"]})
    people: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in by_id(PEOPLE):
        people[row["organization_id"]].append({
            "name": row["name"], "title": row["title"] or "",
            "profile_url": row["profile_url"], "source": row["source"]})
    inferred: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in by_id(CONTACTS, CONTACTS.c.kind == "inferred"):
        inferred[row["organization_id"]].append({
            "channel": row["channel"], "value": row["value"], "source": row["source"],
            "confidence": row["confidence"]})
    seen = func.max(func.coalesce(RUNS.c.started_at, RUNS.c.created_at)).label("seen")
    last_seen = {int(row["organization_id"]): aware(row["seen"]) for row in _rows(
        conn, lambda chunk: select(MEMBERS.c.organization_id, seen)
        .join(RUNS, RUNS.c.id == MEMBERS.c.run_id)
        .where(MEMBERS.c.organization_id.in_(chunk), RUNS.c.workspace_id == ws.id)
        .group_by(MEMBERS.c.organization_id), ids)}
    return {"signals": signals, "people": people, "inferred": inferred, "last_seen": last_seen}


def _run_context(conn, run: dict[str, Any]) -> dict[str, Any]:
    """What the Overview says about a run: sources, units, failures, AI spend."""
    run_id = run["id"]
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    units: Counter = Counter()
    skip_reasons: Counter = Counter()
    for row in conn.execute(select(UNITS.c.lead_type, UNITS.c.source, UNITS.c.status,
                                   UNITS.c.found, UNITS.c.new, UNITS.c.skip_reason)
                            .where(UNITS.c.run_id == run_id).order_by(UNITS.c.id)).mappings():
        item = sources.setdefault((row["lead_type"], row["source"]), {
            "lead_type": row["lead_type"], "source": row["source"], "units": 0, "done": 0,
            "failed": 0, "skipped": 0, "found": 0, "new": 0})
        item["units"] += 1
        status = row["status"]
        if status in ("done", "failed", "skipped"):
            item[status] += 1
            units[status] += 1
        item["found"] += int(row["found"] or 0)
        item["new"] += int(row["new"] or 0)
        units["total"] += 1
        if status == "skipped" and row["skip_reason"]:
            skip_reasons[row["skip_reason"]] += 1
    count = func.count()
    failures = [{"reason": reason, "source": source, "count": int(n)} for reason, source, n
                in conn.execute(select(FAILURES.c.reason, FAILURES.c.source, count)
                                .where(FAILURES.c.run_id == run_id)
                                .group_by(FAILURES.c.reason, FAILURES.c.source)
                                .order_by(count.desc(), FAILURES.c.reason, FAILURES.c.source)
                                .limit(10))]
    spend = conn.execute(select(func.coalesce(func.sum(AI_USAGE.c.cost_usd), 0.0),
                                func.coalesce(func.sum(AI_USAGE.c.calls), 0))
                         .where(AI_USAGE.c.run_id == run_id)).first()
    found = conn.execute(select(func.count()).select_from(MEMBERS)
                         .where(MEMBERS.c.run_id == run_id)).scalar() or 0
    unit_facts: dict[str, Any] = dict(units)
    if skip_reasons:
        unit_facts["skip_reason"] = skip_reasons.most_common(1)[0][0]
    return {"sources": list(sources.values()), "units": unit_facts, "failures": failures,
            "ai_spend": float(spend[0] or 0.0), "ai_calls": int(spend[1] or 0),
            "run_found": int(found), "started_at": aware(run.get("started_at")),
            "duration_seconds": float(run.get("active_seconds") or 0.0)}


def build_table(engine: Engine, ws: Workspace, *, run_id: int | None = None,
                project_id: int | None = None, scope: str | None = None,
                against: int | None = None, generated_at: datetime | None = None,
                config: Any = None) -> ExportTable:
    """The table every format renders.

    ``config``: score, order and lay out with this compiled Config instead of
    the run's snapshot (``--export-only`` after a weights or tiers change).
    """
    from ..core.identity import DedupOptions
    from ..leadtypes import registry
    from ..leadtypes.local_business.scoring import Scorer
    from .runs import diff_items, run_row
    from .suppression import ERASURE_LIST, Matcher

    run = None
    if run_id is not None:
        run = run_row(engine, ws, run_id)
        project_id = int(run["project_id"])
        if scope is None:
            only_new = (run.get("request") or {}).get("only_new_since")
            scope = "new" if only_new is not None else "run"
            if against is None:
                against = only_new
    elif project_id is None:
        raise Invalid("give a run or a project to export")
    scope = scope or "project"
    if scope not in SCOPES:
        raise Invalid(f"scope must be one of {', '.join(SCOPES)}")
    if scope != "project" and run is None:
        raise Invalid(f"scope {scope!r} needs a run")
    with engine.connect() as conn:
        project = conn.execute(select(PROJECTS).where(
            PROJECTS.c.id == project_id, PROJECTS.c.workspace_id == ws.id)).mappings().first()
    if project is None:
        raise NotFound(f"no project {project_id}")
    project = dict(project)
    now = generated_at or datetime.now(timezone.utc)

    snapshot = run["config_snapshot"] if run else _latest_snapshot(engine, project_id)
    if config is None:
        config = config_for(snapshot)
    else:
        snapshot = {"name": config.get("name") or (snapshot or {}).get("name")}
    profile = config.profile if config is not None else "vendors"
    currency = config.currency if config is not None else "INR"
    if config is not None:
        config.apply_name_noise()
    cfg = config if config is not None else SimpleNamespace(
        profile=profile, currency=currency, get=lambda key, default=None: default)

    query = select(ORGS.c.id, ORGS.c.record, ORGS.c.lead_types).where(
        ORGS.c.project_id == project_id)
    used_against = None
    if scope == "run":
        query = query.where(ORGS.c.id.in_(select(MEMBERS.c.organization_id).where(
            MEMBERS.c.run_id == run_id)))
    elif scope == "new":
        used_against, items = diff_items(engine, ws, run_id, against)
        # New ones are among the run's own members: select those, keep the new
        # ones below (no IN list of possibly tens of thousands of ids).
        new_ids = {i["organization_id"] for i in items if i["change"] == "new"}
        query = query.where(ORGS.c.id.in_(select(MEMBERS.c.organization_id).where(
            MEMBERS.c.run_id == run_id)))
    with engine.connect() as conn:
        rows = conn.execute(query.order_by(ORGS.c.id)).all()
        if scope == "new":
            rows = [r for r in rows if int(r[0]) in new_ids]
        facts = _facts(conn, ws, [int(r[0]) for r in rows])
        actions = {int(o): a for o, a in conn.execute(
            select(MEMBERS.c.organization_id, MEMBERS.c.action)
            .where(MEMBERS.c.run_id == run_id))} if run is not None else {}
        context = _run_context(conn, run) if run is not None else {}

    # Rescore with the run's own config (venue tiers included), then fold
    # every lead type's parts with the run's weights.
    scorer = Scorer(config) if config is not None else Scorer()
    places, areas = _places(config)
    names = list((config.get("filters.suppression_lists") if config is not None else None) or [])
    matcher = Matcher.for_names(engine, ws.id, [*names, ERASURE_LIST],
                                region=config.country if config is not None else None)
    keep = _filter(config)
    records: list[ExportRecord] = []
    excluded = filtered = 0
    for org_id, record, lead_types in rows:
        lead = Lead.from_dict(record)
        scorer.apply(lead)
        if matcher.excluded(lead):
            excluded += 1
            continue
        if not keep(lead):
            filtered += 1
            continue
        seen = [d for d in (facts["last_seen"].get(int(org_id)), _epoch(lead.last_updated)) if d]
        records.append(ExportRecord(
            id=int(org_id), lead=lead, lead_types=list(lead_types or []) or ["local_business"],
            signals=facts["signals"].get(org_id, []), people=facts["people"].get(org_id, []),
            inferred=facts["inferred"].get(org_id, []), first_seen=_epoch(lead.first_seen),
            last_seen=max(seen) if seen else None,
            change="new" if scope == "new" else actions.get(int(org_id)),
            flags=matcher.flags(lead), place=place_of(lead, places)))
    score_records(records, cfg, scorer.score_weights)

    keys = _lead_type_keys(config, records)
    lead_types = [registry.get(k) for k in keys]
    signal_specs = {(lt.key, spec.type): spec for lt in lead_types for spec in lt.signal_specs()}
    fields = config.get("fields.include") if config is not None else None
    columns = assemble(records, cfg=cfg, lead_type_keys=keys, fields=fields,
                       places=len(places), has_run=run is not None,
                       signal_specs=signal_specs, now=now)
    mode = str((config.get("output.sort") if config is not None else None) or "auto")
    category = next((c.key for c in columns if c.breakdown
                     and c.key not in ("place", "locality")), None)
    legacy_mode = DedupOptions.from_config(config).sort if config is not None else "vendors"
    ordered, sort_label = sort_records(records, mode if mode in SORTS else "auto", places,
                                       legacy_mode, category)

    ai = None
    country = ""
    if config is not None:
        ai = {"enabled": bool(config.get("ai.enabled")), "provider": config.get("ai.backend"),
              "cap_usd": config.get("ai.max_cost_usd"),
              "spend_usd": context.get("ai_spend", 0.0), "calls": context.get("ai_calls", 0)}
        try:
            country = f"{config.country_info.name} ({config.country})"
        except Exception:
            country = config.country
    context.update({
        "config_name": (snapshot or {}).get("name") or "",
        "places": places, "areas": areas, "country": country,
        "lead_types": [(lt.key, lt.title) for lt in lead_types],
        "depth": config.get("depth") if config is not None else None,
        "time_budget_minutes": config.get("time_budget_minutes") if config is not None else None,
        "ai": ai, "weights": dict(scorer.score_weights), "fields": fields,
        "signal_types": {key: spec.title for key, spec in signal_specs.items()},
        "filtered": filtered, "filters": _filter_text(config),
    })
    return ExportTable(project=project, run=run, scope=scope, profile=profile, currency=currency,
                       columns=columns, records=ordered, excluded=excluded, against=used_against,
                       generated_at=now, sort_label=sort_label, timezone=_timezone(config),
                       money_prefix=_money_prefix(config, currency), context=context)


# --- formats ------------------------------------------------------------------------------

RENDERERS = {"csv": to_csv, "json": to_json, "xlsx": to_xlsx}


def filename_for(table: ExportTable, fmt: str) -> str:
    stem = table.project["slug"] + (f"-run{table.run['id']}" if table.run is not None else "")
    if table.scope == "new":
        stem += "-new"
    return f"{stem}.{fmt}"


def export(engine: Engine, ws: Workspace, fmt: str, *, run_id: int | None = None,
           project_id: int | None = None, scope: str | None = None,
           against: int | None = None, generated_at: datetime | None = None,
           config: Any = None) -> tuple[str, bytes, ExportTable]:
    """(filename, content, table) for one export."""
    if fmt not in FORMATS:
        raise Invalid(f"format must be one of {', '.join(FORMATS)}")
    table = build_table(engine, ws, run_id=run_id, project_id=project_id, scope=scope,
                        against=against, generated_at=generated_at, config=config)
    return filename_for(table, fmt), RENDERERS[fmt](table), table


def to_google_sheets(engine: Engine, ws: Workspace, run_id: int, *,
                     scope: str | None = None) -> dict[str, Any]:
    """Push a run's export to its Google Sheet (the existing service-account flow).

    The Sheet keeps the legacy layout and order its tabs have always had.
    """
    from ..core.identity import DedupOptions
    from ..db.store import sort_leads
    from ..export.sheets import SheetsWriter
    from .runs import run_row

    table = build_table(engine, ws, run_id=run_id, scope=scope)
    config = config_for(run_row(engine, ws, run_id)["config_snapshot"])
    if config is None:
        raise Invalid(f"run {run_id} has no usable config snapshot")
    config.set("sheets.enabled", True, record=False)
    writer = SheetsWriter(config)
    leads = sort_leads([r.lead for r in table.records], DedupOptions.from_config(config).sort)
    if not writer.sync(leads):
        raise Conflict("Google Sheets was not written: "
                       f"{writer.unavailable_reason or 'see the server log'}")
    return {"url": writer.url, "rows": len(leads), "scope": table.scope}


def write_files(table: ExportTable, formats: list[str], directory: Path,
                name: str | None = None) -> list[tuple[str, str]]:
    """Render `table` once per format into `directory`: [(format, path)] of what was written.

    Each format on its own: one that fails is logged and the others are still
    written. Files go through a ``.partial`` name, so a crash never leaves half
    a workbook behind.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        path = directory / (f"{name}.{fmt}" if name else filename_for(table, fmt))
        partial = path.with_name(path.name + ".partial")
        try:
            partial.write_bytes(RENDERERS[fmt](table))
            partial.replace(path)
        except Exception as exc:
            log.warning("%s not written: %s", path.name, exc)
            partial.unlink(missing_ok=True)
            continue
        written.append((fmt, str(path)))
    return written


def write_run_outputs(engine: Engine, run_id: int, config: Any) -> list[str]:
    """Write the files a run's ``output`` section asks for; returns their paths.

    Called by the runner once a run has ended (completed, paused, cancelled or
    failed: what it has is worth a workbook). The table is built once and
    rendered per format; each file is recorded on the run (``runs.files``).
    """
    from ..db.runs import add_run_file
    from ..export.outputs import output_dir, run_formats

    formats = run_formats(config)
    if not formats:
        return []
    with engine.connect() as conn:
        workspace_id = conn.execute(select(RUNS.c.workspace_id)
                                    .where(RUNS.c.id == run_id)).scalar()
    if workspace_id is None:
        raise NotFound(f"no run {run_id}")
    table = build_table(engine, Workspace(int(workspace_id), ""), run_id=run_id)
    written = write_files(table, formats, output_dir(config))
    for fmt, path in written:
        add_run_file(engine, run_id, kind=fmt, path=path, rows=len(table.records))
    return [path for _, path in written]
