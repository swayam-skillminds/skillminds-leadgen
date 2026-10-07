"""Run a config: one project store, one run in history, every enabled lead type.

The engine owns the run (APP-BUILD-PROMPT Phase 2):

1. **Plan.** Each enabled lead type opens a session and lists its work units;
   the depth preset has already scaled their knobs. The estimator answers
   "how long, what fits the budget" before anything starts.
2. **Discover.** The scheduler hands out units -- best expected yield per
   second first when there is a time budget, plan order otherwise -- and the
   dispatcher runs them in parallel across hosts, never two on one host.
   Every finished unit is a checkpoint (its ``work_units`` row), so a killed
   process loses at most the units that were in flight.
3. **Post-discovery.** Enrichment and AI passes, stopping at the deadline.
4. **Finalise.** Always, even after a pause or cancel: rescore and flush.

Guarantees carried over from Phase 1: every lead type writes into the same
project, its rows tagged with its key; a lead type that crashes is contained
-- the others still run and the run is recorded with the error (A.3); the
run's config snapshot, failures, units and organisations are in the database.

The same ``execute`` serves the CLI (in-process; Ctrl-C = pause), the worker
(queued runs, crash recovery) and the preview.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..config import Config
from ..db.runs import (
    ACTIVE, LOST, claim_run, finish_run, get_run, heartbeat, mark_unit, plan_units,
    record_failure, start_run, unit_rows, update_run,
)
from ..db.store import LeadStore, store_target
from ..leadtypes import registry
from ..leadtypes.base import LeadTypeResult, UnitOutcome
from . import events as ev
from .clock import SYSTEM_CLOCK, Clock
from .control import RunControl
from .costs import CostModel, UnitStatsReader, prior_cost
from .estimator import LiveEta, describe_estimate, estimate
from .keepawake import KEEP_AWAKE, KeepAwake, SleepWatch
from .parallel import run_across_hosts
from .scheduler import Planned, Scheduler

log = logging.getLogger(__name__)

#: Defaults for the engine's runtime knobs (``runtime.*`` in a run config).
DEFAULT_DISCOVERY_WORKERS = 3
DEFAULT_TAIL_SECONDS = 90.0
DEFAULT_ENRICHMENT_SHARE = 0.3
DEFAULT_HEARTBEAT_SECONDS = 5.0
#: A run whose heartbeat is older than this may be taken over (resume).
DEFAULT_STALE_SECONDS = 60.0
#: How long a stop waits for in-flight units before abandoning them.
DEFAULT_STOP_GRACE_SECONDS = 120.0


class RunBusyError(RuntimeError):
    """The run is held by another live executor."""


@dataclass
class RunRequest:
    config: Config
    localities: list | None = None
    categories: list | None = None
    source_names: list[str] | None = None
    enricher_names: list[str] | None = None
    dry_run: bool = False
    #: Restrict to these lead types; default: every one the config enables.
    lead_types: list[str] | None = None
    #: CLI mode: Ctrl-C pauses gracefully (a second one stops at once).
    install_signals: bool = True
    # --- Phase 2 ---
    #: Resume this run (its checkpoint) instead of starting a new one.
    run_id: int | None = None
    depth: str | None = None
    time_budget_minutes: float | None = None
    force_enrich: bool = False
    discovery_workers: int | None = None
    kind: str = "run"
    #: "worker:<name>"; default "cli:<host>:<pid>".
    executor: str | None = None
    clock: Clock | None = None
    heartbeat_seconds: float | None = None
    #: Extra event sinks (the CLI's live printer, tests).
    sinks: list[Callable] = field(default_factory=list)
    #: Called with the run's RunControl as soon as it exists (the worker uses
    #: it to forward its own shutdown).
    on_control: Callable[[RunControl], None] | None = None
    keep_awake: KeepAwake | None = None
    #: Print the dry-run plan (the CLI); False just returns the estimate.
    print_plan: bool = True
    # --- Phase 3 ---
    #: The application database (an Engine) to run in: the worker's or the
    #: API's. Default: the one the config names (``runtime.db_url``, else
    #: ``LEADSCRAPER_DB_URL``, else the per-user database).
    engine: Any = None
    #: Tell the workspace's notification targets how the run ended.
    notify: bool = True


@dataclass
class LeadTypeContext:
    """What a lead type's session (or ``run()``) gets."""

    config: Config
    store: LeadStore | None
    run_id: int | None
    localities: list | None = None
    categories: list | None = None
    source_names: list[str] | None = None
    enricher_names: list[str] | None = None
    dry_run: bool = False
    install_signals: bool = False
    failure_sink: Any = None
    unit_sink: Any = None
    control: RunControl | None = None
    events: ev.EventBus | None = None
    force_enrich: bool | None = None
    #: Set by local_business: the pipeline it ran (results, stats).
    pipeline: Any = None
    #: The run's AI client (Phase 5): one budget, cache and ledger for every
    #: lead type. None when AI is off.
    ai: Any = None


@dataclass
class RunOutcome:
    run_id: int | None
    project: str
    results: dict[str, LeadTypeResult] = field(default_factory=dict)
    contexts: dict[str, LeadTypeContext] = field(default_factory=dict)
    status: str = "completed"
    error: str = ""
    estimate: dict[str, Any] = field(default_factory=dict)
    #: (unit key, reason) for every unit that did not run.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    #: Files written from the database after the run (the workbook; Phase 4).
    files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def enabled_lead_types(config: Config) -> list[str]:
    run_config = getattr(config, "run_config", None)
    if run_config is not None:
        return run_config.enabled_lead_types()
    return ["local_business"]


def _snapshot(config: Config) -> dict[str, Any]:
    """The immutable record of what the run was asked to do."""
    run_config = getattr(config, "run_config", None)
    document = run_config.to_document() if run_config is not None else dict(config._data)
    overrides = getattr(config, "overrides", None)
    if overrides:
        document = {**document, "cli_overrides": dict(overrides)}
    return document


def config_from_snapshot(snapshot: dict[str, Any], path=None, *, geocode: bool = False) -> Config:
    """Rebuild a run's Config from its snapshot (a worker resuming it).

    ``geocode``: fill a missing map centre / state first (a run queued through
    the API was never geocoded; the CLI does it before snapshotting).
    """
    from ..config.compile import compile_run_config
    from ..config.schema import RunConfig

    document = {k: v for k, v in snapshot.items() if k != "cli_overrides"}
    if "schema_version" in document:
        run_config = RunConfig.model_validate(document)
        if geocode:
            from ..config.geography import needs_geocoding, resolve_geography

            if needs_geocoding(run_config):
                run_config = resolve_geography(run_config)
        config = compile_run_config(run_config, path=path)
    else:
        config = Config(document, path)
    for dotted, value in (snapshot.get("cli_overrides") or {}).items():
        config.set(dotted, value)
    return config


def request_document(request: RunRequest) -> dict[str, Any]:
    """The parts of a request beyond the config, stored on the run row."""
    def names(items):
        return None if items is None else [getattr(i, "name", i) for i in items]

    config_path = getattr(request.config, "path", None)
    return {"localities": names(request.localities), "categories": names(request.categories),
            "source_names": request.source_names, "enricher_names": request.enricher_names,
            "lead_types": request.lead_types, "depth": request.depth,
            "time_budget_minutes": request.time_budget_minutes,
            "force_enrich": request.force_enrich, "discovery_workers": request.discovery_workers,
            "config_path": str(config_path) if config_path else None}


def request_from_row(row: dict[str, Any], *, geocode: bool = False,
                     **overrides: Any) -> RunRequest:
    """The RunRequest that resumes a stored run."""
    from pathlib import Path

    stored = row.get("request") or {}
    path = Path(stored["config_path"]) if stored.get("config_path") else None
    config = config_from_snapshot(row.get("config_snapshot") or {}, path, geocode=geocode)
    localities = (config.select_localities(stored["localities"])
                  if stored.get("localities") else None)
    categories = (config.select_categories(stored["categories"])
                  if stored.get("categories") else None)
    values = dict(config=config, localities=localities, categories=categories,
                  source_names=stored.get("source_names"),
                  enricher_names=stored.get("enricher_names"),
                  lead_types=stored.get("lead_types"), depth=stored.get("depth"),
                  time_budget_minutes=stored.get("time_budget_minutes"),
                  force_enrich=bool(stored.get("force_enrich")),
                  discovery_workers=stored.get("discovery_workers"),
                  run_id=int(row["id"]), kind=row.get("kind") or "run", install_signals=False)
    values.update(overrides)
    return RunRequest(**values)


def default_executor() -> str:
    return f"cli:{socket.gethostname()}:{os.getpid()}"


def _number(config: Config, path: str, default: float) -> float:
    value = config.get(path)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# --- the run ---------------------------------------------------------------------------

class _Run:
    """One execution of a run (a fresh run, or a resume of a stored one)."""

    def __init__(self, request: RunRequest):
        self.request = request
        self.config = request.config
        self.clock = request.clock or SYSTEM_CLOCK
        self.sessions: dict[str, Any] = {}
        self.by_key: dict[str, Planned] = {}
        self.outcome: RunOutcome | None = None
        self.control: RunControl | None = None
        self.scheduler: Scheduler | None = None
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._old_handlers: dict[int, Any] = {}
        self._interrupts = 0
        self.executor: str | None = None
        #: Another executor took the run over (we stalled): write nothing more.
        self.lost = False
        self.units_total = 0
        self.units_done = 0
        self.units_skipped = 0
        self.new = 0
        self.merged = 0
        self.discovery_started = 0.0

    # --- setup ------------------------------------------------------------------------

    def _configure(self) -> tuple[str, float | None]:
        config, request = self.config, self.request
        depth = request.depth or str(config.get("depth") or "balanced")
        config.set("depth", depth, record=request.depth is not None)
        minutes = (request.time_budget_minutes if request.time_budget_minutes is not None
                   else config.get("time_budget_minutes"))
        if request.time_budget_minutes is not None:
            config.set("time_budget_minutes", minutes)
        budget = float(minutes) * 60 if minutes else None
        return depth, budget

    def execute(self) -> RunOutcome:
        request, config = self.request, self.config
        depth, budget = self._configure()
        keys = request.lead_types or enabled_lead_types(config)
        for key in keys:
            lead_type = registry.get(key)
            if lead_type is not None:
                for note in lead_type.apply_depth(config, depth):
                    log.info("depth %s: %s", depth, note)

        target, store_kwargs = store_target(config)
        if request.engine is not None and store_kwargs:
            target = request.engine                 # a project run: the caller's database
        from ..leadtypes.local_business.scoring import Scorer

        store = LeadStore(target, Scorer(config), **store_kwargs)
        self.store = store
        self._apply_workspace_settings(store)
        outcome = RunOutcome(run_id=None, project=store.project_name)
        self.outcome = outcome
        engine, workspace_id, project_id = store.engine, store.workspace_id, store.project_id
        self.engine = engine

        run_config = getattr(config, "run_config", None)
        for note in (run_config.unapplied() if run_config is not None else []):
            log.warning("Config: %s", note)

        executor = request.executor or default_executor()
        self.executor = executor
        run_id, used_seconds, row = None, 0.0, None
        if not request.dry_run:
            if request.run_id is not None:
                row = self._claim(engine, request.run_id, executor)
                run_id = int(row["id"])
                used_seconds = float(row.get("active_seconds") or 0.0)
                update_run(engine, run_id, finished_at=None,
                           heartbeat_at=datetime.now(timezone.utc),
                           started_at=row.get("started_at") or datetime.now(timezone.utc))
            else:
                run_id = start_run(engine, workspace_id, project_id, kind=request.kind,
                                   config_snapshot=_snapshot(config), worker_id=executor,
                                   request=request_document(request))
            store.run_id = run_id
        outcome.run_id = run_id

        tail = _number(config, "runtime.tail_seconds", DEFAULT_TAIL_SECONDS)
        control = RunControl(budget_seconds=budget, tail_seconds=tail, clock=self.clock,
                             used_seconds=used_seconds)
        self.control = control
        if request.on_control is not None:
            request.on_control(control)

        bus = ev.EventBus(run_id, start_seq=ev.last_seq(engine, run_id) if run_id else 0)
        self.event_writer = None
        if run_id is not None:
            self.event_writer = ev.DbEventSink(engine, workspace_id)
            bus.subscribe(self.event_writer)
        for sink in request.sinks:
            bus.subscribe(sink)
        self.bus = bus

        def failure_sink(entry: dict) -> None:
            record_failure(engine, workspace_id, entry, project_id=project_id, run_id=run_id)
            bus.publish(ev.FailureEvent(source=str(entry.get("source") or ""),
                                        reason=str(entry.get("reason") or ""),
                                        detail=str(entry.get("detail") or "")[:300],
                                        url=entry.get("url")))

        keep_awake = request.keep_awake or KEEP_AWAKE
        holding = False
        status, error = "completed", ""
        self.ai_client = self._ai_client(store)
        try:
            for key in keys:
                lead_type = registry.get(key)
                if lead_type is None:
                    outcome.results[key] = LeadTypeResult(lead_type=key, failed=True,
                                                          error=f"unknown lead type {key!r}")
                    continue
                store.lead_type = key
                context = LeadTypeContext(
                    config=config, store=store, run_id=run_id,
                    localities=request.localities, categories=request.categories,
                    source_names=request.source_names, enricher_names=request.enricher_names,
                    dry_run=request.dry_run,
                    failure_sink=None if request.dry_run else failure_sink,
                    control=control, events=bus,
                    force_enrich=request.force_enrich or None,
                    ai=self.ai_client,
                )
                outcome.contexts[key] = context
                try:
                    self.sessions[key] = lead_type.open(context)
                except Exception as exc:  # contained: one lead type never ends the run
                    log.exception("lead type %s failed to start", key)
                    outcome.results[key] = LeadTypeResult(lead_type=key, failed=True,
                                                          error=repr(exc))

            planned, done_rows = self._plan(run_id, workspace_id)
            workers = int(request.discovery_workers
                          or _number(config, "runtime.discovery_workers",
                                     DEFAULT_DISCOVERY_WORKERS))
            share = _number(config, "runtime.enrichment_share", DEFAULT_ENRICHMENT_SHARE)
            order = str(config.get("runtime.order") or ("priority" if budget else "plan"))
            self.costs = CostModel(UnitStatsReader(engine, workspace_id), self._prior,
                                   self._stats_key)
            est = estimate(planned, self.costs, self.sessions, workers=workers,
                           budget_seconds=budget, tail_seconds=tail, enrichment_share=share,
                           order=order, done_units=len(done_rows))
            outcome.estimate = est

            if request.dry_run:
                if request.print_plan:
                    self._print_dry_run(est)
                return outcome

            if row is None or not row.get("estimate"):
                update_run(engine, run_id, estimate=est)
            if request.install_signals:
                self._install_signals()
            if keep_awake.enabled and config.get("runtime.keep_awake", True) is not False:
                keep_awake.acquire()
                holding = True
            self._start_heartbeat()
            bus.publish(ev.RunState(status="running",
                                    reason="resumed" if row is not None else ""))
            breakdown: dict[str, dict[str, int]] = {}
            for p in planned:
                sources = breakdown.setdefault(p.unit.lead_type, {})
                sources[p.unit.source] = sources.get(p.unit.source, 0) + 1
            bus.publish(ev.PlanEvent(units_total=len(planned) + len(done_rows),
                                     units_resumed=len(done_rows), breakdown=breakdown,
                                     estimate={k: est[k] for k in ("p50_seconds", "p90_seconds",
                                                                   "expected_new", "budget")}))
            self.units_total = len(planned) + len(done_rows)
            self.units_done = len(done_rows)
            self.new = sum(int(r.get("new") or 0) for r in done_rows)
            self.merged = sum(int(r.get("merged") or 0) for r in done_rows)

            phase = ((row or {}).get("checkpoint") or {}).get("phase", "discovery")
            post_p50 = est["post"]["p50_seconds"]
            reserve = min(share * budget, post_p50) if budget and post_p50 else 0.0
            self.eta = LiveEta(workers, post_p50, est["post"]["p90_seconds"],
                               tail if budget else est["finalise_seconds"])
            self.scheduler = Scheduler(planned, self.costs, control, order=order,
                                       reserve=lambda: reserve, on_skip=self._skipped)
            if phase == "discovery":
                self._discover(workers)
                if control.deadline_reached:
                    self.scheduler.skip_rest("time budget spent")
                elif control.requested == "cancel":
                    self.scheduler.skip_rest("run cancelled")
            if not control.stop_requested:
                update_run(engine, run_id, checkpoint={"phase": "post"})
                for key, session in self.sessions.items():
                    if control.stop_requested:
                        break
                    store.lead_type = key
                    bus.publish(ev.Activity(phase="post", text=f"{key}: enrichment and AI passes",
                                            lead_type=key))
                    try:
                        session.post_discovery(control)
                    except Exception as exc:
                        log.exception("lead type %s failed after discovery", key)
                        failure_sink({"source": key, "reason": "post_discovery_failed",
                                      "detail": repr(exc)})
                    self._publish_ai_spend(session)
        except KeyboardInterrupt:
            # The second Ctrl-C: stop now. The units in flight stay "running"
            # in the checkpoint and run again on resume.
            log.warning("stopped immediately -- flushing what we have; "
                        "`leadscraper runs resume %s` continues it", run_id)
            status, error = "paused", "interrupted (hard stop); in-flight units re-run on resume"
        except Exception as exc:
            # The engine itself failed (the database went away, a bug): the run
            # is recorded as failed -- never "completed" with units pending --
            # and stays resumable from its checkpoint.
            log.exception("run %s failed in the engine", run_id)
            status, error = "failed", f"engine error: {exc!r}"
        finally:
            try:
                status, error = self._finish(status, error)
            finally:
                self._stop_heartbeat()
                self._restore_signals()
                if holding:
                    keep_awake.release()
                if self.event_writer is not None:
                    self.event_writer.close()
                self._flush_ai_usage()
                if self.ai_client is not None:
                    self.ai_client.close()
                if request.dry_run:
                    try:
                        store.drop_project_if_unused()
                    except Exception as exc:
                        log.debug("could not drop the dry run's project: %s", exc)
                store.close()
        outcome.status, outcome.error = status, error
        return outcome

    def _apply_workspace_settings(self, store: LeadStore) -> None:
        """Workspace settings (keep-awake, robots.txt) fill what the config leaves unset."""
        try:
            from ..services.settings import apply_to_config

            apply_to_config(store.engine, store.workspace_id, self.config)
        except Exception as exc:  # settings never stop a run
            log.debug("workspace settings not applied: %s", exc)

    def _claim(self, engine, run_id: int, executor: str) -> dict:
        """Take the run for this executor, atomically, or refuse."""
        row = get_run(engine, run_id)
        if row is None:
            self.store.close()
            raise ValueError(f"no run {run_id}")
        if row["status"] == "running" and row.get("worker_id") == executor:
            return row                                  # a worker already claimed it
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=_number(self.config, "runtime.stale_seconds", DEFAULT_STALE_SECONDS))
        if not (claim_run(engine, run_id, executor, from_statuses=("queued", "paused", "failed"))
                or claim_run(engine, run_id, executor, from_statuses=ACTIVE,
                             stale_before=stale)):
            self.store.close()
            raise RunBusyError(f"run {run_id} is {row['status']} on {row.get('worker_id')}; "
                               "it can only be resumed once that stops (or its heartbeat "
                               "goes stale)")
        return get_run(engine, run_id) or row

    # --- planning -----------------------------------------------------------------------

    def _plan(self, run_id: int | None, workspace_id: int) -> tuple[list[Planned], list[dict]]:
        planned: list[Planned] = []
        index = 0
        for key, session in self.sessions.items():
            try:
                units = session.units()
            except Exception as exc:
                log.exception("lead type %s could not plan", key)
                self.outcome.results[key] = LeadTypeResult(lead_type=key, failed=True,
                                                           error=repr(exc))
                continue
            for unit in units:
                try:
                    hosts = frozenset(session.hosts(unit))
                except Exception:
                    hosts = frozenset({f"{key}:{unit.source}"})
                planned.append(Planned(unit=unit, session=session, index=index, hosts=hosts))
                index += 1
        self.by_key = {p.key: p for p in planned}
        if run_id is None:
            return planned, []

        existing = {r["unit_key"]: r for r in unit_rows(self.engine, run_id) if r["unit_key"]}
        # Only finished units are skipped on resume. Units cut mid-way
        # (partial), skipped for a budget that has since been resumed, or in
        # flight when the executor died (running) run again.
        done_rows = [r for k, r in existing.items()
                     if r["status"] in ("done", "failed") and k in self.by_key]
        lost = [k for k, r in existing.items() if r["status"] == "running"]
        if lost:
            log.warning("resuming: %d unit(s) were in flight when the run stopped and run "
                        "again: %s", len(lost), ", ".join(lost[:5]))
        rerun = [k for k, r in existing.items() if r["status"] in ("partial", "skipped")]
        if rerun:
            update_units = [k for k in rerun if k in self.by_key]
            for key in update_units:
                mark_unit(self.engine, run_id, key, status="pending", skip_reason="")
        finished = {r["unit_key"] for r in done_rows}
        costs = CostModel(UnitStatsReader(self.engine, workspace_id), self._prior,
                          self._stats_key)
        new_rows = []
        for p in planned:
            if p.key in existing:
                continue
            cost = costs.cost(p.unit)
            new_rows.append({"lead_type": p.unit.lead_type, "source": p.unit.source,
                             "area": p.unit.area, "category": p.unit.category,
                             "scope": p.unit.scope, "unit_key": p.key,
                             "expected_seconds": round(cost.seconds, 2),
                             "expected_new": round(cost.new, 3),
                             "priority": round(cost.priority, 5)})
        plan_units(self.engine, workspace_id, run_id, new_rows)
        return [p for p in planned if p.key not in finished], done_rows

    def _session_of(self, unit) -> Any:
        planned = self.by_key.get(unit.key)
        return planned.session if planned else None

    def _prior(self, unit):
        session = self._session_of(unit)
        return session.prior(unit) if session else prior_cost(60.0, 3.0)

    def _stats_key(self, unit) -> tuple[str, str, str]:
        session = self._session_of(unit)
        if session is None:
            return unit.source, unit.area or "*", unit.category or "*"
        return session.stats_key(unit)

    # --- discovery ----------------------------------------------------------------------

    def _discover(self, workers: int) -> None:
        engine, run_id, bus, scheduler = self.engine, self.outcome.run_id, self.bus, self.scheduler
        self.discovery_started = self.clock.now()

        def next_item(busy: set[str]):
            item = scheduler.next_ready(busy)
            if isinstance(item, Planned):
                cost = item.cost
                try:
                    mark_unit(engine, run_id, item.key, status="running",
                              started_at=datetime.now(timezone.utc))
                except Exception as exc:  # a lost checkpoint write costs a re-run, no more
                    log.warning("could not checkpoint the start of %s: %s", item.key, exc)
                bus.publish(ev.UnitStarted(unit=item.key, lead_type=item.unit.lead_type,
                                           source=item.unit.source, area=item.unit.area,
                                           category=item.unit.category,
                                           expected_seconds=round(cost.seconds, 1)
                                           if cost else None))
            return item

        def work(planned: Planned):
            self.store.tag_thread(planned.session.lead_type)
            started = self.clock.now()
            try:
                outcome = planned.session.run_unit(planned.unit)
            except Exception as exc:  # run_unit must not raise; contain it if it does
                log.exception("unit %s crashed", planned.key)
                outcome = UnitOutcome(status="failed", error=repr(exc))
            return outcome, self.clock.now() - started

        def done(planned: Planned, result, error) -> None:
            try:
                self._unit_done(planned, result, error)
            except Exception as exc:
                log.exception("bookkeeping for unit %s failed (the run continues): %s",
                              planned.key, exc)

        control = self.control
        grace = _number(self.config, "runtime.stop_grace_seconds", DEFAULT_STOP_GRACE_SECONDS)
        if control.budgeted:
            grace = min(grace, control.tail_seconds / 2)
        abandoned = run_across_hosts(next_item, lambda p: p.hosts, work, done,
                                     workers=workers, should_stop=lambda: control.stop_requested,
                                     thread_name="unit", grace_seconds=grace)
        for planned in abandoned:
            log.warning("unit %s did not finish within %.0fs of the stop; abandoned (it runs "
                        "again on resume)", planned.key, grace)

    def _unit_done(self, planned: Planned, result, error) -> None:
        if error is not None or result is None:
            outcome, seconds = UnitOutcome(status="failed", error=repr(error)), 0.0
        else:
            outcome, seconds = result
        unit = planned.unit
        mark_unit(self.engine, self.outcome.run_id, planned.key, status=outcome.status,
                  found=outcome.found, new=outcome.new, merged=outcome.merged,
                  seconds=round(seconds, 3), error=(outcome.error or "")[:5000],
                  finished_at=datetime.now(timezone.utc))
        if seconds > 0:
            source, area, category = self._stats_key(unit)
            try:
                self.store.record_unit(source, area, category, seconds, outcome.new)
            except Exception as exc:
                log.warning("could not record unit stats for %s: %s", planned.key, exc)
            self.costs.observe(unit, seconds, outcome.new)
            self.eta.observe(seconds)
        self.units_done += 1
        self.new += outcome.new
        self.merged += outcome.merged
        self.bus.publish(ev.UnitFinished(unit=planned.key, lead_type=unit.lead_type,
                                         source=unit.source, status=outcome.status,
                                         found=outcome.found, new=outcome.new,
                                         merged=outcome.merged, seconds=round(seconds, 2),
                                         error=(outcome.error or "")[:300]))
        self._progress()
        checkpoint = getattr(planned.session, "checkpoint", None)
        if callable(checkpoint):
            checkpoint()                               # periodic flush, on this thread

    def _ai_client(self, store):
        """The run's AI client: config + workspace settings; None when AI is off."""
        if not self.config.get("ai.enabled", False):
            return None
        try:
            from ..ai.client import AIClient
            from ..ai.settings import resolve
            from ..services.settings import ai_settings

            settings = resolve(self.config, ai_settings(store.engine, store.workspace_id))
            return AIClient(settings, cache_path=self.config.path_for(
                "ai_cache", "data/ai_cache.sqlite3"))
        except Exception as exc:                # AI never stops a run
            log.warning("AI layer not started (%s) -- continuing without AI", exc)
            return None

    def _publish_ai_spend(self, session) -> None:
        client = self.ai_client
        if client is None or not client.enabled:
            return
        self.bus.publish(ev.AISpend(**client.spend()))
        self._flush_ai_usage()

    def _flush_ai_usage(self) -> None:
        """History: the run's AI usage per provider, model and feature (``ai_usage``)."""
        client = getattr(self, "ai_client", None)
        run_id = self.outcome.run_id if self.outcome else None
        if client is None or run_id is None or self.lost:
            return
        try:
            client.ledger.flush(self.engine, self.store.workspace_id, run_id)
        except Exception as exc:
            log.debug("could not record AI usage: %s", exc)

    def _skipped(self, planned: Planned, reason: str) -> None:
        self.units_skipped += 1
        self.outcome.skipped.append((planned.key, reason))
        run_id = self.outcome.run_id
        if run_id is not None:
            mark_unit(self.engine, run_id, planned.key, status="skipped", skip_reason=reason)
        self.bus.publish(ev.UnitSkipped(unit=planned.key, lead_type=planned.unit.lead_type,
                                        source=planned.unit.source, reason=reason))

    def _progress(self) -> None:
        control = self.control
        queued_mean, queued_var = self.scheduler.expected_remaining()
        wall = self.clock.now() - self.discovery_started
        budget_left = (control.remaining() + control.tail_seconds) if control.budgeted else None
        p50, p90 = self.eta.remaining(queued_mean, queued_var ** 0.5, wall,
                                      budget_left=budget_left)
        self.bus.publish(ev.Progress(
            units_done=self.units_done, units_total=self.units_total,
            units_skipped=self.units_skipped, new=self.new, merged=self.merged,
            elapsed_seconds=round(control.elapsed(), 1), eta_p50_seconds=round(p50, 1),
            eta_p90_seconds=round(p90, 1),
            budget_remaining_seconds=round(control.remaining(), 1) if control.budgeted else None))

    # --- the end ------------------------------------------------------------------------

    def _finish(self, status: str, error: str) -> tuple[str, str]:
        outcome, control = self.outcome, self.control
        run_id = outcome.run_id
        if control is not None and status == "completed":
            status = control.final_status("completed")
        complete = status == "completed"
        if control is not None:
            # Measured before finalising: rescoring and export are not billed
            # to the budget again every time a run is paused and resumed.
            outcome.elapsed_seconds = round(control.elapsed(), 1)
        if run_id is not None and self.sessions and not self.lost:
            update_run(self.engine, run_id,
                       checkpoint={"phase": "finalise" if complete else "discovery"})
        for key, session in self.sessions.items():
            self.store.lead_type = key
            try:
                if not self.request.dry_run:
                    self.bus.publish(ev.Activity(phase="finalise", text=f"{key}: rescore, export",
                                                 lead_type=key))
                    session.finalize(complete=complete)
            except Exception as exc:
                log.exception("lead type %s failed to finalise", key)
                outcome.results.setdefault(key, LeadTypeResult(lead_type=key, failed=True,
                                                               error=repr(exc)))
            finally:
                try:
                    session.close()
                except Exception as exc:
                    log.debug("closing %s failed (ignored): %s", key, exc)
            if key not in outcome.results:
                try:
                    outcome.results[key] = session.result()
                except Exception as exc:
                    outcome.results[key] = LeadTypeResult(lead_type=key, failed=True,
                                                          error=repr(exc))
        for key, result in outcome.results.items():
            if result.failed:
                log.error("lead type %s failed: %s -- the run continues", key, result.error)
        if outcome.results and all(r.failed for r in outcome.results.values()) \
                and status == "completed":
            status = "failed"
            error = "; ".join(f"{k}: {r.error}" for k, r in outcome.results.items())
        if run_id is not None and not self.request.dry_run:
            if self.lost:
                log.warning("run %s was taken over by another executor; this one stops "
                            "without recording an outcome", run_id)
                return "lost", "taken over by another executor"
            summary = self._summary(status)
            stats = {key: {"new": r.new, "merged": r.merged, "units_done": r.units_done,
                           "units_planned": r.units_planned, "failed": r.failed,
                           "error": r.error, **r.stats}
                     for key, r in outcome.results.items()}
            stats["engine"] = summary
            written = finish_run(self.engine, run_id, status=status, error=error, stats=stats,
                                 worker_id=self.executor,
                                 active_seconds=outcome.elapsed_seconds)
            if written is None:
                log.warning("run %s is no longer held by %s; its outcome was not recorded",
                            run_id, self.executor)
                return "lost", "taken over by another executor"
            if written != status:
                log.warning("run %s: %s requested while it was finishing; ending as %s",
                            run_id, "cancel", written)
                status = written
                summary["status"] = status
            self.bus.publish(ev.RunFinished(status=status, summary=summary, error=error[:500]))
            self.bus.publish(ev.RunState(status=status, reason=error[:200]))
        return status, error

    def _summary(self, status: str) -> dict[str, Any]:
        control = self.control
        est = self.outcome.estimate or {}
        basis = (est.get("budget") or est) if est else {}
        return {
            "status": status,
            "elapsed_seconds": self.outcome.elapsed_seconds,
            "budget_seconds": control.budget_seconds,
            "tail_seconds": control.tail_seconds,
            "stop_request": control.requested,
            "deadline_reached": control.deadline_reached,
            "units_total": self.units_total, "units_done": self.units_done,
            "units_skipped": self.units_skipped,
            "skipped_units": [{"unit": k, "reason": r} for k, r in self.outcome.skipped[:500]],
            "new": self.new, "merged": self.merged,
            "estimate_p50_seconds": basis.get("p50_seconds"),
            "estimate_p90_seconds": basis.get("p90_seconds"),
        }

    # --- heartbeat, signals, dry run ------------------------------------------------------

    def _start_heartbeat(self) -> None:
        run_id = self.outcome.run_id
        if run_id is None:
            return
        interval = float(self.request.heartbeat_seconds
                         or _number(self.config, "runtime.heartbeat_seconds",
                                    DEFAULT_HEARTBEAT_SECONDS))
        watch = SleepWatch(SYSTEM_CLOCK, interval=interval)

        def beat() -> None:
            while not self._hb_stop.wait(interval):
                try:
                    status = heartbeat(self.engine, run_id, active_seconds=self.control.elapsed(),
                                       worker_id=self.executor)
                except Exception as exc:
                    log.debug("heartbeat failed (ignored): %s", exc)
                    continue
                if status == LOST:
                    log.error("run %s was taken over by another executor (this one stalled); "
                              "stopping without writing", run_id)
                    self.lost = True
                    self.control.request("shutdown")
                    return
                if status == "pausing":
                    if self.control.request("pause"):
                        log.warning("pause requested: finishing in-flight work, then flushing")
                elif status == "stopping":
                    if self.control.request("cancel"):
                        log.warning("cancel requested: finishing in-flight work, then flushing")
                slept = watch.tick()
                if slept:
                    self._woke(*slept)

        self._hb_thread = threading.Thread(target=beat, name=f"heartbeat-{run_id}", daemon=True)
        self._hb_thread.start()

    def _woke(self, gap: float, counted: float) -> None:
        """The machine slept: don't bill the gap; renew browsers and connections."""
        self.control.discount(counted)
        log.warning("the machine slept for ~%.0fs: re-validating browsers and connections", gap)
        for session in self.sessions.values():
            revalidate = getattr(session, "revalidate", None)
            if callable(revalidate):
                try:
                    revalidate()
                except Exception as exc:
                    log.debug("revalidate failed (ignored): %s", exc)
        self.bus.publish(ev.SleepDetected(gap_seconds=round(gap, 1)))

    def _stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=5)

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, frame):
            self._interrupts += 1
            if self._interrupts > 1:
                log.warning("second interrupt -- stopping immediately")
                raise KeyboardInterrupt
            self.control.request("pause")
            log.warning("interrupt received: finishing in-flight work, then flushing. "
                        "The run is paused; `leadscraper runs resume %s` continues it. "
                        "Ctrl-C again to stop immediately.", self.outcome.run_id)

        # SIGBREAK is Ctrl-Break on Windows; SIGTERM what a service manager sends.
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                self._old_handlers[number] = signal.signal(number, handler)
            except (ValueError, OSError):
                pass

    def _restore_signals(self) -> None:
        for number, old in self._old_handlers.items():
            try:
                signal.signal(number, old)
            except (ValueError, OSError, TypeError):
                pass
        self._old_handlers.clear()

    def _print_dry_run(self, est: dict[str, Any]) -> None:
        for session in self.sessions.values():
            printer = getattr(session, "print_plan", None)
            if callable(printer):
                printer()
        print("Estimate (no requests made):")
        print(describe_estimate(est))
        print()


def execute(request: RunRequest) -> RunOutcome:
    """Run (or resume) every requested lead type of `request.config` against its project.

    Afterwards the run's files are written from the database -- the workbook
    and JSON its ``output`` section asks for (Phase 4) -- and the workspace's
    notification targets hear how it ended (Phase 3). A failure in either is
    logged, never raised.
    """
    run = _Run(request)
    outcome = run.execute()
    if (not request.dry_run and outcome.run_id is not None and request.kind == "run"
            and outcome.status != "lost"):
        try:
            from ..services.exports import write_run_outputs

            outcome.files = write_run_outputs(run.engine, outcome.run_id, request.config)
            for path in outcome.files:
                log.info("run %s: wrote %s", outcome.run_id, path)
        except Exception as exc:  # the run's data is safe in the database either way
            log.warning("run %s: the workbook was not written: %s", outcome.run_id, exc)
    if (request.notify and not request.dry_run and outcome.run_id is not None
            and request.kind == "run" and outcome.status != "lost"):
        try:
            from ..services.notifications import notify_run

            notify_run(run.engine, outcome.run_id)
        except Exception as exc:
            log.warning("run %s: notifications failed: %s", outcome.run_id, exc)
    return outcome


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
