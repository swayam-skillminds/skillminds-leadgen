"""A synthetic lead type for engine tests.

Its units spend time on the run's clock (a ``FakeClock`` in-process, or real
sleeps when ``real_seconds`` is set -- the subprocess worker test) and write
real organisations to the store. Load it in another process with
``LEADSCRAPER_PLUGINS=tests.engine_fixtures``.

Config (``lead_types.synthetic``)::

    units:   [{source, area, category, host, seconds, new}]   # one per unit
    recorded: path to a JSON of recorded durations; attempt picks the run
    priors:  {source: [seconds, new]}                          # cold-start guesses
    post_seconds: post-discovery work, spent in 10 s slices
    log_path: append "<unit key>" per execution (the kill test counts them)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from leadscraper.core.lead import Lead
from leadscraper.engine.costs import prior_cost
from leadscraper.leadtypes.base import (
    LeadType, LeadTypeResult, LeadTypeSession, UnitOutcome, WorkUnit,
)

#: Test hooks: called with (session, unit, index) after each unit.
HOOKS: dict[str, Callable] = {}
RECORDED = Path(__file__).parent / "fixtures" / "recorded_units.json"


class SyntheticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    units: list[dict[str, Any]] = Field(default_factory=list)
    recorded: str | None = None
    attempt: int = 0
    priors: dict[str, list[float]] = Field(default_factory=dict)
    post_seconds: float = 0.0
    real_seconds: float | None = None
    log_path: str | None = None


class SyntheticSession(LeadTypeSession):
    lead_type = "synthetic"

    def __init__(self, context):
        self.context = context
        self.cfg: SyntheticConfig = context.config.run_config.lead_type("synthetic")
        self.store = context.store
        self.control = context.control
        self.clock = self.control.clock if self.control is not None else None
        self._result = LeadTypeResult(lead_type=self.lead_type)
        self._specs: dict[str, dict] = {}
        self._units: list[WorkUnit] = []
        for spec in self._unit_specs():
            unit = WorkUnit(self.lead_type, spec["source"], spec.get("area"),
                            spec.get("category"), spec.get("scope", "combo"))
            self._specs[unit.key] = spec
            self._units.append(unit)
        self._result.units_planned = len(self._units)
        self.post_done = 0.0
        self.finalized: list[bool] = []

    def _unit_specs(self) -> list[dict]:
        if not self.cfg.recorded:
            return list(self.cfg.units)
        recorded = json.loads(Path(self.cfg.recorded).read_text(encoding="utf-8"))
        specs = []
        for row in recorded["units"]:
            run = row["runs"][self.cfg.attempt % len(row["runs"])]
            specs.append({"source": row["source"], "area": row["area"],
                          "category": row["category"], "host": row["host"],
                          "seconds": run["seconds"], "new": run["new"]})
        return specs

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        return frozenset({self._specs[unit.key].get("host") or unit.source})

    def prior(self, unit: WorkUnit):
        seconds, new = self.cfg.priors.get(unit.source, (60.0, 3.0))
        return prior_cost(seconds, new)

    def _spend(self, seconds: float) -> None:
        if self.cfg.real_seconds is not None:
            time.sleep(self.cfg.real_seconds)
        elif self.clock is not None:
            self.clock.sleep(seconds)

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        spec = self._specs[unit.key]
        if self.cfg.log_path:
            with open(self.cfg.log_path, "a", encoding="utf-8") as handle:
                handle.write(unit.key + "\n")
        count = int(spec.get("new", 1))
        seconds = float(spec.get("seconds", 60))
        slices = max(1, count)
        outcome = UnitOutcome()
        for i in range(slices):
            self._spend(seconds / slices)
            if count:
                name = f"{unit.source} {unit.area} {unit.category} {i}"
                digits = sum(ord(ch) * (k + 1) for k, ch in enumerate(name)) % 100_000_000
                _, action, _ = self.store.upsert(Lead(
                    business_name=name.title(), locality=unit.area or "", category="Synthetic",
                    phones=[f"+9198{digits:08d}"], sources=[unit.source]))
                outcome.found += 1
                outcome.new += action == "new"
                outcome.merged += action == "merged"
            control = self.control
            # Like the pipeline: a unit that overruns into the tail is cut.
            if control is not None and control.budgeted and \
                    control.remaining() < -control.tail_seconds / 2:
                outcome.status = "partial"
                break
        self._result.units_done += 1
        self._result.new += outcome.new
        self._result.merged += outcome.merged
        hook = HOOKS.get("after_unit")
        if hook is not None:
            hook(self, unit, self._result.units_done)
        return outcome

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        return self.cfg.post_seconds, self.cfg.post_seconds * 0.2

    def post_discovery(self, control) -> None:
        remaining = self.cfg.post_seconds
        while remaining > 0 and not control.stop_requested:
            step = min(10.0, remaining)
            self._spend(step)
            self.post_done += step
            remaining -= step

    def finalize(self, *, complete: bool) -> None:
        self.finalized.append(complete)

    def result(self) -> LeadTypeResult:
        return self._result


class SyntheticLeadType(LeadType):
    key = "synthetic"
    title = "Synthetic (tests)"
    config_model = SyntheticConfig

    def sources(self, cfg, geo=None):
        return {"discovery": [], "enrichment": []}

    def plan(self, cfg, geo=None, depth="balanced"):
        return []

    def signal_specs(self):
        return []

    def columns(self, cfg):
        return []

    def score(self, org, signals, cfg):
        from leadscraper.core.scoring import ScoreParts

        return ScoreParts(lead_type=self.key, fit=0.5)

    def open(self, context):
        return SyntheticSession(context)

    def run(self, context):  # pragma: no cover - the engine uses open()
        raise NotImplementedError


LEAD_TYPE = SyntheticLeadType()


def synthetic_config(name: str = "synthetic", *, budget: float | None = None,
                     runtime: dict | None = None, **section) -> dict:
    document = {
        "name": name, "project": name,
        "geography": {"country": "IN", "places": ["Testville"], "center": [19.0, 72.8],
                      "admin_area": "Testshire"},
        "lead_types": {"synthetic": section},
        "runtime": {"heartbeat_seconds": 0.05, **(runtime or {})},
    }
    if budget is not None:
        document["time_budget_minutes"] = budget
    return document
