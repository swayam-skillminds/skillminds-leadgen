"""The ``funding_news`` lead type: companies in the news for a reason to buy now.

Funding rounds, launches, expansion and new offices, acquisitions, digital
transformation and leadership changes, read from news headlines: GDELT's open
news index (on by default), Google News RSS (opt-in: its feed terms forbid
other than personal use), GlobeNewswire's press releases per country (opt-in)
and any RSS / Atom feeds a config lists. Each event is a signal with its
published date and, where the story states it, the amount; it scores **intent**.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ...core.scoring import ScoreParts
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section
from .config import FundingNewsConfig
from .session import SPEC_OF, SPECS, plan_units

log = logging.getLogger(__name__)

#: A news signal older than this many half-lives no longer counts.
STALE_HALF_LIVES = 4


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _when(signal: dict[str, Any]) -> datetime | None:
    return _aware(signal.get("published_at")) or _aware(signal.get("observed_at"))


def weight(signal: dict[str, Any], now: datetime) -> float:
    """The signal's strength, halving every half-life of its type since it was published."""
    spec = SPEC_OF.get(signal.get("type") or "")
    if spec is None:
        return 0.0
    when = _when(signal)
    age = max(0.0, (now - when).total_seconds() / 86400) if when else 0.0
    half_life = float(spec.half_life_days or 60)
    if age > STALE_HALF_LIVES * half_life:
        return 0.0
    return float(signal.get("strength") or spec.default_strength) * math.pow(0.5, age / half_life)


def news_signals(signals: list[dict[str, Any]], now: datetime | None = None) -> list[dict]:
    """News signals still worth something, strongest (after ageing) first."""
    now = now or datetime.now(timezone.utc)
    rows = [s for s in signals if s.get("type") in SPEC_OF and weight(s, now) > 0]
    return sorted(rows, key=lambda s: (weight(s, now), _when(s) or now), reverse=True)


def _label(signal: dict[str, Any]) -> str:
    spec = SPEC_OF.get(signal.get("type") or "")
    return spec.title if spec else str(signal.get("type") or "")


class FundingNewsLeadType(LeadType):
    key = "funding_news"
    title = "Funding & news"
    description = ("Companies in the news with a reason to buy now: funding rounds, launches, "
                   "expansion and new offices, acquisitions, digital transformation and "
                   "leadership changes, from GDELT's open news index, press releases and "
                   "your own feeds.")
    config_model = FundingNewsConfig
    applicable_countries = None

    def _section(self, cfg: Any) -> FundingNewsConfig:
        return lead_type_section(cfg, self.key, FundingNewsConfig)

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        return {"discovery": sorted({u.source for u in plan_units(cfg, self._section(cfg))}),
                "enrichment": []}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return plan_units(cfg, self._section(cfg))

    @staticmethod
    def depth_changes(section: FundingNewsConfig, depth: str) -> tuple[dict[str, Any], list[str]]:
        """(field updates, notes) a depth preset makes; "balanced" changes nothing."""
        gd = section.sources.gdelt
        target = {"quick": 50, "deep": 250}.get(depth)
        if target is None or (depth == "quick" and gd.max_records <= target) or \
                (depth == "deep" and gd.max_records >= target):
            return {}, []
        sources = section.sources.model_copy(
            update={"gdelt": gd.model_copy(update={"max_records": target})})
        return {"sources": sources}, [f"funding_news: {target} GDELT articles per query"]

    def effective(self, cfg: Any) -> FundingNewsConfig:
        section = self._section(cfg)
        depth = str(cfg.get("depth") or "balanced") if hasattr(cfg, "get") else "balanced"
        update, _ = self.depth_changes(section, depth)
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return list(SPECS)

    def catalog_sources(self) -> list[dict[str, Any]]:
        defaults = FundingNewsConfig().sources
        return [{"name": "gdelt", "kind": "discovery", "scope": "locality",
                 "defaults": defaults.gdelt.model_dump()},
                {"name": "google_news", "kind": "discovery", "scope": "locality",
                 "defaults": defaults.google_news.model_dump()},
                {"name": "press_releases", "kind": "discovery", "scope": "run",
                 "defaults": defaults.press_releases.model_dump()},
                {"name": "feeds", "kind": "discovery", "scope": "run",
                 "defaults": defaults.feeds.model_dump()}]

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("event", "News Event", placement="front", width=20, breakdown=True,
                       description="The strongest recent news: funding round, launch, "
                                   "expansion, acquisition, digital transformation or "
                                   "leadership change."),
            ColumnSpec("headline", "Latest News", width=50,
                       description="That story's headline (or what it says: the amount "
                                   "raised, who was appointed)."),
            ColumnSpec("event_date", "News Date", "date", width=12, number_format="dd-mmm-yy",
                       description="When the story was published."),
            ColumnSpec("amount", "Amount Raised", width=16,
                       description="As the story states it (\"Rs 22 crore\", \"$10M\"); "
                                   "blank when it names none."),
            ColumnSpec("news_link", "News Link", "url", width=12, link_label="Story",
                       description="The story itself."),
            ColumnSpec("events", "News Events", placement="tail", width=30,
                       description="Every recent event found for it."),
            ColumnSpec("outlets", "News Sources", placement="tail", width=30,
                       description="Outlets that covered it (from GDELT, feeds or press "
                                   "releases)."),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        now = getattr(org, "as_of", None) or datetime.now(timezone.utc)
        rows = news_signals([s for s in getattr(org, "signals", []) or []
                             if s.get("lead_type") == self.key], now)
        if not rows:
            return {}
        top = rows[0]
        when = _when(top)
        amount = next(((s.get("payload") or {}).get("amount") or {} for s in rows
                       if s.get("type") == "funding_round"
                       and (s.get("payload") or {}).get("amount")), {})
        counts = Counter(_label(s) for s in rows)
        outlets = list(dict.fromkeys(c.get("publisher") for s in rows
                                     for c in (s.get("payload") or {}).get("coverage") or []
                                     if c.get("publisher")))
        more = f" +{len(outlets) - 8}" if len(outlets) > 8 else ""
        return {"event": _label(top),
                "headline": top.get("title") or (top.get("payload") or {}).get("headline"),
                "event_date": when.replace(tzinfo=None) if when else None,
                "amount": amount.get("text"), "news_link": top.get("source_url"),
                "events": ", ".join(f"{k} ×{n}" if n > 1 else k
                                    for k, n in counts.most_common()),
                "outlets": (", ".join(outlets[:8]) + more) or None}

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Intent: the strongest recent event, fading by its type's half-life, plus a
        little for each further kind of event."""
        now = now or datetime.now(timezone.utc)
        rows = news_signals(signals, now)
        if not rows:
            return ScoreParts(self.key, intent=0.0)
        kinds = {s.get("type") for s in rows}
        intent = round(min(1.0, weight(rows[0], now) + 0.1 * (len(kinds) - 1)), 4)
        reasons = []
        for signal in rows[:3]:
            when = _when(signal)
            outlet = (signal.get("payload") or {}).get("publisher")
            facts = ", ".join(x for x in (f"{when:%Y-%m-%d}" if when else "", outlet or "") if x)
            reasons.append(f"{_label(signal).lower()}: {signal.get('title') or ''}"
                           + (f" ({facts})" if facts else ""))
        return ScoreParts(self.key, intent=intent, reasons=reasons)

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import FundingNewsSession

        return FundingNewsSession(context, self.effective(context.config))

    def run(self, context: Any) -> LeadTypeResult:
        """Without the engine: every unit in one go. Never raises."""
        try:
            session = self.open(context)
        except Exception as exc:
            return LeadTypeResult(lead_type=self.key, failed=True, error=repr(exc))
        try:
            for unit in session.units():
                session.run_unit(unit)
            return session.result()
        except Exception as exc:
            log.exception("lead type %s failed", self.key)
            return LeadTypeResult(lead_type=self.key, failed=True, error=repr(exc))
        finally:
            session.close()


FUNDING_NEWS = FundingNewsLeadType()
