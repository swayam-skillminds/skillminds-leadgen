"""decision_makers as an engine session: enrichment over any lead type's organisations.

No discovery units. In post-discovery (after the finders and hiring, before the
audit: ``post_order``), for each of the run's (or the project's) organisations
not looked into within ``refresh_after_days``, several at once, one request at
a time per site:

* ``team_pages``: its homepage, then up to ``max_pages`` team / leadership /
  about pages it links (robots.txt honoured); people with titles read from
  JSON-LD, microdata, name-and-title cards and "founded by" prose;
* ``registry``: company registries in the country's packs (UK: Companies House
  directors), with the registry's key;
* ``linkedin_search`` (opt-in): search-indexed public profiles;
* ``github`` (opt-in): public members of the GitHub org its site links, whose
  own profiles name a leadership title.

Each person is a ``people`` row of this lead type (name, title, profile URL,
the page it came from, an address published *for them*). A source read in
full is a snapshot: someone it no longer lists is removed. The organisation's
record is never touched (rule 6). With ``email_pattern`` on, named people get
a labelled inferred address when published ones reveal the pattern (see
``email_pattern.py``).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin

from ...core.identity import identity_domain, is_shared_host
from ...core.lead import Lead
from ...core.normalize import domain_of, is_vendor_website, normalize_url
from ...net.http import PrivateAddressError
from ...packs import registries_for
from ...sources.people import github, linkedin
from ...sources.people.roles import employer_of, family_of, person_name, rank, same_company
from ...sources.people.team_pages import Found, emails_on, people_on, team_links
from ..base import LeadTypeResult, LeadTypeSession, UnitOutcome, WorkUnit
from ..toolkit import Kit
from .config import DecisionMakersConfig
from .email_pattern import learn, mx_hosts

log = logging.getLogger(__name__)

KEY = "decision_makers"
MARK = "decision_makers:people"
PATTERN_SOURCE = "email_pattern"
SERP_HOSTS = ("www.google.com", "duckduckgo.com")
#: Source label stored on each person, by how it was found.
SOURCE_OF = {"jsonld": "team_pages", "microdata": "team_pages", "card": "team_pages",
             "prose": "team_pages", "registry": "registry",
             "linkedin_search": "linkedin_search", "github": "github"}


def _email_domain(address: str) -> str | None:
    host = address.rsplit("@", 1)[-1].casefold() if "@" in address else ""
    return host if host and not is_shared_host(host) else None


class DecisionMakersSession(LeadTypeSession):
    lead_type = KEY
    #: Its estimate is sized by every lead type's organisations: it looks into them all.
    post_scope = "run"

    def __init__(self, context: Any, section: DecisionMakersConfig, *, kit: Kit | None = None,
                 clock=None, resolve_mx=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.resolve_mx = resolve_mx
        self._lock = threading.Lock()
        self._mx: dict[str, list[str] | None] = {}
        self.stats: dict[str, int] = {"organisations": 0, "people": 0, "with_people": 0,
                                      "recent_skipped": 0, "pages": 0, "registry": 0,
                                      "linkedin": 0, "github": 0, "inferred": 0,
                                      "removed": 0}
        sources = section.sources
        limiter = self.kit.limiter
        # Only for sources that run: the limiter is the run's, shared with other lead types.
        if sources.github.enabled:
            limiter.configure(github.HOST, sources.github.min_delay, sources.github.max_delay)
        if sources.linkedin_search.enabled:
            for host in SERP_HOSTS:
                limiter.configure(host, sources.linkedin_search.min_delay,
                                  sources.linkedin_search.max_delay)
        fields = getattr(self.config, "included_fields", None)
        fields = set(fields) if fields is not None else {"decision_makers", "emails"}
        #: A run whose fields.include leaves decision_makers out needs nothing from us (A.8).
        self.wanted = "decision_makers" in fields
        self.emails_wanted = section.email_pattern.enabled and "emails" in fields
        self.registries: list[Any] = []
        self.registry_key: str | None = None
        if sources.registry.enabled and self.wanted:
            self.registries = [cls() for cls in registries_for(self.config.country)]
            if self.registries:
                self.registry_key = self._secret(sources.registry.key_secret_name)
                for registry in self.registries:
                    limiter.configure(registry.host, sources.registry.min_delay,
                                      sources.registry.max_delay)
                if not self.registry_key:
                    self.kit.failures.record(
                        "registry", "key_missing",
                        detail=f"{', '.join(r.name for r in self.registries)} needs the secret "
                               f"{sources.registry.key_secret_name} (leadscraper secrets set "
                               f"{sources.registry.key_secret_name}); skipped")
        self.github_token = self._secret(sources.github.token_secret_name) \
            if sources.github.enabled else None

    @staticmethod
    def _secret(name: str) -> str | None:
        if not name:
            return None
        try:
            from ...core.secrets import get_secret

            return get_secret(name)
        except Exception:
            return None

    # --- planning ---------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return []

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        return UnitOutcome(status="failed", error="decision_makers has no discovery units")

    def site_seconds(self) -> float:
        """Expected seconds to look into one organisation."""
        sources = self.section.sources
        seconds = 0.0
        if sources.team_pages.enabled:
            delay = (sources.team_pages.min_delay + sources.team_pages.max_delay) / 2
            seconds += (2 + 0.6 * sources.team_pages.max_pages) * (delay + 0.5)
        if self.registries and self.registry_key:
            seconds += 2 * (sources.registry.min_delay + 0.3)
        if sources.linkedin_search.enabled:
            seconds += (sources.linkedin_search.min_delay
                        + sources.linkedin_search.max_delay) / 2
        return seconds

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        if not self.wanted:
            return 0.0, 0.0
        count = min(float(self.section.max_orgs), expected_new)
        mean = count * self.site_seconds() / max(1, self.section.workers)
        return mean, mean * 0.5

    # --- post-discovery -----------------------------------------------------------------

    def candidates(self) -> list[tuple[int, Lead]]:
        run_id = getattr(self.context, "run_id", None)
        if self.section.scope == "run":
            if run_id is None:
                return []
            rows = self.store.tagged(run_id=run_id)
        else:
            rows = self.store.tagged()
        todo = [(i, lead) for i, lead, _ in rows if lead.business_name]
        force = bool(getattr(self.context, "force_enrich", False))
        if self.section.refresh_after_days and not force:
            cutoff = self.clock() - timedelta(days=self.section.refresh_after_days)
            done = self.store.enriched_times(MARK)
            fresh = [(i, lead) for i, lead in todo if not (done.get(i) and done[i] > cutoff)]
            with self._lock:
                self.stats["recent_skipped"] += len(todo) - len(fresh)
            todo = fresh
        return todo[:self.section.max_orgs]

    def post_discovery(self, control: Any) -> None:
        if self.store is None or getattr(self.context, "dry_run", False):
            return
        if not self.wanted:
            log.info("decision_makers: fields.include leaves out decision_makers; nothing to do")
            return
        try:
            todo = self.candidates()
        except Exception as exc:
            log.exception("decision_makers could not list organisations")
            self.kit.failures.record(KEY, "post_discovery_failed", detail=repr(exc))
            return

        def one(item: tuple[int, Lead]) -> None:
            if control is not None and control.stop_requested:
                return
            self.store.tag_thread(KEY)
            org_id, lead = item
            try:
                self.look_into(org_id, lead, control)
            except Exception as exc:              # contained: one organisation's problem
                log.exception("decision_makers failed on %s", lead.business_name)
                self.kit.failures.record(KEY, "enrich_failed", url=lead.website,
                                         business=lead.business_name, detail=repr(exc)[:300])

        with ThreadPoolExecutor(max_workers=self.section.workers,
                                thread_name_prefix="people") as pool:
            list(pool.map(one, todo))

    def look_into(self, org_id: int, lead: Lead, control: Any = None) -> list[Found]:
        """One organisation's decision-makers, written; what was kept."""
        sources = self.section.sources
        site = normalize_url(lead.website) if lead.website else None
        site = site if site and is_vendor_website(site) else None
        found: list[Found] = []
        page_emails: list[str] = []
        complete: set[str] = set()       # sources read in full: their people are a snapshot
        transient = False
        home_links: list[str] = []
        if sources.team_pages.enabled and site:
            status, people, emails, home_links = self.read_site(site, lead)
            found.extend(people)
            page_emails.extend(emails)
            if status == "complete":
                complete.add("team_pages")
            transient |= status == "retry"
        for registry in self.registries if self.registry_key else []:
            try:
                people, why = registry.find(self.kit.http, lead.business_name,
                                            key=self.registry_key, locality=lead.locality,
                                            address=lead.address)
            except Exception as exc:
                transient = True
                self.kit.failures.record(registry.name, "fetch_failed",
                                         business=lead.business_name,
                                         detail=type(exc).__name__)
                continue
            if why:
                self.kit.failures.record(registry.name, why, business=lead.business_name)
            else:
                complete.add("registry")
            with self._lock:
                self.stats["registry"] += len(people)
            found.extend(people)
        if control is not None and control.stop_requested:
            return []                        # stopping: write nothing half-read
        if sources.linkedin_search.enabled:
            ctx = SimpleNamespace(config=self.config, http=self.kit.http,
                                  limiter=self.kit.limiter, failures=self.kit.failures)
            try:
                people = linkedin.find(ctx, lead.business_name, place=lead.locality or None,
                                       max_results=sources.linkedin_search.max_results)
            except Exception as exc:
                transient = True
                self.kit.failures.record("linkedin_search", "serp_failed",
                                         business=lead.business_name, detail=repr(exc)[:200])
                people = []
            with self._lock:
                self.stats["linkedin"] += len(people)
            found.extend(people)
        if sources.github.enabled and site:
            for login in github.org_logins(home_links)[:2]:
                try:
                    people, why = github.find(self.kit.http, login, domain=domain_of(site),
                                              token=self.github_token,
                                              max_members=sources.github.max_members)
                except Exception as exc:
                    transient = True
                    self.kit.failures.record("github", "fetch_failed",
                                             url=f"https://github.com/{login}",
                                             business=lead.business_name,
                                             detail=type(exc).__name__)
                    continue
                if why:
                    self.kit.failures.record("github", why, url=f"https://github.com/{login}",
                                             business=lead.business_name)
                with self._lock:
                    self.stats["github"] += len(people)
                found.extend(people)
        kept = self.keep(found, lead)
        self.write(org_id, lead, kept, complete, page_emails, site, transient=transient)
        if not transient:
            self.store.mark_enriched(org_id, MARK)
        with self._lock:
            self.stats["organisations"] += 1
            self.stats["people"] += len(kept)
            self.stats["with_people"] += bool(kept)
        return kept

    def keep(self, found: list[Found], lead: Lead | None = None) -> list[Found]:
        """The wanted decision-makers, one per name, owners and chief executives first.

        A title naming another employer ("CIO, Rosendin" on a vendor's testimonials)
        is someone else's decision-maker, and is left out.
        """
        merged: dict[str, Found] = {}
        for person in found:
            if family_of(person.title) not in self.section.roles:
                continue
            employer = employer_of(person.title)
            if employer and lead is not None and not self._same_org(employer, lead):
                continue
            held = merged.get(person.name.casefold())
            if held is None:
                merged[person.name.casefold()] = Found(**vars(person))
                continue
            held.email = held.email or person.email
            held.profile_url = held.profile_url or person.profile_url
        ordered = sorted(merged.values(), key=lambda p: rank(p.title))
        return ordered[:self.section.max_people]

    @staticmethod
    def _same_org(employer: str, lead: Lead) -> bool:
        if same_company(employer, lead.business_name) >= 80:
            return True
        label = (domain_of(lead.website or "") or "").split(".")[0].replace("-", "")
        squashed = "".join(ch for ch in employer.casefold() if ch.isalnum())
        return bool(label) and len(squashed) >= 3 and (squashed in label or label in squashed)

    def read_site(self, site: str, lead: Lead) -> tuple[str, list[Found], list[str], list[str]]:
        """(complete | partial | retry | refused, people, published emails, homepage links)."""
        host = domain_of(site) or ""
        team = self.section.sources.team_pages
        self.kit.limiter.configure(host, team.min_delay, team.max_delay)
        home = self._fetch(site, host, lead)
        if not isinstance(home, tuple):
            return home, [], [], []
        url, text = home
        soup = self.kit.http.soup(text)
        links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]
        pages = [(url, text)]
        status = "complete"
        for page_url in team_links(soup, url, team.max_pages):
            page = self._fetch(page_url, host, lead)
            if isinstance(page, tuple):
                pages.append(page)
            elif status == "complete" or page == "retry":
                status = "retry" if page == "retry" else "partial"
        people: list[Found] = []
        emails: list[str] = []
        for index, (page_url, page_text) in enumerate(pages):
            emails.extend(emails_on(self.kit.http.soup(page_text)))
            # The homepage's people only when the site has no team or about page: on a
            # bigger site they are testimonials, authors and customers, not its leaders.
            if index or len(pages) == 1:
                people.extend(people_on(self.kit.http.soup(page_text), page_url,
                                        company=lead.business_name))
        with self._lock:
            self.stats["pages"] += len(pages)
        return status, people, list(dict.fromkeys(emails)), links

    def _fetch(self, url: str, host: str, lead: Lead):
        """(final url, html), or why not: "refused" (robots, 4xx, internal) | "retry"."""
        if not self.kit.robots.allowed(url, rate_key=host):
            self.kit.failures.record("team_pages", "robots_disallowed", url=url,
                                     business=lead.business_name)
            return "refused"
        try:
            response = self.kit.http.get_once(url, rate_key=host)
        except PrivateAddressError as exc:
            self.kit.failures.record("team_pages", "private_address", url=url,
                                     business=lead.business_name, detail=str(exc)[:200])
            return "refused"
        except Exception as exc:
            self.kit.failures.record("team_pages", "fetch_failed", url=url,
                                     business=lead.business_name, detail=type(exc).__name__)
            return "retry"
        status = int(response.status_code)
        if status != 200:
            return "retry" if status in (429, 500, 502, 503, 504) else "refused"
        return response.url, response.text or ""

    # --- writing ----------------------------------------------------------------------

    def write(self, org_id: int, lead: Lead, kept: list[Found], complete: set[str],
              page_emails: list[str], site: str | None, *, transient: bool = False) -> None:
        by_source: dict[str, list[str]] = {}
        for person in kept:
            source = SOURCE_OF.get(person.how, person.how)
            by_source.setdefault(source, []).append(person.name)
            self.store.add_person(org_id, person.name, title=person.title,
                                  profile_url=person.profile_url, source=source,
                                  source_url=person.page, email=person.email)
        for source in sorted(complete):
            removed = self.store.prune_people(org_id, source, by_source.get(source, []))
            with self._lock:
                self.stats["removed"] += removed
        # A site that didn't answer this time can't tell us its pattern changed: keep the
        # addresses inferred before (as when DNS can't answer).
        if self.emails_wanted and not transient:
            self.infer_emails(org_id, lead, kept, page_emails, site)

    def infer_emails(self, org_id: int, lead: Lead, kept: list[Found], page_emails: list[str],
                     site: str | None) -> None:
        """The labelled email pattern (off by default): see ``email_pattern.py``."""
        published = [*lead.emails, *page_emails, *(p.email for p in kept if p.email)]
        # Named people only (A.2.5): "Front Office" as a record's contact name is nobody.
        people = [p for p in self.store.people(org_id) if person_name(p["name"])]
        names = [p["name"] for p in people] + \
            ([lead.contact_name] if person_name(lead.contact_name or "") else [])
        domain = (identity_domain(site) or domain_of(site)) if site else None
        if domain is None:                    # no site: the domain its own addresses use
            domain = next((d for d in map(_email_domain, published) if d), None)
        learned = learn(published, names, domain)
        record: dict[str, Any] = {"pattern": None, "domain": domain, "reason": learned.reason,
                                  "examples": [], "mx": [],
                                  "checked_at": self.clock().isoformat(timespec="seconds")}
        items: list[tuple[str, str, str, float]] = []
        if learned.pattern is not None:
            mx = self._mx_of(learned.pattern.domain)
            if mx is None:
                return                        # DNS couldn't say: keep what we had
            record.update(pattern=learned.pattern.pattern,
                          examples=list(learned.pattern.examples), mx=mx[:5],
                          reason="" if mx else "no_mx")
            known = {e.casefold() for e in published}
            for person in people if mx else []:
                # Never for people found on GitHub (its AUP), nor for anyone whose own
                # address is published.
                if person.get("source") == "github" or person.get("email"):
                    continue
                address = learned.pattern.apply(person["name"])
                if address and address not in known:
                    items.append(("email", address, person["name"], learned.pattern.confidence))
        count = self.store.set_inferred_contacts(org_id, PATTERN_SOURCE, items)
        self.store.set_attribute(org_id, "email_pattern", record, origin="rule")
        with self._lock:
            self.stats["inferred"] += count

    def _mx_of(self, domain: str) -> list[str] | None:
        with self._lock:
            if domain in self._mx:
                return self._mx[domain]
        hosts = mx_hosts(domain, resolve=self.resolve_mx)
        with self._lock:
            self._mx[domain] = hosts
        return hosts

    # --- the end ----------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=0, units_done=0,
                              stats=dict(self.stats))
