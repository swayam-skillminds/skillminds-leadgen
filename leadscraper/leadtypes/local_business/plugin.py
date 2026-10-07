"""The ``local_business`` lead type: businesses by category and location.

This is the original scraper, wrapped in the A.7 contract. Its config section
(``lead_types.local_business``) compiles into the ``Config`` view its
sources and pipeline read; ``cfg`` below is that compiled view.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.scoring import ScoreParts
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from .config import LocalBusinessConfig
from .planning import plan_discovery, select_sources

log = logging.getLogger(__name__)

def _verticals(cfg: Any) -> list[str] | None:
    """The run's verticals, or None when the config can't say (then: everything)."""
    try:
        return list(cfg.verticals)
    except Exception:
        return None


class LocalBusinessLeadType(LeadType):
    key = "local_business"
    title = "Local businesses"
    description = ("Businesses by category and location from maps, search, directories "
                   "and booking platforms; venues are its hospitality vertical.")
    config_model = LocalBusinessConfig
    applicable_countries = None

    # --- planning -------------------------------------------------------------

    def _registries(self):
        from ...sources import registry

        return registry.DISCOVERY_SOURCES, registry.ENRICHMENT_SOURCES

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        discovery, enrichment = self._registries()
        found, _ = select_sources("source", discovery, None, cfg)
        enrich, _ = select_sources("enricher", enrichment, None, cfg)
        return {"discovery": [c.name for c in found], "enrichment": [c.name for c in enrich]}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        discovery, _ = self._registries()
        classes, _ = select_sources("source", discovery, None, cfg)
        return plan_discovery(cfg, classes, cfg.localities, cfg.categories).units()

    def signal_specs(self) -> list[SignalSpec]:
        # Ratings and listings are fit evidence, not a reason to buy now.
        return []

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        from .columns import column_table

        profile = getattr(cfg, "profile", "vendors")
        currency = getattr(cfg, "currency", "") or "INR"
        return [spec for _, spec in column_table(profile, currency, _verticals(cfg))]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        from .columns import values

        lead = getattr(org, "lead", org)
        return values(lead, getattr(cfg, "profile", "vendors"),
                      getattr(cfg, "currency", "") or "INR", _verticals(cfg))

    def score(self, org: Any, signals: list, cfg: Any) -> ScoreParts:
        """``org`` is the merged ``Lead``; venue fields must already be annotated
        (``Scorer.apply``), as every store write and export does."""
        scorer = cfg if hasattr(cfg, "parts") else self._scorer(cfg)
        return scorer.parts(getattr(org, "lead", org))

    _cached: tuple[Any, Any] | None = None

    def _scorer(self, cfg: Any):
        """One Scorer per config: an export scores thousands of organisations."""
        from .scoring import Scorer

        cached = self._cached
        if cached is None or cached[0] is not cfg:
            cached = (cfg, Scorer(cfg))
            self._cached = cached
        return cached[1]

    # --- execution ------------------------------------------------------------

    def open(self, context: Any):
        """The engine's session: the pipeline, driven unit by unit."""
        from .session import LocalBusinessSession

        return LocalBusinessSession(context)

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        from .estimates import apply_depth

        return apply_depth(cfg, depth)

    def run(self, context: Any) -> LeadTypeResult:
        """Run the pipeline over `context`; any crash is contained and reported."""
        from .pipeline import Pipeline

        result = LeadTypeResult(lead_type=self.key)
        try:
            config = context.config
            pipeline = Pipeline(
                config,
                localities=getattr(context, "localities", None) or config.localities,
                categories=getattr(context, "categories", None) or config.categories,
                source_names=getattr(context, "source_names", None),
                enricher_names=getattr(context, "enricher_names", None),
                headless=bool(config.get("runtime.headless", True)),
                dry_run=bool(getattr(context, "dry_run", False)),
                store=getattr(context, "store", None),
                install_signals=bool(getattr(context, "install_signals", True)),
                failure_sink=getattr(context, "failure_sink", None),
                unit_sink=getattr(context, "unit_sink", None),
            )
            result.units_planned = len(plan_discovery(
                config, pipeline.sources, pipeline.localities, pipeline.categories).units())
            stats = pipeline.run()
            result.units_done = stats.units_done
            result.new, result.merged = stats.new_leads, stats.merged_leads
            result.stats = {"leads_seen": stats.leads_seen, "enriched": stats.enriched,
                            "combos_done": stats.combos_done,
                            "combos_total": stats.combos_total}
            context.pipeline = pipeline
        except Exception as exc:  # contained: one lead type never ends the run
            log.exception("lead type %s failed", self.key)
            result.failed, result.error = True, repr(exc)
        return result


LOCAL_BUSINESS = LocalBusinessLeadType()
