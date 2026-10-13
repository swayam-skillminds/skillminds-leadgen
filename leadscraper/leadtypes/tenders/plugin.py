"""The ``tenders`` lead type: public buyers inviting bids for IT work.

Procurement notices from each country's official portals (country packs): TED
for the EU and EEA, Find a Tender and Contracts Finder for the UK, SAM.gov for the
US (a free API key), CPPP and GeM for India (opt-in). A notice that matches the
run's IT classification codes or words is a signal on its buyer -- a public body,
marked ``org_type: public_buyer`` -- with its title, value, closing date
(``expires_at``) and documents. It scores **intent**; a tender that has closed no
longer counts.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from ...core.scoring import ScoreParts
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section, secret
from .config import TendersConfig
from .session import SPEC_OF, SPECS, plan_units

log = logging.getLogger(__name__)

#: A tender older than this many half-lives (with no closing date) no longer counts.
STALE_HALF_LIVES = 4
_PORTAL_TITLES = {"ted": "TED", "find_a_tender": "Find a Tender",
                  "contracts_finder": "Contracts Finder", "sam_gov": "SAM.gov", "cppp": "CPPP",
                  "gem": "GeM"}


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


def is_open(signal: dict[str, Any], now: datetime) -> bool:
    expires = _aware(signal.get("expires_at"))
    return expires is None or expires > now


def weight(signal: dict[str, Any], now: datetime) -> float:
    """A tender's strength while it is open, halving every half-life since it was published;
    0 once it has closed (``expires_at``) or, undated, gone stale."""
    spec = SPEC_OF.get(signal.get("type") or "")
    if spec is None or not is_open(signal, now):
        return 0.0
    when = _when(signal)
    age = max(0.0, (now - when).total_seconds() / 86400) if when else 0.0
    half_life = float(spec.half_life_days or 60)
    if signal.get("expires_at") is None and age > STALE_HALF_LIVES * half_life:
        return 0.0
    return float(signal.get("strength") or spec.default_strength) * math.pow(0.5, age / half_life)


def tender_signals(signals: list[dict[str, Any]], now: datetime | None = None) -> list[dict]:
    """Open tenders, strongest (after ageing) first."""
    now = now or datetime.now(timezone.utc)
    rows = [s for s in signals if s.get("type") in SPEC_OF and weight(s, now) > 0]
    return sorted(rows, key=lambda s: (weight(s, now), _when(s) or now), reverse=True)


def _deadline(signal: dict[str, Any]) -> datetime | None:
    return _aware(signal.get("expires_at")) or _aware((signal.get("payload") or {})
                                                     .get("deadline"))


class TendersLeadType(LeadType):
    key = "tenders"
    title = "Public tenders"
    description = ("Public buyers inviting bids for IT work: open tenders and planned "
                   "procurements from TED (EU), Find a Tender and Contracts Finder (UK), "
                   "SAM.gov (US) and, opt-in, CPPP and GeM (India).")
    config_model = TendersConfig
    applicable_countries = None
    post_order = 0

    def _section(self, cfg: Any) -> TendersConfig:
        return lead_type_section(cfg, self.key, TendersConfig)

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        return {"discovery": [u.source for u in plan_units(cfg, self._section(cfg))],
                "enrichment": []}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return plan_units(cfg, self._section(cfg))

    @staticmethod
    def depth_changes(section: TendersConfig, depth: str) -> tuple[dict[str, Any], list[str]]:
        """(field updates, notes): quick reads half as much, deep twice as much."""
        factor = {"quick": 0.5, "deep": 2.0}.get(depth)
        if factor is None:
            return {}, []
        s = section.sources

        def scale(n: int, top: int) -> int:
            return max(1, min(top, int(round(n * factor))))

        sources = s.model_copy(update={
            "ted": s.ted.model_copy(update={"max_results": scale(s.ted.max_results, 2500)}),
            "find_a_tender": s.find_a_tender.model_copy(
                update={"max_pages": scale(s.find_a_tender.max_pages, 200)}),
            "cppp": s.cppp.model_copy(update={"max_pages": scale(s.cppp.max_pages, 100)}),
            "gem": s.gem.model_copy(update={"max_pages": scale(s.gem.max_pages, 20)}),
        })
        return {"sources": sources}, [f"tenders: {depth} reads {factor:g}× the notices"]

    def effective(self, cfg: Any) -> TendersConfig:
        section = self._section(cfg)
        depth = str(cfg.get("depth") or "balanced") if hasattr(cfg, "get") else "balanced"
        update, _ = self.depth_changes(section, depth)
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return list(SPECS)

    def catalog_sources(self) -> list[dict[str, Any]]:
        from ...packs import all_tender_sources

        defaults = TendersConfig().sources
        out = []
        for cls in all_tender_sources():
            settings = getattr(defaults, cls.name)
            entry: dict[str, Any] = {
                "name": cls.name, "kind": "discovery", "scope": "run",
                "pack": getattr(cls, "pack", None),
                "applicable_countries": cls.applicable_countries,
                "defaults": settings.model_dump()}
            if getattr(cls, "needs_key", False):
                name = settings.key_secret_name
                entry["key"] = {"secret_name": name, "configured": secret(name) is not None,
                                "register_url": cls.register_url}
            out.append(entry)
        return out

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("org_type", "Org Type", placement="front", width=14, breakdown=True,
                       description="Public buyer: a public body that publishes tenders."),
            ColumnSpec("tender", "Open Tender", width=50,
                       description="Its strongest open tender (or planned procurement)."),
            ColumnSpec("closes", "Tender Closes", "date", width=13, number_format="dd-mmm-yy",
                       description="When bids for that tender close."),
            ColumnSpec("value", "Tender Value", width=18,
                       description="As the notice states it, in its own currency; blank when "
                                   "it states none."),
            ColumnSpec("tender_link", "Tender Link", "url", width=12, link_label="Notice",
                       description="The notice on the portal."),
            ColumnSpec("documents", "Tender Documents", "url", width=12,
                       link_label="Documents", description="Where its tender documents are."),
            ColumnSpec("open_tenders", "Open Tenders", "number", placement="tail", width=10,
                       number_format="0", description="Its tenders still open."),
            ColumnSpec("tenders", "Tenders", placement="tail", width=50,
                       description="Every open tender: title and closing date."),
            ColumnSpec("portals", "Tender Portals", placement="tail", width=22,
                       description="Where its tenders were published."),
            ColumnSpec("codes", "Tender Codes", placement="tail", width=24,
                       description="The classification codes (CPV, NAICS) or words that "
                                   "matched."),
            ColumnSpec("contact", "Tender Contact", placement="tail", width=24,
                       description="The contact point the notice names."),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        now = getattr(org, "as_of", None) or datetime.now(timezone.utc)
        attributes = getattr(org, "attributes", None) or {}
        public = (attributes.get("org_type") or {}).get("value") == "public_buyer"
        values: dict[str, Any] = {"org_type": "Public buyer" if public else None}
        rows = tender_signals([s for s in getattr(org, "signals", []) or []
                               if s.get("lead_type") == self.key], now)
        if not rows:
            return values if public else {}
        top = rows[0]
        payload = top.get("payload") or {}
        closes = _deadline(top)
        listed = []
        for signal in rows[:10]:
            when = _deadline(signal)
            listed.append(f"{signal.get('title') or ''}"
                          + (f" (closes {when:%Y-%m-%d})" if when else " (planned)"))
        matched = list(dict.fromkeys(c for s in rows for c in
                                     ((s.get("payload") or {}).get("matched_codes") or [])
                                     + ((s.get("payload") or {}).get("matched_keywords") or [])))
        return {**values,
                "tender": top.get("title"),
                "closes": closes.replace(tzinfo=None) if closes else None,
                "value": (payload.get("value") or {}).get("text") or None,
                "tender_link": top.get("source_url"),
                "documents": payload.get("documents_url"),
                "open_tenders": sum(1 for s in rows if s.get("type") == "tender"),
                "tenders": "; ".join(listed) + (f" +{len(rows) - 10}" if len(rows) > 10 else ""),
                "portals": ", ".join(dict.fromkeys(
                    _PORTAL_TITLES.get(str((s.get("payload") or {}).get("portal")),
                                       str((s.get("payload") or {}).get("portal") or ""))
                    for s in rows)) or None,
                "codes": ", ".join(matched[:12]) or None,
                "contact": next(((s.get("payload") or {}).get("contact") for s in rows
                                 if (s.get("payload") or {}).get("contact")), None)}

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Intent: its strongest open tender, fading by age, a little more for each further
        open tender; a closed tender counts nothing."""
        now = now or datetime.now(timezone.utc)
        rows = tender_signals(signals, now)
        if not rows:
            return ScoreParts(self.key, intent=0.0)
        intent = round(min(1.0, weight(rows[0], now) + 0.05 * (len(rows) - 1)), 4)
        reasons = []
        for signal in rows[:3]:
            payload = signal.get("payload") or {}
            closes = _deadline(signal)
            facts = [f"closes {closes:%Y-%m-%d}" if closes else "",
                     (payload.get("value") or {}).get("text") or "",
                     _PORTAL_TITLES.get(str(payload.get("portal")), "")]
            label = SPEC_OF[signal["type"]].title.lower()
            reasons.append(f"{label}: {signal.get('title') or ''}"
                           + (f" ({', '.join(f for f in facts if f)})" if any(facts) else ""))
        return ScoreParts(self.key, intent=intent, reasons=reasons)

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import TendersSession

        return TendersSession(context, self.effective(context.config))

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


TENDERS = TendersLeadType()
