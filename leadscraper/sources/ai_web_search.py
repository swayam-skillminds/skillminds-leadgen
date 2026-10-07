"""``ai_web_search``: an AI provider's web search as a discovery source (Phase 5.4).

Runs only with the ``web_search_discovery`` feature on and a provider whose
web search is available (Anthropic, OpenAI, Gemini). For each locality x
category, the model searches and lists businesses with the page it found each
one on. Then, for every one of them, *we* check:

1. the page's site is among those the provider's search actually returned (a
   URL the model made up is dropped);
2. we fetch the page ourselves (paced per host, robots.txt honoured);
3. the business's name appears on that page;
4. each contact detail appears verbatim on that page (``ai.grounding``).

Only then is a lead yielded, with the fetched URL as its source URL. Anything
that fails a check is recorded as a failure with its reason.
"""

from __future__ import annotations

import logging
from typing import Iterator
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..core.lead import Lead
from ..core.normalize import domain_of
from ..net.ratelimit import BlockedError
from .base import Source

log = logging.getLogger(__name__)

SYSTEM = (
    "You find local businesses with web search for a B2B lead list. Search for "
    "businesses of the given category in the given area, and list real, currently "
    "operating businesses you found on the web. For each: its name exactly as published, "
    "`source_url` (the page where you found it -- its own website or a listing page -- "
    "copied from your search results), and its website, phone, email and address only "
    "if that page shows them. Never invent a business, URL or contact detail. Return ONLY "
    "JSON: {\"items\": [...]}."
)


class Found(BaseModel):
    name: str
    source_url: str = Field(..., description="The page the business was found on.")
    website: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None


class Findings(BaseModel):
    items: list[Found] = Field(default_factory=list)


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def returned_by_search(url: str, sources: list[str]) -> bool:
    """The URL's site is among the provider's search results (URLs or domains)."""
    host = _host(url)
    if not host:
        return False
    for source in sources:
        other = _host(source) if "://" in source else source.lower().removeprefix("www.")
        if other and (host == other or host.endswith("." + other)):
            return True
    return False


class AIWebSearchSource(Source):
    name = "ai_web_search"
    priority = 90
    #: Selected only when this AI task is switched on (planning.select_sources).
    ai_task = "web_search"

    def rate_keys(self) -> list[str]:
        return ["ai_web_search"]

    def hosts(self) -> list[str]:
        # The provider's search, not a scraped host: runs beside every other source.
        return ["ai_web_search"]

    def _robots(self):
        robots = getattr(self, "_robots_cache", None)
        if robots is None:
            from ..net.robots import RobotsCache

            enabled = self.ctx.config.get("runtime.robots_txt")
            robots = RobotsCache(self.ctx.http, enabled=enabled is not False)
            self._robots_cache = robots
        return robots

    def discover(self, locality, category) -> Iterator[Lead]:
        client = getattr(self.ctx, "ai", None)
        if client is None or not client.uses("web_search"):
            return
        if not client.can_search():
            self.fail("ai_no_web_search", detail="no configured AI provider offers web search")
            return
        area = getattr(locality, "name", None) or self.ctx.config.city
        what = getattr(category, "name", None) or "businesses"
        place = f"{area}, {self.ctx.config.city}" if area != self.ctx.config.city else area
        result = client.call(
            "web_search", system=SYSTEM,
            user=f"Category: {what}\nArea: {place}\nCountry: {self.ctx.config.country}\n"
                 f"List at most {int(self.cfg.get('max_results', 10))}.",
            output=Findings, web_search=True,
            max_searches=int(self.cfg.get("max_searches", 3)), max_tokens=4000)
        if result is None:
            return
        for item in result.data.items[:int(self.cfg.get("max_results", 10))]:
            lead = self._verify(item, result.sources, locality, category)
            if lead is not None:
                yield lead

    def _verify(self, item: Found, sources: list[str], locality, category) -> Lead | None:
        from ..ai.grounding import SourceText, ground_contacts
        from ..ai.pageprep import prepare

        url = (item.source_url or "").strip()
        if urlparse(url).scheme not in ("http", "https"):
            self.fail("ai_unsourced", business=item.name, detail="no http(s) source URL")
            return None
        if not returned_by_search(url, sources):
            self.fail("ai_unsourced", url=url, business=item.name,
                      detail="the source URL is not among the provider's search results")
            return None
        host = domain_of(url) or _host(url)
        min_delay, max_delay = self.ctx.config.delays("website")
        self.ctx.limiter.configure(host, min_delay, max_delay)
        if not self._robots().allowed(url, rate_key=host):
            self.fail("robots_disallowed", url=url, business=item.name,
                      detail="robots.txt disallows the source page")
            return None
        try:
            response = self.ctx.http.get(url, rate_key=host)
        except BlockedError as exc:
            self.fail("blocked", url=url, business=item.name, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("fetch_failed", url=url, business=item.name, detail=repr(exc)[:300])
            return None
        page = prepare(response.text, response.url or url)
        source = SourceText(page.source_text, self.region)
        if not source.has_text(item.name):
            self.fail("ai_ungrounded", url=url, business=item.name,
                      detail="the business's name is not on the fetched page")
            return None
        grounded = ground_contacts({"phones": item.phone, "emails": item.email,
                                    "address": item.address, "website": item.website}, source)
        if grounded.dropped:
            self.fail("ai_ungrounded_dropped", url=url, business=item.name,
                      detail="; ".join(grounded.dropped)[:400])
        website = grounded.website
        if website is None and item.website and _host(item.website) == _host(url):
            website = item.website                  # the page *is* the business's site
        lead = self.make_lead(
            business_name=item.name.strip()[:300],
            category=getattr(category, "name", "") or "",
            locality=getattr(locality, "name", "") or "",
            phones=grounded.phones, emails=grounded.emails, address=grounded.address,
            website=website, source_urls=[response.url or url],
            notes=["found by AI web search; name and contacts checked on the fetched page"])
        return self.assign_locality(lead, locality)
