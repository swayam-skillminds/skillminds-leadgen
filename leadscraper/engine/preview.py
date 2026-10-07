"""A preview run (Phase 2.7): a real sample, fast, plus a full-run estimate.

One area x one category x the top discovery sources of each lead type, at
``quick`` depth, inside a ~2-minute time budget. It writes into a scratch
project (``<project> (preview)``, emptied first), so the real project never
holds preview rows, and returns:

* sample records, best first;
* the estimate of the full run, calibrated by how the preview's own units
  compared with their expected cost (the units also feed ``unit_stats``, so
  every later estimate learns from them).
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from .runner import RunRequest, _snapshot, config_from_snapshot, execute

log = logging.getLogger(__name__)

PREVIEW_SECONDS = 120
#: Top discovery sources per lead type, in their run order (Maps first).
PREVIEW_SOURCES = 2
PREVIEW_TAIL = 15


@dataclass
class PreviewResult:
    run_id: int | None
    status: str
    area: str | None
    category: str | None
    sources: list[str]
    seconds: float
    records: list[dict[str, Any]] = field(default_factory=list)
    estimate: dict[str, Any] = field(default_factory=dict)
    #: actual / expected seconds of the preview's units (1.0 = the estimate held).
    calibration: float | None = None


def _top_sources(config: Config, count: int) -> list[str]:
    from ..leadtypes.local_business.plugin import LOCAL_BUSINESS

    return LOCAL_BUSINESS.sources(config)["discovery"][:count]


def _copy(config: Config) -> Config:
    return config_from_snapshot(_snapshot(config), getattr(config, "path", None))


def scratch_document(config: Config) -> dict[str, Any]:
    """The preview's own config: the scratch project, a short tail, no Sheets.

    A document (not edits to the compiled view) so a queued preview's snapshot
    -- which is all a worker gets -- also names the scratch project.
    """
    document = copy.deepcopy(_snapshot(config))
    project = str(config.get("project") or "default")
    document["project"] = f"{project} (preview)"
    runtime = document.setdefault("runtime", {})
    runtime["tail_seconds"] = PREVIEW_TAIL
    if config.get("runtime.csv_export"):
        csv_path = Path(str(config.get("runtime.csv_export")))
        runtime["csv_export"] = str(csv_path.with_name("preview.csv"))
    if "schema_version" in document:
        document.setdefault("output", {})["google_sheets"] = False
    else:
        document.setdefault("sheets", {})["enabled"] = False
    return document


@dataclass
class PreviewPlan:
    """What a preview will run: the scratch config and its one slice."""

    scratch: Config
    localities: list
    categories: list
    sources: list[str]
    seconds: float

    def request(self, **overrides: Any) -> RunRequest:
        return RunRequest(
            config=self.scratch, localities=self.localities, categories=self.categories,
            source_names=self.sources, enricher_names=[], depth="quick",
            time_budget_minutes=self.seconds / 60, kind="preview", install_signals=False,
            **overrides)


def plan_preview(config: Config, *, seconds: float = PREVIEW_SECONDS, area: str | None = None,
                 category: str | None = None, sources: int = PREVIEW_SOURCES) -> PreviewPlan:
    """The scratch project's config and the slice to sample (no I/O)."""
    scratch = config_from_snapshot(scratch_document(config), getattr(config, "path", None))
    localities = scratch.select_localities([area] if area else None)[:1]
    categories = scratch.select_categories([category] if category else None)
    categories = [c for c in categories if c.runs_in(localities[0])][:1] if localities else []
    return PreviewPlan(scratch, localities, categories, _top_sources(scratch, sources), seconds)


def clear_scratch(scratch: Config, engine: Any = None) -> int:
    """Empty the scratch project, so a preview only ever shows its own sample."""
    from ..db.store import LeadStore, store_target

    target, kwargs = store_target(scratch)
    store = LeadStore(engine if engine is not None and kwargs else target, **kwargs)
    try:
        cleared = store.clear()
    finally:
        store.close()
    if cleared:
        log.info("preview: cleared %d organisations from the last preview", cleared)
    return cleared


def finish_preview(config: Config, plan: PreviewPlan, outcome, *, records: int = 15,
                   engine: Any = None) -> PreviewResult:
    """Sample records, calibration, and the calibrated full-run estimate."""
    from ..db.runs import unit_rows
    from ..db.store import LeadStore, store_target

    sample: list[dict[str, Any]] = []
    calibration = None
    target, kwargs = store_target(plan.scratch)
    store = LeadStore(engine if engine is not None and kwargs else target, **kwargs)
    try:
        for lead in store.sorted_leads()[:records]:
            row = lead.to_row()
            sample.append({k: row.get(k) for k in ("Business Name", "Category", "Locality",
                                                   "Phones", "Emails", "Website", "Score",
                                                   "Sources") if k in row})
        if outcome.run_id is not None:
            done = [r for r in unit_rows(store.engine, outcome.run_id)
                    if r["status"] in ("done", "partial") and r["seconds"]]
            expected = sum(float(r["expected_seconds"] or 0) for r in done)
            actual = sum(float(r["seconds"] or 0) for r in done)
            if expected > 0:
                calibration = round(actual / expected, 3)
    finally:
        store.close()

    full = execute(RunRequest(config=_copy(config), dry_run=True, print_plan=False,
                              install_signals=False, engine=engine))
    estimate = dict(full.estimate)
    if calibration is not None:
        # The units still estimated from cold priors are scaled by what the
        # preview saw; learned ones already carry the preview's history.
        factor = min(3.0, max(1 / 3, calibration))
        basis = estimate.get("basis", {})
        prior_share = basis.get("prior", 0) / max(1, sum(basis.values()))
        scale = 1 + (factor - 1) * prior_share
        estimate["calibrated"] = {
            "factor": round(scale, 3),
            "p50_seconds": round(estimate["p50_seconds"] * scale, 1),
            "p90_seconds": round(estimate["p90_seconds"] * scale, 1),
        }
    return PreviewResult(
        run_id=outcome.run_id, status=outcome.status,
        area=plan.localities[0].name if plan.localities else None,
        category=plan.categories[0].name if plan.categories else None, sources=plan.sources,
        seconds=outcome.elapsed_seconds, records=sample, estimate=estimate,
        calibration=calibration)


def preview(config: Config, *, seconds: float = PREVIEW_SECONDS, area: str | None = None,
            category: str | None = None, sources: int = PREVIEW_SOURCES,
            records: int = 15, **request_overrides: Any) -> PreviewResult:
    """Run a preview here and now (the CLI); the API queues one for a worker instead."""
    plan = plan_preview(config, seconds=seconds, area=area, category=category, sources=sources)
    engine = request_overrides.get("engine")
    clear_scratch(plan.scratch, engine)
    outcome = execute(plan.request(**request_overrides))
    return finish_preview(config, plan, outcome, records=records, engine=engine)


def run_queued_preview(row: dict[str, Any], **request_overrides: Any) -> tuple[Any, PreviewResult]:
    """Execute a preview a worker took off the queue (``runs.kind == "preview"``).

    The row's snapshot is the scratch config and its request the slice; the
    original config, kept in ``request.preview.config``, gives the full-run
    estimate. Returns (outcome, result); the caller stores the result.
    """
    from .runner import request_from_row

    stored = (row.get("request") or {}).get("preview") or {}
    original = config_from_snapshot(stored.get("config") or row.get("config_snapshot") or {})
    request = request_from_row(row, **request_overrides)
    plan = PreviewPlan(request.config, request.localities or [], request.categories or [],
                       list(request.source_names or []),
                       float(stored.get("seconds") or PREVIEW_SECONDS))
    if int(row.get("attempts") or 0) <= 1:
        clear_scratch(plan.scratch, request_overrides.get("engine"))
    outcome = execute(request)
    result = finish_preview(original, plan, outcome, records=int(stored.get("records") or 15),
                            engine=request_overrides.get("engine"))
    return outcome, result
