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


def preview(config: Config, *, seconds: float = PREVIEW_SECONDS, area: str | None = None,
            category: str | None = None, sources: int = PREVIEW_SOURCES,
            records: int = 15, **request_overrides: Any) -> PreviewResult:
    from ..db.runs import unit_rows
    from ..db.store import LeadStore, store_target

    project = str(config.get("project") or "default")
    scratch = _copy(config)
    scratch.set("project", f"{project} (preview)", record=False)
    scratch.set("runtime.tail_seconds", PREVIEW_TAIL, record=False)
    scratch.set("sheets.enabled", False, record=False)
    if scratch.get("runtime.csv_export"):
        csv_path = Path(str(scratch.get("runtime.csv_export")))
        scratch.set("runtime.csv_export", str(csv_path.with_name("preview.csv")), record=False)
    localities = scratch.select_localities([area] if area else None)[:1]
    categories = scratch.select_categories([category] if category else None)
    categories = [c for c in categories if c.runs_in(localities[0])][:1] if localities else []
    chosen = _top_sources(scratch, sources)

    target, kwargs = store_target(scratch)
    store = LeadStore(target, **kwargs)
    cleared = store.clear()
    store.close()
    if cleared:
        log.info("preview: cleared %d organisations from the last preview", cleared)

    outcome = execute(RunRequest(
        config=scratch, localities=localities, categories=categories, source_names=chosen,
        enricher_names=[], depth="quick", time_budget_minutes=seconds / 60, kind="preview",
        install_signals=False, **request_overrides))

    sample: list[dict[str, Any]] = []
    calibration = None
    store = LeadStore(target, **kwargs)
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
                              install_signals=False))
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
        area=localities[0].name if localities else None,
        category=categories[0].name if categories else None, sources=chosen,
        seconds=outcome.elapsed_seconds, records=sample, estimate=estimate,
        calibration=calibration)
