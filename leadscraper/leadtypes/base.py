"""The lead-type plugin contract (APP-BUILD-PROMPT A.7; see docs/lead-types.md).

A lead type is a self-contained way of finding organisations worth selling
to: its own config model (``lead_types.<key>`` in a run config), its sources,
the signals it emits, the Records columns it contributes, and its share of
the score. Rules every lead type keeps:

* it depends only on core interfaces (``core``, ``sources``, ``packs``,
  ``db``), never on another lead type;
* enabling, disabling or deleting it never changes another's output;
* a failure inside it is contained to its own work units;
* every signal / evidence row it writes is tagged with its ``key``.

Execution (Phase 2): the engine owns the unit loop. ``open(context)`` returns
a ``LeadTypeSession`` that lists its work units and runs them one at a time
when the scheduler says so; the engine checkpoints after every unit, runs the
session's post-discovery work (enrichment, AI) and always lets it finalise.
A lead type that only implements ``run()`` still works: it becomes a single
unit (``MonolithicSession``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from ..core.scoring import ScoreParts
from ..engine.costs import UnitCost, prior_cost

ColumnKind = Literal["text", "number", "url", "phone", "email", "date", "score", "flag", "money"]
ColumnPlacement = Literal["front", "body", "contacts", "tail"]


@dataclass(frozen=True)
class SignalSpec:
    """A kind of signal a lead type can emit."""

    type: str
    title: str
    #: Strength of a fresh signal, 0..1.
    default_strength: float = 0.5
    #: Days for the signal's weight to halve; None = it does not age.
    half_life_days: float | None = None
    description: str = ""


@dataclass(frozen=True)
class ColumnSpec:
    """One Records-tab column a lead type contributes (Phase 4: ``export/columns.py``).

    The workbook, CSV and JSON exports all render these. Values come from the
    lead type's ``column_values()``; a column that is empty on every row of an
    export is left out of it.
    """

    key: str
    header: str
    kind: ColumnKind = "text"
    #: Shown in the Overview's column legend; leave empty for self-explanatory columns.
    description: str = ""
    #: The `fields.include` entry that switches this column on; None = always.
    field: str | None = None
    #: Where it sits in Records: "front" (right after the name: classification),
    #: "body" (before the contacts: the main attributes), "contacts" (after the
    #: published contacts) or "tail" (after scores and signals: detail).
    placement: ColumnPlacement = "body"
    #: Excel width in characters; None = the default for its kind.
    width: float | None = None
    #: Excel number format; None = the default for its kind.
    number_format: str | None = None
    #: The Overview counts records by this column's values ("By tier").
    breakdown: bool = False
    #: Value -> font colour (hex) in the workbook, e.g. one colour per tier.
    palette: tuple[tuple[str, str], ...] = ()
    #: Display text for a link column ("View map"); None = a short form of the URL.
    link_label: str | None = None


@dataclass
class WorkUnit:
    """One schedulable piece of work: (lead type, source, area, category)."""

    lead_type: str
    source: str
    area: str | None = None
    category: str | None = None
    #: "run" (once), "locality" (per area) or "combo" (per area x category).
    scope: str = "combo"

    @property
    def key(self) -> str:
        return "|".join([self.lead_type, self.source, self.area or "*", self.category or "*"])


@dataclass
class LeadTypeResult:
    lead_type: str
    units_planned: int = 0
    units_done: int = 0
    new: int = 0
    merged: int = 0
    failed: bool = False
    error: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


class LeadType(ABC):
    key: str
    title: str
    description: str = ""
    config_model: type[BaseModel]
    #: ISO codes the lead type works in; None = anywhere.
    applicable_countries: frozenset[str] | None = None
    #: Post-discovery order (Phase 6): a lead type that assesses organisations
    #: other lead types found (``digital_audit``) runs its post-discovery work
    #: after theirs. Lower first; ties keep the registry order.
    post_order: int = 0

    @abstractmethod
    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        """{"discovery": [...], "enrichment": [...]} source names that will run."""

    @abstractmethod
    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        """The work units a run of this lead type would execute, in order."""

    @abstractmethod
    def signal_specs(self) -> list[SignalSpec]:
        """Signal types this lead type emits."""

    @abstractmethod
    def columns(self, cfg: Any) -> list[ColumnSpec]:
        """Records-tab columns it contributes, in order.

        Only its own: the core columns (name, location, published contacts,
        scores, signals, decision-makers, provenance) belong to the export.
        """

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        """One organisation's values for its ``columns()``, keyed by ``ColumnSpec.key``.

        ``org`` is an ``export.records.ExportRecord``: the merged record
        (``org.lead``), its signals, people and provenance. Values are plain:
        str, int, float, bool, datetime or None (blank).
        """
        return {}

    @abstractmethod
    def score(self, org: Any, signals: list, cfg: Any) -> ScoreParts:
        """Its fit / intent / reach contribution for one organisation, with reasons."""

    @abstractmethod
    def run(self, context: Any) -> LeadTypeResult:
        """Execute its work units against `context.store`. Must not raise."""

    def open(self, context: Any) -> "LeadTypeSession":
        """A session the engine drives unit by unit. Default: ``run()`` as one unit."""
        return MonolithicSession(self, context)

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        """Scale this lead type's own knobs for a depth preset; returns what changed."""
        return []

    def catalog_sources(self) -> list[dict[str, Any]]:
        """Its own sources for the catalog (``GET /catalog/sources``), beyond the
        shared source registry: name, kind, pack, applicable_countries, defaults."""
        return []


@dataclass
class UnitOutcome:
    """What one work unit did."""

    found: int = 0
    new: int = 0
    merged: int = 0
    #: done | failed
    status: str = "done"
    error: str = ""


class LeadTypeSession(ABC):
    """One lead type's share of one run, driven by the engine.

    Contract, in call order: ``units()`` once; ``run_unit()`` for each unit the
    scheduler starts (possibly from worker threads, one unit per host group at
    a time; must not raise); ``post_discovery()`` unless the run is stopping;
    ``finalize()`` always, even after a pause or cancel (flush what we have);
    ``close()`` last.
    """

    lead_type: str = ""
    #: What ``post_estimate`` is sized by: "own" (this lead type's expected new
    #: organisations) or "run" (every lead type's: it assesses them all).
    post_scope: str = "own"

    @abstractmethod
    def units(self) -> list[WorkUnit]:
        """The discovery units, in plan order."""

    @abstractmethod
    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        """Execute one unit. Never raises: failures are recorded and returned."""

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        """Hosts the unit hits: units sharing one never run at the same time."""
        return frozenset({f"{self.lead_type}:{unit.source}"})

    def stats_key(self, unit: WorkUnit) -> tuple[str, str, str]:
        """The ``unit_stats`` row that learns this unit's cost and yield."""
        source = unit.source if self.lead_type == "local_business" \
            else f"{self.lead_type}:{unit.source}"
        return source, unit.area or "*", unit.category or "*"

    def prior(self, unit: WorkUnit) -> UnitCost:
        """Cold-start guess of the unit's seconds and new organisations."""
        return prior_cost(60.0, 3.0)

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        """(mean, sd) seconds of post-discovery work for this many new organisations."""
        return 0.0, 0.0

    def ai_estimate(self, expected_new: float) -> dict[str, Any] | None:
        """Expected AI calls and cost, when this lead type uses AI this run."""
        return None

    def checkpoint(self) -> None:
        """After every finished unit, on the engine's dispatcher thread (flush output)."""

    def ai_spend(self) -> dict[str, Any] | None:
        """{cost_usd, cap_usd, calls} spent on AI so far, when this lead type uses AI."""
        return None

    def post_discovery(self, control: Any) -> None:
        """Enrichment, AI passes. Must watch ``control.stop_requested`` and not raise."""

    def finalize(self, *, complete: bool) -> None:
        """Rescore and write output. ``complete`` is False after a pause or cancel."""

    def close(self) -> None:
        """Release browsers, connections."""

    @abstractmethod
    def result(self) -> LeadTypeResult:
        """Counts so far."""


class MonolithicSession(LeadTypeSession):
    """A lead type that only has ``run()``: one unit, run whole."""

    def __init__(self, lead_type: LeadType, context: Any):
        self.lead_type = lead_type.key
        self._lead_type = lead_type
        self._context = context
        self._result = LeadTypeResult(lead_type=lead_type.key, units_planned=1)

    def units(self) -> list[WorkUnit]:
        return [WorkUnit(self.lead_type, "*", None, None, "run")]

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        try:
            result = self._lead_type.run(self._context)
        except Exception as exc:  # run() must not raise; contain it if it does
            result = LeadTypeResult(lead_type=self.lead_type, failed=True, error=repr(exc))
        result.units_planned = max(result.units_planned, 1)
        self._result = result
        return UnitOutcome(new=result.new, merged=result.merged,
                           status="failed" if result.failed else "done", error=result.error)

    def result(self) -> LeadTypeResult:
        return self._result
