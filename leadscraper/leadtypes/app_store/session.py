"""app_store as an engine session: apps with poor ratings, long-stale updates or
complaining reviewers, resolved to their developers' organisations.

Units (stage 1: after the finders, so a company they hold gets the signal and
keeps its record):

* ``itunes``, one per keyword: Apple's Search API in the run's storefront. With
  ``google_play.match_ios``, an app's bundle id is looked up on Play too, and the
  listing kept only when it names the same developer website;
* ``listed``, once, when the config names apps (store links, Apple ids, packages).

Post-discovery (``site_apps``): the own sites of the run's (or project's)
organisations, read for App Store and Play links; an app a site links is that
organisation's app.

Each app is assessed (``assess``): ``low_rating`` (at or below the ceiling, from
enough ratings), ``stale_app`` (no update for ``stale_months``),
``review_complaints`` (enough low-star reviews on the Play listing naming a theme;
AI for those the rules can't place). An app with a finding is a signal on its
developer's organisation, keyed by store and app, refreshed by later runs; an app
that has become healthy loses its signals.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from ...core.lead import Lead
from ...core.normalize import domain_of, is_vendor_website, normalize_phone, normalize_url
from ...engine.costs import UnitCost, prior_cost
from ...net.http import DeadDomainError, PrivateAddressError
from ...net.ratelimit import BlockedError
from ...sources.apps import google_play, itunes
from ...sources.apps.base import (APP_STORE, GOOGLE_PLAY, AppListing, is_package,
                                     parse_app_ref, store_links)
from ...sources.apps.themes import THEMES, Theme, complaints, themes, unplaced
from ..base import LeadTypeResult, LeadTypeSession, SignalSpec, UnitOutcome, WorkUnit
from ..toolkit import Kit
from .config import AppStoreConfig

log = logging.getLogger(__name__)

KEY = "app_store"
ITUNES = "itunes"
LISTED = "listed"
SITE_APPS = "site_apps"
SPECS = [
    SignalSpec("low_rating", "Low-rated app", 0.6, 120.0,
               "An app rated at or below the run's ceiling by enough users: a rebuild or a "
               "fix is worth pitching."),
    SignalSpec("stale_app", "App not updated", 0.55, 120.0,
               "An app with no update for a long time: likely neglected, soon incompatible."),
    SignalSpec("review_complaints", "App complaints", 0.5, 120.0,
               "Low-star reviews naming fixable problems: crashes, login, speed, payments, "
               "bugs, usability."),
]
SPEC_OF = {s.type: s for s in SPECS}


def plan_units(section: AppStoreConfig) -> list[WorkUnit]:
    units: list[WorkUnit] = []
    if section.sources.itunes.enabled:
        units.extend(WorkUnit(KEY, ITUNES, None, keyword, "run") for keyword in section.keywords)
    if section.apps:
        units.append(WorkUnit(KEY, LISTED, None, None, "run"))
    return units


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", (text or "").casefold()))


class AppStoreSession(LeadTypeSession):
    lead_type = KEY
    post_scope = "run"
    discovery_stage = 1

    def __init__(self, context: Any, section: AppStoreConfig, *, kit: Kit | None = None,
                 clock=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.ai = getattr(context, "ai", None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dry_run = bool(getattr(context, "dry_run", False))
        self.country = (getattr(self.config, "country", "") or "").lower()
        self._lock = threading.Lock()
        self.units_done = 0
        self.stats: dict[str, int] = {"apps": 0, "assessed": 0, "flagged": 0, "signals": 0,
                                      "healthy": 0, "new": 0, "merged": 0, "attached": 0,
                                      "play_listings": 0, "play_matched": 0, "sites_read": 0,
                                      "site_apps": 0, "skipped_genre": 0, "skipped_place": 0,
                                      "skipped_country": 0, "skipped_size": 0,
                                      "site_apps_not_own": 0, "play_budget_spent": 0,
                                      "skipped_installs": 0, "skipped_unknown": 0,
                                      "ai_themes": 0}
        #: (store:id, organisation or None) assessed this run: each once.
        self.seen: set[tuple[str, int | None]] = set()
        #: Play listings read per budget ("discovery": twins and listed apps; "sites").
        self.play_spent = {"discovery": 0, "sites": 0}
        self.budget_noted: set[str] = set()
        self.places = [p for p in dict.fromkeys(str(p).strip() for p in
                                                (self.config.get("places") or [])) if p]
        try:
            info = self.config.country_info
            self.country_words = [w for w in dict.fromkeys([info.name, *info.name_noise]) if w]
        except Exception:
            self.country_words = []
        code = self.country
        #: The country's own domain ending (GB's is .uk).
        self.country_tld = {"gb": "uk"}.get(code, code)
        sources = section.sources
        self.kit.limiter.configure(itunes.HOST, sources.itunes.min_delay,
                                   sources.itunes.max_delay)
        self.kit.limiter.configure(google_play.HOST, sources.google_play.min_delay,
                                   sources.google_play.max_delay)
        self._units = plan_units(section)

    # --- planning ---------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        return frozenset({itunes.HOST, google_play.HOST})

    def _delay(self, source) -> float:
        return (source.min_delay + source.max_delay) / 2 + 0.5

    def prior(self, unit: WorkUnit) -> UnitCost:
        sources = self.section.sources
        if unit.source == ITUNES:
            keywords = max(1, sum(1 for u in self._units if u.source == ITUNES))
            play = min(sources.itunes.max_results,
                       sources.google_play.max_listings / keywords) \
                if sources.google_play.enabled and sources.google_play.match_ios else 0
            return prior_cost(self._delay(sources.itunes) + play * self._delay(
                sources.google_play), 2.0)
        return prior_cost(len(self.section.apps) * self._delay(sources.google_play), 1.0)

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        if self.section.check_sites == "none":
            return 0.0, 0.0
        sites = min(float(self.section.max_sites), expected_new)
        site = 2 * ((self.section.min_delay + self.section.max_delay) / 2 + 0.5)
        mean = sites * site / max(1, self.section.workers) + 0.1 * sites * self._delay(
            self.section.sources.google_play)
        return mean, mean * 0.5

    # --- discovery ----------------------------------------------------------------------

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        try:
            if self.store is None or self.dry_run:
                outcome = UnitOutcome(status="done")
            elif unit.source == ITUNES:
                outcome = self.search(unit.category or "")
            elif unit.source == LISTED:
                outcome = self.listed()
            else:
                outcome = UnitOutcome(status="failed", error=f"unknown source {unit.source}")
        except Exception as exc:  # contained: a bug costs its unit
            log.exception("app_store unit %s failed", unit.key)
            self.kit.failures.record(unit.source, "unit_failed", query=unit.category,
                                     detail=repr(exc)[:300])
            outcome = UnitOutcome(status="failed", error=repr(exc)[:300])
        with self._lock:
            self.units_done += 1
        return outcome

    def _fetch_failed(self, source: str, exc: Exception, **where) -> UnitOutcome:
        reason = "blocked" if isinstance(exc, BlockedError) else "fetch_failed"
        self.kit.failures.record(source, reason, detail=str(exc)[:300], **where)
        return UnitOutcome(status="failed", error=f"{reason}: {str(exc)[:250]}")

    def search(self, keyword: str) -> UnitOutcome:
        try:
            found = itunes.search(self.kit.http, keyword, self.country,
                                  self.section.sources.itunes.max_results)
        except Exception as exc:
            return self._fetch_failed(ITUNES, exc, query=keyword)
        if not found:
            self.kit.failures.record(ITUNES, "no_results", query=keyword)
        outcome = UnitOutcome(status="done", found=len(found))
        for listing in found:
            self._add(outcome, self.safe_consider([listing]))
        return outcome

    def listed(self) -> UnitOutcome:
        """The apps the config names: Apple ids looked up together, Play packages one by one."""
        refs = [r for r in (parse_app_ref(a) for a in self.section.apps) if r]
        outcome = UnitOutcome(status="done")
        apple = [app_id for store, app_id in refs if store == APP_STORE]
        if apple:
            try:
                found = itunes.lookup(self.kit.http, apple, self.country)
            except Exception as exc:
                self._fetch_failed(ITUNES, exc, query="lookup")
                found = []
            else:
                for missing in sorted(set(apple) - {x.app_id for x in found}):
                    self.kit.failures.record(LISTED, "app_not_found", query=f"id{missing}",
                                             detail=f"no App Store app id{missing} in the "
                                                    f"{self.country.upper()} storefront")
            for listing in found:
                self._add(outcome, self.safe_consider([listing], explicit=True))
        for store, package in refs:
            if store == GOOGLE_PLAY:
                listing = self.play(package, budget="discovery", note_missing=True)
                if listing is not None:
                    self._add(outcome, self.safe_consider([listing], explicit=True))
        return outcome

    def play(self, package: str, *, budget: str, note_missing: bool = False) -> AppListing | None:
        """One Play listing, within its budget; None if not listed, not readable or over
        budget. Discovery (iOS twins, listed apps) and organisations' own sites each have
        ``max_listings``: a busy search never starves the sites' own apps."""
        settings = self.section.sources.google_play
        if not settings.enabled:
            return None
        with self._lock:
            if self.play_spent[budget] >= settings.max_listings:
                if budget not in self.budget_noted:
                    self.budget_noted.add(budget)
                    self.stats["play_budget_spent"] += 1
                    self.kit.failures.record(GOOGLE_PLAY, "budget_spent", detail=(
                        f"{settings.max_listings} Play listings read for {budget}; the rest "
                        "were not (sources.google_play.max_listings)"))
                return None
            self.play_spent[budget] += 1
            self.stats["play_listings"] += 1
        try:
            listing = google_play.fetch(self.kit.http, package, self.country or "us")
        except Exception as exc:
            self._fetch_failed(GOOGLE_PLAY, exc, url=google_play.public_url(package))
            return None
        if listing is None and note_missing:
            self.kit.failures.record(GOOGLE_PLAY, "app_not_found",
                                     url=google_play.public_url(package),
                                     detail=f"{package} isn't listed in the "
                                            f"{(self.country or 'us').upper()} store")
        return listing

    def _ios_twin(self, listing: AppListing) -> AppListing | None:
        """The Play listing of an App Store app: its bundle id as a package, kept only when
        the listing names the same developer website."""
        play = self.section.sources.google_play
        # A shared host (a social profile, a link-in-bio page) proves nothing about who
        # published an app: only the developer's own site confirms a twin.
        if not (play.enabled and play.match_ios and listing.store == APP_STORE
                and listing.developer_website and is_vendor_website(listing.developer_website)
                and is_package(listing.bundle_id)):
            return None
        twin = self.play(listing.bundle_id, budget="discovery")
        if twin is None or domain_of(twin.developer_website) != domain_of(
                listing.developer_website):
            return None
        with self._lock:
            self.stats["play_matched"] += 1
        return twin

    @staticmethod
    def _add(total: UnitOutcome, part: UnitOutcome) -> None:
        total.found += part.found
        total.new += part.new
        total.merged += part.merged

    # --- which apps count -------------------------------------------------------------------

    def installs_out(self, listing: AppListing) -> bool:
        """Outside the run's download band (Play's; an App Store listing shows none)."""
        installs = self.section.installs
        return listing.installs_min is not None and (
            (installs.min is not None and listing.installs_min < installs.min) or
            (installs.max is not None and listing.installs_min > installs.max))

    def unwanted(self, listing: AppListing) -> str:
        """"" to assess it, else the stat that says why not."""
        genre = _words(listing.genre)
        if self.section.exclude_genres and any(_words(g) and _words(g) in genre
                                               for g in self.section.exclude_genres):
            return "skipped_genre"
        if self.section.genres and not any(_words(g) and _words(g) in genre
                                           for g in self.section.genres):
            return "skipped_genre"
        if self.installs_out(listing):
            return "skipped_installs"
        return ""

    def too_big(self, listings: list[AppListing]) -> str:
        """"skipped_size" for an app rated by more people than ``max_ratings`` in a store."""
        cap = self.section.max_ratings
        if cap is not None and any((x.ratings or 0) > cap for x in listings):
            return "skipped_size"
        return ""

    def out_of_scope(self, listings: list[AppListing]) -> str:
        """"" when the app (with its twin) is in the run's scope, else the stat saying why.

        ``country``: the developer is evidently in the run's country -- Play's developer
        address or the app's own words name the country or a run place, or the developer's
        site is on the country's domain (.in, .co.uk …). A storefront lists apps from
        everywhere; without evidence an app is left out. ``place``: a run place is named.
        """
        scope = self.section.scope
        if scope == "storefront":
            return ""
        text = " " + " ".join(f"{_words(x.name)} {_words(x.description)} "
                              f"{_words(x.developer_address)}" for x in listings) + " "
        if any(f" {_words(p)} " in text for p in self.places):
            return ""
        if scope == "place":
            return "skipped_place"
        if any(f" {_words(w)} " in text for w in self.country_words):
            return ""
        tld = self.country_tld
        if tld and any((domain_of(x.developer_website) or "").endswith(f".{tld}")
                       for x in listings if x.developer_website):
            return ""
        return "skipped_country"

    def consider(self, listings: list[AppListing], org_id: int | None = None, *,
                 explicit: bool = False) -> UnitOutcome:
        """Assess an app (with its Play twin) and record it on its organisation.

        ``org_id``: an organisation whose own site links the app; ``explicit``: the config
        names it. Either way the run's genre, download and scope filters don't apply."""
        outcome = UnitOutcome(status="done")
        first = listings[0]
        # Seen per (app, organisation): an app the search passed over (or gave to its
        # developer) is still the question for the organisation whose own site links it.
        with self._lock:
            if (first.key, org_id) in self.seen:
                return outcome
            self.seen.add((first.key, org_id))
            self.stats["apps"] += 1
        chosen = org_id is not None or explicit
        # Cheap checks first: no Play listing is read for an app they already leave out.
        why = "" if chosen else (self.unwanted(first) or self.too_big([first]))
        if why:
            with self._lock:
                self.stats[why] += 1
            return outcome
        twin = self._ios_twin(first) if org_id is None else None
        if twin is not None:
            with self._lock:
                if (twin.key, org_id) in self.seen:
                    twin = None
                else:
                    self.seen.add((twin.key, org_id))
            if twin is not None:
                listings = [first, twin]
        why = "" if chosen else (
            ("skipped_installs" if any(self.installs_out(x) for x in listings) else "")
            or self.too_big(listings) or self.out_of_scope(listings))
        if why:
            with self._lock:
                self.stats[why] += 1
            return outcome
        return self.record(listings, org_id, outcome)

    def safe_consider(self, listings: list[AppListing], org_id: int | None = None, *,
                      explicit: bool = False) -> UnitOutcome:
        """``consider``, contained: one app's surprise costs that app, not its unit."""
        try:
            return self.consider(listings, org_id, explicit=explicit)
        except Exception as exc:
            log.exception("assessing %s failed", listings[0].key)
            self.kit.failures.record(listings[0].store, "assess_failed", url=listings[0].url,
                                     detail=repr(exc)[:300])
            return UnitOutcome(status="done")

    def owns(self, lead: Lead, listing: AppListing) -> bool:
        """Is an app a site links that organisation's own? Its developer's site is the
        organisation's, or the app (or its developer) carries the organisation's name
        ("Baner Bakes Order" for Baner Bakes). A delivery platform or booking aggregator a
        site links is somebody else's app (review 2026-10-13)."""
        from rapidfuzz import fuzz

        if listing.developer_website and lead.website and \
                domain_of(listing.developer_website) == domain_of(lead.website):
            return True
        name = _words(lead.business_name)
        if len(name) < 4:
            return False
        return any(fuzz.token_set_ratio(name, _words(text)) >= 90
                   for text in (listing.name, listing.developer) if text)

    # --- assessing ----------------------------------------------------------------------

    def assess(self, listing: AppListing) -> list[dict[str, Any]]:
        """The app's findings: ``{type, strength, title, payload}``."""
        section = self.section
        today = self.clock().date()
        base = {"store": listing.store, "app_id": listing.app_id, "app": listing.name,
                "url": listing.url, "developer": listing.developer,
                "rating": listing.rating, "ratings": listing.ratings,
                "installs": listing.installs or None,
                "updated": listing.updated.isoformat() if listing.updated else None,
                "version": listing.version or None, "genre": listing.genre or None}
        where = f"{listing.name} ({listing.store_title})"
        out: list[dict[str, Any]] = []
        if listing.rating is not None and (listing.ratings or 0) >= section.min_ratings \
                and listing.rating <= section.rating_ceiling:
            strength = 0.5 + 0.4 * (section.rating_ceiling - listing.rating) / \
                section.rating_ceiling
            out.append({"type": "low_rating", "strength": round(strength, 3),
                        "title": f"{where}: rated {listing.rating:.1f} from "
                                 f"{listing.ratings:,} ratings", "payload": base})
        months = listing.months_since_update(today)
        if months is not None and months >= section.stale_months:
            strength = min(0.85, 0.5 + 0.05 * (months - section.stale_months) / 6)
            out.append({"type": "stale_app", "strength": round(strength, 3),
                        "title": f"{where}: last updated {listing.updated:%b %Y}, "
                                 f"{int(months)} months ago",
                        "payload": {**base, "months_since_update": int(months)}})
        found = self.complaint_themes(listing)
        if found:
            labels = [t.label for t in found]
            strength = min(0.75, 0.45 + 0.1 * len(found))
            # Play shows its "most relevant" reviews, often the harsh ones: on an app rated
            # well overall, complaints count for less (live check 2026-10-13: a 4.5 app).
            if listing.rating is not None and listing.rating > section.rating_ceiling:
                strength *= max(0.3, (5.0 - listing.rating) / (5.0 - section.rating_ceiling))
            out.append({"type": "review_complaints", "strength": round(strength, 3),
                        "title": f"{where}: reviewers complain of {', '.join(labels[:4])}",
                        "payload": {**base, "themes": [
                            {"theme": t.key, "label": t.label, "reviews": t.count,
                             "quote": t.quote} for t in found],
                            "low_star_reviews": len(complaints(listing.reviews))}})
        return out

    def complaint_themes(self, listing: AppListing) -> list[Theme]:
        """Themes named by at least ``min_complaints`` low-star reviews (all of them together),
        the rules first, then the AI for reviews they couldn't place."""
        low = complaints(listing.reviews)
        found = themes(listing.reviews)
        placed = len(low) - len(unplaced(listing.reviews))
        if self.ai is not None and unplaced(listing.reviews):
            try:
                from ...ai.features.review_themes import classify

                rest = unplaced(listing.reviews)
                verdicts = classify(self.ai, [{"id": str(i), "text": r.text}
                                              for i, r in enumerate(rest)],
                                    {k: label for k, (label, _) in THEMES.items()})
            except Exception as exc:                    # AI never stops a run
                log.warning("review themes failed: %s", exc)
                verdicts = {}
            by_key = {t.key: t for t in found}
            for verdict in verdicts.values():
                placed += 1
                for key in verdict["themes"]:
                    if key in by_key:
                        by_key[key].count += 1
                    else:
                        by_key[key] = Theme(key, THEMES[key][0], 1, verdict["evidence"])
            with self._lock:
                self.stats["ai_themes"] += len(verdicts)
            found = sorted(by_key.values(), key=lambda t: -t.count)
        return found if placed >= self.section.min_complaints else []

    # --- writing ------------------------------------------------------------------------

    def developer_lead(self, listings: list[AppListing]) -> Lead:
        """The developer as the listings describe it: its name, own website, and the
        contacts Play publishes for it."""
        play = next((x for x in listings if x.store == GOOGLE_PLAY), None)
        website = next((normalize_url(x.developer_website) for x in listings
                        if x.developer_website and is_vendor_website(x.developer_website)),
                       None)
        if website:
            website = f"{website.split('//', 1)[0]}//{domain_of(website)}/"
        phone = normalize_phone(play.developer_phone, self.config.country) \
            if play is not None and play.developer_phone else None
        name = next((x.developer for x in listings if x.developer), "") or listings[0].name
        return Lead(business_name=name, website=website,
                    emails=[play.developer_email] if play is not None and play.developer_email
                    else [], phones=[phone] if phone else [],
                    address=play.developer_address if play is not None else "",
                    sources=sorted({x.store for x in listings}),
                    source_urls=[x.url for x in listings])

    def record(self, listings: list[AppListing], org_id: int | None,
               outcome: UnitOutcome) -> UnitOutcome:
        findings = [(listing, finding) for listing in listings for finding in self.assess(listing)]
        with self._lock:
            self.stats["assessed"] += len(listings)
            self.stats["flagged"] += bool(findings)
            self.stats["healthy"] += not findings
        with self.store._lock:
            if org_id is None:
                lead = self.developer_lead(listings)
                match = self.store.find_match(lead.normalized())
                if match is not None:
                    org_id = match
                    with self._lock:
                        self.stats["attached"] += bool(findings)
                elif findings and self.section.create_new:
                    org_id, action, _ = self.store.upsert(lead)
                    outcome.new += action == "new"
                    outcome.merged += action == "merged"
                    with self._lock:
                        self.stats["new"] += action == "new"
                        self.stats["merged"] += action == "merged"
                elif findings:
                    with self._lock:
                        self.stats["skipped_unknown"] += 1
            if org_id is None:
                return outcome
            if findings:
                outcome.found += 1
            self._write(org_id, listings, findings)
        return outcome

    def _write(self, org_id: int, listings: list[AppListing],
               findings: list[tuple[AppListing, dict[str, Any]]]) -> None:
        """This app's signals replace its last ones; other apps' signals on the
        organisation stay. A healthy app only clears what it had."""
        prefixes = tuple(f"{x.key}:" for x in listings)
        existing = self.store.signals(org_id)
        if not findings and not any(s.get("key", "").startswith(prefixes) for s in existing):
            return                          # nothing to say, nothing to clear
        keep = {(s["type"], s["key"]) for s in existing
                if not str(s.get("key") or "").startswith(prefixes)}
        added = 0
        for listing, finding in findings:
            key = f"{listing.key}:{finding['type']}"
            keep.add((finding["type"], key))
            added += bool(self.store.add_signal(
                org_id, finding["type"], key=key, title=finding["title"],
                payload=finding["payload"], strength=finding["strength"],
                source_url=listing.url, source=listing.store))
        self.store.prune_signals(org_id, keep)
        with self._lock:
            self.stats["signals"] += added

    # --- post-discovery: store links on the organisations' own sites ----------------------

    def site_candidates(self) -> list[tuple[int, Lead]]:
        run_id = getattr(self.context, "run_id", None)
        if self.section.check_sites == "run" and run_id is not None:
            rows = self.store.tagged(run_id=run_id)
        elif self.section.check_sites == "project":
            rows = self.store.tagged()
        else:
            rows = []
        out = [(i, lead) for i, lead, _ in rows
               if lead.website and is_vendor_website(lead.website)]
        return out[:self.section.max_sites]

    def post_discovery(self, control: Any) -> None:
        if self.store is None or self.dry_run or self.section.check_sites == "none":
            return
        try:
            todo = self.site_candidates()
        except Exception as exc:
            log.exception("app_store could not list its organisations")
            self.kit.failures.record(KEY, "post_discovery_failed", detail=repr(exc))
            return
        found: list[tuple[int, Lead, str, str]] = []

        def read(item: tuple[int, Lead]) -> None:
            if control is not None and control.stop_requested:
                return
            org_id, lead = item
            for store, app_id in self.links(lead):
                with self._lock:
                    found.append((org_id, lead, store, app_id))

        with ThreadPoolExecutor(max_workers=self.section.workers,
                                thread_name_prefix="appsites") as pool:
            list(pool.map(read, todo))
        self.store.tag_thread(KEY)
        with self._lock:
            self.stats["site_apps"] += len(found)
        apple: dict[str, list[tuple[int, Lead]]] = {}
        for org_id, lead, store, app_id in found:
            if store == APP_STORE:
                apple.setdefault(app_id, []).append((org_id, lead))
        listings: list[tuple[int, Lead, AppListing]] = []
        if apple and not (control is not None and control.stop_requested):
            try:
                for listing in itunes.lookup(self.kit.http, list(apple), self.country):
                    listings.extend((o, lead, listing) for o, lead in apple.get(listing.app_id,
                                                                                 []))
            except Exception as exc:
                self._fetch_failed(ITUNES, exc, query="lookup")
        for org_id, lead, store, package in found:
            if store != GOOGLE_PLAY or (control is not None and control.stop_requested):
                continue
            listing = self.play(package, budget="sites")
            if listing is not None:
                listings.append((org_id, lead, listing))
        for org_id, lead, listing in listings:
            if control is not None and control.stop_requested:
                break
            if self.owns(lead, listing):
                self.safe_consider([listing], org_id=org_id)
            else:                       # somebody else's app: judged like any found app
                with self._lock:
                    self.stats["site_apps_not_own"] += 1
                self.safe_consider([listing])

    def links(self, lead: Lead) -> list[tuple[str, str]]:
        """App Store and Play links on an organisation's homepage (robots.txt honoured)."""
        url = normalize_url(lead.website)
        host = domain_of(url) if url else None
        if not url or not host:
            return []
        self.kit.limiter.configure(host, self.section.min_delay, self.section.max_delay)
        if not self.kit.robots.allowed(url, rate_key=host):
            return []
        try:
            response = self.kit.http.get_once(url, rate_key=host)
        except (PrivateAddressError, DeadDomainError):
            return []
        except Exception as exc:
            self.kit.failures.record(SITE_APPS, "fetch_failed", business=lead.business_name,
                                     url=url, detail=type(exc).__name__)
            return []
        with self._lock:
            self.stats["sites_read"] += 1
        if int(response.status_code) != 200:
            return []
        soup = self.kit.http.soup(response.text or "")
        return store_links(a["href"] for a in soup.find_all("a", href=True))

    # --- the end ------------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"], stats=dict(self.stats))
