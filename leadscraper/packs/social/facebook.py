"""Facebook Business Pages.

Facebook's own search needs a login, so we find pages the free way: a
`site:facebook.com` SERP query, then a plain fetch of the public page. We use
mbasic.facebook.com, the no-JS mobile surface, because it renders contact
details as ordinary HTML far more often than the main site does.

Pages behind a login wall are logged as skips, never guessed at.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from ...config import Category, Locality
from ...core.lead import Lead
from ...core.normalize import (
    clean_text, extract_emails, extract_facebook, extract_instagram, extract_phones,
    is_vendor_website, normalize_url,
)
from ...core.parsing import meta_content, visible_text
from ...net.ratelimit import BlockedError
from ...sources.base import Source
from ...sources.google_search import SerpClient, business_name_from_title

log = logging.getLogger(__name__)

_PAGE_URL = re.compile(r"facebook\.com/(?!sharer|plugins|groups|events|photo|watch)([\w.\-]{3,})", re.I)
_LOGIN_WALL = ("you must log in to continue", "log into facebook", "log in or sign up")


class FacebookSource(Source):
    name = "facebook"
    priority = 50

    def rate_keys(self) -> list[str]:
        return ["facebook", "mbasic.facebook.com", "www.facebook.com",
                "www.google.com", "duckduckgo.com"]

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        for query_term in category.queries[:2]:
            serp_query = f'site:facebook.com "{query_term}" {locality.primary_term}'
            try:
                results = SerpClient(self.ctx).search(serp_query, pages=1, per_page=20)
            except Exception as exc:
                self.fail("serp_failed", query=serp_query, locality=locality.name,
                          category=category.name, detail=repr(exc))
                continue

            if not results:
                self.fail("no_results", query=serp_query, locality=locality.name,
                          category=category.name)
                continue

            for result in results:
                page_url = extract_facebook(result.url)
                if not page_url or self.ctx.store.already_seen(page_url):
                    continue
                lead = self._read_page(page_url, result.title, locality, category)
                if lead:
                    self.ctx.store.mark_seen(page_url, self.name)
                    yield lead

    def _read_page(self, page_url: str, serp_title: str,
                   locality: Locality, category: Category) -> Lead | None:
        slug = _PAGE_URL.search(page_url)
        if not slug:
            return None
        mbasic_url = f"https://mbasic.facebook.com/{slug.group(1)}"

        try:
            response = self.ctx.http.get(mbasic_url, rate_key="mbasic.facebook.com")
        except BlockedError as exc:
            self.fail("blocked", url=mbasic_url, locality=locality.name,
                      category=category.name, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("fetch_failed", url=mbasic_url, locality=locality.name,
                      category=category.name, detail=repr(exc))
            return None

        body = response.text
        lowered = body[:5000].lower()
        if any(marker in lowered for marker in _LOGIN_WALL):
            # Still worth a lead from the SERP title alone -- we know the page exists.
            name = business_name_from_title(
                serp_title, locality.name, tuple(category.queries), self.place_words
            )
            self.fail("login_wall", url=page_url, business=name or None,
                      locality=locality.name, category=category.name,
                      detail="public page requires login; recorded URL only")
            if not name:
                return None
            return self.make_lead(
                business_name=name, category=category.name, locality=locality.name,
                facebook=page_url, source_urls=[page_url],
                notes=["Facebook page found but contact details behind login"],
            )

        soup = self.ctx.http.soup(body)
        text = visible_text(soup, limit=15_000)

        name = meta_content(soup, "og:title") or business_name_from_title(
            serp_title, locality.name, tuple(category.queries), self.place_words
        )
        name = clean_text(re.split(r"\s+[\|\-–]\s+", name)[0]) if name else ""
        if not name:
            self.fail("no_name", url=page_url, locality=locality.name, category=category.name)
            return None

        website = next(
            (u for u in self._outbound_links(soup) if is_vendor_website(u)), None
        )

        return self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            phones=extract_phones(text, self.region)[:2],
            emails=extract_emails(text)[:2],
            website=website,
            facebook=page_url,
            instagram=extract_instagram(body),
            address=self._address(text),
            source_urls=[page_url],
        )

    @staticmethod
    def _outbound_links(soup) -> list[str]:
        """External URLs a page links to -- often the vendor's own site."""
        out: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            # mbasic wraps external links as /l.php?u=<encoded>
            match = re.search(r"[?&]u=(https?[^&]+)", href)
            if match:
                from urllib.parse import unquote
                href = unquote(match.group(1))
            clean = normalize_url(href)
            if clean and clean not in out:
                out.append(clean)
        return out

    @staticmethod
    def _address(text: str) -> str:
        """Grab the line after an address-ish label, if present."""
        match = re.search(
            r"(?:Address|Located in|Get Directions)[:\s]+(.{10,160}?)(?:\s{2,}|Phone|Email|$)",
            text, re.I,
        )
        return clean_text(match.group(1)) if match else ""
