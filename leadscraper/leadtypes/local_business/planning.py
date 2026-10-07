"""Which sources run, and the order of work units -- one source of truth for
the pipeline that executes them and the plugin's ``plan()`` that reports them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ...packs import inactive_reason
from ..base import WorkUnit

LEAD_TYPE = "local_business"


@dataclass
class SelectionPlan:
    """What source selection did with one registry, kept so a run can say it out loud.

    Four sources sat in the registry for months without ever executing because
    the skip was silent; every entry now leaves a trace with its reason.
    """

    kind: str
    registered: list[str] = field(default_factory=list)
    #: What the caller asked for -- ["all"] when nothing was named.
    requested: list[str] = field(default_factory=list)
    built: list[str] = field(default_factory=list)
    #: name -> why it was not built.
    skipped: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        skipped = ", ".join(f"{name} ({reason})" for name, reason in self.skipped.items())
        return (f"{self.kind}s registered [{', '.join(self.registered) or 'none'}]"
                f" | requested [{', '.join(self.requested) or 'none'}]"
                f" | built [{', '.join(self.built) or 'none'}]"
                f" | skipped [{skipped or 'none'}]")


def select_sources(kind: str, registry: dict[str, type], requested: list[str] | None,
                   config) -> tuple[list[type], SelectionPlan]:
    """The registry entries that are requested, enabled and applicable, in order.

    `None` means "everything enabled"; an empty list means "none" -- that is
    how --enrich-only turns discovery off. A source whose pack does not apply
    to the run's country / verticals is skipped with the reason, never failed.
    """
    explicit = requested is not None and "all" not in [n.lower() for n in requested]
    if explicit:
        wanted = {n.strip().lower() for n in requested}
        unknown = wanted - set(registry)
        if unknown:
            raise ValueError(
                f"Unknown source(s): {sorted(unknown)}. Available: {sorted(registry)}"
            )
    else:
        wanted = set(registry)

    plan = SelectionPlan(kind=kind, registered=list(registry),
                         requested=sorted(wanted) if explicit else ["all"])
    asked_for = ", ".join(sorted(wanted)) or "nothing"
    chosen: list[type] = []
    for name, cls in registry.items():
        ai_task = getattr(cls, "ai_task", None)
        if name not in wanted:
            plan.skipped[name] = f"not requested -- this run asked for {asked_for}"
        elif ai_task is not None and (reason := ai_inactive_reason(ai_task, name, config)):
            plan.skipped[name] = reason
        elif ai_task is None and not config.source_enabled(name):
            plan.skipped[name] = "disabled in config"
        elif (reason := inactive_reason(cls, config.country, config.verticals)):
            # A pack that does not apply is inactive, not failing: it is
            # reported here and never reaches the failure log.
            plan.skipped[name] = f"not applicable -- {reason}"
        elif (reason := unneeded_reason(cls, config.included_fields)):
            plan.skipped[name] = f"not needed -- {reason}"
        else:
            chosen.append(cls)
            plan.built.append(name)
    return chosen, plan


def ai_inactive_reason(task: str, name: str, config) -> str | None:
    """Why an AI source is not built for this run, or None (Phase 5).

    An AI source follows its AI feature, not ``sources.<name>.enabled``: it is
    on when AI is on and the feature is listed, unless the config switches the
    source off explicitly.
    """
    from ...ai.catalog import feature_of, tasks_for

    if config.get(f"sources.{name}.enabled") is False:
        return "disabled in config"
    if not config.get("ai.enabled", False):
        return f"needs AI (the {feature_of(task)} feature); ai.enabled is false"
    if task not in tasks_for(list(config.get("ai.features") or [])):
        return f"needs the AI feature {feature_of(task)} in ai.features"
    return None


def unneeded_reason(source_cls: type, included: set[str]) -> str | None:
    """Why a source has nothing to do for this run's fields, or None (A.8).

    A source that only exists to fill certain fields -- an enricher, or the
    decision-maker search -- is not built when ``fields.include`` leaves all of
    them out. Sources that find businesses always run.
    """
    fills = getattr(source_cls, "fills", None)
    if fills is None or fills & included:
        return None
    return (f"it only fills {', '.join(sorted(fills))}, "
            f"and fields.include asks for {', '.join(sorted(included)) or 'none'}")


# --- discovery order ---------------------------------------------------------

@dataclass
class ComboPlan:
    category: Any
    units: list[tuple[Any, WorkUnit]] = field(default_factory=list)


@dataclass
class LocalityPlan:
    locality: Any
    units: list[tuple[Any, WorkUnit]] = field(default_factory=list)
    combos: list[ComboPlan] = field(default_factory=list)


@dataclass
class DiscoveryPlan:
    run_units: list[tuple[Any, WorkUnit]] = field(default_factory=list)
    localities: list[LocalityPlan] = field(default_factory=list)
    #: Locality x category combinations, for progress reporting.
    combos_total: int = 0

    def units(self) -> list[WorkUnit]:
        out = [unit for _, unit in self.run_units]
        for loc in self.localities:
            out.extend(unit for _, unit in loc.units)
            for combo in loc.combos:
                out.extend(unit for _, unit in combo.units)
        return out


def _scoped(sources: Iterable[Any], scope: str) -> list[Any]:
    return [s for s in sources if getattr(s, "scope", "combo") == scope]


def plan_discovery(config, sources: list[Any], localities: list[Any],
                   categories: list[Any]) -> DiscoveryPlan:
    """Every discovery unit, in the order the pipeline runs them.

    Whole-region sources first (sitemaps: they give the richest venue records,
    which later Maps/directory hits merge into), then per locality: its
    locality-wide sources, then each category's per-combination sources. A
    city-level source (one listing covers the whole city) runs once per
    category, at the first locality the category runs in.
    """
    plan = DiscoveryPlan()
    for source in _scoped(sources, "run"):
        plan.run_units.append((source, WorkUnit(LEAD_TYPE, source.name, None, None, "run")))

    combo_sources = _scoped(sources, "combo")
    plan.combos_total = sum(
        1 for loc in localities for cat in categories if cat.runs_in(loc)
    ) if combo_sources else 0

    city_level_done: set[tuple[str, str]] = set()
    for locality in localities:
        loc_plan = LocalityPlan(locality=locality)
        for source in _scoped(sources, "locality"):
            loc_plan.units.append(
                (source, WorkUnit(LEAD_TYPE, source.name, locality.name, None, "locality")))
        if combo_sources:
            for category in categories:
                if not category.runs_in(locality):
                    continue
                combo = ComboPlan(category=category)
                for source in combo_sources:
                    if not category.runs_source(source.name):
                        continue
                    if config.get(f"sources.{source.name}.city_level", False):
                        key = (source.name, category.name)
                        if key in city_level_done:
                            continue
                        city_level_done.add(key)
                    combo.units.append(
                        (source, WorkUnit(LEAD_TYPE, source.name, locality.name,
                                          category.name, "combo")))
                loc_plan.combos.append(combo)
        plan.localities.append(loc_plan)
    return plan
