"""Scoring v2: fit, intent and reachability, combined across lead types.

Each lead type scores an organisation on up to three components, each 0..1,
with human-readable reasons:

* **fit**    -- is this the kind of organisation we sell to? (for
  ``local_business``: today's legitimacy / venue score);
* **intent** -- is there a reason to buy now? (hiring, funding, tenders...);
* **reach**  -- can we actually get hold of someone?

``combine`` folds every lead type's parts into one 0..100 score with the
run's weights. A component no enabled lead type provides drops out and the
remaining weights are renormalised, so a run with no intent signals is not
capped at 60. When two lead types score the same component, the stronger
evidence wins (max), since they assess different things.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

COMPONENTS = ("fit", "intent", "reach")

DEFAULT_WEIGHTS: dict[str, float] = {"fit": 0.4, "intent": 0.4, "reach": 0.2}
#: What a pre-Phase-1 config means: the legacy score is the whole score.
LEGACY_WEIGHTS: dict[str, float] = {"fit": 1.0, "intent": 0.0, "reach": 0.0}


@dataclass
class ScoreParts:
    """One lead type's view of one organisation."""

    lead_type: str
    fit: float | None = None
    intent: float | None = None
    reach: float | None = None
    reasons: list[str] = field(default_factory=list)

    def value(self, component: str) -> float | None:
        return getattr(self, component)


@dataclass
class OverallScore:
    total: float = 0.0                      # 0..100
    fit: float | None = None                # 0..1, combined
    intent: float | None = None
    reach: float | None = None
    reasons: list[str] = field(default_factory=list)
    #: lead type -> component -> 0..1, for explaining a ranking.
    contributions: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"total": self.total, "fit": self.fit, "intent": self.intent,
                "reach": self.reach, "reasons": list(self.reasons),
                "contributions": self.contributions}


def normalise_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
    raw = dict(DEFAULT_WEIGHTS if weights is None else weights)
    unknown = set(raw) - set(COMPONENTS)
    if unknown:
        raise ValueError(f"unknown score components {sorted(unknown)}; expected {COMPONENTS}")
    return {c: max(0.0, float(raw.get(c, 0.0))) for c in COMPONENTS}


def combine(parts: Iterable[ScoreParts], weights: Mapping[str, float] | None) -> OverallScore:
    weights = normalise_weights(weights)
    parts = list(parts)
    combined: dict[str, float | None] = {}
    contributions: dict[str, dict[str, float]] = {}
    for component in COMPONENTS:
        values = [p.value(component) for p in parts if p.value(component) is not None]
        combined[component] = max(values) if values else None
    for p in parts:
        contributions[p.lead_type] = {c: round(float(p.value(c)), 4)
                                      for c in COMPONENTS if p.value(c) is not None}

    in_play = [c for c in COMPONENTS if combined[c] is not None and weights[c] > 0]
    weight_sum = sum(weights[c] for c in in_play)
    total = 0.0
    if weight_sum > 0:
        total = 100.0 * sum(weights[c] * combined[c] for c in in_play) / weight_sum

    reasons: list[str] = []
    for p in parts:
        reasons.extend(r for r in p.reasons if r not in reasons)
    return OverallScore(
        total=round(total, 2),
        fit=combined["fit"], intent=combined["intent"], reach=combined["reach"],
        reasons=reasons, contributions=contributions,
    )


#: Published contact channels and how much each says about reachability.
#: ``local_business`` scores its reach with the same weights by default.
CONTACT_WEIGHTS: dict[str, float] = {
    "phones": 0.30,
    "emails": 0.25,
    "website": 0.15,
    "address": 0.10,
    "instagram": 0.08,
    "whatsapp": 0.05,
    "facebook": 0.04,
    "linkedin": 0.03,
}


def contact_reach(record, weights: Mapping[str, float] | None = None) -> float:
    """Reach, 0..1: the weighted share of published contact channels a record has.

    A property of every organisation, whichever lead type found it, so the
    export scores it for all (``export/records.score_records``).
    """
    weights = weights or CONTACT_WEIGHTS
    possible = sum(float(w) for w in weights.values())
    have = sum(float(w) for name, w in weights.items() if getattr(record, name, None))
    return have / possible if possible else 0.0
