"""Run history rows: a run's lifecycle, its work units and its failures.

Phase 2's worker drives the full state machine (queued -> running -> paused
-> ...); for now runs are created, completed or failed in-process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .models import FailureRow, Project, Run, WorkUnitRow, Workspace

RUNS = Run.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def start_run(engine: Engine, workspace_id: int, project_id: int, *, kind: str = "run",
              config_snapshot: dict[str, Any] | None = None) -> int:
    with engine.begin() as conn:
        return int(conn.execute(RUNS.insert().values(
            workspace_id=workspace_id, project_id=project_id, kind=kind, status="running",
            config_snapshot=config_snapshot or {}, started_at=utcnow(), stats={}, error="",
            created_at=utcnow())).inserted_primary_key[0])


def finish_run(engine: Engine, run_id: int, *, status: str = "completed",
               stats: dict[str, Any] | None = None, error: str = "") -> None:
    with engine.begin() as conn:
        conn.execute(update(RUNS).where(RUNS.c.id == run_id).values(
            status=status, finished_at=utcnow(), stats=stats or {}, error=error[:5000]))


def get_run(engine: Engine, run_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(RUNS).where(RUNS.c.id == run_id)).mappings().first()
    return dict(row) if row else None


def record_unit(engine: Engine, workspace_id: int, run_id: int, *, lead_type: str, source: str,
                area: str | None, category: str | None, scope: str, status: str, found: int,
                started_at: datetime, error: str = "") -> None:
    with engine.begin() as conn:
        conn.execute(UNITS.insert().values(
            workspace_id=workspace_id, run_id=run_id, lead_type=lead_type, source=source,
            area=area, category=category, scope=scope, status=status, found=found,
            started_at=started_at, finished_at=utcnow(), error=error[:5000]))


def record_failure(engine: Engine, workspace_id: int, entry: dict[str, Any], *,
                   project_id: int | None = None, run_id: int | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(FAILURES.insert().values(
            workspace_id=workspace_id, project_id=project_id, run_id=run_id,
            source=str(entry.get("source") or "")[:100], reason=str(entry.get("reason") or "")[:100],
            url=entry.get("url"), query=entry.get("query"), locality=entry.get("locality"),
            category=entry.get("category"), business=entry.get("business"),
            detail=str(entry.get("detail") or ""), created_at=utcnow()))


def project_id_for(engine: Engine, workspace: str, slug: str) -> int | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(Project.__table__.c.id)
            .join(Workspace.__table__, Workspace.__table__.c.id == Project.__table__.c.workspace_id)
            .where(Workspace.__table__.c.name == workspace, Project.__table__.c.slug == slug)
        ).first()
    return int(row[0]) if row else None
