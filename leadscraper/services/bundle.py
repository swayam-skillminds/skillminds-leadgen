"""Bundles: replicate a workspace on another device (Phase 3.5).

A bundle is a zip:

==========================  ===============================================
``manifest.json``           format + version, app version, schema revision,
                            where and when it was made, what it contains
``saved_configs.json``      every version of every saved config
``presets/*.yaml``          the shipped presets and the user's own
``suppression_lists.json``  lists with their entries
``projects.json``           projects (name, description, lead retention)
``schedules.json``          schedules, by saved-config name (imported disabled)
``settings.json``           workspace settings
``leads/<project>.json``    optional: organisations with their contacts,
                            people, signals, evidence and identity keys
``history/<project>.json``  optional: runs with units, failures, AI usage,
                            members
==========================  ===============================================

**Secrets are never included**: config documents and run snapshots go
through ``core.secrets.scrub``; notification targets (webhook URLs are
bearer secrets) are left out entirely; API keys and credentials never live
in the database in the first place.

**Import is version-checked**: an unknown bundle format version, or a schema
revision this installation does not know (a bundle from a newer version),
is refused before anything is written. Import is additive and idempotent:

* a config version identical to one already stored is skipped; a different
  document becomes a new version;
* lists, projects and presets are matched by name; list entries are merged;
* organisations go into an *empty* project verbatim (ids remapped, every
  fact kept); into a project that already has organisations they are merged
  through the identity rules (``LeadStore.upsert``), as a run would;
* history runs come in as finished history -- a run that was active when the
  bundle was made is imported as ``failed``, so no worker picks it up -- and
  a run already imported (same ref and creation time) is skipped.

Everything is plain JSON with ISO-8601 UTC times and ``/`` paths inside the
zip, so a bundle made on Windows imports on macOS or Linux and vice versa
(CI proves it: the bundle round-trip job in ``.github/workflows/tests.yml``).
"""

from __future__ import annotations

import io
import json
import logging
import platform
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import DateTime, select
from sqlalchemy.engine import Engine

from .. import __version__
from ..core.secrets import scrub
from ..db.models import (
    AIUsage, Contact, Evidence, FailureRow, IdentityKey, Organization, OrgAttribute, Person,
    Project, Run, RunOrganization, SavedConfig, Schedule, Signal, SuppressionList, WorkUnitRow,
)
from ..db.store import slugify_project
from .errors import Invalid, ServiceError
from .query import utcnow
from .workspace import Workspace

log = logging.getLogger(__name__)

FORMAT = "leadscraper-bundle"
FORMAT_VERSION = 1

ORGS = Organization.__table__
KEYS = IdentityKey.__table__
CONTACTS = Contact.__table__
PEOPLE = Person.__table__
SIGNALS = Signal.__table__
EVIDENCE = Evidence.__table__
ATTRS = OrgAttribute.__table__
RUNS = Run.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__
MEMBERS = RunOrganization.__table__
AI = AIUsage.__table__
CONFIGS = SavedConfig.__table__
LISTS = SuppressionList.__table__
SCHEDULES = Schedule.__table__
PROJECTS = Project.__table__

#: Columns never carried across: local ids and foreign keys are remapped.
_LOCAL = frozenset({"id", "workspace_id", "project_id", "organization_id", "run_id", "list_id",
                    "saved_config_id", "worker_id", "heartbeat_at"})
_LEAD_CHILDREN = (("identity_keys", KEYS), ("contacts", CONTACTS), ("people", PEOPLE),
                  ("signals", SIGNALS), ("evidence", EVIDENCE), ("attributes", ATTRS))


def _plain_row(table, row: dict[str, Any], drop: frozenset[str] = frozenset()) -> dict[str, Any]:
    out = {}
    for column in table.c:
        if column.name in _LOCAL or column.name in drop:
            continue
        value = row.get(column.name)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.isoformat()
        out[column.name] = value
    return out


def _restore(table, data: dict[str, Any]) -> dict[str, Any]:
    """A bundle row back into column values: ISO strings to datetimes, unknown keys dropped."""
    out = {}
    for column in table.c:
        if column.name not in data or column.name in _LOCAL:
            continue
        value = data[column.name]
        if isinstance(column.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        out[column.name] = value
    return out


def _iso(value: Any) -> str:
    """A timestamp in one comparable form (SQLite hands back naive datetimes)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _dump(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str).encode("utf-8")


def preset_dirs() -> list[Path]:
    """Shipped presets, then the user's own (``<data_dir>/presets``)."""
    from ..paths import PROJECT_ROOT, data_dir

    return [PROJECT_ROOT / "presets", data_dir() / "presets"]


# --- export -----------------------------------------------------------------------------------

def export_bundle(engine: Engine, ws: Workspace, *, project_ids: list[int] | None = None,
                  include_leads: bool = False, include_history: bool = False) -> bytes:
    from ..db.engine import head_revision
    from .settings import stored
    from .suppression import all_entries

    buffer = io.BytesIO()
    counts: dict[str, Any] = {}
    with engine.connect() as conn, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        projects = [dict(r) for r in conn.execute(select(PROJECTS).where(
            PROJECTS.c.workspace_id == ws.id).order_by(PROJECTS.c.id)).mappings()]
        if project_ids is not None:
            wanted = set(project_ids)
            projects = [p for p in projects if int(p["id"]) in wanted]
        zf.writestr("projects.json", _dump([_plain_row(PROJECTS, p) for p in projects]))
        counts["projects"] = len(projects)

        configs = [dict(r) for r in conn.execute(select(CONFIGS).where(
            CONFIGS.c.workspace_id == ws.id).order_by(CONFIGS.c.name, CONFIGS.c.version))
            .mappings()]
        zf.writestr("saved_configs.json", _dump([
            {**_plain_row(CONFIGS, c, frozenset({"document"})),
             "document": scrub(c["document"] or {})} for c in configs]))
        counts["saved_configs"] = len(configs)
        config_names = {int(c["id"]): (c["name"], int(c["version"])) for c in configs}

        lists = [dict(r) for r in conn.execute(select(LISTS).where(
            LISTS.c.workspace_id == ws.id).order_by(LISTS.c.id)).mappings()]
        zf.writestr("suppression_lists.json", _dump([
            {**_plain_row(LISTS, s), "entries": all_entries(engine, int(s["id"]))}
            for s in lists]))
        counts["suppression_lists"] = len(lists)

        schedules = [dict(r) for r in conn.execute(select(SCHEDULES).where(
            SCHEDULES.c.workspace_id == ws.id).order_by(SCHEDULES.c.id)).mappings()]
        zf.writestr("schedules.json", _dump([
            {**_plain_row(SCHEDULES, s, frozenset({"last_run_id", "next_run_at",
                                                   "last_run_at"})),
             "saved_config": config_names.get(int(s["saved_config_id"]), (None, None))[0]}
            for s in schedules]))
        counts["schedules"] = len(schedules)
        zf.writestr("settings.json", _dump(stored(engine, ws.id)))

        presets = 0
        for directory in preset_dirs():
            if directory.is_dir():
                for path in sorted(directory.glob("*.yaml")):
                    zf.writestr(f"presets/{path.name}", path.read_bytes())
                    presets += 1
        counts["presets"] = presets

        counts["organizations"] = counts["runs"] = 0
        for project in projects:
            slug = project["slug"]
            if include_leads:
                leads = _export_leads(conn, int(project["id"]))
                zf.writestr(f"leads/{slug}.json", _dump(leads))
                counts["organizations"] += len(leads)
            if include_history:
                runs = _export_history(conn, int(project["id"]), config_names)
                zf.writestr(f"history/{slug}.json", _dump(runs))
                counts["runs"] += len(runs)

        manifest = {
            "format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
            "schema_revision": head_revision(), "created_at": utcnow().isoformat(),
            "created_on": {"os": platform.system(), "python": platform.python_version()},
            "includes": {"leads": include_leads, "history": include_history},
            "counts": counts,
        }
        zf.writestr("manifest.json", _dump(manifest))
    return buffer.getvalue()


def _children(conn, table, org_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(org_ids), 500):
        chunk = org_ids[start:start + 500]
        for row in conn.execute(select(table).where(table.c.organization_id.in_(chunk))
                                .order_by(table.c.id)).mappings():
            row = dict(row)
            out.setdefault(int(row["organization_id"]), []).append(_plain_row(table, row))
    return out


def _export_leads(conn, project_id: int) -> list[dict[str, Any]]:
    orgs = [dict(r) for r in conn.execute(select(ORGS).where(ORGS.c.project_id == project_id)
                                          .order_by(ORGS.c.id)).mappings()]
    ids = [int(o["id"]) for o in orgs]
    children = {name: _children(conn, table, ids) for name, table in _LEAD_CHILDREN}
    return [{"ref": int(o["id"]), **_plain_row(ORGS, o),
             **{name: rows.get(int(o["id"]), []) for name, rows in children.items()}}
            for o in orgs]


def _export_history(conn, project_id: int, config_names: dict[int, tuple]) -> list[dict]:
    runs = [dict(r) for r in conn.execute(select(RUNS).where(RUNS.c.project_id == project_id)
                                          .order_by(RUNS.c.id)).mappings()]
    out = []
    for run in runs:
        run_id = int(run["id"])
        name, version = config_names.get(int(run["saved_config_id"] or 0), (None, None))
        out.append({
            "ref": run_id, **_plain_row(RUNS, run, frozenset({"config_snapshot"})),
            "config_snapshot": scrub(run["config_snapshot"] or {}),
            "saved_config": {"name": name, "version": version} if name else None,
            "units": [_plain_row(UNITS, dict(u)) for u in conn.execute(
                select(UNITS).where(UNITS.c.run_id == run_id).order_by(UNITS.c.id)).mappings()],
            "failures": [_plain_row(FAILURES, dict(f)) for f in conn.execute(
                select(FAILURES).where(FAILURES.c.run_id == run_id)
                .order_by(FAILURES.c.id)).mappings()],
            "ai_usage": [_plain_row(AI, dict(a)) for a in conn.execute(
                select(AI).where(AI.c.run_id == run_id)).mappings()],
            "members": [[int(m.organization_id), m.action, m.lead_type] for m in conn.execute(
                select(MEMBERS).where(MEMBERS.c.run_id == run_id).order_by(MEMBERS.c.id))],
        })
    return out


# --- import -----------------------------------------------------------------------------------

def read_manifest(data: bytes) -> dict[str, Any]:
    """The manifest, after checking this installation can import the bundle."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Invalid("not a bundle: the file is not a zip archive") from None
    with zf:
        _check_sizes(zf)
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            raise Invalid("not a bundle: manifest.json is missing") from None
    if manifest.get("format") != FORMAT:
        raise Invalid(f"not a bundle: format is {manifest.get('format')!r}")
    version = int(manifest.get("format_version") or 0)
    if version < 1 or version > FORMAT_VERSION:
        raise Invalid(f"bundle format version {version} is not supported (this version, "
                      f"{__version__}, reads 1-{FORMAT_VERSION}); upgrade first")
    revision = manifest.get("schema_revision")
    if revision and not _known_revision(revision):
        raise Invalid(f"the bundle was made by a newer version (database schema {revision}); "
                      "upgrade this installation first")
    return manifest


#: Limits on what a bundle may unpack to (a zip bomb would exhaust memory).
MAX_UNPACKED = 2 * 1024 ** 3
MAX_RATIO = 200


def _check_sizes(zf: zipfile.ZipFile) -> None:
    total = 0
    for info in zf.infolist():
        total += info.file_size
        if info.file_size > 1024 ** 2 and info.file_size > MAX_RATIO * max(1, info.compress_size):
            raise Invalid(f"refusing {info.filename}: it would unpack {info.file_size} bytes "
                          f"from {info.compress_size} (not a bundle this tool made)")
    if total > MAX_UNPACKED:
        raise Invalid(f"refusing a bundle that unpacks to {total} bytes "
                      f"(limit {MAX_UNPACKED})")


def _known_revision(revision: str) -> bool:
    from alembic.script import ScriptDirectory

    from ..db.engine import alembic_config

    try:
        return ScriptDirectory.from_config(alembic_config()).get_revision(revision) is not None
    except Exception:
        return False


def import_bundle(engine: Engine, ws: Workspace, data: bytes, *,
                  leads: bool = True, history: bool = True) -> dict[str, Any]:
    manifest = read_manifest(data)
    report: dict[str, Any] = {
        "manifest": manifest,
        "saved_configs": {"added": 0, "skipped": 0, "invalid": []},
        "presets": {"added": 0, "skipped": 0},
        "suppression_lists": {"lists": 0, "entries": 0},
        "projects": {"created": 0, "existing": 0},
        "schedules": {"added": 0, "skipped": 0},
        "organizations": {}, "runs": 0}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())

        def load(name: str, default: Any) -> Any:
            return json.loads(zf.read(name)) if name in names else default

        _import_configs(engine, ws, load("saved_configs.json", []), report)
        _import_presets(zf, sorted(n for n in names
                                   if n.startswith("presets/") and n.endswith(".yaml")), report)
        _import_lists(engine, ws, load("suppression_lists.json", []), report)
        project_ids = _import_projects(engine, ws, load("projects.json", []), report)
        _import_schedules(engine, ws, load("schedules.json", []), report)
        _import_settings(engine, ws, load("settings.json", {}))
        org_maps: dict[str, dict[int, int]] = {}
        if leads:
            for slug, project_id in project_ids.items():
                if f"leads/{slug}.json" in names:
                    org_maps[slug] = _import_leads(engine, ws, project_id,
                                                   load(f"leads/{slug}.json", []), report, slug)
        if history:
            for slug, project_id in project_ids.items():
                if f"history/{slug}.json" in names:
                    _import_history(engine, ws, project_id, load(f"history/{slug}.json", []),
                                    org_maps.get(slug, {}), report)
    return report


def _import_configs(engine: Engine, ws: Workspace, rows: list[dict], report: dict) -> None:
    from . import configs

    for row in rows:
        name, document = row.get("name"), row.get("document") or {}
        with engine.connect() as conn:
            existing = [dict(r) for r in conn.execute(select(CONFIGS).where(
                CONFIGS.c.workspace_id == ws.id, CONFIGS.c.name == name)
                .order_by(CONFIGS.c.version)).mappings()]
        try:
            canonical = configs.canonical(document)
        except ServiceError as exc:
            report["saved_configs"]["invalid"].append({"name": name, "error": exc.detail})
            continue
        if any(e["document"] == canonical for e in existing):
            report["saved_configs"]["skipped"] += 1
            continue
        note = row.get("description") or "imported from a bundle"
        if existing:
            configs.save_version(engine, ws, int(existing[0]["id"]), canonical, description=note)
        else:
            configs.create_config(engine, ws, canonical, name=name, description=note)
        report["saved_configs"]["added"] += 1


def _import_presets(zf: zipfile.ZipFile, names: list[str], report: dict) -> None:
    shipped, target = preset_dirs()
    for name in names:
        content = zf.read(name)
        filename = Path(name).name
        try:
            yaml.safe_load(content)
        except yaml.YAMLError:
            report["presets"]["skipped"] += 1
            continue
        same = [d / filename for d in (shipped, target) if (d / filename).exists()]
        if any(p.read_bytes() == content for p in same):
            report["presets"]["skipped"] += 1
            continue
        target.mkdir(parents=True, exist_ok=True)
        path = target / filename
        if same:                                     # a different preset of the same name
            path = target / f"{Path(filename).stem}-imported.yaml"
        path.write_bytes(content)
        report["presets"]["added"] += 1


def _import_lists(engine: Engine, ws: Workspace, rows: list[dict], report: dict) -> None:
    from .suppression import add_entries, ensure_list

    for row in rows:
        listing = ensure_list(engine, ws, row["name"], row.get("mode") or "exclude")
        result = add_entries(engine, ws, listing["id"], row.get("entries") or [])
        report["suppression_lists"]["lists"] += 1
        report["suppression_lists"]["entries"] += result["added"]


def _import_projects(engine: Engine, ws: Workspace, rows: list[dict],
                     report: dict) -> dict[str, int]:
    from .projects import create_project, find_project

    out = {}
    for row in rows:
        existing = find_project(engine, ws, row["name"])
        if existing is None:
            existing = create_project(engine, ws, name=row["name"],
                                      description=row.get("description") or "",
                                      retention_days=row.get("retention_days"))
            report["projects"]["created"] += 1
        else:
            report["projects"]["existing"] += 1
        out[row.get("slug") or slugify_project(row["name"])] = int(existing["id"])
    return out


def _import_schedules(engine: Engine, ws: Workspace, rows: list[dict], report: dict) -> None:
    from .schedules import create_schedule

    for row in rows:
        with engine.connect() as conn:
            config = conn.execute(select(CONFIGS.c.id).where(
                CONFIGS.c.workspace_id == ws.id, CONFIGS.c.name == row.get("saved_config"))
                .order_by(CONFIGS.c.version)).first()
            clash = None if config is None else conn.execute(select(SCHEDULES.c.id).where(
                SCHEDULES.c.workspace_id == ws.id, SCHEDULES.c.saved_config_id == config[0],
                SCHEDULES.c.cron == row.get("cron"))).first()
        if config is None or clash is not None:
            report["schedules"]["skipped"] += 1
            continue
        try:
            # Imported disabled: a copied schedule must not start running on its own.
            create_schedule(engine, ws, saved_config_id=int(config[0]), cron=row["cron"],
                            timezone=row.get("timezone") or "UTC", name=row.get("name") or "",
                            only_new=bool(row.get("only_new")), enabled=False,
                            overrides=row.get("overrides") or {})
            report["schedules"]["added"] += 1
        except ServiceError:
            report["schedules"]["skipped"] += 1


def _import_settings(engine: Engine, ws: Workspace, settings: dict) -> None:
    """Settings only fill a workspace that has none of its own."""
    from .settings import stored, update_settings

    if settings and not stored(engine, ws.id):
        try:
            update_settings(engine, ws, settings)
        except ServiceError as exc:
            log.warning("bundle settings not applied: %s", exc.detail)


def _import_leads(engine: Engine, ws: Workspace, project_id: int, rows: list[dict],
                  report: dict, slug: str) -> dict[int, int]:
    """Organisations into a project; returns bundle ref -> local id."""
    with engine.connect() as conn:
        occupied = conn.execute(select(ORGS.c.id).where(ORGS.c.project_id == project_id)
                                .limit(1)).first() is not None
    mapping: dict[int, int] = {}
    if not occupied:
        with engine.begin() as conn:
            for row in rows:
                org_id = int(conn.execute(ORGS.insert().values(
                    workspace_id=ws.id, project_id=project_id, **_restore(ORGS, row)))
                    .inserted_primary_key[0])
                mapping[int(row["ref"])] = org_id
                for name, table in _LEAD_CHILDREN:
                    children = row.get(name) or []
                    if children:
                        extra = {"project_id": project_id} if table is KEYS else {}
                        conn.execute(table.insert(), [
                            {"workspace_id": ws.id, "organization_id": org_id, **extra,
                             **_restore(table, child)} for child in children])
        report["organizations"][slug] = {"mode": "verbatim", "imported": len(mapping)}
        return mapping

    from ..core.lead import Lead
    from ..db.store import LeadStore

    with engine.connect() as conn:
        name = conn.execute(select(PROJECTS.c.name).where(PROJECTS.c.id == project_id)).scalar()
    store = LeadStore(engine, project=name)
    store.source_override = "bundle import"
    actions = {"new": 0, "merged": 0, "unchanged": 0, "rejected": 0}
    try:
        for row in rows:
            try:
                org_id, action, _ = store.upsert(Lead.from_dict(row.get("record") or {}))
            except ValueError:
                actions["rejected"] += 1
                continue
            actions[action] += 1
            mapping[int(row["ref"])] = org_id
    finally:
        store.close()
    report["organizations"][slug] = {"mode": "merged", **actions}
    return mapping


def _import_history(engine: Engine, ws: Workspace, project_id: int, rows: list[dict],
                    org_map: dict[int, int], report: dict) -> None:
    from ..db.runs import TERMINAL

    with engine.connect() as conn:
        # A run this bundle (or an identical one) already brought in: same ref
        # and creation time. Importing twice must not duplicate history.
        seen_runs = {(str((r.request or {}).get("imported_from_bundle", {}).get("ref")),
                      _iso(r.created_at))
                     for r in conn.execute(select(RUNS.c.request, RUNS.c.created_at).where(
                         RUNS.c.project_id == project_id))
                     if (r.request or {}).get("imported_from_bundle")}
    with engine.begin() as conn:
        for row in rows:
            if (str(row.get("ref")), _iso(row.get("created_at"))) in seen_runs:
                report["runs_skipped"] = report.get("runs_skipped", 0) + 1
                continue
            values = _restore(RUNS, row)
            values["config_snapshot"] = row.get("config_snapshot") or {}
            if values.get("status") not in TERMINAL:
                values["status"] = "failed"
                values["error"] = (f"imported from a bundle while {row.get('status')}; "
                                   "resume it on the machine it ran on")
            request = dict(values.get("request") or {})
            request["imported_from_bundle"] = {"ref": row.get("ref"),
                                               "saved_config": row.get("saved_config")}
            values["request"] = request
            run_id = int(conn.execute(RUNS.insert().values(
                workspace_id=ws.id, project_id=project_id, **values)).inserted_primary_key[0])
            for key, table in (("units", UNITS), ("failures", FAILURES), ("ai_usage", AI)):
                items = row.get(key) or []
                if items:
                    extra = {"project_id": project_id} if table is FAILURES else {}
                    conn.execute(table.insert(), [{"workspace_id": ws.id, "run_id": run_id,
                                                   **extra, **_restore(table, item)}
                                                  for item in items])
            seen: set[int] = set()
            members = []
            for ref, action, lead_type in row.get("members") or []:
                org_id = org_map.get(int(ref))
                if org_id is not None and org_id not in seen:
                    seen.add(org_id)
                    members.append({"workspace_id": ws.id, "run_id": run_id,
                                    "organization_id": org_id, "action": action,
                                    "lead_type": lead_type or ""})
            if members:
                conn.execute(MEMBERS.insert(), members)
            report["runs"] += 1
