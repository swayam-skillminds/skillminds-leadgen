"""Retention (Phase 3.1 settings): purge what is old, keep what matters.

Workspace settings (``retention``):

* ``logs_days`` -- run logs and failure logs (``*.log``, ``*.jsonl``) under
  the data directory's ``projects/*/logs``;
* ``events_days`` -- the progress events of runs finished that long ago
  (the run row, its units, failures and summary stay in history);
* ``seen_urls_days`` -- fetched-URL memos (those pages get fetched again).

Per project, ``retention_days`` purges organisations not updated for that
long (lead retention), with everything hanging off them.

The worker runs this at most once an hour; ``leadscraper purge`` runs it on
demand (``--dry-run`` to see what would go).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from ..db.models import Organization, Project, Run, RunEvent, SeenUrl
from ..db.runs import TERMINAL
from .query import utcnow
from .settings import effective
from .workspace import Workspace

log = logging.getLogger(__name__)

EVENTS = RunEvent.__table__
RUNS = Run.__table__
SEEN = SeenUrl.__table__
ORGS = Organization.__table__
PROJECTS = Project.__table__


def _old_logs(days: int) -> list[Path]:
    from ..paths import data_dir

    cutoff = time.time() - days * 86400
    root = data_dir() / "projects"
    if not root.is_dir():
        return []
    return [p for pattern in ("*/logs/*.log", "*/logs/*.jsonl") for p in root.glob(pattern)
            if p.is_file() and p.stat().st_mtime < cutoff]


def purge(engine: Engine, ws: Workspace, *, dry_run: bool = False) -> dict[str, Any]:
    settings = effective(engine, ws.id).retention
    now = utcnow()
    report: dict[str, Any] = {"dry_run": dry_run, "events": 0, "seen_urls": 0, "log_files": 0,
                              "organizations": {}}
    with engine.begin() as conn:
        if settings.events_days:
            cutoff = now - timedelta(days=settings.events_days)
            old_runs = select(RUNS.c.id).where(RUNS.c.workspace_id == ws.id,
                                               RUNS.c.status.in_(list(TERMINAL)),
                                               RUNS.c.finished_at < cutoff)
            condition = EVENTS.c.run_id.in_(old_runs)
            report["events"] = int(conn.execute(select(func.count()).select_from(EVENTS)
                                                .where(condition)).scalar_one())
            if not dry_run and report["events"]:
                conn.execute(delete(EVENTS).where(condition))
        if settings.seen_urls_days:
            cutoff_epoch = time.time() - settings.seen_urls_days * 86400
            condition = (SEEN.c.workspace_id == ws.id) & (SEEN.c.fetched_at < cutoff_epoch)
            report["seen_urls"] = int(conn.execute(select(func.count()).select_from(SEEN)
                                                   .where(condition)).scalar_one())
            if not dry_run and report["seen_urls"]:
                conn.execute(delete(SEEN).where(condition))
        projects = conn.execute(select(PROJECTS.c.id, PROJECTS.c.name, PROJECTS.c.retention_days)
                                .where(PROJECTS.c.workspace_id == ws.id,
                                       PROJECTS.c.retention_days.is_not(None))).all()
        for project_id, name, days in projects:
            cutoff = now - timedelta(days=int(days))
            condition = (ORGS.c.project_id == project_id) & (ORGS.c.updated_at < cutoff)
            count = int(conn.execute(select(func.count()).select_from(ORGS)
                                     .where(condition)).scalar_one())
            if count:
                report["organizations"][name] = count
                if not dry_run:
                    conn.execute(delete(ORGS).where(condition))
    if settings.logs_days:
        files = _old_logs(settings.logs_days)
        report["log_files"] = len(files)
        if not dry_run:
            for path in files:
                try:
                    path.unlink()
                except OSError as exc:
                    log.debug("could not delete %s: %s", path, exc)
    if not dry_run and (report["events"] or report["seen_urls"] or report["log_files"]
                        or report["organizations"]):
        log.info("retention purge: %s", report)
    return report
