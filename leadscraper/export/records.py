"""One exported organisation, scored and placed (Phase 4).

``services/exports.build_table`` reads the database into ``ExportRecord``s;
every format (workbook, CSV, JSON) renders the same records through the same
column model (``export/columns.py``). Nothing here touches the database.

Scoring: each lead type the organisation belongs to scores it
(``LeadType.score``), and ``core.scoring.combine`` folds the parts with the
run's weights. For an organisation only ``local_business`` found, that is
exactly the legacy rescore, so legacy rankings are unchanged.
"""

from __future__ import annotations

import inspect
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..core.lead import Lead
from ..core.normalize import normalize_name
from ..core.scoring import COMPONENTS, OverallScore, ScoreParts, combine, contact_reach

log = logging.getLogger(__name__)

#: ``output.sort`` values (config/schema.py).
SORTS = ("auto", "score", "place", "area", "category", "legacy")
SORT_LABELS = {
    "score": "score (highest first)",
    "place": "place, then score",
    "area": "place, area, then score",
    "category": "category, then score",
    "legacy": "the run's profile order",
}


@dataclass
class ExportRecord:
    """One organisation as every export format sees it."""

    id: int
    lead: Lead
    #: Lead types that found or enriched it, e.g. ["local_business"].
    lead_types: list[str] = field(default_factory=list)
    #: Signal rows: type, lead_type, title, payload, observed_at, published_at,
    #: expires_at (aware datetimes or None), strength, source_url.
    signals: list[dict[str, Any]] = field(default_factory=list)
    #: People rows: name, title, profile_url, source.
    people: list[dict[str, Any]] = field(default_factory=list)
    #: Contacts of kind "inferred" (Phase 6's email pattern): channel, value, source.
    #: Never mixed into the published-contact columns.
    inferred: list[dict[str, Any]] = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    #: What the exported run did to it: new | updated | unchanged (None: no run).
    change: str | None = None
    #: Names of "flag" suppression lists it matched.
    flags: list[str] = field(default_factory=list)
    #: Which of the run's places it is in ("" when unknown).
    place: str = ""
    #: AI attributes (Phase 5): key -> {value, estimated, producer}. Never facts:
    #: their columns say "(AI)" / "(AI estimate)".
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    overall: OverallScore = field(default_factory=OverallScore)
    #: Column key -> value, filled when the columns are assembled.
    values: dict[str, Any] = field(default_factory=dict)
    #: The moment the export describes (its generated_at): signal ages count to here.
    as_of: datetime | None = None

    @property
    def score(self) -> float:
        return self.overall.total

    def effective_signals(self, now: datetime,
                          specs: dict[tuple[str, str], Any] | None = None) -> list[dict[str, Any]]:
        """Unexpired signals, strongest first: strength halved every half-life of age."""
        out = []
        for signal in self.signals:
            expires = signal.get("expires_at")
            if expires is not None and expires < now:
                continue
            strength = float(signal.get("strength") or 0.0)
            spec = (specs or {}).get((signal.get("lead_type") or "", signal.get("type") or ""))
            half_life = getattr(spec, "half_life_days", None)
            when = signal_date(signal)
            if half_life and when is not None:
                age = max(0.0, (now - when).total_seconds() / 86400)
                strength *= math.pow(0.5, age / float(half_life))
            out.append({**signal, "effective_strength": round(strength, 4)})
        out.sort(key=lambda s: (-s["effective_strength"],
                                -(signal_date(s) or _EPOCH).timestamp(), s.get("title") or ""))
        return out


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def signal_date(signal: dict[str, Any]) -> datetime | None:
    """When the event happened: its publication date, else when we saw it."""
    return signal.get("published_at") or signal.get("observed_at")


# --- scoring ---------------------------------------------------------------------------------

def _takes_now(score: Any) -> bool:
    """Does a lead type's ``score`` take ``now`` (signals that fade with age)?"""
    try:
        return "now" in inspect.signature(score).parameters
    except (TypeError, ValueError):
        return False


def score_records(records: Iterable[ExportRecord], config: Any, weights: dict[str, float],
                  default_lead_type: str = "local_business",
                  now: datetime | None = None) -> None:
    """Every lead type's parts for each record, combined with the run's weights.

    ``now`` is the moment the export describes: a lead type whose ``score``
    takes ``now`` ages its signals to it, so an export is the same whenever it
    is regenerated for the same moment.

    Weights renormalise per *run*, not per organisation: a component some lead
    type in the export scores counts as 0 for an organisation without it (a
    business with no hiring signal has no intent, rather than intent left out
    and its other components scaled up). Reach -- published contact channels --
    is a property of every organisation, so the export scores it for all.
    """
    from ..leadtypes import registry

    records = list(records)
    scored: list[list[ScoreParts]] = []
    for record in records:
        parts: list[ScoreParts] = []
        for key in record.lead_types or [default_lead_type]:
            lead_type = registry.get(key)
            if lead_type is None:
                continue
            signals = [s for s in record.signals if s.get("lead_type") == key]
            extra = {"now": now} if now is not None and _takes_now(lead_type.score) else {}
            try:
                parts.append(lead_type.score(record.lead, signals, config, **extra))
            except Exception as exc:  # one lead type's bug never sinks the export
                log.warning("lead type %s could not score organisation %s: %s",
                            key, record.id, exc)
        if all(p.reach is None for p in parts):
            reach = contact_reach(record.lead)
            channels = [label for label, attr in _CHANNELS if getattr(record.lead, attr)]
            parts.append(ScoreParts("", reach=reach, reasons=[
                f"reachable by {', '.join(channels)}" if channels
                else "no published phone, email or website"]))
        scored.append(parts)
    in_run = {c for parts in scored for p in parts for c in COMPONENTS
              if p.value(c) is not None}
    for record, parts in zip(records, scored):
        missing = [c for c in in_run if all(p.value(c) is None for p in parts)]
        filler = [ScoreParts("", **{c: 0.0 for c in missing})] if missing else []
        overall = combine([*parts, *filler], weights)
        overall.contributions.pop("", None)
        record.overall = overall
        record.lead.score = overall.total


_CHANNELS = (("phone", "phones"), ("email", "emails"), ("website", "website"),
             ("Instagram", "instagram"), ("WhatsApp", "whatsapp"))


# --- places and order ------------------------------------------------------------------------

def place_of(lead: Lead, places: list[str]) -> str:
    """The run place an organisation is in: its locality, or a place its address names."""
    if not places:
        return ""
    if len(places) == 1:
        return places[0]
    by_key = {normalize_name(p): p for p in places}
    hit = by_key.get(normalize_name(lead.locality or ""))
    if hit:
        return hit
    text = f" {normalize_name(lead.locality or '')} {normalize_name(lead.address or '')} "
    for key, place in by_key.items():
        if key and f" {key} " in text:
            return place
    return ""


def _name(record: ExportRecord) -> str:
    return (record.lead.business_name or "").casefold()


def sort_records(records: list[ExportRecord], mode: str, places: list[str],
                 legacy_mode: str = "vendors",
                 category_key: str | None = None) -> tuple[list[ExportRecord], str]:
    """(records in output order, a label for the order used). Ties: name, then id.

    ``auto`` is the deliverable's default: score, highest first -- grouped by
    place when the run covers several places.
    """
    from ..db.store import sort_leads

    if mode == "auto":
        mode = "place" if len(places) > 1 else "score"
    if mode == "legacy":
        by_lead = {id(r.lead): r for r in records}
        ordered = [by_lead[id(lead)] for lead in sort_leads([r.lead for r in records],
                                                            legacy_mode)]
        return ordered, SORT_LABELS["legacy"]

    place_rank = {p: i for i, p in enumerate(places)}

    def place_key(r: ExportRecord) -> tuple[int, str]:
        return (place_rank.get(r.place, len(places)), r.place.casefold())

    def tail(r: ExportRecord) -> tuple:
        return (-r.score, _name(r), r.id)

    if mode == "place":
        def key(r: ExportRecord) -> tuple:
            return (*place_key(r), *tail(r))
    elif mode == "area":
        def key(r: ExportRecord) -> tuple:
            return (*place_key(r), (r.lead.locality or "").casefold(), *tail(r))
    elif mode == "category":
        def key(r: ExportRecord) -> tuple:
            value = r.values.get(category_key) if category_key else r.lead.category
            return (str(value or "").casefold() or "￿", *tail(r))
    else:
        mode = "score"
        key = tail
    return sorted(records, key=key), SORT_LABELS[mode]


SCORE_BANDS = ("80–100", "60–79", "40–59", "20–39", "0–19")


def score_band(score: float) -> str:
    """The Overview's score bands."""
    for floor, label in zip((80, 60, 40, 20), SCORE_BANDS):
        if score >= floor:
            return label
    return SCORE_BANDS[-1]
