"""funding_news as an engine session.

Discovery units (stage 1: after the finders, so a company they hold gets the
signal and keeps its record), each on its own host:

* ``gdelt``: one per place (or one for the country) -- a few short GDELT DOC
  queries over the wanted events' words, newest first;
* ``google_news`` (opt-in): one per place, a Google News RSS search;
* ``press_releases`` (opt-in): GlobeNewswire's feed for the run's country;
* ``feeds``: one per RSS / Atom URL the config lists.

Every headline goes through the event rules (``events.classify``); with the
``signal_interpretation`` AI feature, headlines the rules can't read go to the
model, kept only with a verbatim quote and a company name the item contains.
Stories about the same event (same company, kind, detail and month) become one
signal whose payload lists every outlet that covered it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from ...core.lead import Lead
from ...core.normalize import domain_of, is_vendor_website, normalize_name, normalize_url
from ...engine.costs import UnitCost, prior_cost
from ...net.ratelimit import BlockedError
from ...sources.news import feeds, gdelt, google_news
from ...sources.news.base import NewsItem
from ..base import LeadTypeResult, LeadTypeSession, SignalSpec, UnitOutcome, WorkUnit
from ..toolkit import Kit
from .config import FundingNewsConfig
from .events import TITLES, Event, classify, mentions

log = logging.getLogger(__name__)

KEY = "funding_news"
#: Event kind -> signal type.
SIGNAL_TYPES = {"funding": "funding_round", "launch": "product_launch",
                "expansion": "expansion", "acquisition": "acquisition",
                "digital_transformation": "digital_transformation",
                "leadership_change": "leadership_change"}
KIND_OF = {v: k for k, v in SIGNAL_TYPES.items()}
SPECS = [
    SignalSpec("funding_round", "Funding round", 0.8, 90.0,
               "A funding round, raise or investment: money to spend, often on technology."),
    SignalSpec("product_launch", "Launch", 0.45, 45.0,
               "A new product, app or service launched."),
    SignalSpec("expansion", "Expansion", 0.6, 60.0,
               "A new office, plant, centre or market: new sites need systems and people."),
    SignalSpec("acquisition", "Acquisition", 0.6, 90.0,
               "Buying or being bought: systems to integrate."),
    SignalSpec("digital_transformation", "Digital transformation", 0.75, 90.0,
               "A digital, cloud, ERP or AI programme, or a technology partner chosen."),
    SignalSpec("leadership_change", "Leadership change", 0.6, 60.0,
               "A new CEO, CTO, CIO or head of digital: new leaders review vendors."),
]
SPEC_OF = {s.type: s for s in SPECS}
#: GDELT / Google News search words per event kind (headlines decide; these only find).
QUERY_WORDS = {
    "funding": ["raises", "funding round", "seed round", "series a"],
    "launch": ["launches", "unveils"],
    "expansion": ["new office", "expands", "inaugurates"],
    "acquisition": ["acquires", "acquisition"],
    "digital_transformation": ["digital transformation", "digitisation", "goes live"],
    "leadership_change": ["appoints", "new CEO", "new CTO"],
}
SOURCES = ("gdelt", "google_news", "press_releases", "feeds")
#: OR terms per GDELT query (a longer query is refused as "too short or too long").
GDELT_TERMS = 6
#: Stories about the same event (company, kind, detail) this many days apart are one event.
SAME_EVENT_DAYS = 30
_LANGUAGES = {"en": "english", "de": "german", "fr": "french", "es": "spanish",
              "ar": "arabic", "hi": "hindi", "nl": "dutch", "it": "italian", "pt": "portuguese"}


def strength(kind: str, outlets: int, event: Event | None = None) -> float:
    """The event's base strength, a little more for each further outlet covering it."""
    base = SPEC_OF[SIGNAL_TYPES[kind]].default_strength
    if event is not None and kind == "leadership_change" and event.tech_role:
        base += 0.15                                  # a new CTO / CIO / head of digital
    return round(min(0.95, base + 0.05 * max(0, outlets - 1)), 3)


def plan_units(config: Any, section: FundingNewsConfig) -> list[WorkUnit]:
    sources = section.sources
    places = list(config.get("places") or []) if hasattr(config, "get") else []
    units: list[WorkUnit] = []
    for name, source in (("gdelt", sources.gdelt), ("google_news", sources.google_news)):
        if not source.enabled:
            continue
        if places:
            units.extend(WorkUnit(KEY, name, p, None, "locality") for p in places)
        else:
            units.append(WorkUnit(KEY, name, None, None, "run"))
    if sources.press_releases.enabled:
        units.append(WorkUnit(KEY, "press_releases", None, None, "run"))
    if sources.feeds.enabled:
        units.extend(WorkUnit(KEY, "feeds", None, url, "run") for url in sources.feeds.urls)
    return units


def _month(when: datetime | None) -> str:
    return when.strftime("%Y-%m") if when else "undated"


class FundingNewsSession(LeadTypeSession):
    lead_type = KEY
    #: After the finders: a company they already hold gets the signal, untouched.
    discovery_stage = 1

    def __init__(self, context: Any, section: FundingNewsConfig, *, kit: Kit | None = None,
                 clock=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ai = getattr(context, "ai", None)
        self._lock = threading.Lock()
        self.units_done = 0
        self.stats: dict[str, int] = {"items_seen": 0, "events": 0, "signals": 0, "new": 0,
                                      "merged": 0, "skipped_place": 0, "skipped_old": 0,
                                      "skipped_unknown": 0, "ai_matched": 0}
        sources = section.sources
        limiter = self.kit.limiter
        limiter.configure(gdelt.HOST, sources.gdelt.min_delay, sources.gdelt.max_delay)
        limiter.configure(google_news.HOST, sources.google_news.min_delay,
                          sources.google_news.max_delay)
        limiter.configure(feeds.GLOBENEWSWIRE_HOST, sources.press_releases.min_delay,
                          sources.press_releases.max_delay)
        for url in sources.feeds.urls:
            limiter.configure(domain_of(url) or url, sources.feeds.min_delay,
                              sources.feeds.max_delay)
        self.places = self._places()
        try:
            info = self.config.country_info
            self.country_name, self.locale = info.name, info.locale
        except Exception:
            self.country_name, self.locale = "", "en-US"
        self._seen: set[str] = set()
        #: GDELT said "too many requests": no more GDELT queries this run.
        self._gdelt_throttled = False
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

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        if unit.source == "gdelt":
            return frozenset({gdelt.HOST})
        if unit.source == "google_news":
            return frozenset({google_news.HOST})
        if unit.source == "press_releases":
            return frozenset({feeds.GLOBENEWSWIRE_HOST})
        return frozenset({domain_of(unit.category or "") or f"{KEY}:feeds"})

    def prior(self, unit: WorkUnit) -> UnitCost:
        if unit.source == "gdelt":
            gd = self.section.sources.gdelt
            return prior_cost(len(self.gdelt_queries(unit.area)) * ((gd.min_delay + gd.max_delay)
                                                                    / 2 + 3.0), 3.0)
        return prior_cost(5.0, 2.0)

    # --- discovery --------------------------------------------------------------------

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        try:
            if unit.source == "gdelt":
                outcome = self.read_gdelt(unit.area)
            elif unit.source == "google_news":
                outcome = self.read_google_news(unit.area)
            elif unit.source == "press_releases":
                outcome = self.read_feed(feeds.globenewswire_url(self.config.country,
                                                                 self.country_name),
                                         "press_releases", feeds.GLOBENEWSWIRE_HOST)
            elif unit.source == "feeds":
                outcome = self.read_feed(unit.category or "", "feeds", None)
            else:
                outcome = UnitOutcome(status="failed", error=f"unknown source {unit.source}")
        except Exception as exc:  # contained: a source's bug costs its unit
            log.exception("funding_news unit %s failed", unit.key)
            self.kit.failures.record(unit.source, "unit_failed", detail=repr(exc))
            outcome = UnitOutcome(status="failed", error=repr(exc))
        with self._lock:
            self.units_done += 1
        return outcome

    def gdelt_queries(self, place: str | None) -> list[str]:
        """The place's GDELT queries: the wanted events' words, at most ``GDELT_TERMS`` per
        query (GDELT answers a longer one with "too short or too long"; 5-6 terms were
        verified live on 2026-10-08)."""
        language = _LANGUAGES.get((self.locale or "en").split("-")[0].lower())
        words = list(dict.fromkeys(w for kind in self.section.events for w in QUERY_WORDS[kind]))
        out = []
        for start in range(0, len(words), GDELT_TERMS):
            query = gdelt.build_query(words[start:start + GDELT_TERMS], place=place,
                                      country=self.country_name or None, language=language)
            if self.section.keywords:
                query = f"{query} {gdelt.build_query(self.section.keywords[:GDELT_TERMS])}"
            out.append(query)
        return out

    def read_gdelt(self, place: str | None) -> UnitOutcome:
        """GDELT for one place, a few short queries; the headline rules sort the events
        out. GDELT throttles per IP: once it says so, the run asks it nothing more."""
        items: list[NewsItem] = []
        failed = 0
        queries = self.gdelt_queries(place)
        for query in queries:
            if self._gdelt_throttled:
                break
            try:
                items.extend(gdelt.search(self.kit.http, query, days=self.section.recency_days,
                                          max_records=self.section.sources.gdelt.max_records))
                continue
            except Exception as exc:
                throttled = isinstance(exc, BlockedError) or (
                    isinstance(exc, gdelt.GdeltError) and "limit requests" in str(exc).lower())
                reason = "rate_limited" if throttled else (
                    "bad_query" if isinstance(exc, gdelt.GdeltError) else "fetch_failed")
                detail = str(exc) if reason != "fetch_failed" else repr(exc)
            failed += 1
            if throttled:
                with self._lock:
                    self._gdelt_throttled = True
            self.kit.failures.record("gdelt", reason, query=query, locality=place,
                                     detail=detail[:200])
        if self._gdelt_throttled and not items:
            return UnitOutcome(status="failed", error="GDELT throttled this run")
        outcome = self.process(items, query_place=place)
        if queries and failed == len(queries):
            outcome.status, outcome.error = "failed", "every GDELT query failed"
        return outcome

    def read_google_news(self, place: str | None) -> UnitOutcome:
        words = [w for kind in self.section.events for w in QUERY_WORDS[kind]]
        query = google_news.build_query(words + list(self.section.keywords), place=place,
                                        days=self.section.recency_days)
        try:
            items = google_news.search(self.kit.http, query, locale=self.locale,
                                       country=self.config.country or "US")
        except Exception as exc:
            reason = "blocked" if isinstance(exc, BlockedError) else "fetch_failed"
            self.kit.failures.record("google_news", reason, query=query, locality=place,
                                     detail=repr(exc)[:200])
            return UnitOutcome(status="failed", error=repr(exc)[:300])
        return self.process(items, query_place=place)

    def read_feed(self, url: str, source: str, rate_key: str | None) -> UnitOutcome:
        try:
            items = feeds.read(self.kit.http, url, source=source, rate_key=rate_key)
        except Exception as exc:
            reason = "blocked" if isinstance(exc, BlockedError) else "fetch_failed"
            self.kit.failures.record(source, reason, url=url, detail=repr(exc)[:200])
            return UnitOutcome(status="failed", error=repr(exc)[:300])
        if not items:
            self.kit.failures.record(source, "no_listings", url=url,
                                     detail="the feed had no items (or was not a feed)")
        return self.process(items, query_place=None)

    # --- reading headlines ---------------------------------------------------------------

    def _keep(self, item: NewsItem, query_place: str | None) -> bool:
        cutoff = self.clock() - timedelta(days=self.section.recency_days)
        if item.published_at is not None and item.published_at < cutoff:
            with self._lock:
                self.stats["skipped_old"] += 1
            return False
        if self.section.place_filter == "none" or not self.places:
            return True
        if mentions(item.text, self.places):
            return True
        if self.section.place_filter == "article" and query_place and item.source in (
                "gdelt", "google_news"):
            return True                    # the search matched the place in the article
        with self._lock:
            self.stats["skipped_place"] += 1
        return False

    def classify(self, item: NewsItem) -> Event | None:
        names = [*self.places, self.country_name]
        event = classify(item.title, issuer=item.company, kinds=self.section.events,
                         places=names)
        if event is None and item.company and item.summary:
            # A press release: its first sentence often says what the headline doesn't.
            first = re.split(r"(?<=[.!?])\s", item.summary, maxsplit=1)[0]
            event = classify(first, issuer=item.company, kinds=self.section.events,
                             places=names)
        return event

    def process(self, items: list[NewsItem], *, query_place: str | None) -> UnitOutcome:
        outcome = UnitOutcome(status="done", found=len(items))
        with self._lock:
            self.stats["items_seen"] += len(items)
            fresh = [i for i in items if i.key not in self._seen]
            self._seen.update(i.key for i in fresh)
        groups: dict[tuple, list[tuple[NewsItem, Event]]] = defaultdict(list)
        unread: list[NewsItem] = []
        for item in fresh:
            if not self._keep(item, query_place):
                continue
            event = self.classify(item)
            if event is None:
                unread.append(item)
                continue
            groups[self._group(item, event)].append((item, event))
        for item, event in self.interpret(unread):
            groups[self._group(item, event)].append((item, event))
        for stories in groups.values():
            part = self.record(stories)
            outcome.new += part.new
            outcome.merged += part.merged
        return outcome

    @staticmethod
    def _group(item: NewsItem, event: Event) -> tuple:
        return (normalize_name(event.company), event.kind, event.detail)

    def interpret(self, items: list[NewsItem]) -> list[tuple[NewsItem, Event]]:
        """AI (``signal_interpretation``) for headlines the rules couldn't read. A verdict
        needs a verbatim quote and a company name the item itself contains."""
        if not items or self.ai is None:
            return []
        specs = [SPEC_OF[SIGNAL_TYPES[k]] for k in self.section.events]
        try:
            from ...ai.features.signals import interpret

            verdicts = interpret(self.ai, [{"id": str(i), "title": it.title, "text": it.summary,
                                            "url": it.url, "published_at": it.published_at}
                                           for i, it in enumerate(items)],
                                 specs, lead_type=KEY, with_company=True)
        except Exception as exc:                      # AI never stops a run
            log.warning("news interpretation failed: %s", exc)
            return []
        out = []
        for index, verdict in verdicts.items():
            item = items[int(index)]
            company = item.company or verdict.get("company") or ""
            kind = KIND_OF.get(verdict.get("type") or "")
            if not company or kind is None:
                continue
            summary = verdict.get("summary") or ""
            out.append((item, Event(kind, company, item.title, verdict.get("evidence") or "",
                                    detail=f"ai:{normalize_name(summary)[:40]}",
                                    interpreted_by=verdict.get("interpreted_by", ""),
                                    summary=summary)))
        with self._lock:
            self.stats["ai_matched"] += len(out)
        return out

    # --- writing ----------------------------------------------------------------------

    def record(self, stories: list[tuple[NewsItem, Event]]) -> UnitOutcome:
        """One event, however many outlets covered it: a signal on its organisation."""
        outcome = UnitOutcome(status="done")
        if self.store is None or not stories:
            return outcome
        stories.sort(key=lambda s: s[0].published_at or self.clock())
        item, event = stories[0]
        named = mentions(" ".join(i.text for i, _ in stories), self.places)
        site = self._site(event.company, [link for i, _ in stories for link in i.links])
        lead = Lead(business_name=event.company, website=site,
                    locality=named[0] if named else "", sources=[item.source],
                    source_urls=[item.url])
        try:
            org_id = self._resolve(lead, outcome)
        except Exception as exc:
            self.kit.failures.record(item.source, "company_rejected", business=event.company,
                                     url=item.url, detail=repr(exc)[:200])
            return outcome
        if org_id is None:
            return outcome
        type_ = SIGNAL_TYPES[event.kind]
        previous = self._same_event(org_id, type_, event.detail, item.published_at)
        key = previous["key"] if previous else \
            f"{event.kind}:{event.detail}:{_month(item.published_at)}"[:300]
        coverage = list(((previous or {}).get("payload") or {}).get("coverage") or [])
        known = {c.get("url") for c in coverage}
        for story, _ in stories:
            if story.url not in known:
                known.add(story.url)
                coverage.append({"publisher": story.publisher, "url": story.url,
                                 "title": story.title[:300], "source": story.source,
                                 "published_at": story.published_at.isoformat()
                                 if story.published_at else None})
        coverage.sort(key=lambda c: c.get("published_at") or "")
        best = max((e for _, e in stories), key=lambda e: (e.amount is not None,
                                                         e.round is not None,
                                                         e.investors is not None))
        payload = {"event": event.kind, "event_title": TITLES[event.kind],
                   "detail": event.detail,
                   "headline": item.title[:300], "publisher": item.publisher,
                   "company": event.company, "evidence": event.evidence[:300],
                   "amount": best.amount.as_dict() if best.amount else None,
                   "round": best.round, "investors": best.investors,
                   "person": best.person, "role": best.role, "other": best.other,
                   "places": named, "source": item.source, "coverage": coverage[:30],
                   "published_at": item.published_at.isoformat() if item.published_at
                   else None}
        if event.interpreted_by:
            payload["interpreted_by"] = event.interpreted_by
            payload["summary"] = event.summary
        added = self.store.add_signal(
            org_id, type_, key=key, title=self._title(best, item), payload=payload,
            strength=strength(event.kind, len(coverage), best),
            published_at=item.published_at, source_url=item.url, source=item.source)
        with self._lock:
            self.stats["events"] += 1
            self.stats["signals"] += bool(added)
            self.stats["new"] += outcome.new
            self.stats["merged"] += outcome.merged
        outcome.found = 1
        return outcome

    def _same_event(self, org_id: int, type_: str, detail: str,
                    when: datetime | None) -> dict | None:
        """This organisation's signal for the same event (same type and detail, published
        within ``SAME_EVENT_DAYS``): a later story about it refreshes that row."""
        for signal in self.store.signals(org_id):
            payload = signal.get("payload") or {}
            if signal["type"] != type_ or payload.get("detail") != detail:
                continue
            seen = signal.get("published_at")
            if seen is not None and seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if when is None or seen is None or abs((when - seen).days) <= SAME_EVENT_DAYS:
                return signal
        return None

    @staticmethod
    def _title(event: Event, item: NewsItem) -> str:
        """"Raised Rs 22 crore (Pre-Series A)", "Appointed Priya Shah as CTO", else the
        headline."""
        if event.kind == "funding" and (event.amount or event.round):
            bits = [f"Raised {event.amount.text}" if event.amount else f"{event.round} round"]
            if event.amount and event.round:
                bits.append(f"({event.round})")
            if event.investors:
                bits.append(f"led by {event.investors}" if "led by" in event.evidence.lower()
                            else f"from {event.investors}")
            return " ".join(bits)[:300]
        if event.kind == "leadership_change" and event.role:
            who = f"{event.person} as " if event.person else "a new "
            return f"Appointed {who}{event.role}"[:300]
        return item.title[:300]

    def _resolve(self, lead: Lead, outcome: UnitOutcome) -> int | None:
        """The organisation a company is: one the project holds keeps its record (A.3);
        a company nobody holds becomes a new organisation when ``create_new``."""
        match = self.store.find_match(lead.normalized())
        if match is not None:
            return match
        if not self.section.create_new:
            with self._lock:
                self.stats["skipped_unknown"] += 1
            return None
        org_id, action, _ = self.store.upsert(lead)
        outcome.new += action == "new"
        outcome.merged += action == "merged"
        return org_id

    @staticmethod
    def _site(company: str, links: list[str]) -> str | None:
        """The company's own site among a story's links: its domain spells the name."""
        words = [w for w in re.findall(r"[a-z0-9]+", company.casefold()) if len(w) > 2]
        if not words:
            return None
        for link in links:
            url = normalize_url(link)
            if not url or not is_vendor_website(url):
                continue
            host = urlsplit(url).hostname or ""
            label = (domain_of(url) or "").split(".")[0].replace("-", "")
            if label and (words[0] in label or label in "".join(words)) and \
                    not any(wire in host for wire in ("globenewswire", "prnewswire",
                                                      "businesswire", "news.google")):
                return url
        return None

    # --- the end ----------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"], stats=dict(self.stats))
