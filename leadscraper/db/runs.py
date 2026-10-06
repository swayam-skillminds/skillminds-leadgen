"""Run history rows: a run's lifecycle, its work units and its failures.

Lifecycle (Phase 2)::

    queued -> running -> completed | failed
                 |  \\-> pausing -> paused -> (resume) queued
                 \\----> stopping -> cancelled

* ``queued`` runs wait for a worker (``engine/worker.py``), which claims one
  atomically (``claim_run``) and executes it.
* ``pausing`` / ``stopping`` are requests (``request_control``): the runner
  sees them on its next heartbeat, lets in-flight work finish, flushes, and
  sets ``paused`` / ``cancelled``.
* A run whose executor died (no heartbeat) is resumed from its checkpoint:
  its ``work_units`` rows say which units are done.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import Engine

from .models import FailureRow, Project, Run, WorkUnitRow, Workspace

RUNS = Run.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__

#: States a run can be in, in rough lifecycle order.
STATES = ("queued", "running", "pausing", "paused", "stopping", "completed", "failed",
          "cancelled")
TERMINAL = frozenset({"completed", "failed", "cancelled"})
#: States in which an executor is (or was, if it died) working on the run.
ACTIVE = frozenset({"running", "pausing", "stopping"})


class TransitionError(ValueError):
    """The requested lifecycle change does not apply to the run's state."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def start_run(engine: Engine, workspace_id: int, project_id: int, *, kind: str = "run",
              config_snapshot: dict[str, Any] | None = None, status: str = "running",
              request: dict[str, Any] | None = None, worker_id: str | None = None) -> int:
    now = utcnow()
    running = status == "running"
    with engine.begin() as conn:
        return int(conn.execute(RUNS.insert().values(
            workspace_id=workspace_id, project_id=project_id, kind=kind, status=status,
            config_snapshot=config_snapshot or {}, request=request or {},
            started_at=now if running else None, stats={}, error="", created_at=now,
            worker_id=worker_id if running else None, heartbeat_at=now if running else None,
            attempts=1 if running else 0)).inserted_primary_key[0])


def finish_run(engine: Engine, run_id: int, *, status: str = "completed",
               stats: dict[str, Any] | None = None, error: str = "") -> None:
    values: dict[str, Any] = {"status": status, "stats": stats or {}, "error": error[:5000]}
    if status in TERMINAL:
        values["finished_at"] = utcnow()
    if status not in ACTIVE:
        values["worker_id"] = None
    with engine.begin() as conn:
        conn.execute(update(RUNS).where(RUNS.c.id == run_id).values(**values))


def update_run(engine: Engine, run_id: int, **values: Any) -> None:
    with engine.begin() as conn:
        conn.execute(update(RUNS).where(RUNS.c.id == run_id).values(**values))


def get_run(engine: Engine, run_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(RUNS).where(RUNS.c.id == run_id)).mappings().first()
    return dict(row) if row else None


def list_runs(engine: Engine, *, project_id: int | None = None,
              statuses: Iterable[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = select(RUNS).order_by(RUNS.c.id.desc()).limit(limit)
    if project_id is not None:
        query = query.where(RUNS.c.project_id == project_id)
    if statuses:
        query = query.where(RUNS.c.status.in_(list(statuses)))
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(query).mappings().all()]


# --- the queue ---------------------------------------------------------------------

def claim_run(engine: Engine, run_id: int, worker_id: str, *,
              from_statuses: Iterable[str] = ("queued",),
              stale_before: datetime | None = None) -> bool:
    """Atomically take a run for `worker_id`. False if someone else got it first.

    A single conditional UPDATE: on SQLite and Postgres alike only one
    claimant's statement can match the row.
    """
    condition = and_(RUNS.c.id == run_id, RUNS.c.status.in_(list(from_statuses)))
    if stale_before is not None:
        condition = and_(condition, or_(RUNS.c.heartbeat_at.is_(None),
                                        RUNS.c.heartbeat_at < stale_before))
    now = utcnow()
    with engine.begin() as conn:
        claimed = conn.execute(update(RUNS).where(condition).values(
            status="running", worker_id=worker_id, heartbeat_at=now,
            attempts=RUNS.c.attempts + 1)).rowcount
    return claimed == 1


def next_queued(engine: Engine) -> list[int]:
    """Queued run ids, oldest first."""
    with engine.connect() as conn:
        rows = conn.execute(select(RUNS.c.id).where(RUNS.c.status == "queued")
                            .order_by(RUNS.c.created_at, RUNS.c.id)).all()
    return [int(r[0]) for r in rows]


def orphaned_runs(engine: Engine, *, stale_seconds: float,
                  executor_prefix: str = "worker:") -> list[int]:
    """Runs a worker was executing when it died: active state, no recent heartbeat.

    Only runs a *worker* held are recovered automatically; a CLI run that
    crashed is resumed by its user (``leadscraper runs resume``).
    """
    cutoff = utcnow() - timedelta(seconds=stale_seconds)
    with engine.connect() as conn:
        rows = conn.execute(select(RUNS.c.id, RUNS.c.heartbeat_at, RUNS.c.worker_id).where(
            RUNS.c.status.in_(list(ACTIVE)))).all()
    out = []
    for row in rows:
        beat = _aware(row.heartbeat_at)
        if (row.worker_id or "").startswith(executor_prefix) and (beat is None or beat < cutoff):
            out.append(int(row.id))
    return out


def heartbeat(engine: Engine, run_id: int, *, active_seconds: float) -> str | None:
    """Record that the run is alive; returns its status (a pause/cancel request shows here)."""
    with engine.begin() as conn:
        conn.execute(update(RUNS).where(RUNS.c.id == run_id).values(
            heartbeat_at=utcnow(), active_seconds=float(active_seconds)))
        row = conn.execute(select(RUNS.c.status).where(RUNS.c.id == run_id)).first()
    return row[0] if row else None


def request_control(engine: Engine, run_id: int, action: str) -> str:
    """Pause, resume or cancel a run; returns its new status.

    ====== ======================= =============================================
    action from                    to
    ====== ======================= =============================================
    pause  queued / running        paused / pausing (the runner finishes it)
    cancel queued, paused / active cancelled / stopping
    resume paused, failed          queued (a worker resumes it from checkpoint)
    ====== ======================= =============================================
    """
    transitions = {
        "pause": {"queued": "paused", "running": "pausing", "pausing": "pausing"},
        "cancel": {"queued": "cancelled", "paused": "cancelled", "failed": "cancelled",
                   "running": "stopping", "pausing": "stopping", "stopping": "stopping"},
        "resume": {"paused": "queued", "failed": "queued", "queued": "queued"},
    }
    if action not in transitions:
        raise TransitionError(f"unknown action {action!r}")
    with engine.begin() as conn:
        row = conn.execute(select(RUNS.c.status).where(RUNS.c.id == run_id)).first()
        if row is None:
            raise TransitionError(f"no run {run_id}")
        current = row[0]
        target = transitions[action].get(current)
        if target is None:
            raise TransitionError(f"cannot {action} a run that is {current}")
        values: dict[str, Any] = {"status": target}
        if target == "cancelled":
            values["finished_at"] = utcnow()
            values["worker_id"] = None
        if target == "queued":
            values.update(finished_at=None, error="")
        conn.execute(update(RUNS).where(RUNS.c.id == run_id, RUNS.c.status == current)
                     .values(**values))
    return target


# --- work units (the checkpoint) ----------------------------------------------------

def plan_units(engine: Engine, workspace_id: int, run_id: int,
               rows: list[dict[str, Any]]) -> None:
    """Record the planned units as ``pending``."""
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(UNITS.insert(), [{
            "workspace_id": workspace_id, "run_id": run_id, "status": "pending", "found": 0,
            "error": "", "skip_reason": "", "new": 0, "merged": 0, **row} for row in rows])


def unit_rows(engine: Engine, run_id: int) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(UNITS).where(UNITS.c.run_id == run_id).order_by(UNITS.c.id)).mappings()]


def mark_unit(engine: Engine, run_id: int, unit_key: str, **values: Any) -> None:
    with engine.begin() as conn:
        conn.execute(update(UNITS).where(UNITS.c.run_id == run_id, UNITS.c.unit_key == unit_key)
                     .values(**values))


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
