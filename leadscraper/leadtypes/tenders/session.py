"""tenders as an engine session: one discovery unit per procurement portal.

Units (stage 1: after the finders, so a buyer they already hold gets the signal
and keeps its record), one per portal that applies to the run's country and is
enabled: TED (EU, EEA), Find a Tender and Contracts Finder (UK), SAM.gov (US), CPPP
and GeM (India, opt-in). A portal that needs an API key it doesn't have is planned
but skipped (``unit_skip_reason``): the run reports "key not configured" and never
asks the portal without it.

Every notice goes through ``wanted``: its classification codes (CPV / NAICS) or
words, its stage (planned notices only when ``planned``), its closing date (still
open after ``closing_after``), its value range, and with ``scope: place`` the
buyer's or the work's place. A kept notice is a signal on its buyer, a public body:
an organisation the project already holds keeps its record (rule 6); one nobody
holds becomes a new organisation with the contacts the notice publishes. Either way
the buyer is marked ``org_type: public_buyer`` (an attribute, never the record).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any

from ...core.lead import Lead
from ...core.normalize import extract_emails, normalize_phone, normalize_url
from ...engine.costs import UnitCost, prior_cost
from ...net.ratelimit import BlockedError, PermanentError
from ...packs import tender_sources_for
from ...sources.tenders.base import Tender, TenderQuery, code_matches, keyword_hits
from ..base import LeadTypeResult, LeadTypeSession, SignalSpec, UnitOutcome, WorkUnit
from ..toolkit import KEY_NOT_CONFIGURED, Kit, key_not_configured, secret
from .config import TendersConfig

log = logging.getLogger(__name__)

KEY = "tenders"
SPECS = [
    SignalSpec("tender", "Open tender", 0.75, 60.0,
               "A public buyer is inviting bids for IT work now; it drops out once it closes."),
    SignalSpec("tender_planned", "Planned tender", 0.5, 90.0,
               "A public buyer has announced a procurement to come (prior information, "
               "pre-solicitation, sources sought)."),
]
SPEC_OF = {s.type: s for s in SPECS}
#: Expected seconds per request, by portal (fetch and parse, besides the polite delay).
_WORK = {"ted": 2.0, "find_a_tender": 1.5, "contracts_finder": 3.0, "sam_gov": 3.0,
         "cppp": 2.0, "gem": 2.0}


def portals(config: Any) -> list[type]:
    """The procurement portals of the packs that apply to the run's country."""
    return tender_sources_for(getattr(config, "country", "") or "")


def plan_units(config: Any, section: TendersConfig) -> list[WorkUnit]:
    return [WorkUnit(KEY, cls.name, None, None, "run") for cls in portals(config)
            if getattr(section.sources, cls.name, None) is not None
            and getattr(section.sources, cls.name).enabled]


def strength(tender: Tender, by_code: bool) -> float:
    """An open tender found by its codes is the clearest signal; one found by words only
    a little less; a planned one is a procurement still to come."""
    if tender.stage == "planned":
        return SPEC_OF["tender_planned"].default_strength
    return SPEC_OF["tender"].default_strength if by_code else 0.65


class TendersSession(LeadTypeSession):
    lead_type = KEY
    #: After the finders: a buyer they already hold gets the signal, untouched.
    discovery_stage = 1

    def __init__(self, context: Any, section: TendersConfig, *, kit: Kit | None = None,
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
        self.stats: dict[str, int] = {"notices": 0, "kept": 0, "signals": 0, "new": 0,
                                      "merged": 0, "attached": 0, "skipped_unmatched": 0,
                                      "skipped_closed": 0, "skipped_value": 0,
                                      "skipped_place": 0, "skipped_planned": 0,
                                      "skipped_old": 0, "skipped_unknown": 0}
        self.portals: dict[str, Any] = {}
        self.keys: dict[str, str | None] = {}
        self.missing: dict[str, str] = {}
        self._reported: set[str] = set()
        self._buyers: dict[str, int] = {}
        try:
            self.country_name = self.config.country_info.name
        except Exception:
            self.country_name = ""
        self.region = getattr(self.config, "admin_area", "") or None
        self.places = self._places()
        limiter = self.kit.limiter
        for cls in portals(self.config):
            settings = getattr(section.sources, cls.name, None)
            if settings is None or not settings.enabled:
                continue
            self.portals[cls.name] = cls()
            limiter.configure(cls.host, settings.min_delay, settings.max_delay)
            if getattr(cls, "needs_key", False):
                name = getattr(settings, "key_secret_name", None) or cls.default_secret
                self.keys[cls.name] = secret(name)
                if not self.keys[cls.name]:
                    self.missing[cls.name] = key_not_configured(
                        cls.title, name, getattr(cls, "register_url", ""))
        self._units = plan_units(self.config, section)

    def _places(self) -> list[str]:
        names: list[str] = list(self.config.get("places") or [])
        for locality in getattr(self.config, "localities", None) or []:
            names.append(locality.name)
            names.extend(getattr(locality, "aliases", None) or [])
        return [n for n in dict.fromkeys(n.strip() for n in names) if n]

    # --- planning ---------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def unit_skip_reason(self, unit: WorkUnit) -> str | None:
        """A portal whose key isn't configured is skipped, and the run says so."""
        reason = self.missing.get(unit.source)
        if reason and not self.dry_run and unit.source not in self._reported:
            self._reported.add(unit.source)
            self.kit.failures.record(unit.source, KEY_NOT_CONFIGURED, detail=reason)
        return reason

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        portal = self.portals.get(unit.source)
        return frozenset({portal.host}) if portal else super().hosts(unit)

    def requests(self, source: str) -> int:
        """How many requests a portal's unit makes at most."""
        sources = self.section.sources
        if source == "ted":
            return max(1, -(-sources.ted.max_results // 250))
        if source == "find_a_tender":
            return sources.find_a_tender.max_pages
        if source == "sam_gov":
            return sources.sam_gov.max_requests
        if source == "cppp":
            return sources.cppp.max_pages
        if source == "gem":
            return 1 + sources.gem.max_pages * min(len(self.section.keywords),
                                                   sources.gem.max_keywords)
        return 1

    def prior(self, unit: WorkUnit) -> UnitCost:
        settings = getattr(self.section.sources, unit.source, None)
        delay = (settings.min_delay + settings.max_delay) / 2 if settings else 2.0
        seconds = self.requests(unit.source) * (delay + _WORK.get(unit.source, 2.0))
        return prior_cost(seconds, 5.0)

    # --- discovery ----------------------------------------------------------------------

    def _closing(self) -> datetime:
        if self.section.closing_after is not None:
            return datetime.combine(self.section.closing_after, time(0), tzinfo=timezone.utc)
        return self.clock()

    def query(self, source: str) -> TenderQuery:
        now = self.clock()
        sources = self.section.sources
        max_results = {"ted": sources.ted.max_results,
                       "contracts_finder": sources.contracts_finder.max_results,
                       "sam_gov": sources.sam_gov.max_results}.get(source, 1000)
        max_pages = {"ted": max(1, -(-sources.ted.max_results // 250)),
                     "find_a_tender": sources.find_a_tender.max_pages,
                     "sam_gov": sources.sam_gov.max_requests,
                     "cppp": sources.cppp.max_pages,
                     "gem": sources.gem.max_pages}.get(source, 1)
        keywords = list(self.section.keywords)
        if source == "gem":
            keywords = keywords[:sources.gem.max_keywords]
        query = TenderQuery(
            country=(self.config.country or "").upper(), country_name=self.country_name,
            places=list(self.places),
            region=self.region if self.section.scope == "place" else None,
            cpv=list(self.section.cpv), naics=list(self.section.naics), keywords=keywords,
            posted_from=now - timedelta(days=self.section.posted_within_days),
            closing_after=self._closing(), max_results=max_results, max_pages=max_pages)
        query.match = self.section.match            # type: ignore[attr-defined]
        return query

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        portal = self.portals.get(unit.source)
        try:
            if portal is None:
                outcome = UnitOutcome(status="failed", error=f"unknown portal {unit.source}")
            elif unit.source in self.missing:          # without the engine (run())
                self.unit_skip_reason(unit)
                outcome = UnitOutcome(status="done", error=self.missing[unit.source])
            else:
                outcome = self.read(portal)
        except Exception as exc:  # contained: a portal's bug costs its unit
            log.exception("tenders unit %s failed", unit.key)
            self.kit.failures.record(unit.source, "unit_failed", detail=repr(exc)[:300])
            outcome = UnitOutcome(status="failed", error=repr(exc)[:300])
        with self._lock:
            self.units_done += 1
        return outcome

    def read(self, portal: Any) -> UnitOutcome:
        name = portal.name
        try:
            tenders = portal.search(self.kit.http, self.query(name), key=self.keys.get(name))
        except PermanentError as exc:
            # SAM.gov answers 404 for "no data" (and, live, for a key it doesn't take).
            self.kit.failures.record(name, "no_results", detail=str(exc)[:300])
            return UnitOutcome(status="done")
        except BlockedError as exc:
            self.kit.failures.record(name, "blocked", detail=str(exc)[:300])
            return UnitOutcome(status="failed", error=str(exc)[:300])
        except Exception as exc:
            rejected = type(exc).__name__ in ("SamKeyRejected", "PermissionError")
            reason = "key_rejected" if rejected else "fetch_failed"
            self.kit.failures.record(name, reason, detail=str(exc)[:300])
            return UnitOutcome(status="failed", error=f"{reason}: {str(exc)[:250]}")
        return self.process(tenders)

    # --- which notices count ----------------------------------------------------------------

    def wanted(self, tender: Tender) -> tuple[bool, str, dict[str, list[str]]]:
        """(keep it, the stat that says why not, what matched)."""
        section = self.section
        if tender.stage == "planned" and not section.planned:
            return False, "skipped_planned", {}
        if tender.deadline is not None and tender.deadline <= self._closing():
            return False, "skipped_closed", {}
        oldest = self.clock() - timedelta(days=section.posted_within_days)
        if tender.published_at is not None and tender.published_at < oldest and \
                tender.deadline is None:
            return False, "skipped_old", {}
        value = tender.value
        if value is not None and value.amount:
            low = value.low if value.low else value.amount
            if section.min_value is not None and value.amount < section.min_value:
                return False, "skipped_value", {}
            if section.max_value is not None and low > section.max_value:
                return False, "skipped_value", {}
        wanted_codes = section.cpv if tender.scheme == "cpv" else \
            section.naics if tender.scheme == "naics" else []
        codes = code_matches(tender.codes, wanted_codes) if tender.codes else []
        words = keyword_hits(tender.text, section.keywords)
        coded = bool(tender.scheme and tender.codes)
        keep = {"auto": bool(codes) if coded else bool(words), "codes": bool(codes),
                "keywords": bool(words), "any": bool(codes or words)}[section.match]
        if not keep:
            return False, "skipped_unmatched", {}
        if section.scope == "place" and self.places and not self.in_place(tender):
            return False, "skipped_place", {}
        return True, "", {"codes": codes, "keywords": words}

    def in_place(self, tender: Tender) -> bool:
        buyer = tender.buyer
        text = " ".join([buyer.locality, buyer.address, buyer.postcode, buyer.region,
                         " ".join(tender.places), tender.title]).casefold()
        names = [*self.places, *([self.region] if self.region else [])]
        return any(n.casefold() in text for n in names if n)

    def process(self, tenders: list[Tender]) -> UnitOutcome:
        outcome = UnitOutcome(status="done", found=len(tenders))
        with self._lock:
            self.stats["notices"] += len(tenders)
        for tender in tenders:
            keep, why, matched = self.wanted(tender)
            if not keep:
                with self._lock:
                    self.stats[why] += 1
                continue
            part = self.record(tender, matched)
            outcome.new += part.new
            outcome.merged += part.merged
        return outcome

    # --- writing ------------------------------------------------------------------------

    def buyer_lead(self, tender: Tender) -> Lead:
        buyer = tender.buyer
        region = (buyer.country or self.config.country or "").upper() or None
        phone = normalize_phone(buyer.phone, region) if buyer.phone else None
        emails = extract_emails(buyer.email or "")[:1]
        address = ", ".join(x for x in dict.fromkeys(
            [buyer.address, buyer.locality, buyer.postcode]) if x)
        named = [p for p in self.places if p.casefold() in
                 f"{buyer.locality} {buyer.address}".casefold()]
        return Lead(business_name=buyer.name, website=normalize_url(buyer.website),
                    phones=[phone] if phone else [], emails=emails, address=address,
                    locality=named[0] if named else buyer.locality,
                    registry_ids={buyer.scheme: buyer.identifier}
                    if buyer.scheme and buyer.identifier else {},
                    sources=[tender.source], source_urls=[tender.url])

    def record(self, tender: Tender, matched: dict[str, list[str]]) -> UnitOutcome:
        outcome = UnitOutcome(status="done")
        if self.store is None:
            return outcome
        lead = self.buyer_lead(tender)
        # Find-or-create, the attribute and the signal under the store's lock: another
        # portal's unit (or another lead type's) can't slip in between.
        with self.store._lock:
            try:
                org_id = self._resolve(lead, outcome)
            except Exception as exc:
                self.kit.failures.record(tender.source, "company_rejected",
                                         business=tender.buyer.name, url=tender.url,
                                         detail=repr(exc)[:200])
                return outcome
            if org_id is None:
                return outcome
            self.store.set_attribute(org_id, "org_type", "public_buyer", origin="rule",
                                     producer=tender.source)
            type_ = "tender_planned" if tender.stage == "planned" else "tender"
            buyer = tender.buyer
            payload = {
                "notice_id": tender.notice_id, "portal": tender.source,
                "notice_type": tender.notice_type, "stage": tender.stage,
                "buyer": buyer.name, "buyer_id": buyer.identifier or None,
                "contact": buyer.contact or None,
                "value": tender.value.as_dict() if tender.value and tender.value.amount
                else None,
                "deadline": tender.deadline.isoformat() if tender.deadline else None,
                "documents_url": tender.documents_url,
                "codes": tender.codes[:20], "scheme": tender.scheme,
                "matched_codes": matched.get("codes") or [],
                "matched_keywords": matched.get("keywords") or [],
                "places": tender.places[:10],
                "description": tender.description[:600],
            }
            added = self.store.add_signal(
                org_id, type_, key=tender.key[:300], title=tender.title[:500],
                payload=payload, strength=strength(tender, bool(matched.get("codes"))),
                published_at=tender.published_at, expires_at=tender.deadline,
                source_url=tender.url, source=tender.source)
        with self._lock:
            self.stats["kept"] += 1
            self.stats["signals"] += bool(added)
            self.stats["new"] += outcome.new
            self.stats["merged"] += outcome.merged
        outcome.found = 1
        return outcome

    def _resolve(self, lead: Lead, outcome: UnitOutcome) -> int | None:
        """The buyer's organisation: one the project holds keeps its record (A.3); a buyer
        nobody holds becomes a new organisation when ``create_new``."""
        ids = lead.registry_ids
        cache_key = f"{next(iter(ids))}:{next(iter(ids.values()))}" if ids else \
            lead.business_name.casefold()
        cached = self._buyers.get(cache_key)
        if cached is not None and self.store.get(cached) is not None:
            return cached
        match = self.store.find_match(lead.normalized())
        if match is not None:
            self._buyers[cache_key] = match
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
        self._buyers[cache_key] = org_id
        return org_id

    # --- the end ------------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"],
                              stats={**self.stats, "keys_missing": sorted(self.missing)})
