"""The worker process: takes runs off the DB-backed queue (Phase 2.5).

``python -m leadscraper worker`` runs one. It:

* registers itself in ``workers`` and heartbeats there (Phase 3's ``/health``
  reads "worker alive" from it);
* first **recovers** runs a dead worker left behind -- an active state with a
  stale heartbeat -- and resumes each from its checkpoint (``running``), or
  settles it (``pausing`` -> ``paused``, ``stopping`` -> ``cancelled``);
* then claims ``queued`` runs, oldest first, one at a time, with an atomic
  conditional UPDATE (several workers can share one database);
* holds the keep-awake lock while a run is active (``runner.execute`` does).

Stopping it is graceful and needs no POSIX signal: ``leadscraper worker
stop`` sets ``stop_requested`` in the database, which the worker polls. Ctrl-C,
SIGTERM and (Windows) Ctrl-Break do the same. The run in flight finishes its
in-flight units, flushes, and goes back to ``queued``; the next worker picks
it up from its checkpoint.

The API process never scrapes: it only writes ``queued`` runs and
``pausing`` / ``stopping`` requests.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from ..db.engine import open_database
from ..db.models import Worker as WorkerRow
from ..db.runs import (
    ACTIVE, claim_run, finish_run, get_run, next_queued, orphaned_runs, settle_run, update_run,
)
from ..paths import default_db_url
from .control import RunControl
from .keepawake import KEEP_AWAKE, KeepAwake

log = logging.getLogger(__name__)

WORKERS = WorkerRow.__table__
#: A worker or run whose heartbeat is older than this is considered dead.
DEFAULT_STALE_SECONDS = 60.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Worker:
    def __init__(self, *, db_url: str | None = None, engine: Engine | None = None,
                 name: str | None = None, poll_seconds: float = 2.0,
                 stale_seconds: float = DEFAULT_STALE_SECONDS, heartbeat_seconds: float = 5.0,
                 keep_awake: KeepAwake | None = None, install_signals: bool = True):
        self.engine = engine or open_database(db_url or default_db_url())
        self._owns_engine = engine is None
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self.executor = f"worker:{self.name}"
        self.poll_seconds = poll_seconds
        self.stale_seconds = stale_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.keep_awake = keep_awake or KEEP_AWAKE
        self.install_signals = install_signals
        self._stop = threading.Event()
        self._control: RunControl | None = None
        self._control_lock = threading.RLock()   # taken in signal handlers
        self._beat_thread: threading.Thread | None = None
        self.runs_executed: list[int] = []

    # --- registration and heartbeat --------------------------------------------------

    def register(self) -> None:
        now = utcnow()
        values = dict(hostname=socket.gethostname(), pid=os.getpid(), status="idle",
                      current_run_id=None, stop_requested=False, started_at=now,
                      heartbeat_at=now)
        with self.engine.begin() as conn:
            exists = conn.execute(select(WORKERS.c.id).where(WORKERS.c.name == self.name)).first()
            if exists:
                conn.execute(update(WORKERS).where(WORKERS.c.name == self.name).values(**values))
            else:
                conn.execute(WORKERS.insert().values(name=self.name, **values))

    def _set(self, **values: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(WORKERS).where(WORKERS.c.name == self.name).values(**values))

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                with self.engine.begin() as conn:
                    conn.execute(update(WORKERS).where(WORKERS.c.name == self.name)
                                 .values(heartbeat_at=utcnow()))
                    row = conn.execute(select(WORKERS.c.stop_requested)
                                       .where(WORKERS.c.name == self.name)).first()
                if row is not None and row[0]:
                    log.warning("worker %s: stop requested", self.name)
                    self.stop()
            except Exception as exc:
                log.debug("worker heartbeat failed (ignored): %s", exc)

    # --- stopping ---------------------------------------------------------------------

    def stop(self) -> None:
        """Graceful: finish in-flight work, flush, requeue the run, exit."""
        self._stop.set()
        with self._control_lock:
            control = self._control
        if control is not None:
            control.request("shutdown")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _install_signals(self) -> dict:
        if not self.install_signals or threading.current_thread() is not threading.main_thread():
            return {}
        old = {}
        presses = [0]

        def handler(signum, frame):
            presses[0] += 1
            if presses[0] > 1:
                raise KeyboardInterrupt
            log.warning("worker %s stopping: finishing in-flight work (again to stop at once)",
                        self.name)
            self.stop()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                try:
                    old[number] = signal.signal(number, handler)
                except (ValueError, OSError):
                    pass
        return old

    # --- the queue ---------------------------------------------------------------------

    def recover(self) -> list[int]:
        """Settle or claim the runs dead workers left behind; returns claimed ids."""
        claimed = []
        stale_before = utcnow() - timedelta(seconds=self.stale_seconds)
        for run_id in orphaned_runs(self.engine, stale_seconds=self.stale_seconds):
            row = get_run(self.engine, run_id)
            if row is None:
                continue
            if row["status"] in ("pausing", "stopping"):
                target = "paused" if row["status"] == "pausing" else "cancelled"
                settle_run(self.engine, run_id, from_status=row["status"], to_status=target,
                           stale_before=stale_before,
                           error=f"its executor died while {row['status']}"
                                 + ("; resume to continue" if target == "paused" else ""))
                continue
            if claim_run(self.engine, run_id, self.executor, from_statuses=ACTIVE,
                         stale_before=stale_before):
                log.warning("worker %s: recovering run %s (its executor %s stopped "
                            "heartbeating)", self.name, run_id, row.get("worker_id"))
                claimed.append(run_id)
        return claimed

    def claim_next(self) -> int | None:
        recovered = self.recover()
        if recovered:
            # Execute one; any others stay claimed by us -- hand them back.
            for run_id in recovered[1:]:
                update_run(self.engine, run_id, status="queued", worker_id=None)
            return recovered[0]
        for run_id in next_queued(self.engine):
            if claim_run(self.engine, run_id, self.executor):
                return run_id
        return None

    def execute(self, run_id: int) -> str:
        """Execute (or resume) one claimed run; returns its final status."""
        from .runner import execute, request_from_row

        row = get_run(self.engine, run_id)
        if row is None:
            return "missing"
        self._set(status="busy", current_run_id=run_id)

        def keep(control: RunControl) -> None:
            with self._control_lock:
                self._control = control
            if self.stopping:
                control.request("shutdown")

        try:
            request = request_from_row(row, executor=self.executor, on_control=keep,
                                       heartbeat_seconds=self.heartbeat_seconds,
                                       keep_awake=self.keep_awake)
            outcome = execute(request)
            status = outcome.status
        except Exception as exc:  # a run that cannot even start fails; the worker lives on
            log.exception("worker %s: run %s failed to execute", self.name, run_id)
            finish_run(self.engine, run_id, status="failed", stats=row.get("stats") or {},
                       error=f"worker could not execute the run: {exc!r}",
                       worker_id=self.executor)
            status = "failed"
        finally:
            with self._control_lock:
                self._control = None
            self._set(status="idle", current_run_id=None)
        self.runs_executed.append(run_id)
        log.info("worker %s: run %s ended %s", self.name, run_id, status)
        return status

    def run(self, *, once: bool = False, max_runs: int | None = None) -> int:
        """Serve the queue until stopped. ``once``: exit when nothing is queued."""
        self.register()
        old_handlers = self._install_signals()
        self._beat_thread = threading.Thread(target=self._beat, name=f"worker-{self.name}",
                                             daemon=True)
        self._beat_thread.start()
        log.info("worker %s ready (poll %.1fs)", self.name, self.poll_seconds)
        try:
            while not self.stopping:
                run_id = self.claim_next()
                if run_id is not None:
                    self.execute(run_id)
                    if max_runs is not None and len(self.runs_executed) >= max_runs:
                        break
                    continue
                if once:
                    break
                self._stop.wait(self.poll_seconds)
        finally:
            self._stop.set()
            try:
                self._set(status="stopped", current_run_id=None, heartbeat_at=utcnow())
            except Exception:
                pass
            for number, old in old_handlers.items():
                try:
                    signal.signal(number, old)
                except (ValueError, OSError, TypeError):
                    pass
            if self._owns_engine:
                self.engine.dispose()
        return len(self.runs_executed)


def request_worker_stop(engine: Engine, name: str | None = None) -> int:
    """Ask one worker (or all) to stop gracefully; returns how many were asked."""
    query = update(WORKERS).where(WORKERS.c.status != "stopped").values(stop_requested=True)
    if name:
        query = query.where(WORKERS.c.name == name)
    with engine.begin() as conn:
        return conn.execute(query).rowcount


def list_workers(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(WORKERS).order_by(WORKERS.c.id)).mappings()]


def submit_run(request) -> int:
    """Queue a run for a worker instead of running it here; returns its id."""
    from ..db.runs import start_run
    from ..db.store import LeadStore, store_target
    from .runner import _snapshot, request_document

    config = request.config
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        return start_run(store.engine, store.workspace_id, store.project_id, kind=request.kind,
                         config_snapshot=_snapshot(config), status="queued",
                         request=request_document(request))
    finally:
        store.close()
