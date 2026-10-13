"""registrations as an engine session: newly incorporated companies, register by register.

Units (stage 1: after the finders, so a company they already hold gets the signal
and keeps its record): one per register that covers the run's country and is
enabled, per place when the register can narrow by place (Companies House's
``location`` filter) and ``scope`` is ``place``. A register whose key isn't
configured is planned but skipped (``unit_skip_reason``): the run reports "key not
configured" and never asks it without the key.

Each company kept (incorporated within the window, its industry wanted, not dormant)
is an ``incorporation`` signal on its organisation: one the project holds keeps its
record (rule 6), one nobody holds becomes a new organisation (its registered name,
office and register number). With ``directors`` on, its current directors become
people of this lead type (one request per company, at most ``max_director_lookups``
a run); a register read in full is a snapshot, so a director who resigned goes.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any

from ...core.lead import Lead
from ...engine.costs import UnitCost, prior_cost
from ...net.ratelimit import BlockedError, PermanentError
from ...packs import incorporation_sources_for
from ...sources.registers.base import Incorporation, IncorporationQuery
from ...sources.registers.sic import describe, division
from ..base import LeadTypeResult, LeadTypeSession, SignalSpec, UnitOutcome, WorkUnit
from ..toolkit import KEY_NOT_CONFIGURED, Kit, key_not_configured, secret
from .config import RegistrationsConfig

log = logging.getLogger(__name__)

KEY = "registrations"
SPECS = [
    SignalSpec("incorporation", "Newly incorporated", 0.7, 90.0,
               "A company incorporated recently: it needs a website, software, accounts and "
               "systems from scratch."),
]
SPEC_OF = {s.type: s for s in SPECS}
#: People this lead type records are the register's officers.
PEOPLE_SOURCE = "registry"


def registers(config: Any) -> list[type]:
    """The registers that cover the run's country (its packs', then generic ones)."""
    return incorporation_sources_for(getattr(config, "country", "") or "")


def _settings(section: RegistrationsConfig, name: str) -> Any:
    return getattr(section.sources, name, None)


def _places(config: Any) -> list[str]:
    return [p for p in dict.fromkeys(str(p).strip() for p in (config.get("places") or [])) if p]


def plan_units(config: Any, section: RegistrationsConfig) -> list[WorkUnit]:
    units: list[WorkUnit] = []
    places = _places(config) if section.scope == "place" else []
    for cls in registers(config):
        settings = _settings(section, cls.name)
        if settings is None or not settings.enabled:
            continue
        if getattr(cls, "by_place", False) and places:
            units.extend(WorkUnit(KEY, cls.name, place, None, "locality") for place in places)
        else:
            units.append(WorkUnit(KEY, cls.name, None, None, "run"))
    return units


def _secret_name(settings: Any, cls: type) -> str:
    return getattr(settings, "key_secret_name", None) or \
        getattr(settings, "token_secret_name", None) or cls.default_secret


class RegistrationsSession(LeadTypeSession):
    lead_type = KEY
    #: After the finders: a company they already hold gets the signal, untouched.
    discovery_stage = 1

    def __init__(self, context: Any, section: RegistrationsConfig, *, kit: Kit | None = None,
                 clock=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dry_run = bool(getattr(context, "dry_run", False))
        self._lock = threading.Lock()
        self.units_done = 0
        self.stats: dict[str, int] = {"companies": 0, "kept": 0, "signals": 0, "new": 0,
                                      "merged": 0, "attached": 0, "directors": 0,
                                      "director_lookups": 0, "removed_directors": 0,
                                      "skipped_industry": 0, "skipped_dormant": 0,
                                      "skipped_type": 0, "skipped_place": 0,
                                      "skipped_old": 0, "skipped_unknown": 0}
        self.registers: dict[str, Any] = {}
        self.keys: dict[str, str | None] = {}
        self.missing: dict[str, str] = {}
        self._reported: set[str] = set()
        fields = getattr(self.config, "included_fields", None)
        #: A run whose fields.include leaves decision_makers out wants no directors (A.8).
        self.want_directors = section.directors and (
            fields is None or "decision_makers" in set(fields))
        try:
            self.country_name = self.config.country_info.name
        except Exception:
            self.country_name = ""
        limiter = self.kit.limiter
        for cls in registers(self.config):
            settings = _settings(section, cls.name)
            if settings is None or not settings.enabled:
                continue
            self.registers[cls.name] = cls()
            limiter.configure(cls.host, settings.min_delay, settings.max_delay)
            if getattr(cls, "needs_key", False):
                name = _secret_name(settings, cls)
                self.keys[cls.name] = secret(name)
                if not self.keys[cls.name]:
                    self.missing[cls.name] = key_not_configured(
                        cls.title, name, getattr(cls, "register_url", ""))
        self._units = plan_units(self.config, section)

    # --- planning ---------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def unit_skip_reason(self, unit: WorkUnit) -> str | None:
        """A register whose key isn't configured is skipped, and the run says so (once)."""
        reason = self.missing.get(unit.source)
        if reason and not self.dry_run and unit.source not in self._reported:
            self._reported.add(unit.source)
            self.kit.failures.record(unit.source, KEY_NOT_CONFIGURED, detail=reason)
        return reason

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        register = self.registers.get(unit.source)
        return frozenset({register.host}) if register else super().hosts(unit)

    def prior(self, unit: WorkUnit) -> UnitCost:
        settings = _settings(self.section, unit.source)
        delay = (settings.min_delay + settings.max_delay) / 2 if settings else 1.0
        lookups = 0
        register = self.registers.get(unit.source)
        if self.want_directors and getattr(register, "has_officers", False):
            per_unit = max(1, len(self._units))
            lookups = min(self.section.max_director_lookups // per_unit, 40)
        return prior_cost((2 + lookups) * (delay + 0.5), 10.0)

    # --- discovery ----------------------------------------------------------------------

    def query(self, source: str) -> IncorporationQuery:
        today = self.clock().date()
        settings = _settings(self.section, source)
        return IncorporationQuery(
            country=(self.config.country or "").upper(), country_name=self.country_name,
            incorporated_from=today - timedelta(days=self.section.incorporated_within_days),
            incorporated_to=today,
            industry_codes=list(self.section.industry_codes),
            company_types=list(self.section.company_types),
            max_results=getattr(settings, "max_results", 500),
            max_pages=getattr(settings, "max_pages", 1) or 1)

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        register = self.registers.get(unit.source)
        try:
            if register is None:
                outcome = UnitOutcome(status="failed", error=f"unknown register {unit.source}")
            elif unit.source in self.missing:          # without the engine (run())
                self.unit_skip_reason(unit)
                outcome = UnitOutcome(status="done", error=self.missing[unit.source])
            else:
                outcome = self.read(register, unit.area)
        except Exception as exc:  # contained: a register's bug costs its unit
            log.exception("registrations unit %s failed", unit.key)
            self.kit.failures.record(unit.source, "unit_failed", locality=unit.area,
                                     detail=repr(exc)[:300])
            outcome = UnitOutcome(status="failed", error=repr(exc)[:300])
        with self._lock:
            self.units_done += 1
        return outcome

    def read(self, register: Any, place: str | None) -> UnitOutcome:
        name = register.name
        try:
            companies = register.search(self.kit.http, self.query(name),
                                        key=self.keys.get(name), place=place)
        except PermanentError as exc:
            self.kit.failures.record(name, "no_results", locality=place, detail=str(exc)[:300])
            return UnitOutcome(status="done")
        except BlockedError as exc:
            self.kit.failures.record(name, "blocked", locality=place, detail=str(exc)[:300])
            return UnitOutcome(status="failed", error=str(exc)[:300])
        except Exception as exc:
            text = str(exc)
            rejected = isinstance(exc, PermissionError) or "401" in text
            reason = "key_rejected" if rejected else "fetch_failed"
            self.kit.failures.record(name, reason, locality=place, detail=text[:300])
            return UnitOutcome(status="failed", error=f"{reason}: {text[:250]}")
        outcome = UnitOutcome(status="done", found=len(companies))
        with self._lock:
            self.stats["companies"] += len(companies)
        for company in companies:
            why = self.unwanted(company, place)
            if why:
                with self._lock:
                    self.stats[why] += 1
                continue
            part = self.record(register, company, place)
            outcome.new += part.new
            outcome.merged += part.merged
        return outcome

    # --- which companies count --------------------------------------------------------------

    def unwanted(self, company: Incorporation, place: str | None) -> str:
        """"" to keep it, else the stat that says why not."""
        section = self.section
        oldest = self.clock().date() - timedelta(days=section.incorporated_within_days)
        if company.incorporated_on is None or company.incorporated_on < oldest:
            return "skipped_old"
        codes = company.industry_codes
        if codes and section.exclude_codes and all(
                any(c.startswith(x) for x in section.exclude_codes) for c in codes):
            return "skipped_dormant"
        if section.industry_codes and not any(
                c.startswith(p) for c in codes for p in section.industry_codes):
            return "skipped_industry"
        if section.company_types and company.company_type.lower() not in section.company_types:
            return "skipped_type"
        if place and place.casefold() not in f"{company.address} {company.locality} " \
                                             f"{company.region}".casefold():
            return "skipped_place"
        return ""

    # --- writing ------------------------------------------------------------------------

    def record(self, register: Any, company: Incorporation, place: str | None) -> UnitOutcome:
        outcome = UnitOutcome(status="done")
        if self.store is None:
            return outcome
        lead = Lead(business_name=company.name, address=company.address,
                    locality=place or company.locality,
                    registry_ids={company.scheme: company.number},
                    sources=[company.source], source_urls=[company.url])
        when = datetime.combine(company.incorporated_on, time(0), tzinfo=timezone.utc) \
            if company.incorporated_on else None
        # Find-or-create and the signal under the store's lock: another unit (another
        # place, another lead type) can't slip in between.
        with self.store._lock:
            try:
                org_id = self._resolve(lead, outcome)
            except Exception as exc:
                self.kit.failures.record(company.source, "company_rejected",
                                         business=company.name, url=company.url,
                                         detail=repr(exc)[:200])
                return outcome
            if org_id is None:
                return outcome
            payload = {
                "register": company.source, "number": company.number,
                "scheme": company.scheme,
                "incorporated_on": company.incorporated_on.isoformat()
                if company.incorporated_on else None,
                "company_type": company.company_type, "status": company.status,
                "industry_codes": company.industry_codes[:8],
                "industries": [describe(c) for c in company.industry_codes[:8]],
                "code_scheme": company.code_scheme,
                "registered_office": company.address, "postcode": company.postcode,
                "url": company.url,
            }
            # Shown after its label: "Newly incorporated: 26 Sep 2026 (computer programming…)".
            field = division(company.industry_codes[0]) if company.industry_codes else ""
            title = " ".join(x for x in (
                f"{company.incorporated_on:%d %b %Y}" if company.incorporated_on else "",
                f"({field})" if field else "") if x) or company.name
            added = self.store.add_signal(
                org_id, "incorporation", key=company.key[:300], title=title, payload=payload,
                strength=SPEC_OF["incorporation"].default_strength, published_at=when,
                source_url=company.url, source=company.source)
        with self._lock:
            self.stats["kept"] += 1
            self.stats["signals"] += bool(added)
            self.stats["new"] += outcome.new
            self.stats["merged"] += outcome.merged
        self.directors(register, company, org_id)
        outcome.found = 1
        return outcome

    def directors(self, register: Any, company: Incorporation, org_id: int) -> None:
        """Its current directors as people (a snapshot: a resigned one goes)."""
        if not (self.want_directors and getattr(register, "has_officers", False)):
            return
        with self._lock:
            if self.stats["director_lookups"] >= self.section.max_director_lookups:
                return
            self.stats["director_lookups"] += 1
        try:
            officers = register.officers(self.kit.http, company.number,
                                         key=self.keys.get(register.name))
        except Exception as exc:                  # people are extra: the company stands
            self.kit.failures.record(register.name, "fetch_failed", business=company.name,
                                     url=company.url, detail=f"officers: {str(exc)[:200]}")
            return
        page = f"{company.url.rstrip('/')}/officers"
        for officer in officers:
            self.store.add_person(org_id, officer.name, title=officer.role,
                                  source=PEOPLE_SOURCE, source_url=page)
        removed = self.store.prune_people(org_id, PEOPLE_SOURCE, [o.name for o in officers])
        with self._lock:
            self.stats["directors"] += len(officers)
            self.stats["removed_directors"] += removed

    def _resolve(self, lead: Lead, outcome: UnitOutcome) -> int | None:
        """The company's organisation: one the project holds keeps its record (A.3); one
        nobody holds becomes a new organisation when ``create_new``."""
        match = self.store.find_match(lead.normalized())
        if match is not None:
            with self._lock:
                self.stats["attached"] += 1
            return match
        if not self.section.create_new:
            with self._lock:
                self.stats["skipped_unknown"] += 1
            return None
        org_id, action, _ = self.store.upsert(lead)
        outcome.new += action == "new"
        outcome.merged += action == "merged"
        return org_id

    # --- the end ------------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"],
                              stats={**self.stats, "keys_missing": sorted(self.missing)})
