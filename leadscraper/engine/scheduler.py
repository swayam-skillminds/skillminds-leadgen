"""The time-budget scheduler (REBUILD-PROMPT Part 3; APP-BUILD-PROMPT Phase 2.1).

Given every lead type's work units and a wall-clock budget, hand out the
units most likely to produce new organisations per second, and never one
that can't finish:

* ``priority = expected new / expected seconds`` (``costs.CostModel``);
  cold-start priors, then learned per-unit stats, re-sorted after every unit
  because each finished unit is observed by the cost model;
* a unit whose expected duration exceeds what is left of the budget (minus
  the reserved tail and the share kept back for enrichment) is skipped with
  its reason -- the next one that fits is taken instead. The budget only
  shrinks, so a unit that doesn't fit now never will;
* whole-region units (scope "run": sitemaps) go first in every mode: the
  richest records land first and later hits merge into them.

Without a time budget the units run in **plan order** -- exactly the order
the original scraper used -- so unbudgeted runs (both legacy presets) are
unchanged. ``order="priority"`` asks for priority order regardless.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .control import RunControl
from .costs import CostModel, UnitCost
from .parallel import EXHAUSTED

SKIP_BUDGET = "would not finish inside the time budget"


@dataclass(eq=False)
class Planned:
    """One unit in the queue, with the session that executes it."""

    unit: Any
    session: Any
    index: int
    hosts: frozenset[str] = field(default_factory=frozenset)
    #: The cost the scheduler started it with (recorded on the work unit).
    cost: UnitCost | None = None

    @property
    def key(self) -> str:
        return self.unit.key


class Scheduler:
    def __init__(self, planned: list[Planned], costs: CostModel, control: RunControl, *,
                 order: str = "plan", reserve: Callable[[], float] = lambda: 0.0,
                 on_skip: Callable[[Planned, str], None] | None = None):
        self.costs = costs
        self.control = control
        self.order = order
        self._reserve = reserve
        self._on_skip = on_skip
        self._pending: list[Planned] = list(planned)
        self._lock = threading.Lock()
        self.skipped: list[tuple[Planned, str]] = []

    @property
    def pending(self) -> list[Planned]:
        with self._lock:
            return list(self._pending)

    def _ordered(self) -> list[Planned]:
        if self.order != "priority":
            return sorted(self._pending, key=lambda p: (p.unit.scope != "run", p.index))
        return sorted(self._pending, key=lambda p: (p.unit.scope != "run",
                                                    -self.costs.cost(p.unit).priority, p.index))

    def next_ready(self, busy: set[str]) -> Any:
        """The best unit that fits and whose hosts are free; None to wait; EXHAUSTED."""
        with self._lock:
            if not self._pending:
                return EXHAUSTED
            for planned in self._ordered():
                if planned.hosts & busy:
                    continue
                cost = self.costs.cost(planned.unit)
                if self.control.budgeted and not self.control.fits(cost.seconds,
                                                                   reserve=self._reserve()):
                    self._skip(planned, SKIP_BUDGET)
                    continue
                self._pending.remove(planned)
                planned.cost = cost
                return planned
            return EXHAUSTED if not self._pending else None

    def _skip(self, planned: Planned, reason: str) -> None:
        self._pending.remove(planned)
        self.skipped.append((planned, reason))
        if self._on_skip is not None:
            self._on_skip(planned, reason)

    def skip_rest(self, reason: str) -> None:
        """A stop: every unit not started is skipped, with the reason."""
        with self._lock:
            for planned in list(self._pending):
                self._skip(planned, reason)

    def expected_remaining(self) -> tuple[float, float]:
        """(mean seconds, variance) of everything still queued."""
        with self._lock:
            pending = list(self._pending)
        total = var = 0.0
        for planned in pending:
            cost = self.costs.cost(planned.unit)
            total += cost.seconds
            var += cost.sd ** 2
        return total, var
