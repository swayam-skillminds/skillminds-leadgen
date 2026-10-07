"""Exports, regenerated from the database at any time (Phase 3.1).

Re-exporting never needs a re-scrape: every format is built from the stored
organisations, rescored with the run's own config, in the shared output
order (``db.store.sort_leads``). Scopes:

* ``run`` -- the organisations this run created, updated or found again;
* ``new`` -- only those new against an earlier run (default: the previous
  one; a scheduled "only new" run compares with its schedule's last run);
* ``project`` -- the project's whole table, as the run's CSV has it.

Suppression lists attached to the config (``filters.suppression_lists``),
plus the workspace's "Erasure requests" list, apply to every export:
``exclude`` lists drop rows, ``flag`` lists add a ``Suppressed`` column.

Formats: CSV (the legacy column layout of the run's profile), JSON (rows
plus ids and scores) and XLSX. The XLSX here has the two required tabs,
Overview and Records, with plain styling; Phase 4 replaces it with the
formatted deliverable workbook.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..core.lead import Lead
from ..db.models import Organization, Project, Run, RunOrganization
from ..db.store import sort_leads
from .errors import Conflict, Invalid, NotFound
from .query import aware
from .workspace import Workspace

ORGS = Organization.__table__
RUNS = Run.__table__
PROJECTS = Project.__table__
MEMBERS = RunOrganization.__table__

SCOPES = ("run", "new", "project")
FORMATS = ("csv", "json", "xlsx")
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
SUPPRESSED = "Suppressed"


@dataclass
class ExportTable:
    """What every format is rendered from."""

    project: dict[str, Any]
    run: dict[str, Any] | None
    scope: str
    profile: str
    currency: str
    headers: list[str]
    #: (organisation id, Lead, suppression flags)
    rows: list[tuple[int, Lead, list[str]]] = field(default_factory=list)
    excluded: int = 0
    against: int | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record(self, lead: Lead, flags: list[str]) -> dict[str, Any]:
        row = lead.row_for(self.profile, self.currency)
        out = {header: row.get(header, "") for header in self.headers if header != SUPPRESSED}
        if SUPPRESSED in self.headers:
            out[SUPPRESSED] = ", ".join(flags)
        return out


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


def build_table(engine: Engine, ws: Workspace, *, run_id: int | None = None,
                project_id: int | None = None, scope: str | None = None,
                against: int | None = None) -> ExportTable:
    from ..core.identity import DedupOptions
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

    config = config_for(run["config_snapshot"] if run else _latest_snapshot(engine, project_id))
    profile = str(config.get("profile") or "vendors") if config is not None else "vendors"
    currency = config.currency if config is not None else "INR"
    if config is not None:
        config.apply_name_noise()

    query = select(ORGS.c.id, ORGS.c.record).where(ORGS.c.project_id == project_id)
    used_against = None
    if scope == "run":
        query = query.where(ORGS.c.id.in_(select(MEMBERS.c.organization_id).where(
            MEMBERS.c.run_id == run_id)))
    elif scope == "new":
        used_against, items = diff_items(engine, ws, run_id, against)
        new_ids = [i["organization_id"] for i in items if i["change"] == "new"]
        query = query.where(ORGS.c.id.in_(new_ids))
    with engine.connect() as conn:
        rows = conn.execute(query.order_by(ORGS.c.id)).all()

    scorer = Scorer(config) if config is not None else Scorer()
    ids: dict[int, int] = {}
    leads: list[Lead] = []
    for org_id, record in rows:
        lead = Lead.from_dict(record)
        scorer.apply(lead)
        ids[id(lead)] = int(org_id)
        leads.append(lead)
    mode = DedupOptions.from_config(config).sort if config is not None else "vendors"
    ordered = sort_leads(leads, mode)

    names = list((config.get("filters.suppression_lists") if config is not None else None) or [])
    matcher = Matcher.for_names(engine, ws.id, [*names, ERASURE_LIST],
                                region=config.country if config is not None else None)
    headers = list(Lead.row_headers(profile, currency))
    table = ExportTable(project=project, run=run, scope=scope, profile=profile,
                        currency=currency, headers=headers, against=used_against)
    any_flag = False
    for lead in ordered:
        if matcher.excluded(lead):
            table.excluded += 1
            continue
        flags = matcher.flags(lead)
        any_flag = any_flag or bool(flags)
        table.rows.append((ids[id(lead)], lead, flags))
    if any_flag:
        table.headers.append(SUPPRESSED)
    return table


# --- formats ------------------------------------------------------------------------------

def to_csv(table: ExportTable) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(table.headers)
    for _, lead, flags in table.rows:
        record = table.record(lead, flags)
        writer.writerow([record.get(h, "") for h in table.headers])
    return buffer.getvalue().encode("utf-8-sig")


def to_json(table: ExportTable) -> bytes:
    records = [{"id": org_id, "score": lead.score, "suppressed": flags,
                "row": table.record(lead, flags)} for org_id, lead, flags in table.rows]
    document = {
        "project": {"id": table.project["id"], "name": table.project["name"]},
        "run_id": table.run["id"] if table.run else None, "scope": table.scope,
        "against": table.against, "profile": table.profile, "currency": table.currency,
        "generated_at": table.generated_at.isoformat(), "columns": table.headers,
        "count": len(records), "excluded_by_suppression": table.excluded, "records": records,
    }
    return json.dumps(document, ensure_ascii=False, indent=1, default=str).encode("utf-8")


#: Columns written as text even when they look numeric (phones): Excel would
#: otherwise drop a leading "+" or turn them into numbers.
_TEXT_COLUMNS = {"Phones", "Booking Contact", "WhatsApp"}
_URL_COLUMNS = {"Website", "Instagram", "LinkedIn", "Facebook", "Google Maps URL",
                "Photo URL", "WhatsApp"}


def _overview(table: ExportTable) -> list[tuple[str, Any]]:
    run = table.run or {}
    snapshot = run.get("config_snapshot") or {}
    geography = snapshot.get("geography") or {}
    stats = (run.get("stats") or {}).get("engine") or {}
    started = aware(run.get("started_at"))
    ai = snapshot.get("ai") or {}
    scope_text = {"run": "organisations this run found",
                  "new": f"new since run {table.against}" if table.against else "new",
                  "project": "the whole project"}[table.scope]
    return [
        ("Project", table.project["name"]),
        ("Config", snapshot.get("name") or ""),
        ("Run", run.get("id") or "(whole project)"),
        ("Status", run.get("status") or ""),
        ("Run started", started.strftime("%Y-%m-%d %H:%M UTC") if started else ""),
        ("Duration (min)", round(float(run.get("active_seconds") or 0) / 60, 1) if run else ""),
        ("Places", ", ".join(geography.get("places") or [])),
        ("Country", geography.get("country") or ""),
        ("Lead types", ", ".join(sorted((snapshot.get("lead_types") or {}).keys()))),
        ("AI", "on" if ai.get("enabled") else "off"),
        ("Scope", scope_text),
        ("Records", len(table.rows)),
        ("New in this run", stats.get("new", "") if run else ""),
        ("Updated in this run", stats.get("merged", "") if run else ""),
        ("Excluded by suppression lists", table.excluded),
        ("Generated", table.generated_at.strftime("%Y-%m-%d %H:%M UTC")),
    ]


def to_xlsx(table: ExportTable) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    overview = workbook.active
    overview.title = "Overview"
    bold = Font(bold=True)
    for label, value in _overview(table):
        overview.append([label, value])
        overview.cell(overview.max_row, 1).font = bold
    overview.column_dimensions["A"].width = 32
    overview.column_dimensions["B"].width = 60

    records = workbook.create_sheet("Records")
    records.append(table.headers)
    header_fill = PatternFill("solid", fgColor="1F3864")
    for cell in records[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    link = Font(color="0563C1", underline="single")
    for _, lead, flags in table.rows:
        record = table.record(lead, flags)
        records.append([record.get(h, "") for h in table.headers])
        row = records.max_row
        for col, header in enumerate(table.headers, start=1):
            cell = records.cell(row, col)
            if header in _TEXT_COLUMNS and cell.value not in (None, ""):
                cell.value = str(cell.value)
                cell.number_format = "@"
            if header in _URL_COLUMNS and isinstance(cell.value, str) \
                    and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.font = link
    records.freeze_panes = "C2"
    if table.rows:
        records.auto_filter.ref = (f"A1:{get_column_letter(len(table.headers))}"
                                   f"{len(table.rows) + 1}")
    for col, header in enumerate(table.headers, start=1):
        width = 40 if header in ("Business Name", "Address") else \
            30 if header in _URL_COLUMNS else 16
        records.column_dimensions[get_column_letter(col)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


RENDERERS = {"csv": to_csv, "json": to_json, "xlsx": to_xlsx}


def export(engine: Engine, ws: Workspace, fmt: str, *, run_id: int | None = None,
           project_id: int | None = None, scope: str | None = None,
           against: int | None = None) -> tuple[str, bytes, ExportTable]:
    """(filename, content, table) for one export."""
    if fmt not in FORMATS:
        raise Invalid(f"format must be one of {', '.join(FORMATS)}")
    table = build_table(engine, ws, run_id=run_id, project_id=project_id, scope=scope,
                        against=against)
    stem = table.project["slug"] + (f"-run{run_id}" if run_id is not None else "")
    if table.scope == "new":
        stem += "-new"
    return f"{stem}.{fmt}", RENDERERS[fmt](table), table


def to_google_sheets(engine: Engine, ws: Workspace, run_id: int, *,
                     scope: str | None = None) -> dict[str, Any]:
    """Push a run's export to its Google Sheet (the existing service-account flow)."""
    from ..export.sheets import SheetsWriter
    from .runs import run_row

    table = build_table(engine, ws, run_id=run_id, scope=scope)
    config = config_for(run_row(engine, ws, run_id)["config_snapshot"])
    if config is None:
        raise Invalid(f"run {run_id} has no usable config snapshot")
    config.set("sheets.enabled", True, record=False)
    writer = SheetsWriter(config)
    leads = [lead for _, lead, _ in table.rows]
    if not writer.sync(leads):
        raise Conflict("Google Sheets was not written: "
                       f"{writer.unavailable_reason or 'see the server log'}")
    return {"url": writer.url, "rows": len(leads), "scope": table.scope}
