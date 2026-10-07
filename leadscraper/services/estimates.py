"""Estimates and previews of a config, before running it (Phase 3.1).

``estimate`` is the engine's dry run (``engine/estimator.py``): units per
lead type and source, p50/p90 duration, what fits a time budget, AI cost. It
makes no requests and needs no worker. It reads the workspace's learned unit
costs, so it improves with every run.

``submit_preview`` queues a ~2-minute sample run (``engine/preview.py``) for
a worker: the API process never scrapes. Its result -- sample records and the
calibrated full-run estimate -- lands on the run (``GET /runs/{id}``,
``preview``) when the worker is done.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from ..db.models import Organization, Project, Run
from .errors import Invalid
from .workspace import Workspace

PROJECTS = Project.__table__
ORGS = Organization.__table__
RUNS = Run.__table__


def _project_id(engine: Engine, ws: Workspace, slug: str) -> int | None:
    with engine.connect() as conn:
        row = conn.execute(select(PROJECTS.c.id).where(
            PROJECTS.c.workspace_id == ws.id, PROJECTS.c.slug == slug)).first()
    return int(row[0]) if row else None


def _drop_if_empty(engine: Engine, project_id: int) -> None:
    """An estimate must not leave an empty project behind."""
    with engine.begin() as conn:
        orgs = conn.execute(select(func.count()).select_from(ORGS)
                            .where(ORGS.c.project_id == project_id)).scalar_one()
        runs = conn.execute(select(func.count()).select_from(RUNS)
                            .where(RUNS.c.project_id == project_id)).scalar_one()
        if not orgs and not runs:
            conn.execute(delete(PROJECTS).where(PROJECTS.c.id == project_id))


def estimate(engine: Engine, ws: Workspace, document: dict[str, Any],
             overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..db.store import slugify_project
    from ..engine.runner import execute
    from .runs import build_request

    request = build_request(document, overrides)
    request.dry_run, request.print_plan, request.engine = True, False, engine
    slug = slugify_project(str(request.config.get("project")))
    existed = _project_id(engine, ws, slug) is not None
    outcome = execute(request)
    if not existed:
        created = _project_id(engine, ws, slug)
        if created is not None:
            _drop_if_empty(engine, created)
    return outcome.estimate


def submit_preview(engine: Engine, ws: Workspace, document: dict[str, Any], *,
                   seconds: float = 120, area: str | None = None, category: str | None = None,
                   records: int = 15) -> dict[str, Any]:
    from ..engine.preview import plan_preview
    from ..engine.runner import _snapshot
    from .runs import build_request, get_run, queue_request

    if not 10 <= float(seconds) <= 900:
        raise Invalid("a preview lasts between 10 and 900 seconds")
    original = build_request(document).config
    try:
        plan = plan_preview(original, seconds=float(seconds), area=area, category=category)
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    if not plan.localities or not plan.categories:
        raise Invalid("nothing to preview: no area/category combination runs in this config")
    request = plan.request()
    run_id = queue_request(engine, ws, request, extra={"preview": {
        "config": _snapshot(original), "seconds": float(seconds), "records": int(records)}})
    return get_run(engine, ws, run_id)
