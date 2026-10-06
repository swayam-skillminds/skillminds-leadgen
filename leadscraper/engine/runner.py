"""Run a config: one project store, one run in history, every enabled lead type.

Phase 1's runner is in-process and sequential; Phase 2 replaces the loop with
a planner/scheduler and a worker process. What it already guarantees:

* every lead type writes into the same project, its rows tagged with its key;
* a lead type that crashes is contained -- the others still run and the run
  is recorded with the error (A.3);
* the run's config snapshot, failures, work units and the organisations it
  created or updated are in the database, so history can show and diff it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..db.runs import finish_run, record_failure, record_unit, start_run
from ..db.store import LeadStore, store_target
from ..leadtypes import registry
from ..leadtypes.base import LeadTypeResult

log = logging.getLogger(__name__)


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
    install_signals: bool = True


@dataclass
class LeadTypeContext:
    """What a lead type's ``run()`` gets."""

    config: Config
    store: LeadStore | None
    run_id: int | None
    localities: list | None = None
    categories: list | None = None
    source_names: list[str] | None = None
    enricher_names: list[str] | None = None
    dry_run: bool = False
    install_signals: bool = True
    failure_sink: Any = None
    unit_sink: Any = None
    #: Set by local_business: the pipeline it ran (results, stats).
    pipeline: Any = None


@dataclass
class RunOutcome:
    run_id: int | None
    project: str
    results: dict[str, LeadTypeResult] = field(default_factory=dict)
    contexts: dict[str, LeadTypeContext] = field(default_factory=dict)
    status: str = "completed"

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


def execute(request: RunRequest) -> RunOutcome:
    """Run every requested lead type of `request.config` against its project."""
    from ..leadtypes.local_business.scoring import Scorer

    config = request.config
    keys = request.lead_types or enabled_lead_types(config)
    target, store_kwargs = store_target(config)
    store = LeadStore(target, Scorer(config), **store_kwargs)
    outcome = RunOutcome(run_id=None, project=store.project_name)
    engine, workspace_id, project_id = store.engine, store.workspace_id, store.project_id

    run_config = getattr(config, "run_config", None)
    for note in (run_config.unapplied() if run_config is not None else []):
        log.warning("Config: %s", note)

    run_id = None
    if not request.dry_run:
        run_id = start_run(engine, workspace_id, project_id, config_snapshot=_snapshot(config))
        store.run_id = run_id
    outcome.run_id = run_id

    def failure_sink(entry: dict) -> None:
        record_failure(engine, workspace_id, entry, project_id=project_id, run_id=run_id)

    def unit_sink(unit: dict) -> None:
        record_unit(engine, workspace_id, run_id, **unit)

    status, error = "completed", ""
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
                dry_run=request.dry_run, install_signals=request.install_signals,
                failure_sink=None if request.dry_run else failure_sink,
                unit_sink=None if request.dry_run else unit_sink,
            )
            started = time.time()
            result = lead_type.run(context)
            result.stats.setdefault("seconds", round(time.time() - started, 1))
            outcome.results[key] = result
            outcome.contexts[key] = context
            if result.failed:
                log.error("lead type %s failed: %s -- the run continues", key, result.error)
        if outcome.results and all(r.failed for r in outcome.results.values()):
            status = "failed"
            error = "; ".join(f"{k}: {r.error}" for k, r in outcome.results.items())
    except KeyboardInterrupt:
        status, error = "cancelled", "interrupted"
        raise
    finally:
        outcome.status = status
        if run_id is not None:
            finish_run(engine, run_id, status=status, error=error, stats={
                key: {"new": r.new, "merged": r.merged, "units_done": r.units_done,
                      "units_planned": r.units_planned, "failed": r.failed,
                      "error": r.error, **r.stats}
                for key, r in outcome.results.items()})
        store.close()
    return outcome


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
