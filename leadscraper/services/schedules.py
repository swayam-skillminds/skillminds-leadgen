"""Schedules: recurring runs of a saved config (Phase 3.1).

A schedule names a saved config and a cron expression (``core.cron``, read
in the schedule's timezone). Each time it is due, the worker queues a run of
the config's *latest* version with the schedule's overrides (depth, time
budget). With ``only_new``, the run remembers the schedule's previous run,
and its exports default to "new since then" -- next month's run delivers
only what is new.

Firing is a conditional UPDATE on ``next_run_at``, so two workers sharing a
database never queue the same firing twice.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine

from ..core.cron import CronError, next_run
from ..db.models import SavedConfig, Schedule
from .errors import Invalid, NotFound
from .query import Page, aware, clamp, fetch_page, order_by, utcnow
from .workspace import WORKSPACES, Workspace

log = logging.getLogger(__name__)

SCHEDULES = Schedule.__table__
CONFIGS = SavedConfig.__table__
OVERRIDES = ("depth", "time_budget_minutes")


def _check(cron: str, tz: str) -> datetime:
    try:
        return next_run(cron, utcnow(), tz)
    except CronError as exc:
        raise Invalid(str(exc)) from None


def _overrides(values: dict[str, Any] | None) -> dict[str, Any]:
    values = {k: v for k, v in (values or {}).items() if v is not None}
    unknown = set(values) - set(OVERRIDES)
    if unknown:
        raise Invalid(f"schedules can override {', '.join(OVERRIDES)} only "
                      f"(got {', '.join(sorted(unknown))})")
    if values.get("depth") not in (None, "quick", "balanced", "deep"):
        raise Invalid("depth must be quick, balanced or deep")
    return values


def _shape(row: dict[str, Any], config_name: str | None) -> dict[str, Any]:
    return {"id": int(row["id"]), "name": row["name"] or config_name or f"schedule {row['id']}",
            "saved_config_id": int(row["saved_config_id"]), "config_name": config_name,
            "cron": row["cron"], "timezone": row["timezone"] or "UTC",
            "enabled": bool(row["enabled"]), "only_new": bool(row["only_new"]),
            "overrides": dict(row.get("overrides") or {}),
            "next_run_at": aware(row.get("next_run_at")),
            "last_run_at": aware(row.get("last_run_at")), "last_run_id": row.get("last_run_id"),
            "created_at": aware(row["created_at"])}


def _query(ws: Workspace):
    return (select(SCHEDULES, CONFIGS.c.name.label("config_name"))
            .join(CONFIGS, CONFIGS.c.id == SCHEDULES.c.saved_config_id, isouter=True)
            .where(SCHEDULES.c.workspace_id == ws.id))


def list_schedules(engine: Engine, ws: Workspace, *, saved_config_id: int | None = None,
                   enabled: bool | None = None, sort: str | None = None,
                   limit: int | None = None, offset: int | None = None) -> Page[dict[str, Any]]:
    limit, offset = clamp(limit, offset)
    query = _query(ws)
    if saved_config_id is not None:
        query = query.where(SCHEDULES.c.saved_config_id == saved_config_id)
    if enabled is not None:
        query = query.where(SCHEDULES.c.enabled.is_(enabled))
    query = query.order_by(*order_by(sort, {"id": SCHEDULES.c.id, "name": SCHEDULES.c.name,
                                            "next_run_at": SCHEDULES.c.next_run_at}, "id"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    return Page([_shape(r, r["config_name"]) for r in rows], total, limit, offset)


def get_schedule(engine: Engine, ws: Workspace, schedule_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(_query(ws).where(SCHEDULES.c.id == schedule_id)).mappings().first()
    if row is None:
        raise NotFound(f"no schedule {schedule_id}")
    return _shape(dict(row), row["config_name"])


def create_schedule(engine: Engine, ws: Workspace, *, saved_config_id: int, cron: str,
                    timezone: str = "UTC", name: str = "", only_new: bool = False,
                    enabled: bool = True,
                    overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    from .configs import get_config

    get_config(engine, ws, saved_config_id)                 # must exist
    next_at = _check(cron, timezone)
    with engine.begin() as conn:
        schedule_id = conn.execute(SCHEDULES.insert().values(
            workspace_id=ws.id, saved_config_id=saved_config_id, cron=cron.strip(),
            timezone=timezone or "UTC", name=(name or "").strip(), only_new=only_new,
            enabled=enabled, overrides=_overrides(overrides),
            next_run_at=next_at if enabled else None, created_at=utcnow())
        ).inserted_primary_key[0]
    return get_schedule(engine, ws, int(schedule_id))


def update_schedule(engine: Engine, ws: Workspace, schedule_id: int,
                    **fields: Any) -> dict[str, Any]:
    current = get_schedule(engine, ws, schedule_id)
    values: dict[str, Any] = {}
    for key in ("name", "only_new", "enabled", "cron", "timezone"):
        if fields.get(key) is not None:
            values[key] = fields[key]
    if fields.get("overrides") is not None:
        values["overrides"] = _overrides(fields["overrides"])
    cron = values.get("cron", current["cron"])
    tz = values.get("timezone", current["timezone"])
    enabled = values.get("enabled", current["enabled"])
    next_at = _check(cron, tz)
    values["next_run_at"] = next_at if enabled else None
    with engine.begin() as conn:
        conn.execute(update(SCHEDULES).where(SCHEDULES.c.id == schedule_id).values(**values))
    return get_schedule(engine, ws, schedule_id)


def delete_schedule(engine: Engine, ws: Workspace, schedule_id: int) -> dict[str, Any]:
    shaped = get_schedule(engine, ws, schedule_id)
    with engine.begin() as conn:
        conn.execute(delete(SCHEDULES).where(SCHEDULES.c.id == schedule_id))
    return shaped


def _queue(engine: Engine, ws: Workspace, row: dict[str, Any]) -> int:
    from .configs import get_config
    from .runs import build_request, queue_request

    saved = get_config(engine, ws, int(row["saved_config_id"]))     # the latest version
    request = build_request(saved["document"], dict(row.get("overrides") or {}))
    extra: dict[str, Any] = {"schedule_id": int(row["id"])}
    if row.get("only_new"):
        extra["only_new_since"] = row.get("last_run_id")
    return queue_request(engine, ws, request, saved_config_id=saved["version_id"], extra=extra)


def _mark_fired(engine: Engine, schedule_id: int, run_id: int, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(update(SCHEDULES).where(SCHEDULES.c.id == schedule_id).values(
            last_run_at=when, last_run_id=run_id))


def run_now(engine: Engine, ws: Workspace, schedule_id: int) -> int:
    """Queue the schedule's run immediately (its timetable is unchanged)."""
    get_schedule(engine, ws, schedule_id)
    with engine.connect() as conn:
        row = dict(conn.execute(select(SCHEDULES).where(
            SCHEDULES.c.id == schedule_id)).mappings().one())
    run_id = _queue(engine, ws, row)
    _mark_fired(engine, schedule_id, run_id, utcnow())
    return run_id


def fire_due(engine: Engine, now: datetime | None = None) -> list[int]:
    """Queue a run for every enabled schedule that is due; returns the run ids.

    Called by the worker on every poll. A firing that cannot queue (its saved
    config was changed into an invalid one) moves on to its next time and is
    logged; it never stops the worker.
    """
    now = now or utcnow()
    with engine.connect() as conn:
        due = [dict(r) for r in conn.execute(select(SCHEDULES).where(
            SCHEDULES.c.enabled.is_(True), SCHEDULES.c.next_run_at.is_not(None),
            SCHEDULES.c.next_run_at <= now)).mappings()]
        names = {int(r.id): r.name for r in conn.execute(select(WORKSPACES.c.id,
                                                                 WORKSPACES.c.name))}
    queued = []
    for row in due:
        try:
            following = next_run(row["cron"], now, row["timezone"] or "UTC")
        except CronError as exc:
            log.error("schedule %s: %s; disabling it", row["id"], exc)
            with engine.begin() as conn:
                conn.execute(update(SCHEDULES).where(SCHEDULES.c.id == row["id"])
                             .values(enabled=False, next_run_at=None))
            continue
        with engine.begin() as conn:                 # claim this firing
            claimed = conn.execute(update(SCHEDULES).where(
                SCHEDULES.c.id == row["id"], SCHEDULES.c.next_run_at == row["next_run_at"])
                .values(next_run_at=following)).rowcount == 1
        if not claimed:
            continue
        ws = Workspace(int(row["workspace_id"]), names.get(int(row["workspace_id"]), ""))
        try:
            run_id = _queue(engine, ws, row)
        except Exception as exc:
            log.error("schedule %s could not queue its run: %s", row["id"], exc)
            continue
        _mark_fired(engine, int(row["id"]), run_id, now)
        log.info("schedule %s queued run %s (next %s)", row["id"], run_id, following)
        queued.append(run_id)
    return queued
