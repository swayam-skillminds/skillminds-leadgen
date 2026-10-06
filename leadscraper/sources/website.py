"""Per-lead website enrichment.

Once any source gives us a website, this fetches the site's Contact/About
pages with requests + BeautifulSoup and harvests the emails, phones and social
links that never appear on Maps or the directories. In practice this is where
most usable email addresses come from.
"""

from __future__ import annotations

import logging
import re
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

    def rate_keys(self) -> list[str]:
        return ["website"]

    def targets(self, store) -> list:
        """Leads that have a website but no email yet."""
        return store.website_enrichment_targets()

    #: Set when a page fetched fine but yielded no contacts. The pipeline
    #: drains this and hands the text to the AI fallback extractor.
    pending_text: str | None = None

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
        fetched = 0
        self.pending_text = None

        while pages_to_try and fetched < max_pages:
            url = pages_to_try.pop(0)
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
            enrich_from_text(harvested, " ".join(collected_text)[:40_000], price_source="website")
            venue_found = bool(harvested.amenities or harvested.bedrooms or harvested.price_min
                               or harvested.event_friendly is not None)
        if not (harvested.emails or harvested.phones or harvested.instagram
                or harvested.facebook or harvested.whatsapp or harvested.linkedin):
            if venue_found:
                harvested.sources = [self.name]
                return harvested
            self.fail("nothing_found", url=lead.website, business=lead.business_name,
                      detail=f"fetched {fetched} page(s), no contact details present")
            # Hand the raw text to the AI fallback rather than giving up: this
            # is exactly the case where a redesign broke the regexes.
            joined = " ".join(collected_text).strip()
            self.pending_text = joined[:8000] if joined else None
            return None

        harvested.sources = [self.name]
        return harvested

    # --- helpers ----------------------------------------------------------

    def _harvest(self, soup: BeautifulSoup, raw_html: str, page_url: str,
                 into: Lead, text: str) -> None:

        # mailto:/tel: links are the highest-confidence source on a page.
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.lower().startswith("mailto:"):
                into.emails.extend(extract_emails(href[7:].split("?")[0]))
            elif href.lower().startswith("tel:"):
                into.phones.extend(extract_phones(href[4:]))

        into.emails.extend(extract_emails(text))
        into.phones.extend(extract_phones(text))

        # Socials: check raw HTML so links inside scripts/attributes count too.
        into.instagram = into.instagram or extract_instagram(raw_html)
        into.facebook = into.facebook or extract_facebook(raw_html)
        into.linkedin = into.linkedin or extract_linkedin(raw_html)
        into.whatsapp = into.whatsapp or extract_whatsapp(raw_html)

        if not into.address:
            into.address = self._address(soup, text, self.ctx.config.city)

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
    def _address(soup: BeautifulSoup, text: str, city: str = "Mumbai") -> str:
        tag = soup.find("address")
        if tag:
            value = " ".join(tag.get_text(" ", strip=True).split())
            if len(value) > 10:
                return value[:300]
        # Indian addresses reliably end in a 6-digit PIN.
        places = "|".join(re.escape(p) for p in dict.fromkeys([city, "Maharashtra"]) if p)
        match = re.search(r"([A-Z][^.\n]{15,150}?\b(?:" + places + r")\b[^.\n]{0,40}?\b\d{6}\b)", text)
        return " ".join(match.group(1).split())[:300] if match else ""
