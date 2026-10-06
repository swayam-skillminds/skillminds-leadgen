"""What a work unit is expected to cost and yield (REBUILD-PROMPT Part 3.1).

``priority = expected new organisations / expected seconds``.

* **Cold start:** each lead type gives a static prior per unit (its
  ``prior()``), derived from the source, the depth and the configured delays;
  local_business orders them Maps > directories > SERP, as Part 3.1 asks.
* **Warm:** what the same unit -- ``(source, area, category)`` -- cost and
  returned on earlier runs (``unit_stats``), shrunk towards the prior while
  there is little history. A unit never run before borrows its source's
  average over every other area and category.
* **Within the run:** every finished unit is observed immediately, so the
  queue re-sorts on what this run is actually seeing.

Every number carries a spread (``sd``): from the observed variance once a
unit has two runs, else a coefficient of variation by basis. The estimator
turns those into p50/p90 ranges.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

#: Spread assumed for a cost that has no observed variance yet.
CV_BY_BASIS = {"learned": 0.35, "source": 0.5, "prior": 0.6}
#: How many runs' worth of weight the prior keeps against observations.
PRIOR_WEIGHT = 0.5


@dataclass(frozen=True)
class UnitCost:
    seconds: float
    new: float
    sd: float
    #: "learned" (this unit's history), "source" (its source's), "prior".
    basis: str = "prior"
    runs: int = 0

    @property
    def priority(self) -> float:
        """Expected new organisations per second."""
        return (self.new + 0.01) / max(self.seconds, 1.0)

    def as_dict(self) -> dict[str, Any]:
        return {"seconds": round(self.seconds, 1), "new": round(self.new, 2),
                "sd": round(self.sd, 1), "basis": self.basis, "runs": self.runs}


def prior_cost(seconds: float, new: float, cv: float | None = None) -> UnitCost:
    seconds = max(1.0, float(seconds))
    return UnitCost(seconds=seconds, new=max(0.0, float(new)),
                    sd=seconds * (CV_BY_BASIS["prior"] if cv is None else cv), basis="prior")


@dataclass
class Tally:
    runs: int = 0
    seconds: float = 0.0
    sq_seconds: float = 0.0
    new: float = 0.0

    def add(self, seconds: float, new: float) -> None:
        self.runs += 1
        self.seconds += seconds
        self.sq_seconds += seconds * seconds
        self.new += new

    def merged(self, other: "Tally | None") -> "Tally":
        if other is None:
            return self
        return Tally(self.runs + other.runs, self.seconds + other.seconds,
                     self.sq_seconds + other.sq_seconds, self.new + other.new)


class UnitStatsReader:
    """``unit_stats`` of one workspace: the history behind warm estimates. Read once."""

    def __init__(self, engine: Engine | None = None, workspace_id: int | None = None):
        self.engine = engine
        self.workspace_id = workspace_id
        self._units: dict[tuple[str, str, str], Tally] | None = None

    def _load(self) -> dict[tuple[str, str, str], Tally]:
        if self._units is not None:
            return self._units
        self._units = {}
        if self.engine is None or self.workspace_id is None:
            return self._units
        from ..db.models import UnitStat

        table = UnitStat.__table__
        with self.engine.connect() as conn:
            rows = conn.execute(select(table).where(
                table.c.workspace_id == self.workspace_id)).all()
        for row in rows:
            sq = row.total_sq_seconds or 0.0
            if not sq and row.runs:
                # Rows imported from a legacy store carry no variance: treat
                # them as spread-free; the basis' default CV covers it.
                sq = (row.total_seconds / row.runs) ** 2 * row.runs
            self._units[(row.source, row.area, row.category)] = Tally(
                int(row.runs), float(row.total_seconds), float(sq), float(row.total_new_leads))
        return self._units

    def unit(self, source: str, area: str, category: str) -> Tally | None:
        return self._load().get((source, area, category))

    def source(self, source: str) -> Tally | None:
        total = None
        for (name, _area, _cat), tally in self._load().items():
            if name == source:
                total = tally if total is None else total.merged(tally)
        return total


class CostModel:
    """Expected cost of each unit: prior -> history -> this run's observations.

    ``stats_key(unit)`` maps a unit onto its ``unit_stats`` row (source, area,
    category); ``prior(unit)`` is the lead type's cold-start guess.
    """

    def __init__(self, reader: UnitStatsReader | None,
                 prior: Callable[[Any], UnitCost],
                 stats_key: Callable[[Any], tuple[str, str, str]]):
        self.reader = reader or UnitStatsReader()
        self._prior = prior
        self._stats_key = stats_key
        self._lock = threading.Lock()
        self._seen_units: dict[tuple[str, str, str], Tally] = {}
        self._seen_sources: dict[str, Tally] = {}

    def observe(self, unit: Any, seconds: float, new: float) -> None:
        key = self._stats_key(unit)
        with self._lock:
            self._seen_units.setdefault(key, Tally()).add(seconds, new)
            self._seen_sources.setdefault(key[0], Tally()).add(seconds, new)

    def cost(self, unit: Any) -> UnitCost:
        prior = self._prior(unit)
        key = self._stats_key(unit)
        with self._lock:
            seen_unit = self._seen_units.get(key)
            seen_source = self._seen_sources.get(key[0])
        tally = _combined(self.reader.unit(*key), seen_unit)
        basis = "learned"
        if tally is None:
            tally = _combined(self.reader.source(key[0]), seen_source)
            basis = "source"
        if tally is None:
            return prior
        return blend(prior, tally, basis)


def _combined(history: Tally | None, seen: Tally | None) -> Tally | None:
    if history is None and seen is None:
        return None
    total = (history or Tally()).merged(seen)
    return total if total.runs else None


def blend(prior: UnitCost, tally: Tally, basis: str) -> UnitCost:
    """Observations, shrunk towards the prior while they are few."""
    runs = tally.runs
    mean_s = tally.seconds / runs
    mean_new = tally.new / runs
    weight = runs / (runs + PRIOR_WEIGHT)
    seconds = weight * mean_s + (1 - weight) * prior.seconds
    new = weight * mean_new + (1 - weight) * prior.new
    if runs >= 2:
        variance = max(0.0, tally.sq_seconds / runs - mean_s * mean_s) * runs / (runs - 1)
        sd = max(math.sqrt(variance), seconds * 0.1)
    else:
        sd = seconds * CV_BY_BASIS[basis]
    return UnitCost(seconds=max(1.0, seconds), new=max(0.0, new), sd=sd, basis=basis, runs=runs)
