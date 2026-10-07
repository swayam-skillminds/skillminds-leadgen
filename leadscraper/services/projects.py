"""Projects: long-lived lead databases that runs merge into (A.6).

A run config names its project (``project: "Pune dentists"``); the project is
found by its slug (``pune-dentists``), so the same name always lands in the
same database. Creating a project ahead of a run is optional -- a run creates
its project on first use -- but lets a user set a description and a lead
retention period first.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from ..db.models import Organization, Project, Run
from ..db.store import slugify_project
from .errors import Conflict, Invalid, NotFound
from .query import Page, aware, clamp, fetch_page, order_by, utcnow
from .workspace import Workspace

PROJECTS = Project.__table__
ORGS = Organization.__table__
RUNS = Run.__table__

SORTABLE = {"name": PROJECTS.c.name, "created_at": PROJECTS.c.created_at,
            "updated_at": PROJECTS.c.updated_at, "id": PROJECTS.c.id}


def _counts():
    orgs = (select(func.count()).select_from(ORGS)
            .where(ORGS.c.project_id == PROJECTS.c.id).scalar_subquery())
    runs = (select(func.count()).select_from(RUNS)
            .where(RUNS.c.project_id == PROJECTS.c.id).scalar_subquery())
    last = (select(func.max(RUNS.c.created_at))
            .where(RUNS.c.project_id == PROJECTS.c.id).scalar_subquery())
    return orgs.label("organizations"), runs.label("runs"), last.label("last_run_at")


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "name": row["name"], "slug": row["slug"],
        "description": row.get("description") or "",
        "retention_days": row.get("retention_days"),
        "organizations": int(row.get("organizations") or 0), "runs": int(row.get("runs") or 0),
        "last_run_at": aware(row.get("last_run_at")),
        "created_at": aware(row["created_at"]), "updated_at": aware(row.get("updated_at")),
    }


def _base_query(ws: Workspace):
    return select(PROJECTS, *_counts()).where(PROJECTS.c.workspace_id == ws.id)


def list_projects(engine: Engine, ws: Workspace, *, q: str | None = None,
                  sort: str | None = None, limit: int | None = None,
                  offset: int | None = None) -> Page[dict[str, Any]]:
    limit, offset = clamp(limit, offset)
    query = _base_query(ws)
    if q:
        query = query.where(PROJECTS.c.name.ilike(f"%{q}%"))
    query = query.order_by(*order_by(sort, SORTABLE, "name"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    return Page([_shape(r) for r in rows], total, limit, offset)


def get_project(engine: Engine, ws: Workspace, project_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(_base_query(ws).where(PROJECTS.c.id == project_id)).mappings().first()
    if row is None:
        raise NotFound(f"no project {project_id}")
    return _shape(dict(row))


def find_project(engine: Engine, ws: Workspace, name: str) -> dict[str, Any] | None:
    """The project a config's ``project`` name refers to, if it exists yet."""
    with engine.connect() as conn:
        row = conn.execute(_base_query(ws).where(
            PROJECTS.c.slug == slugify_project(name))).mappings().first()
    return _shape(dict(row)) if row else None


def _check_retention(days: int | None) -> None:
    if days is not None and int(days) < 1:
        raise Invalid("retention_days must be at least 1 (or null to keep organisations)")


def create_project(engine: Engine, ws: Workspace, *, name: str, description: str = "",
                   retention_days: int | None = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise Invalid("a project needs a name")
    _check_retention(retention_days)
    slug = slugify_project(name)
    now = utcnow()
    with engine.begin() as conn:
        clash = conn.execute(select(PROJECTS.c.id).where(
            PROJECTS.c.workspace_id == ws.id, PROJECTS.c.slug == slug)).first()
        if clash is not None:
            raise Conflict(f"a project named like {name!r} already exists (id {clash[0]})")
        project_id = conn.execute(PROJECTS.insert().values(
            workspace_id=ws.id, name=name, slug=slug, description=description or "",
            retention_days=retention_days, created_at=now, updated_at=now)
        ).inserted_primary_key[0]
    return get_project(engine, ws, int(project_id))


def ensure_project(engine: Engine, ws: Workspace, name: str) -> dict[str, Any]:
    return find_project(engine, ws, name) or create_project(engine, ws, name=name)


def update_project(engine: Engine, ws: Workspace, project_id: int,
                   **fields: Any) -> dict[str, Any]:
    """Change name, description or retention_days. A rename moves the slug too:
    configs that name the old project start a new one, so say so in the UI."""
    current = get_project(engine, ws, project_id)
    values: dict[str, Any] = {}
    if fields.get("name") is not None and fields["name"].strip() != current["name"]:
        name = fields["name"].strip()
        if not name:
            raise Invalid("a project needs a name")
        slug = slugify_project(name)
        with engine.connect() as conn:
            clash = conn.execute(select(PROJECTS.c.id).where(
                PROJECTS.c.workspace_id == ws.id, PROJECTS.c.slug == slug,
                PROJECTS.c.id != project_id)).first()
        if clash is not None:
            raise Conflict(f"a project named like {name!r} already exists (id {clash[0]})")
        values.update(name=name, slug=slug)
    if "description" in fields and fields["description"] is not None:
        values["description"] = fields["description"]
    if "retention_days" in fields:
        _check_retention(fields["retention_days"])
        values["retention_days"] = fields["retention_days"]
    if values:
        values["updated_at"] = utcnow()
        with engine.begin() as conn:
            conn.execute(update(PROJECTS).where(PROJECTS.c.id == project_id,
                                                PROJECTS.c.workspace_id == ws.id).values(**values))
    return get_project(engine, ws, project_id)


def delete_project(engine: Engine, ws: Workspace, project_id: int) -> dict[str, Any]:
    """Delete a project with its organisations and run history. Irreversible."""
    project = get_project(engine, ws, project_id)
    with engine.begin() as conn:
        # Checked and deleted in one transaction, queued runs included: a worker
        # must not claim a run into a project that is being deleted.
        active = [int(r[0]) for r in conn.execute(select(RUNS.c.id).where(
            RUNS.c.project_id == project_id,
            RUNS.c.status.in_(["queued", "running", "pausing", "stopping"])))]
        if active:
            raise Conflict(f"project {project_id} has queued or active runs "
                           f"({', '.join(map(str, active))}); cancel them first")
        conn.execute(delete(PROJECTS).where(PROJECTS.c.id == project_id,
                                            PROJECTS.c.workspace_id == ws.id))
    return project
