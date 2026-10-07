"""Saved configs: named, versioned run configs (A.6 SavedConfig).

A saved config is a family of rows sharing a name; each save of a changed
document adds a version, and no version is ever overwritten, so a run can
always point at exactly what it ran (``runs.saved_config_id`` is the version's
row id). The family's ``id`` is its first version's row id, stable across
versions and renames.

Documents are validated against the run-config schema on the way in and
stored in their canonical form (``RunConfig.to_document``). Legacy
``config.yaml`` files are accepted by import and converted, as the CLI's
``config import-legacy`` does. Exports are YAML or JSON with secret-looking
values removed (``core.secrets.scrub``).
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Connection, Engine

from ..config.schema import RunConfig
from ..core.secrets import scrub
from ..db.models import Project, SavedConfig
from ..db.store import slugify_project
from .errors import Conflict, Invalid, NotFound, validation_errors
from .query import Page, aware, clamp, utcnow
from .workspace import Workspace

CONFIGS = SavedConfig.__table__
PROJECTS = Project.__table__


# --- documents -------------------------------------------------------------------------

def validate_document(document: Any) -> RunConfig:
    """A validated run config, or ``Invalid`` with the schema's error list.

    The API never lets a config choose the database: ``runtime.db_url`` would
    send a run somewhere the worker does not look (and put a credential in
    the run history).
    """
    if not isinstance(document, dict):
        raise Invalid("a run config is a JSON object / YAML mapping")
    if "schema_version" not in document:
        from ..config.importer import is_legacy

        if is_legacy(document):
            raise Invalid("this is a legacy config (no schema_version); import it "
                          "(POST /configs/import or `leadscraper config import-legacy`)")
    runtime = document.get("runtime") or {}
    if isinstance(runtime, dict) and runtime.get("db_url"):
        raise Invalid("runtime.db_url cannot be set through the API: runs use the "
                      "application database (LEADSCRAPER_DB_URL)")
    try:
        return RunConfig.model_validate(document)
    except ValueError as exc:
        raise Invalid("the run config is not valid", errors=validation_errors(exc)) from None


def canonical(document: dict[str, Any]) -> dict[str, Any]:
    """The validated document as the database stores it.

    JSON-normalised: the JSON column turns YAML's integer keys (price bands)
    into strings, so documents are compared -- "is this a new version?" -- in
    the form they are stored in.
    """
    return json.loads(json.dumps(validate_document(document).to_document(), default=str))


def parse_text(text: str | bytes, *, filename: str | None = None) -> dict[str, Any]:
    """A run config from YAML or JSON text; a legacy config.yaml is converted."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    try:
        data = yaml.safe_load(text)          # JSON is YAML too
    except yaml.YAMLError as exc:
        raise Invalid(f"not valid YAML or JSON: {exc}") from None
    if not isinstance(data, dict):
        raise Invalid("a run config is a mapping at the top level")
    from ..config.importer import is_legacy, legacy_to_document

    if is_legacy(data):
        try:
            data = legacy_to_document(data, source=filename or "uploaded config")
        except Exception as exc:
            raise Invalid(f"could not convert the legacy config: {exc}") from None
    return data


def dump(document: dict[str, Any], fmt: str = "yaml") -> str:
    clean = scrub(document)
    if fmt == "json":
        return json.dumps(clean, indent=2, ensure_ascii=False, default=str) + "\n"
    if fmt != "yaml":
        raise Invalid(f"unknown format {fmt!r} (yaml or json)")
    from ..config.importer import dump_document

    return dump_document(clean)


# --- rows ------------------------------------------------------------------------------

def _versions(conn: Connection, ws: Workspace, name: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        select(CONFIGS).where(CONFIGS.c.workspace_id == ws.id, CONFIGS.c.name == name)
        .order_by(CONFIGS.c.version)).mappings()]


def _family_name(conn: Connection, ws: Workspace, config_id: int) -> str:
    row = conn.execute(select(CONFIGS.c.name).where(
        CONFIGS.c.id == config_id, CONFIGS.c.workspace_id == ws.id)).first()
    if row is None:
        raise NotFound(f"no saved config {config_id}")
    return str(row[0])


def _shape(versions: list[dict[str, Any]], version: dict[str, Any] | None = None,
           *, with_document: bool = True) -> dict[str, Any]:
    first, latest = versions[0], versions[-1]
    chosen = version or latest
    document = chosen["document"] or {}
    out = {
        "id": int(first["id"]), "name": first["name"], "version": int(chosen["version"]),
        "version_id": int(chosen["id"]), "latest_version": int(latest["version"]),
        "versions": len(versions), "project": document.get("project"),
        "project_id": chosen.get("project_id"), "schema_version": int(chosen["schema_version"]),
        "description": chosen.get("description") or "",
        "created_at": aware(first["created_at"]), "updated_at": aware(latest["created_at"]),
    }
    if with_document:
        out["document"] = document
    return out


def _project_id(conn: Connection, ws: Workspace, document: dict[str, Any]) -> int | None:
    name = document.get("project")
    if not name:
        return None
    row = conn.execute(select(PROJECTS.c.id).where(
        PROJECTS.c.workspace_id == ws.id, PROJECTS.c.slug == slugify_project(str(name)))).first()
    return int(row[0]) if row else None


def _insert(conn: Connection, ws: Workspace, name: str, version: int,
            document: dict[str, Any], description: str) -> int:
    return int(conn.execute(CONFIGS.insert().values(
        workspace_id=ws.id, project_id=_project_id(conn, ws, document), name=name,
        version=version, schema_version=int(document.get("schema_version", 1)),
        document=document, description=description or "", created_at=utcnow())
    ).inserted_primary_key[0])


# --- operations ------------------------------------------------------------------------

def list_configs(engine: Engine, ws: Workspace, *, q: str | None = None,
                 project: str | None = None, sort: str | None = None,
                 limit: int | None = None, offset: int | None = None) -> Page[dict[str, Any]]:
    """Each saved config once (its latest version), without documents."""
    limit, offset = clamp(limit, offset)
    latest = (select(CONFIGS.c.name, func.max(CONFIGS.c.version).label("v"))
              .where(CONFIGS.c.workspace_id == ws.id).group_by(CONFIGS.c.name).subquery())
    query = select(CONFIGS.c.name).join(latest, (latest.c.name == CONFIGS.c.name)
                                        & (latest.c.v == CONFIGS.c.version)).where(
        CONFIGS.c.workspace_id == ws.id)
    if q:
        query = query.where(CONFIGS.c.name.ilike(f"%{q}%"))
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(query)]
        families = {name: _versions(conn, ws, name) for name in names}
    items = [_shape(families[name], with_document=False) for name in names]
    if project:
        wanted = slugify_project(project)
        items = [i for i in items if slugify_project(str(i["project"] or "")) == wanted]
    keys = {"name": lambda i: i["name"].lower(), "updated_at": lambda i: i["updated_at"],
            "created_at": lambda i: i["created_at"], "id": lambda i: i["id"]}
    for part in reversed([p.strip() for p in (sort or "name").split(",") if p.strip()]):
        field = part.lstrip("-+")
        if field not in keys:
            raise Invalid(f"cannot sort by {field!r}; sortable: {', '.join(sorted(keys))}")
        items.sort(key=keys[field], reverse=part.startswith("-"))
    return Page(items[offset:offset + limit], len(items), limit, offset)


def get_config(engine: Engine, ws: Workspace, config_id: int,
               version: int | None = None) -> dict[str, Any]:
    with engine.connect() as conn:
        versions = _versions(conn, ws, _family_name(conn, ws, config_id))
    chosen = None
    if version is not None:
        chosen = next((v for v in versions if int(v["version"]) == int(version)), None)
        if chosen is None:
            raise NotFound(f"saved config {config_id} has no version {version}")
    return _shape(versions, chosen)


def list_versions(engine: Engine, ws: Workspace, config_id: int) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        versions = _versions(conn, ws, _family_name(conn, ws, config_id))
    return [{"version": int(v["version"]), "version_id": int(v["id"]),
             "description": v.get("description") or "", "created_at": aware(v["created_at"]),
             "project": (v["document"] or {}).get("project")} for v in versions]


def create_config(engine: Engine, ws: Workspace, document: dict[str, Any], *,
                  name: str | None = None, description: str = "") -> dict[str, Any]:
    document = canonical(document)
    name = (name or document.get("name") or "").strip()
    if not name:
        raise Invalid("a saved config needs a name")
    with engine.begin() as conn:
        if _versions(conn, ws, name):
            raise Conflict(f"a saved config named {name!r} already exists; save a new "
                           "version of it instead")
        config_id = _insert(conn, ws, name, 1, document, description)
    return get_config(engine, ws, config_id)


def save_version(engine: Engine, ws: Workspace, config_id: int, document: dict[str, Any], *,
                 description: str = "") -> dict[str, Any]:
    """A new version, unless the document is unchanged (then the latest is returned)."""
    document = canonical(document)
    with engine.begin() as conn:
        name = _family_name(conn, ws, config_id)
        versions = _versions(conn, ws, name)
        if versions[-1]["document"] == document:
            return _shape(versions)
        _insert(conn, ws, name, int(versions[-1]["version"]) + 1, document, description)
    return get_config(engine, ws, config_id)


def rename_config(engine: Engine, ws: Workspace, config_id: int, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise Invalid("a saved config needs a name")
    with engine.begin() as conn:
        old = _family_name(conn, ws, config_id)
        if name != old and _versions(conn, ws, name):
            raise Conflict(f"a saved config named {name!r} already exists")
        conn.execute(update(CONFIGS).where(CONFIGS.c.workspace_id == ws.id,
                                           CONFIGS.c.name == old).values(name=name))
    return get_config(engine, ws, config_id)


def delete_config(engine: Engine, ws: Workspace, config_id: int) -> dict[str, Any]:
    """Delete every version. Runs keep their own config snapshot; schedules of it go."""
    shaped = get_config(engine, ws, config_id)
    with engine.begin() as conn:
        conn.execute(delete(CONFIGS).where(CONFIGS.c.workspace_id == ws.id,
                                           CONFIGS.c.name == shaped["name"]))
    return shaped


def clone_config(engine: Engine, ws: Workspace, config_id: int, name: str, *,
                 version: int | None = None) -> dict[str, Any]:
    source = get_config(engine, ws, config_id, version)
    document = {**source["document"], "name": name}
    return create_config(engine, ws, document, name=name,
                         description=f"cloned from {source['name']} v{source['version']}")


def import_config(engine: Engine, ws: Workspace, text: str | bytes, *,
                  filename: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Import YAML/JSON (or a legacy config.yaml). An existing name gets a new version."""
    document = parse_text(text, filename=filename)
    if name:
        document = {**document, "name": name}
    name = name or str(document.get("name") or "").strip()
    note = f"imported{f' from {filename}' if filename else ''}"
    with engine.connect() as conn:
        existing = _versions(conn, ws, name) if name else []
    if existing:
        return save_version(engine, ws, int(existing[0]["id"]), document, description=note)
    return create_config(engine, ws, document, name=name or None, description=note)


def export_config(engine: Engine, ws: Workspace, config_id: int, *, version: int | None = None,
                  fmt: str = "yaml") -> tuple[str, str]:
    """(filename, text) of one version, secrets removed."""
    shaped = get_config(engine, ws, config_id, version)
    stem = slugify_project(shaped["name"])
    filename = f"{stem}-v{shaped['version']}.{'json' if fmt == 'json' else 'yaml'}"
    return filename, dump(shaped["document"], fmt)
