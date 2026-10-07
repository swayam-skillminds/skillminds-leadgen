"""Per-lead website enrichment.

Once any source gives us a website, this fetches the site's Contact/About
pages with requests + BeautifulSoup and harvests the emails, phones and social
links that never appear on Maps or the directories. In practice this is where
most usable email addresses come from.
"""

from __future__ import annotations

import logging
import re
import threading
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.lead import Lead
from ..core.normalize import (
    domain_of, extract_emails, extract_facebook, extract_instagram, extract_linkedin,
    extract_phones, extract_whatsapp, is_vendor_website, normalize_url,
)
from ..core.parsing import visible_text
from ..net.ratelimit import BlockedError
from ..packs.hospitality.venues import enrich_from_text
from .base import Enricher

log = logging.getLogger(__name__)

# Link text / hrefs that lead to a page carrying contact details.
_CONTACT_HINTS = re.compile(
    r"\b(contact|reach[\-\s]?us|get[\-\s]?in[\-\s]?touch|about|enquiry|enquire|"
    r"inquiry|connect|support|book[\-\s]?now|franchise)\b", re.I
)


class WebsiteEnricher(Enricher):
    name = "website"
    priority = 20
    fills = frozenset({"emails", "phones", "socials", "address", "prices"})

    def rate_keys(self) -> list[str]:
        return ["website"]

    def targets(self, store) -> list:
        """Leads that have a website but no email yet."""
        return store.website_enrichment_targets()

    def target_host(self, lead: Lead) -> str:
        """Each vendor site is its own host: enrichment fetches several at once."""
        return domain_of(lead.website or "") or "website"

    #: Set when a page fetched fine but yielded no contacts. The pipeline
    #: drains this and hands the text to the AI fallback extractor. Per
    #: thread: sites are enriched concurrently.
    _pending = threading.local()

    @property
    def pending_text(self) -> str | None:
        return getattr(self._pending, "text", None)

    @pending_text.setter
    def pending_text(self, value: str | None) -> None:
        self._pending.text = value

    @property
    def pending_source(self) -> str | None:
        """The original page text + hrefs behind `pending_text`: what AI answers are
        grounded in (the Markdown handed to the model is a shortened view of it)."""
        return getattr(self._pending, "source", None)

    @pending_source.setter
    def pending_source(self, value: str | None) -> None:
        self._pending.source = value

    def _robots(self):
        """One robots.txt cache per enricher (per run); ``runtime.robots_txt`` toggles it."""
        robots = getattr(self, "_robots_cache", None)
        if robots is None:
            from ..net.robots import RobotsCache

            enabled = self.ctx.config.get("runtime.robots_txt")
            robots = RobotsCache(self.ctx.http, enabled=enabled is not False)
            self._robots_cache = robots
        return robots

    def enrich(self, lead: Lead) -> Lead | None:
        if not lead.website or not is_vendor_website(lead.website):
            return None

        host = domain_of(lead.website) or "website"
        # Pace per target domain, not globally: unrelated vendor sites don't
        # need to queue behind each other.
        min_delay, max_delay = self.ctx.config.delays(self.name)
        self.ctx.limiter.configure(host, min_delay, max_delay)

        max_pages = int(self.cfg.get("max_pages_per_site", 4))
        pages_to_try = [lead.website]
        harvested = Lead(business_name=lead.business_name)
        collected_text: list[str] = []
        collected_pages: list[tuple[str, str]] = []
        fetched = 0
        self.pending_text = None
        self.pending_source = None

        robots = self._robots()
        while pages_to_try and fetched < max_pages:
            url = pages_to_try.pop(0)
            if not robots.allowed(url, rate_key=host):
                self.fail("robots_disallowed", url=url, business=lead.business_name,
                          detail="robots.txt disallows this page")
                continue
            try:
                response = self.ctx.http.get(url, rate_key=host)
            except BlockedError as exc:
                self.fail("blocked", url=url, business=lead.business_name, detail=str(exc))
                break
            except Exception as exc:
                self.fail("fetch_failed", url=url, business=lead.business_name, detail=repr(exc))
                continue

            fetched += 1
            soup = self.ctx.http.soup(response.text)
            page_text = visible_text(soup, limit=25_000)
            collected_text.append(page_text)
            collected_pages.append((response.url, response.text))
            self._harvest(soup, response.text, response.url, harvested, page_text)

            # Queue contact-ish pages discovered on the homepage.
            if fetched == 1:
                pages_to_try.extend(self._contact_links(soup, response.url, host))

        if fetched == 0:
            return None
        venue_found = False
        if self.ctx.config.is_venue_run:
            # The venue's own site is the most honest description of it: rooms,
            # pool, event policy and, often, a tariff page.
            enrich_from_text(harvested, " ".join(collected_text)[:40_000], price_source="website",
                             currency=self.currency)
            venue_found = bool(harvested.amenities or harvested.bedrooms or harvested.price_min
                               or harvested.event_friendly is not None)
        if not (harvested.emails or harvested.phones or harvested.instagram
                or harvested.facebook or harvested.whatsapp or harvested.linkedin):
            if venue_found:
                harvested.sources = [self.name]
                return harvested
            self.fail("nothing_found", url=lead.website, business=lead.business_name,
                      detail=f"fetched {fetched} page(s), no contact details present")
            # Hand the pages to the AI fallback rather than giving up: this is
            # exactly the case where a redesign broke the regexes. The model
            # gets token-lean Markdown (contact links kept); its answers are
            # grounded in the original text.
            self._queue_for_ai(collected_pages, collected_text)
            return None

        harvested.sources = [self.name]
        return harvested

    def _queue_for_ai(self, pages: list[tuple[str, str]], texts: list[str]) -> None:
        try:
            from ..ai.pageprep import prepare

            prepared = [prepare(html, url) for url, html in pages]
            markdown = "\n\n".join(p.markdown for p in prepared if p.markdown).strip()
            source = " ".join(p.source_text for p in prepared).strip()
        except Exception as exc:          # preparation is an optimisation, never a failure
            log.debug("page preparation failed (raw text used): %s", exc)
            markdown = source = " ".join(texts).strip()
        self.pending_text = markdown[:8000] if markdown else None
        self.pending_source = source or None

    # --- helpers ----------------------------------------------------------

    def _harvest(self, soup: BeautifulSoup, raw_html: str, page_url: str,
                 into: Lead, text: str) -> None:

        # mailto:/tel: links are the highest-confidence source on a page.
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.lower().startswith("mailto:"):
                into.emails.extend(extract_emails(href[7:].split("?")[0]))
            elif href.lower().startswith("tel:"):
                into.phones.extend(extract_phones(href[4:], self.region))

        into.emails.extend(extract_emails(text))
        into.phones.extend(extract_phones(text, self.region))

        # Socials: check raw HTML so links inside scripts/attributes count too.
        into.instagram = into.instagram or extract_instagram(raw_html)
        into.facebook = into.facebook or extract_facebook(raw_html)
        into.linkedin = into.linkedin or extract_linkedin(raw_html)
        into.whatsapp = into.whatsapp or extract_whatsapp(raw_html, self.region)

        if not into.address:
            config = self.ctx.config
            into.address = self._address(soup, text, config.city, config.admin_area,
                                         config.country_info.postal_code)

        into.emails = list(dict.fromkeys(into.emails))[:5]
        into.phones = list(dict.fromkeys(into.phones))[:5]
        if page_url:
            clean = normalize_url(page_url)
            if clean and clean not in into.source_urls:
                into.source_urls.append(clean)

    @staticmethod
    def _contact_links(soup: BeautifulSoup, base_url: str, host: str) -> list[str]:
        """Contact/About URLs on the same domain, best candidates first."""
        scored: list[tuple[int, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            label = anchor.get_text(" ", strip=True)
            if not (_CONTACT_HINTS.search(href) or _CONTACT_HINTS.search(label)):
                continue
            absolute = normalize_url(urljoin(base_url, href))
            if not absolute or domain_of(absolute) != host:
                continue
            # "Contact" beats "About" when both exist.
            weight = 0 if re.search(r"contact", href + label, re.I) else 1
            if absolute not in [u for _, u in scored]:
                scored.append((weight, absolute))
        scored.sort(key=lambda pair: pair[0])
        return [url for _, url in scored][:3]

    @staticmethod
    def _address(soup: BeautifulSoup, text: str, city: str = "", admin_area: str = "",
                 postal_code: str = r"\d{6}") -> str:
        tag = soup.find("address")
        if tag:
            value = " ".join(tag.get_text(" ", strip=True).split())
            if len(value) > 10:
                return value[:300]
        # An address names the city or state and ends in the country's postal
        # code (a 6-digit PIN in India, a ZIP in the US).
        places = "|".join(re.escape(p) for p in dict.fromkeys([city, admin_area]) if p)
        if not places:
            return ""
        match = re.search(r"([A-Z][^.\n]{15,150}?\b(?:" + places + r")\b[^.\n]{0,40}?\b(?:"
                          + postal_code + r")\b)", text)
        return " ".join(match.group(1).split())[:300] if match else ""
