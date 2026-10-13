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
  richest records land first and later hits merge into them;
* **stages** (Phase 6): a session's ``discovery_stage`` orders lead types. A
  unit starts only once every unit of an earlier stage has finished, so a
  lead type that attaches signals to organisations (``hiring``) sees what the
  finders (``local_business``) found, rather than racing them for a record.
  Under a budget a unit leaves time for every queued later-stage unit that
  outranks it (better yield per second), so ordering never costs a later
  stage its turn (live check 2: hiring and funding_news were skipped while
  near-empty SERP units of stage 0 used up the budget).

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
    #: Its session's discovery stage: it waits for every unit of a lower one.
    stage: int = 0

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
        #: Units handed out and not yet finished, by stage.
        self._running: dict[int, int] = {}

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
            while self._pending:
                stages = [p.stage for p in self._pending]
                stages += [stage for stage, count in self._running.items() if count > 0]
                current = min(stages)
                skipped = False
                later = self._later() if self.control.budgeted else []
                for planned in self._ordered():
                    if planned.stage > current or planned.hosts & busy:
                        continue
                    cost = self.costs.cost(planned.unit)
                    if self.control.budgeted and not self.control.fits(
                            cost.seconds, reserve=self._reserve() + _promised(
                                later, planned.stage, cost.priority)):
                        self._skip(planned, SKIP_BUDGET)
                        skipped = True
                        continue
                    self._pending.remove(planned)
                    planned.cost = cost
                    self._running[planned.stage] = self._running.get(planned.stage, 0) + 1
                    return planned
                if not skipped:
                    return None            # wait: an earlier stage or a busy host
                # Skips may have emptied the current stage: look again at the next one.
            return EXHAUSTED

    def finished(self, planned: Planned) -> None:
        """A unit handed out by ``next_ready`` is over (done, failed or abandoned)."""
        with self._lock:
            count = self._running.get(planned.stage, 0)
            if count > 0:
                self._running[planned.stage] = count - 1

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

    def _later(self) -> list[tuple[int, float, float]]:
        """(stage, priority, seconds) of every queued unit past stage 0. Under the lock."""
        out = []
        for planned in self._pending:
            if planned.stage > 0:
                cost = self.costs.cost(planned.unit)
                out.append((planned.stage, cost.priority, cost.seconds))
        return out

    def waiting_behind(self, stage: int, priority: float) -> float:
        """Seconds promised to queued later-stage units that outrank a unit of
        ``stage`` with this priority (see ``_promised``)."""
        with self._lock:
            later = self._later()
        return _promised(later, stage, priority)

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


def _promised(later: list[tuple[int, float, float]], stage: int, priority: float) -> float:
    """Seconds a unit of ``stage`` and ``priority`` must leave to the later
    stages: the expected seconds of each queued later-stage unit that outranks
    it. Under a budget those would have been chosen first had stages not forced
    the order, so time is kept for them. One that doesn't outrank it waits its
    turn and is skipped if the budget runs out, as it would be anyway."""
    return sum(seconds for unit_stage, unit_priority, seconds in later
               if unit_stage > stage and unit_priority > priority)
