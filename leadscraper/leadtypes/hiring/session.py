"""hiring as an engine session.

Discovery units, each on its own host:

* ``ats``: one per configured board (``greenhouse:acme``) -- one company's postings;
* ``hn_whos_hiring``: the latest monthly HN threads, one ad per company;
* ``board_search`` (opt-in): a web search per place for ATS boards, each then read.

Post-discovery: ``careers_pages`` -- the run's (or project's) organisations'
own sites; an ATS board they link or embed is read, otherwise tech job titles
on the careers page itself count (no date). Every posting that matches the
role families, seniority, recency and location becomes a ``job_posting``
signal, keyed by its URL (seen again next month: the same row, refreshed).
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlsplit

from ...core.lead import Lead
from ...core.normalize import domain_of, is_vendor_website, normalize_url
from ...engine.costs import UnitCost, prior_cost
from ...net.http import PrivateAddressError
from ...sources.jobs import hn
from ...sources.jobs.ats import HOSTS, fetch_board
from ...sources.jobs.base import BoardRef, Posting, find_boards
from ..base import LeadTypeResult, LeadTypeSession, SignalSpec, UnitOutcome, WorkUnit
from ..toolkit import Kit
from .config import HiringConfig
from .roles import (FAMILY_TITLES, classify, country_words, families_in, location_match,
                    role_text, seniority_of)

log = logging.getLogger(__name__)

KEY = "hiring"
SIGNAL = "job_posting"
CAREERS_MARK = "hiring:careers"
SERP_HOSTS = ("www.google.com", "duckduckgo.com")
SEARCH_SITES = ("boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
                "apply.workable.com", "careers.smartrecruiters.com")
SEARCH_WORDS = {"developer": "developer", "qa": "QA", "devops": "DevOps",
                "data": "\"data engineer\"", "it_support": "\"IT support\"",
                "digital_marketing": "\"digital marketing\"", "design": "designer",
                "security": "security", "erp": "ERP", "product": "\"product manager\""}
_CAREERS_LINK = re.compile(r"career|jobs?\b|join[-_ ]?(?:us|our[-_ ]team)|work[-_ ]with[-_ ]us|"
                           r"we.?re[-_ ]hiring|vacanc|openings|opportunit", re.I)

SPEC = SignalSpec(SIGNAL, "Hiring", 0.5, 30.0,
                  "An open tech, IT or digital role: the company is building, so it buys.")


def strength(open_roles: int) -> float:
    """One open role is a signal; several are a stronger one."""
    return round(min(0.95, 0.45 + 0.1 * max(0, open_roles - 1)), 3)


@dataclass
class Match:
    posting: Posting
    family: str
    seniority: str
    #: place | remote | site (on the organisation's own careers page) | ai
    where: str
    roles: list[str]
    interpreted_by: str = ""


class HiringSession(LeadTypeSession):
    lead_type = KEY
    post_scope = "run"
    #: After the finders: a company they already hold gets the signals, untouched.
    discovery_stage = 1

    def __init__(self, context: Any, section: HiringConfig, *, kit: Kit | None = None,
                 clock=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ai = getattr(context, "ai", None)
        self._lock = threading.Lock()
        #: Boards read this run, by key: a board found again (a careers page linking a
        #: configured board) is not fetched twice, but its postings still reach the
        #: organisation that asks.
        self._boards: dict[str, Any] = {}
        self._board_locks: dict[str, threading.Lock] = {}
        self.units_done = 0
        self.stats: dict[str, int] = {"postings_seen": 0, "postings_matched": 0, "boards": 0,
                                      "new": 0, "merged": 0, "signals": 0, "careers_scanned": 0,
                                      "ai_matched": 0}
        sources = section.sources
        limiter = self.kit.limiter
        for host in HOSTS.values():
            limiter.configure(host, sources.ats.min_delay, sources.ats.max_delay)
        limiter.configure(hn.HOST, sources.hn_whos_hiring.min_delay,
                          sources.hn_whos_hiring.max_delay)
        for host in SERP_HOSTS:
            limiter.configure(host, sources.board_search.min_delay, sources.board_search.max_delay)
        self.places = self._places()
        try:
            name = self.config.country_info.name
        except Exception:
            name = ""
        self.country_terms = country_words(self.config.country, name)
        self._units = plan_units(self.config, section)
        skipped = [b for b in section.boards
                   if b.split(":", 1)[0] not in section.sources.ats.kinds]
        if skipped:
            self.kit.failures.record(
                "ats", "source_not_enabled", detail=f"boards skipped: {', '.join(skipped)} "
                "(add their ATS to lead_types.hiring.sources.ats.kinds to read them)")

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
        if unit.source == "ats":
            return frozenset({HOSTS.get(BoardRef.parse(unit.category or "").kind, "ats")})
        if unit.source == "hn_whos_hiring":
            return frozenset({hn.HOST})
        return frozenset({*SERP_HOSTS, *HOSTS.values()})

    def prior(self, unit: WorkUnit) -> UnitCost:
        if unit.source == "ats":
            return prior_cost(4.0, 0.8)
        if unit.source == "hn_whos_hiring":
            months = self.section.sources.hn_whos_hiring.months
            return prior_cost(6.0 * months, 3.0 * months)
        return prior_cost(90.0, 2.0)

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        careers = self.section.sources.careers_pages
        if not careers.enabled:
            return 0.0, 0.0
        sites = min(float(careers.max_orgs), expected_new)
        delay = (careers.min_delay + careers.max_delay) / 2
        mean = sites * 3 * (delay + 0.5) / 6
        return mean, mean * 0.5

    # --- discovery --------------------------------------------------------------------

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        try:
            if unit.source == "ats":
                outcome = self.read_board(BoardRef.parse(unit.category or ""))
            elif unit.source == "hn_whos_hiring":
                outcome = self.read_hn()
            elif unit.source == "board_search":
                outcome = self.search_boards(unit.area or "")
            else:
                outcome = UnitOutcome(status="failed", error=f"unknown source {unit.source}")
        except Exception as exc:  # contained: a source's bug costs its unit
            log.exception("hiring unit %s failed", unit.key)
            self.kit.failures.record(unit.source, "unit_failed", detail=repr(exc))
            outcome = UnitOutcome(status="failed", error=repr(exc))
        with self._lock:
            self.units_done += 1
        return outcome

    def accept(self, posting: Posting, *, check_location: bool = True) -> Match | None:
        """The posting as a match, or None: wrong role, level, age or place."""
        family = classify(posting.title, self.section.roles, self.section.keywords)
        if family is None:
            return None
        level = seniority_of(posting.title)
        if self.section.seniority and level not in self.section.seniority:
            return None
        cutoff = self.clock() - timedelta(days=self.section.posted_within_days)
        if posting.posted_at is not None and posting.posted_at < cutoff:
            return None
        where = "site"
        if check_location:
            where = location_match(posting.location, remote=posting.remote, places=self.places,
                                   country_terms=self.country_terms,
                                   policy=self.section.remote)
            if where is None:
                return None
        return Match(posting, family, level, where, [posting.title])

    def read_board(self, ref: BoardRef, *, org_id: int | None = None,
                   company_url: str | None = None) -> UnitOutcome:
        with self._lock:
            lock = self._board_locks.setdefault(ref.key, threading.Lock())
        with lock:                                   # one fetch per board per run
            result = self._boards.get(ref.key)
            fresh = result is None
            if fresh:
                result = fetch_board(self.kit.http, ref,
                                     limit=self.section.sources.ats.max_postings)
                self._boards[ref.key] = result
        if result.error:
            if fresh:
                self.kit.failures.record(ref.kind, result.error, url=result.page,
                                         detail=result.detail)
            return UnitOutcome(status="failed", error=f"{result.error}: {result.detail}"[:300])
        matches = [m for m in (self.accept(p) for p in result.postings) if m is not None]
        if fresh:
            with self._lock:
                self.stats["boards"] += 1
                self.stats["postings_seen"] += len(result.postings)
        outcome = self.record(result.company, matches, org_id=org_id, company_url=company_url,
                              source=ref.kind, page=result.page)
        outcome.found = len(result.postings)
        return outcome

    def read_hn(self) -> UnitOutcome:
        months = self.section.sources.hn_whos_hiring.months
        outcome = UnitOutcome(status="done")
        try:
            threads = hn.threads(self.kit.http, months)
        except Exception as exc:
            self.kit.failures.record("hn_whos_hiring", "fetch_failed", url=hn.THREADS,
                                     detail=repr(exc))
            return UnitOutcome(status="failed", error=repr(exc))
        if not threads:
            self.kit.failures.record("hn_whos_hiring", "no_listings", url=hn.THREADS,
                                     detail="no 'Who is hiring?' thread found")
        cutoff = self.clock() - timedelta(days=self.section.posted_within_days)
        for thread in threads:
            try:
                comments = hn.comments(self.kit.http, thread)
            except Exception as exc:
                self.kit.failures.record("hn_whos_hiring", "fetch_failed",
                                         url=hn.ITEM.format(id=thread.id), detail=repr(exc))
                continue
            ads = [p for p in (hn.parse_comment(c, thread) for c in comments) if p is not None]
            with self._lock:
                self.stats["postings_seen"] += len(ads)
            unmatched: list[Posting] = []
            for ad in ads:
                if ad.posted_at is not None and ad.posted_at < cutoff:
                    continue
                where = location_match(ad.location, remote=ad.remote, places=self.places,
                                       country_terms=self.country_terms,
                                       policy=self.section.remote)
                if where is None:
                    continue
                found = families_in(role_text(ad.title, ad.text), self.section.roles,
                                    self.section.keywords)
                if not found:
                    unmatched.append(ad)
                    continue
                roles = list(dict.fromkeys(phrase for _, phrase in found))
                match = Match(ad, found[0][0], seniority_of(ad.title), where, roles)
                self._add(outcome, self.record(ad.company, [match], company_url=ad.company_url,
                                               source="hn_whos_hiring", page=ad.url,
                                               open_roles=len(roles)))
            for match in self.interpret(unmatched):
                ad = match.posting
                self._add(outcome, self.record(ad.company, [match], company_url=ad.company_url,
                                               source="hn_whos_hiring", page=ad.url))
            outcome.found += len(ads)
        return outcome

    def interpret(self, postings: list[Posting]) -> list[Match]:
        """AI (``signal_interpretation``) for ads the role rules could not place. Grounded:
        a verdict needs a verbatim quote from the ad; no AI, no verdicts."""
        if not postings or self.ai is None:
            return []
        try:
            from ...ai.features.signals import interpret

            verdicts = interpret(self.ai, [{"id": str(i), "title": p.title, "text": p.text,
                                            "url": p.url, "published_at": p.posted_at}
                                           for i, p in enumerate(postings)],
                                 [SPEC], lead_type=KEY)
        except Exception as exc:                      # AI never stops a run
            log.warning("signal interpretation failed: %s", exc)
            return []
        matches = []
        for index, verdict in verdicts.items():
            posting = postings[int(index)]
            matches.append(Match(posting, "custom", seniority_of(posting.title), "ai",
                                 [verdict.get("summary") or verdict.get("evidence") or ""],
                                 interpreted_by=verdict.get("interpreted_by", "")))
        with self._lock:
            self.stats["ai_matched"] += len(matches)
        return matches

    def search_boards(self, place: str) -> UnitOutcome:
        """A web search for ATS boards mentioning the place and the wanted roles."""
        from ...sources.google_search import SerpClient

        words = " OR ".join(SEARCH_WORDS[f] for f in self.section.roles if f in SEARCH_WORDS)
        sites = " OR ".join(f"site:{s}" for s in SEARCH_SITES)
        query = f'({sites}) "{place}"' + (f" ({words})" if words else "")
        ctx = SimpleNamespace(config=self.config, http=self.kit.http, limiter=self.kit.limiter,
                              failures=self.kit.failures, browser=None)
        results = SerpClient(ctx).search(query, per_page=self.section.sources.board_search
                                         .max_results)
        if not results:
            self.kit.failures.record("board_search", "no_results", query=query, locality=place)
            return UnitOutcome(status="done")
        outcome = UnitOutcome(status="done")
        for ref in self._allowed(find_boards(r.url for r in results)):
            self._add(outcome, self.read_board(ref))
        return outcome

    @staticmethod
    def _add(total: UnitOutcome, part: UnitOutcome) -> None:
        total.found += part.found
        total.new += part.new
        total.merged += part.merged

    # --- writing ----------------------------------------------------------------------

    def record(self, company: str, matches: list[Match], *, org_id: int | None = None,
               company_url: str | None = None, source: str, page: str | None,
               open_roles: int | None = None) -> UnitOutcome:
        """Signals for one company's matching postings; the company resolved to an organisation."""
        outcome = UnitOutcome(status="done")
        count = open_roles if open_roles is not None else len(matches)
        if not matches or count < self.section.min_roles or self.store is None:
            return outcome
        if org_id is None:
            name = (company or "").strip()
            if not name:
                return outcome
            site = normalize_url(company_url) if company_url else None
            lead = Lead(business_name=name, website=site if is_vendor_website(site) else None,
                        locality=self._place_of(matches), sources=[source],
                        source_urls=[page] if page else [])
            try:
                org_id = self._resolve(lead, outcome)
            except Exception as exc:
                self.kit.failures.record(source, "company_rejected", business=name,
                                         url=page, detail=repr(exc))
                return outcome
        added = 0
        for match in matches:
            posting = match.posting
            payload = {"role": posting.title[:300], "roles": match.roles[:20],
                       "family": match.family, "family_title": FAMILY_TITLES.get(match.family),
                       "seniority": match.seniority, "location": posting.location[:300],
                       "match": match.where, "remote": match.where == "remote",
                       "department": posting.department, "board": posting.board,
                       "board_page": page, "source": posting.source, "open_roles": count,
                       "posted_at": posting.posted_at.isoformat() if posting.posted_at else None}
            if match.interpreted_by:
                payload["interpreted_by"] = match.interpreted_by
            title = ", ".join(r for r in match.roles[:6] if r) or posting.title
            added += bool(self.store.add_signal(
                org_id, SIGNAL, key=posting.url, title=title[:300], payload=payload,
                strength=strength(count), published_at=posting.posted_at,
                source_url=posting.url, source=posting.source or source))
        with self._lock:
            self.stats["postings_matched"] += len(matches)
            self.stats["signals"] += added
            self.stats["new"] += outcome.new
            self.stats["merged"] += outcome.merged
        outcome.found = len(matches)
        return outcome

    def _resolve(self, lead: Lead, outcome: UnitOutcome) -> int:
        """The organisation a company is. One the project already holds gets the signals
        and is left as it is -- another lead type's record is never rewritten (A.3);
        only a company nobody holds yet becomes a new organisation."""
        match = self.store.find_match(lead.normalized())
        if match is not None:
            return match
        org_id, action, _ = self.store.upsert(lead)
        outcome.new += action == "new"
        outcome.merged += action == "merged"
        return org_id

    def _place_of(self, matches: list[Match]) -> str:
        for match in matches:
            text = match.posting.location.casefold()
            for place in self.places:
                if re.search(rf"(?<!\w){re.escape(place.casefold())}(?!\w)", text):
                    return place
        return ""

    # --- post-discovery: careers pages --------------------------------------------------

    def candidates(self) -> list[tuple[int, Lead]]:
        careers = self.section.sources.careers_pages
        run_id = getattr(self.context, "run_id", None)
        if careers.audit == "run":
            if run_id is None:
                return []
            rows = self.store.tagged(run_id=run_id)
        else:
            rows = self.store.tagged()
        todo = [(i, lead) for i, lead, _ in rows
                if lead.website and is_vendor_website(normalize_url(lead.website))]
        force = bool(getattr(self.context, "force_enrich", False))
        if careers.rescan_after_days and not force:
            cutoff = self.clock() - timedelta(days=careers.rescan_after_days)
            scanned = self.store.enriched_times(CAREERS_MARK)
            todo = [(i, lead) for i, lead in todo if not (scanned.get(i) and scanned[i] > cutoff)]
        return todo[:careers.max_orgs]

    def post_discovery(self, control: Any) -> None:
        if (self.store is None or getattr(self.context, "dry_run", False)
                or not self.section.sources.careers_pages.enabled):
            return
        try:
            todo = self.candidates()
        except Exception as exc:
            log.exception("hiring could not list organisations to scan")
            self.kit.failures.record("careers_pages", "post_discovery_failed", detail=repr(exc))
            return

        def one(item: tuple[int, Lead]) -> None:
            if control is not None and control.stop_requested:
                return
            self.store.tag_thread(KEY)
            org_id, lead = item
            try:
                self.scan_careers(org_id, lead)
            except Exception as exc:
                log.exception("careers scan of %s failed", lead.business_name)
                self.kit.failures.record("careers_pages", "fetch_failed", url=lead.website,
                                         business=lead.business_name, detail=repr(exc))

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="careers") as pool:
            list(pool.map(one, todo))

    def scan_careers(self, org_id: int, lead: Lead) -> None:
        """One organisation's site: ATS boards it links, else job titles on its careers page."""
        careers = self.section.sources.careers_pages
        site = normalize_url(lead.website)
        host = domain_of(site) or ""
        self.kit.limiter.configure(host, careers.min_delay, careers.max_delay)
        home = self._fetch(lead, site, host)
        with self._lock:
            self.stats["careers_scanned"] += 1
        if home is None:
            return
        page_url, soup = home
        boards = self._allowed(find_boards(_urls(soup, page_url)))
        careers_url = None
        if not boards:
            careers_url = _careers_link(soup, page_url)
            if careers_url:
                page = self._fetch(lead, careers_url, host)
                if page is not None:
                    careers_url, soup = page
                    boards = self._allowed(find_boards(_urls(soup, careers_url)))
                else:
                    careers_url = None
        outcomes = [self.read_board(ref, org_id=org_id, company_url=site) for ref in boards]
        if not (boards or not careers_url):
            matches = [m for m in (self.accept(p, check_location=False)
                                   for p in _titles(soup, careers_url, lead.business_name, host))
                       if m is not None]
            self.record(lead.business_name, matches, org_id=org_id, source="careers_pages",
                        page=careers_url)
        # Scanned: not again within rescan_after_days -- unless a board failed to answer.
        if all(o.status != "failed" for o in outcomes):
            self.store.mark_enriched(org_id, CAREERS_MARK)

    def _allowed(self, refs: list[BoardRef]) -> list[BoardRef]:
        """Boards on the ATSs this run may read (``sources.ats.kinds``)."""
        kinds = set(self.section.sources.ats.kinds) if self.section.sources.ats.enabled \
            else set()
        return [ref for ref in refs if ref.kind in kinds]

    def _fetch(self, lead: Lead, url: str, host: str):
        if not self.kit.robots.allowed(url, rate_key=host):
            self.kit.failures.record("careers_pages", "robots_disallowed", url=url,
                                     business=lead.business_name)
            return None
        try:
            response = self.kit.http.get_once(url, rate_key=host)
        except PrivateAddressError as exc:
            self.kit.failures.record("careers_pages", "private_address", url=url,
                                     business=lead.business_name, detail=str(exc)[:200])
            return None
        except Exception as exc:
            self.kit.failures.record("careers_pages", "fetch_failed", url=url,
                                     business=lead.business_name, detail=type(exc).__name__)
            return None
        if int(response.status_code) != 200:
            return None
        return response.url, self.kit.http.soup(response.text or "")

    # --- the end ----------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"], stats=dict(self.stats))


def plan_units(config: Any, section: HiringConfig) -> list[WorkUnit]:
    sources = section.sources
    units: list[WorkUnit] = []
    if sources.ats.enabled:
        units.extend(WorkUnit(KEY, "ats", None, board, "run") for board in section.boards
                     if board.split(":", 1)[0] in sources.ats.kinds)
    if sources.hn_whos_hiring.enabled:
        units.append(WorkUnit(KEY, "hn_whos_hiring", None, None, "run"))
    if sources.board_search.enabled:
        places = list(config.get("places") or []) or [getattr(config, "city", "")]
        units.extend(WorkUnit(KEY, "board_search", p, None, "locality") for p in places if p)
    return units


def _urls(soup, base: str) -> list[str]:
    out = []
    for tag in soup.find_all(["a", "iframe", "script", "link"]):
        ref = tag.get("href") or tag.get("src")
        if ref:
            out.append(urljoin(base, ref))
    return out


def _careers_link(soup, base: str) -> str | None:
    home = domain_of(base)
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base, anchor["href"])
        if urlsplit(url).scheme not in ("http", "https") or domain_of(url) != home:
            continue
        if _CAREERS_LINK.search(f"{urlsplit(url).path} {anchor.get_text(' ', strip=True)}"):
            return url.split("#", 1)[0]
    return None


#: A job title names a person's role; "SEO Services" or "IT Support" in a menu does not.
_ROLE_NOUN = re.compile(r"\b(?:engineer|developer|programmer|architect|tester|sdet|analyst|"
                        r"scientist|administrator|admin|executive|specialist|manager|lead|"
                        r"head|director|consultant|designer|technician|officer|associate|"
                        r"intern|trainee|coordinator|marketer|strategist|writer|expert|"
                        r"professional|support engineer|devops|sre)s?\b", re.I)
_NO_OPENINGS = re.compile(r"\bno (?:current |open )?(?:openings|vacancies|open positions|"
                          r"positions open|open roles|jobs)\b|not hiring\b|"
                          r"no positions (?:available|at the moment)", re.I)


def _titles(soup, page: str, company: str, host: str) -> list[Posting]:
    """Job titles listed on a careers page, no date known.

    Only the page's own content counts: navigation, header, footer, sidebars
    and forms name services and pages, not openings. A title must name a role
    ("Senior PHP Developer", not "SEO Services"); a page that says it has no
    openings has none.
    """
    for tag in soup.find_all(["nav", "header", "footer", "aside", "form", "script", "style",
                              "noscript"]):
        tag.decompose()
    if _NO_OPENINGS.search(soup.get_text(" ", strip=True)):
        return []
    seen: set[str] = set()
    out: list[Posting] = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "li", "a", "td", "dt"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not text or len(text) > 120 or len(text.split()) > 12 or text.casefold() in seen \
                or not _ROLE_NOUN.search(text):
            continue
        seen.add(text.casefold())
        url = page
        # The posting's own page: the tag's link, or the one link inside it with the same text.
        anchor = tag if tag.name == "a" else tag.find("a", href=True)
        if anchor is not None and anchor.get("href") and \
                " ".join(anchor.get_text(" ", strip=True).split()) == text:
            target = urljoin(page, anchor["href"])
            if domain_of(target) == host:
                url = target.split("#", 1)[0]
        out.append(Posting(title=text, url=url if url != page else f"{page}#{text[:60]}",
                           company=company, source="careers_pages",
                           board=f"careers:{host}"))
        if len(out) >= 200:
            break
    return out
