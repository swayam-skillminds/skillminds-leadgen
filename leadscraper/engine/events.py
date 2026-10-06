"""The run's typed progress stream (APP-BUILD-PROMPT Phase 2.8).

Every event is a pydantic model with a ``type`` discriminator, so the future
UI (and the Phase 3 SSE endpoint) can be generated from ``event_json_schema()``.
Events are numbered per run (``seq``) and persisted in ``run_events``, so a
client that connects late -- or reads history next month -- replays exactly
what a live viewer saw, then continues from the last ``seq`` it has.

Types:

=================  ==========================================================
``run_state``      the run entered a lifecycle state (queued, running, ...)
``plan``           units planned, per lead type and source, and the estimate
``unit_started``   a work unit began
``unit_finished``  a work unit ended: found / new / merged, seconds, status
``unit_skipped``   a unit will not run, and why (time budget, stop request)
``progress``       counts, elapsed, live ETA (p50/p90), budget left
``activity``       what the run is doing now ("website enrichment: 40/310")
``failure``        a source could not fetch or parse something (reason code)
``ai_spend``       AI cost so far against its cap
``sleep_detected`` the machine slept; connections were re-validated
``run_finished``   the final status and summary, skipped units included
=================  ==========================================================
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Event(BaseModel):
    run_id: int | None = None
    seq: int = 0
    at: datetime = Field(default_factory=_utcnow)


class RunState(_Event):
    type: Literal["run_state"] = "run_state"
    status: str
    reason: str = ""


class PlanEvent(_Event):
    type: Literal["plan"] = "plan"
    units_total: int
    units_resumed: int = 0
    #: lead type -> source -> units
    breakdown: dict[str, dict[str, int]] = Field(default_factory=dict)
    estimate: dict[str, Any] = Field(default_factory=dict)


class UnitStarted(_Event):
    type: Literal["unit_started"] = "unit_started"
    unit: str
    lead_type: str
    source: str
    area: str | None = None
    category: str | None = None
    expected_seconds: float | None = None


class UnitFinished(_Event):
    type: Literal["unit_finished"] = "unit_finished"
    unit: str
    lead_type: str
    source: str
    status: str
    found: int = 0
    new: int = 0
    merged: int = 0
    seconds: float = 0.0
    error: str = ""


class UnitSkipped(_Event):
    type: Literal["unit_skipped"] = "unit_skipped"
    unit: str
    lead_type: str
    source: str
    reason: str


class Progress(_Event):
    type: Literal["progress"] = "progress"
    units_done: int
    units_total: int
    units_skipped: int = 0
    new: int = 0
    merged: int = 0
    elapsed_seconds: float = 0.0
    eta_p50_seconds: float | None = None
    eta_p90_seconds: float | None = None
    budget_remaining_seconds: float | None = None


class Activity(_Event):
    type: Literal["activity"] = "activity"
    phase: str
    text: str
    lead_type: str | None = None


class FailureEvent(_Event):
    type: Literal["failure"] = "failure"
    source: str
    reason: str
    detail: str = ""
    url: str | None = None


class AISpend(_Event):
    type: Literal["ai_spend"] = "ai_spend"
    cost_usd: float
    cap_usd: float | None = None
    calls: int = 0


class SleepDetected(_Event):
    type: Literal["sleep_detected"] = "sleep_detected"
    gap_seconds: float


class RunFinished(_Event):
    type: Literal["run_finished"] = "run_finished"
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


RunEvent = Annotated[
    Union[RunState, PlanEvent, UnitStarted, UnitFinished, UnitSkipped, Progress, Activity,
          FailureEvent, AISpend, SleepDetected, RunFinished],
    Field(discriminator="type"),
]
_ADAPTER: TypeAdapter = TypeAdapter(RunEvent)


def parse_event(data: dict[str, Any]) -> _Event:
    return _ADAPTER.validate_python(data)


def event_json_schema() -> dict[str, Any]:
    return _ADAPTER.json_schema()


# --- the bus ------------------------------------------------------------------------

Sink = Callable[[_Event], None]


class EventBus:
    """Numbers events and hands them to every sink. A failing sink never stops a run."""

    def __init__(self, run_id: int | None = None, *, start_seq: int = 0):
        self.run_id = run_id
        self._seq = start_seq
        self._lock = threading.Lock()
        self._sinks: list[Sink] = []

    def subscribe(self, sink: Sink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def publish(self, event: _Event) -> _Event:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            event.run_id = self.run_id
            # Sinks run under the lock so persisted order == seq order.
            for sink in self._sinks:
                try:
                    sink(event)
                except Exception as exc:
                    log.debug("event sink failed (ignored): %s", exc)
        return event


class DbEventSink:
    """Persists events into ``run_events`` from one writer thread.

    Publishing only enqueues, so no unit, enrichment task or heartbeat waits on
    a database write (a locked SQLite file can take seconds); the single writer
    keeps them in ``seq`` order. ``close()`` drains the queue.
    """

    def __init__(self, engine: Engine, workspace_id: int):
        import queue

        from ..db.models import RunEvent as RunEventRow

        self.engine = engine
        self.workspace_id = workspace_id
        self.table = RunEventRow.__table__
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._write, name="event-writer", daemon=True)
        self._thread.start()

    def __call__(self, event: _Event) -> None:
        if event.run_id is None:
            return
        payload = event.model_dump(mode="json", exclude={"run_id", "seq", "at", "type"})
        self._queue.put({"workspace_id": self.workspace_id, "run_id": event.run_id,
                         "seq": event.seq, "type": event.type, "payload": payload,
                         "created_at": event.at})

    def _write(self) -> None:
        import queue

        while True:
            row = self._queue.get()
            if row is None:
                return
            batch = [row]
            try:                                   # whatever else is waiting, in one go
                while len(batch) < 200:
                    more = self._queue.get_nowait()
                    if more is None:
                        self._queue.put(None)
                        break
                    batch.append(more)
            except queue.Empty:
                pass
            try:
                with self.engine.begin() as conn:
                    conn.execute(self.table.insert(), batch)
            except Exception as exc:
                log.warning("could not persist %d event(s): %s", len(batch), exc)

    def close(self, timeout: float = 30.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout=timeout)


def last_seq(engine: Engine, run_id: int) -> int:
    from ..db.models import RunEvent as RunEventRow

    table = RunEventRow.__table__
    with engine.connect() as conn:
        value = conn.execute(select(func.max(table.c.seq)).where(table.c.run_id == run_id)).scalar()
    return int(value or 0)


def read_events(engine: Engine, run_id: int, *, after: int = 0,
                limit: int | None = None) -> list[_Event]:
    """Events of a run in order, from ``seq > after`` (replay, or catch up)."""
    from ..db.models import RunEvent as RunEventRow

    table = RunEventRow.__table__
    query = (select(table.c.seq, table.c.type, table.c.payload, table.c.created_at)
             .where(table.c.run_id == run_id, table.c.seq > after).order_by(table.c.seq))
    if limit:
        query = query.limit(limit)
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    events = []
    for row in rows:
        at = row.created_at
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        events.append(parse_event({**(row.payload or {}), "type": row.type, "seq": row.seq,
                                   "run_id": run_id, "at": at}))
    return events


def describe(event: _Event) -> str:
    """One human line per event, for logs and ``runs events``."""
    kind = getattr(event, "type", "")
    if kind == "run_state":
        return f"run {event.status}" + (f" ({event.reason})" if event.reason else "")
    if kind == "plan":
        est = event.estimate or {}
        span = ""
        if est.get("p50_seconds") is not None:
            span = (f"; estimate {est['p50_seconds'] / 60:.1f}-"
                    f"{est['p90_seconds'] / 60:.1f} min (p50-p90)")
        resumed = f", {event.units_resumed} already done" if event.units_resumed else ""
        return f"planned {event.units_total} units{resumed}{span}"
    if kind == "unit_started":
        return f"start {event.unit}"
    if kind == "unit_finished":
        return (f"done  {event.unit}: {event.status}, {event.found} found, {event.new} new, "
                f"{event.merged} merged in {event.seconds:.1f}s")
    if kind == "unit_skipped":
        return f"skip  {event.unit}: {event.reason}"
    if kind == "progress":
        eta = ""
        if event.eta_p50_seconds is not None:
            eta = (f", ETA {event.eta_p50_seconds / 60:.1f}-"
                   f"{(event.eta_p90_seconds or 0) / 60:.1f} min")
        return (f"progress {event.units_done}/{event.units_total} units, {event.new} new, "
                f"{event.merged} merged, {event.elapsed_seconds / 60:.1f} min{eta}")
    if kind == "activity":
        return f"[{event.phase}] {event.text}"
    if kind == "failure":
        return f"failure {event.source}: {event.reason}"
    if kind == "ai_spend":
        return f"AI spend ${event.cost_usd:.3f}"
    if kind == "sleep_detected":
        return f"machine slept ~{event.gap_seconds:.0f}s; connections re-validated"
    if kind == "run_finished":
        return f"run finished: {event.status}" + (f" ({event.error})" if event.error else "")
    return kind
