"""``ai_directory``: the generic AI-assisted extractor for approved candidate sources.

``source_discovery`` proposes directories; a user approves some
(``candidate_sources.status = approved``). In a run whose country matches (and
place / category, when the candidate names one) this source reads each one:

* the listing page, plus the next page when the site links one (``rel=next`` or
  a "Next" link) -- the second page validates generated selectors;
* records come from :class:`~leadscraper.ai.selectors.SelfHealingExtractor`:
  stored selectors at no AI cost, generated (and validated) selectors on first
  contact, regenerated once after a redesign, per-page AI as the last resort;
* every record is grounded on its page; later pages follow the stored
  selectors' ``next_page`` link up to ``max_pages``.

Pages are fetched politely (per-host pacing, robots.txt). Runs only with the
``source_discovery`` feature on.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..core.lead import Lead
from ..net.ratelimit import BlockedError
from .base import Source

log = logging.getLogger(__name__)

_NEXT_TEXT = re.compile(r"^\s*(next|next page|›|»|>|older)\s*[›»>]?\s*$", re.I)


def find_next(html: str, url: str) -> str | None:
    """A listing page's "next page" link, when it has an obvious one."""
    soup = BeautifulSoup(html or "", "lxml")
    link = soup.find("a", rel=lambda v: v and "next" in (v if isinstance(v, list) else [v]))
    if link is None:
        link = next((a for a in soup.find_all("a", href=True)
                     if _NEXT_TEXT.match(a.get_text(" ", strip=True) or "")), None)
    if link is None or not link.get("href"):
        return None
    target = urljoin(url, str(link["href"]))
    return target if target != url and target.startswith("http") else None


class AIDirectorySource(Source):
    name = "ai_directory"
    priority = 95
    scope = "run"
    ai_task = "ai_directory"

    def rate_keys(self) -> list[str]:
        return ["ai_directory"]

    def hosts(self) -> list[str]:
        return [c["host"] for c in self.candidates()] or ["ai_directory"]

    # --- which candidates apply -----------------------------------------------------

    def candidates(self) -> list[dict]:
        cached = getattr(self, "_candidates", None)
        if cached is not None:
            return cached
        store = self.ctx.store
        engine = getattr(store, "engine", None)
        rows: list[dict] = []
        if engine is not None:
            from sqlalchemy import select

            from ..db.models import CandidateSource

            table = CandidateSource.__table__
            with engine.connect() as conn:
                rows = [dict(r) for r in conn.execute(select(table).where(
                    table.c.workspace_id == store.workspace_id, table.c.status == "approved")
                    .order_by(table.c.id)).mappings()]
        country = (self.ctx.config.country or "").upper()
        places = {p.lower() for p in (self.ctx.config.get("places") or [])} | \
            {str(self.ctx.config.city or "").lower()}
        categories = {c.name.lower() for c in self.ctx.config.categories}
        chosen = []
        for row in rows:
            if row.get("country") and row["country"].upper() != country:
                continue
            if row.get("place") and row["place"].lower() not in places:
                continue
            if row.get("category") and row["category"].lower() not in categories:
                continue
            chosen.append(row)
        self._candidates = chosen
        return chosen

    def _robots(self):
        robots = getattr(self, "_robots_cache", None)
        if robots is None:
            from ..net.robots import RobotsCache

            enabled = self.ctx.config.get("runtime.robots_txt")
            robots = RobotsCache(self.ctx.http, enabled=enabled is not False)
            self._robots_cache = robots
        return robots

    def _fetch(self, url: str, host: str, business: str = ""):
        from ..ai.features.source_discovery import excluded
        from ..ai.selectors import Page

        if excluded(url):
            self.fail("ai_excluded_host", url=url, detail="excluded by policy (A.2.4)")
            return None
        if not self._robots().allowed(url, rate_key=host):
            self.fail("robots_disallowed", url=url, detail="robots.txt disallows this page")
            return None
        try:
            response = self.ctx.http.get(url, rate_key=host)
        except BlockedError as exc:
            self.fail("blocked", url=url, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("fetch_failed", url=url, detail=repr(exc)[:300])
            return None
        final = response.url or url
        if excluded(final):                   # a redirect into an excluded site
            self.fail("ai_excluded_host", url=final, detail="redirected to an excluded site")
            return None
        return Page(url=final, html=response.text)

    def _extractor(self):
        extractor = getattr(self, "_self_healing", None)
        if extractor is None:
            from ..ai.selectors import DbSelectorStore, MemorySelectorStore, SelfHealingExtractor

            store = self.ctx.store
            selectors = (DbSelectorStore(store.engine, store.workspace_id)
                         if getattr(store, "engine", None) is not None else MemorySelectorStore())
            extractor = SelfHealingExtractor(getattr(self.ctx, "ai", None), selectors,
                                             failures=self.ctx.failures, region=self.region,
                                             source=self.name)
            self._self_healing = extractor
        return extractor

    # --- discovery -----------------------------------------------------------------------

    def discover(self, locality, category) -> Iterator[Lead]:
        client = getattr(self.ctx, "ai", None)
        candidates = self.candidates()
        if not candidates:
            return
        if client is None or not client.wants("ai_directory"):
            return
        from ..ai.selectors import next_page

        max_pages = int(self.cfg.get("max_pages", 3))
        for candidate in candidates:
            host = candidate["host"] or (urlparse(candidate["url"]).hostname or "")
            min_delay, max_delay = self.ctx.config.delays(self.name)
            self.ctx.limiter.configure(host, max(min_delay, 3.0), max(max_delay, 8.0))
            first = self._fetch(candidate["url"], host)
            if first is None:
                continue
            following = find_next(first.html, first.url)
            second = self._fetch(following, host) if following else None
            extractor = self._extractor()
            page_type = candidate.get("page_type") or "listing"
            outcome = extractor.extract(host, page_type, first, second)
            log.info("ai_directory %s: %d record(s) via %s", host, len(outcome.records),
                     outcome.path)
            yield from self._leads(outcome.records, candidate, locality)
            pages_done = 1
            page = second
            while page is not None and pages_done < max_pages:
                result = extractor.extract(host, page_type, page)
                yield from self._leads(result.records, candidate, locality)
                pages_done += 1
                stored = extractor.store.get(host, page_type)
                link = next_page(stored.selectors, page.html, page.url) if stored else None
                link = link or find_next(page.html, page.url)
                page = self._fetch(link, host) if link and pages_done < max_pages else None
            self._touch(candidate)

    def _leads(self, records: list[dict], candidate: dict, locality) -> Iterator[Lead]:
        categories = {c.name.lower(): c.name for c in self.ctx.config.categories}
        category = categories.get((candidate.get("category") or "").lower(), "")
        if not category and len(categories) == 1:
            category = next(iter(categories.values()))
        for record in records:
            lead = self.make_lead(
                business_name=record["name"], category=category,
                locality=(getattr(locality, "name", None)
                          or self._locality_for(record.get("address") or "")),
                phones=record.get("phones") or [], emails=record.get("emails") or [],
                address=record.get("address") or "", website=record.get("website"),
                source_urls=[u for u in (record.get("profile_url"), record.get("source_url"))
                             if u],
                notes=[f"listed by {candidate['name']} (approved candidate source)"])
            yield lead

    def _locality_for(self, text: str) -> str:
        localities = list(self.ctx.config.localities)
        lowered = text.lower()
        for loc in localities:
            if loc.name.lower() in lowered:
                return loc.name
        return localities[0].name if len(localities) == 1 else ""

    def _touch(self, candidate: dict) -> None:
        engine = getattr(self.ctx.store, "engine", None)
        if engine is None or candidate.get("id") is None:
            return
        from sqlalchemy import update

        from ..db.models import CandidateSource

        table = CandidateSource.__table__
        try:
            with engine.begin() as conn:
                conn.execute(update(table).where(table.c.id == candidate["id"])
                             .values(last_run_at=datetime.now(timezone.utc)))
        except Exception as exc:
            log.debug("could not mark candidate %s run: %s", candidate.get("id"), exc)
