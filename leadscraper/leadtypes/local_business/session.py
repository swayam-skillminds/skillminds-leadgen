"""local_business as an engine session: the Pipeline, driven unit by unit."""

from __future__ import annotations

import logging
from typing import Any

from ...engine.costs import UnitCost
from ..base import LeadTypeResult, LeadTypeSession, UnitOutcome, WorkUnit
from . import estimates

log = logging.getLogger(__name__)


class LocalBusinessSession(LeadTypeSession):
    lead_type = "local_business"

    def __init__(self, context: Any):
        from .pipeline import Pipeline

        config = context.config
        self.context = context
        self.pipeline = Pipeline(
            config,
            localities=getattr(context, "localities", None) or config.localities,
            categories=getattr(context, "categories", None) or config.categories,
            source_names=getattr(context, "source_names", None),
            enricher_names=getattr(context, "enricher_names", None),
            headless=bool(config.get("runtime.headless", True)),
            dry_run=bool(getattr(context, "dry_run", False)),
            store=getattr(context, "store", None),
            install_signals=False,
            failure_sink=getattr(context, "failure_sink", None),
            control=getattr(context, "control", None),
            events=getattr(context, "events", None),
            engine_driven=True,
            force_enrich=getattr(context, "force_enrich", None),
        )
        context.pipeline = self.pipeline
        self._units = self.pipeline.discovery_units()

    # --- planning -------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def _entry(self, unit: WorkUnit):
        return self.pipeline._units.get(unit.key)

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        entry = self._entry(unit)
        if entry is None:
            return super().hosts(unit)
        source = entry[0]
        try:
            hosts = source.hosts()
        except Exception:
            hosts = [source.name]
        return frozenset(hosts) | {f"source:{source.name}"}

    def prior(self, unit: WorkUnit) -> UnitCost:
        entry = self._entry(unit)
        if entry is None:
            return super().prior(unit)
        source, locality, category, _ = entry
        return estimates.unit_prior(source, locality, category, self.pipeline.config)

    def stats_key(self, unit: WorkUnit) -> tuple[str, str, str]:
        depth = str(self.pipeline.config.get("depth") or "balanced")
        source, area, category = super().stats_key(unit)
        # A quick unit costs a fraction of a deep one: learn each depth apart.
        # "balanced" keeps the plain key, which earlier runs and imports wrote.
        if depth != "balanced":
            source = f"{source}@{depth}"
        return source, area, category

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        return estimates.post_estimate(self.pipeline, expected_new)

    def ai_estimate(self, expected_new: float) -> dict[str, Any] | None:
        return estimates.ai_estimate(self.pipeline, expected_new)

    # --- execution ------------------------------------------------------------------

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        result = self.pipeline.run_planned_unit(unit)
        return UnitOutcome(found=result.found, new=result.new, merged=result.merged,
                           status=result.status, error=result.error)

    def checkpoint(self) -> None:
        """After each unit, on the engine's dispatcher thread: flush when due."""
        self.pipeline._maybe_flush()

    def post_discovery(self, control: Any) -> None:
        self.pipeline.post_discovery()

    def finalize(self, *, complete: bool) -> None:
        self.pipeline._finalise()

    def close(self) -> None:
        self.pipeline._close()

    def revalidate(self) -> None:
        """After a machine sleep: fresh connections and browsers."""
        self.pipeline.browser.revalidate()
        try:
            self.pipeline.http.close()       # sessions are recreated on next use
        except Exception as exc:
            log.debug("http reset failed (ignored): %s", exc)

    def print_plan(self) -> None:
        self.pipeline._print_plan()

    def result(self) -> LeadTypeResult:
        stats = self.pipeline.stats
        return LeadTypeResult(
            lead_type=self.lead_type, units_planned=len(self._units),
            units_done=stats.units_done, new=stats.new_leads, merged=stats.merged_leads,
            stats={"leads_seen": stats.leads_seen, "enriched": stats.enriched,
                   "combos_done": stats.combos_done, "combos_total": stats.combos_total})
