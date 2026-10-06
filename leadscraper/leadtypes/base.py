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
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from ..core.scoring import ScoreParts

ColumnKind = Literal["text", "number", "url", "phone", "email", "date", "score", "flag", "money"]


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
    """One Records-tab column a lead type contributes."""

    key: str
    header: str
    kind: ColumnKind = "text"
    description: str = ""
    #: The `fields.include` entry that switches this column on; None = always.
    field: str | None = None


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
        """Records-tab columns it contributes, in order."""

    @abstractmethod
    def score(self, org: Any, signals: list, cfg: Any) -> ScoreParts:
        """Its fit / intent / reach contribution for one organisation, with reasons."""

    @abstractmethod
    def run(self, context: Any) -> LeadTypeResult:
        """Execute its work units against `context.store`. Must not raise."""
