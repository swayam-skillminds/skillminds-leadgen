"""The ``registrations`` lead type: newly incorporated companies.

Company registers' new incorporations: the UK's Companies House (a free API key;
pack ``uk``) and, opt-in, OpenCorporates for any jurisdiction (its free tokens are
only for public-benefit users). A company incorporated within the window -- in a
wanted industry, not dormant, its registered office in one of the run's places --
is an ``incorporation`` signal with its date and industry codes; its current
directors become people. It scores **intent**: a new company needs a website,
software and systems from scratch, most of all in its first months.

India: the MCA has no free API, and the official company master data on data.gov.in
stops at 3 November 2023 (checked 2026-10-13), so no Indian register is built.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from ...core.scoring import ScoreParts
from ...sources.registers.sic import describe, division
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section, secret
from .config import RegistrationsConfig
from .session import SPEC_OF, SPECS, plan_units

log = logging.getLogger(__name__)

#: An incorporation older than this many half-lives no longer counts (about a year).
STALE_HALF_LIVES = 4
_REGISTER_TITLES = {"companies_house": "Companies House", "opencorporates": "OpenCorporates"}
#: Companies House company types, as people say them.
COMPANY_TYPES = {
    "ltd": "Private limited", "plc": "Public limited", "llp": "LLP",
    "private-unlimited": "Private unlimited",
    "private-limited-guarant-nsc": "Private limited by guarantee",
    "private-limited-guarant-nsc-limited-exemption": "Private limited by guarantee",
    "private-limited-shares-section-30-exemption": "Private limited",
    "limited-partnership": "Limited partnership",
    "scottish-partnership": "Scottish partnership",
    "registered-society-non-jurisdictional": "Registered society",
    "charitable-incorporated-organisation": "CIO",
    "scottish-charitable-incorporated-organisation": "SCIO",
}


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
    """The signal's strength, halving every half-life since incorporation; stale after four."""
    spec = SPEC_OF.get(signal.get("type") or "")
    if spec is None:
        return 0.0
    when = _when(signal)
    age = max(0.0, (now - when).total_seconds() / 86400) if when else 0.0
    half_life = float(spec.half_life_days or 90)
    if age > STALE_HALF_LIVES * half_life:
        return 0.0
    return float(signal.get("strength") or spec.default_strength) * math.pow(0.5, age / half_life)


def incorporation_signals(signals: list[dict[str, Any]],
                          now: datetime | None = None) -> list[dict]:
    """Incorporations still worth something, strongest first."""
    now = now or datetime.now(timezone.utc)
    rows = [s for s in signals if s.get("type") in SPEC_OF and weight(s, now) > 0]
    return sorted(rows, key=lambda s: (weight(s, now), _when(s) or now), reverse=True)


def company_type(code: str) -> str:
    return COMPANY_TYPES.get((code or "").lower(), (code or "").replace("-", " ").capitalize())


class RegistrationsLeadType(LeadType):
    key = "registrations"
    title = "New registrations"
    description = ("Newly incorporated companies: the UK's Companies House (free key) and, "
                   "opt-in, OpenCorporates; their current directors become people.")
    config_model = RegistrationsConfig
    applicable_countries = None
    post_order = 0

    def _section(self, cfg: Any) -> RegistrationsConfig:
        return lead_type_section(cfg, self.key, RegistrationsConfig)

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        return {"discovery": sorted({u.source for u in plan_units(cfg, self._section(cfg))}),
                "enrichment": []}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return plan_units(cfg, self._section(cfg))

    @staticmethod
    def depth_changes(section: RegistrationsConfig,
                      depth: str) -> tuple[dict[str, Any], list[str]]:
        """quick: no director lookups; deep: up to 500."""
        if depth == "quick" and section.directors:
            return {"directors": False}, ["registrations: no director lookups (quick)"]
        if depth == "deep" and section.max_director_lookups < 500:
            return {"max_director_lookups": 500}, ["registrations: up to 500 director lookups"]
        return {}, []

    def effective(self, cfg: Any) -> RegistrationsConfig:
        section = self._section(cfg)
        depth = str(cfg.get("depth") or "balanced") if hasattr(cfg, "get") else "balanced"
        update, _ = self.depth_changes(section, depth)
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return list(SPECS)

    def catalog_sources(self) -> list[dict[str, Any]]:
        from ...packs import all_incorporation_sources

        defaults = RegistrationsConfig().sources
        out = []
        for cls in all_incorporation_sources():
            settings = getattr(defaults, cls.name)
            entry: dict[str, Any] = {
                "name": cls.name, "kind": "discovery",
                "scope": "locality" if getattr(cls, "by_place", False) else "run",
                "pack": getattr(cls, "pack", None),
                "applicable_countries": cls.applicable_countries,
                "defaults": settings.model_dump()}
            if getattr(cls, "needs_key", False):
                name = getattr(settings, "key_secret_name", None) or \
                    getattr(settings, "token_secret_name", None)
                entry["key"] = {"secret_name": name, "configured": secret(name) is not None,
                                "register_url": cls.register_url}
            out.append(entry)
        return out

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("industry", "Industry", placement="front", width=24, breakdown=True,
                       description="Its main registered industry (UK SIC 2007 division)."),
            ColumnSpec("incorporated", "Incorporated", "date", width=13,
                       number_format="dd-mmm-yy",
                       description="When the company was incorporated."),
            ColumnSpec("company_number", "Company No.", width=12,
                       description="Its number at the register."),
            ColumnSpec("company_type", "Company Type", width=18),
            ColumnSpec("register_link", "Register Link", "url", width=12, link_label="Register",
                       description="The company's page at the register (officers, filings)."),
            ColumnSpec("industry_codes", "Industry Codes", placement="tail", width=34,
                       description="Every registered industry code, with its division."),
            ColumnSpec("register", "Register", placement="tail", width=16),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        now = getattr(org, "as_of", None) or datetime.now(timezone.utc)
        rows = [s for s in getattr(org, "signals", []) or []
                if s.get("lead_type") == self.key and s.get("type") in SPEC_OF]
        if not rows:
            return {}
        # The incorporation is a fact as well as a signal: shown even once it has faded.
        live = incorporation_signals(rows, now)
        top = live[0] if live else sorted(rows, key=lambda s: _when(s) or now)[-1]
        payload = top.get("payload") or {}
        codes = [str(c) for c in payload.get("industry_codes") or []]
        when = _when(top)
        return {"industry": (division(codes[0]).capitalize() if codes and division(codes[0])
                             else None),
                "incorporated": when.replace(tzinfo=None) if when else None,
                "company_number": payload.get("number"),
                "company_type": company_type(str(payload.get("company_type") or "")) or None,
                "register_link": top.get("source_url"),
                "industry_codes": "; ".join(describe(c) for c in codes) or None,
                "register": _REGISTER_TITLES.get(str(payload.get("register")),
                                                 str(payload.get("register") or "")) or None}

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Intent: how recently it was incorporated (strength halving every 90 days)."""
        now = now or datetime.now(timezone.utc)
        rows = incorporation_signals(signals, now)
        if not rows:
            return ScoreParts(self.key, intent=0.0)
        top = rows[0]
        payload = top.get("payload") or {}
        when = _when(top)
        codes = [describe(c) for c in (payload.get("industry_codes") or [])[:2]]
        facts = [f"{when:%Y-%m-%d}" if when else "", ", ".join(codes),
                 _REGISTER_TITLES.get(str(payload.get("register")), "")]
        reason = "newly incorporated" + (f" ({'; '.join(f for f in facts if f)})"
                                         if any(facts) else "")
        return ScoreParts(self.key, intent=round(min(1.0, weight(top, now)), 4),
                          reasons=[reason])

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import RegistrationsSession

        return RegistrationsSession(context, self.effective(context.config))

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


REGISTRATIONS = RegistrationsLeadType()
