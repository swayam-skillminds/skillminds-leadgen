"""IndiaMART -- supplier-side discovery for catering, decor and sound/lighting.

IndiaMART is the best free source for the *supplier* half of the market
(equipment rental, bulk catering, decor fabrication) that barely appears on
wedding directories. Its search pages are server-rendered enough that we can
read company cards directly, with an opt-in SERP fallback (`serp_fallback`)
for when the layout shifts -- off by default because falling through to search
on every category is what burned the SERP budget and CAPTCHA'd past runs.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from ...config import Category, Locality
from ...core.lead import Lead
from ...core.normalize import clean_text, extract_phones, normalize_url, parse_rating, parse_review_count
from ...core.parsing import businesses_from_page
from ...net.ratelimit import BlockedError
from ...sources.base import Source
from ...sources.google_search import SerpClient

log = logging.getLogger(__name__)

SEARCH_URL = "https://dir.indiamart.com/search.mp?ss={query}&cq={city}&page={page}"

# IndiaMART company profile URLs look like https://www.indiamart.com/<slug>/
_COMPANY_URL = re.compile(r"indiamart\.com/([\w\-]{4,})/?$", re.I)
_SKIP_SLUGS = {"search", "impcat", "proddetail", "company", "products", "city"}


class IndiaMartSource(Source):
    name = "indiamart"
    priority = 40
    uses_browser = True

    #: Google/DuckDuckGo are only hit by the opt-in `serp_fallback`.
    serp_is_fallback = True

    def rate_keys(self) -> list[str]:
        return ["indiamart", "dir.indiamart.com", "www.indiamart.com",
                "www.google.com", "duckduckgo.com"]

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        if not category.indiamart:
            return  # supplier-side categories only

        queries = category.indiamart_queries or category.queries[:2]
        max_pages = int(self.cfg.get("max_pages", 2))
        emitted: set[str] = set()

        for query in queries:
            for page_num in range(1, max_pages + 1):
                url = SEARCH_URL.format(
                    query=quote_plus(query),
                    city=quote_plus(locality.primary_term),
                    page=page_num,
                )
                try:
                    html = self._render(url)
                except BlockedError as exc:
                    self.fail("blocked", url=url, query=query, locality=locality.name,
                              category=category.name, detail=str(exc))
                    return
                except Exception as exc:
                    self.fail("search_failed", url=url, query=query, locality=locality.name,
                              category=category.name, detail=repr(exc))
                    break

                leads = list(self._parse_search(html, locality, category, url))
                if not leads:
                    if page_num == 1 and self.cfg.get("serp_fallback", False):
                        leads = list(self._serp_fallback(query, locality, category))
                    elif page_num == 1:
                        log.info("%s: search for %s/%s yielded no companies and "
                                 "serp_fallback is off; set sources.%s.serp_fallback: true to "
                                 "search instead", self.name, locality.name, category.name,
                                 self.name)
                    if not leads:
                        break

                new_this_page = 0
                for lead in leads:
                    key = lead.name_key
                    if key in emitted:
                        continue
                    emitted.add(key)
                    new_this_page += 1
                    yield lead
                if new_this_page == 0:
                    break

    # --- parsing ----------------------------------------------------------

    def _parse_search(
        self, html: str, locality: Locality, category: Category, page_url: str
    ) -> Iterator[Lead]:
        soup = self.ctx.http.soup(html)

        # Preferred path: structured data.
        for record in businesses_from_page(soup, self.region):
            lead = self._lead_from_record(record, locality, category, page_url)
            if lead:
                yield lead

        # Fallback: company cards in the results list.
        for card in self._cards(soup):
            lead = self._lead_from_card(card, locality, category, page_url)
            if lead:
                yield lead

    @staticmethod
    def _cards(soup: BeautifulSoup) -> list[BeautifulSoup]:
        """Result cards, tried across the class names IndiaMART has used."""
        for selector in ("div.card", "div.lst", "li.lst", "div[class*='listing']",
                         "div.prd-info", "div[class*='resultCard']"):
            cards = soup.select(selector)
            if len(cards) >= 3:
                return cards
        return []

    def _lead_from_card(
        self, card: BeautifulSoup, locality: Locality, category: Category, page_url: str
    ) -> Lead | None:
        name_el = (card.select_one("a[class*='cardlinks'], .companyname a, .lcname a, h2 a, h3 a")
                   or card.select_one("h2, h3"))
        name = clean_text(name_el.get_text(" ", strip=True)) if name_el else ""
        if not name or len(name) < 3:
            return None

        text = clean_text(card.get_text(" ", strip=True))
        phones = extract_phones(text, self.region)[:2]

        profile_url = None
        if name_el is not None and name_el.has_attr("href"):
            candidate = normalize_url(name_el["href"])
            if candidate and self._is_company_url(candidate):
                profile_url = candidate

        lead = self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            phones=phones,
            address=self._address_from_card(card),
            source_urls=[u for u in (profile_url, page_url) if u],
        )
        rating = parse_rating(self._first_text(card, "[class*='rating'], [class*='Rating']"))
        reviews = parse_review_count(self._first_text(card, "[class*='rvw'], [class*='review']"))
        if rating is not None:
            lead.notes.append(
                f"IndiaMART rating {rating}" + (f" ({reviews} reviews)" if reviews else "")
            )
        return lead

    def _lead_from_record(
        self, record: dict, locality: Locality, category: Category, page_url: str
    ) -> Lead | None:
        name = record.get("business_name", "")
        if not name or len(name) < 3:
            return None
        lead = self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            phones=record.get("phones", [])[:2],
            emails=record.get("emails", [])[:2],
            address=record.get("address", ""),
            source_urls=[u for u in (record.get("url"), page_url) if u],
        )
        if record.get("rating") is not None:
            lead.notes.append(f"IndiaMART rating {record['rating']}")
        return lead

    @staticmethod
    def _address_from_card(card: BeautifulSoup) -> str:
        el = card.select_one("[class*='newLocationUi'], [class*='location'], [class*='addr'], .clg")
        return clean_text(el.get_text(" ", strip=True))[:250] if el else ""

    @staticmethod
    def _first_text(card: BeautifulSoup, selector: str) -> str:
        el = card.select_one(selector)
        return clean_text(el.get_text(" ", strip=True)) if el else ""

    @staticmethod
    def _is_company_url(url: str) -> bool:
        match = _COMPANY_URL.search(url)
        return bool(match) and match.group(1).lower() not in _SKIP_SLUGS

    # --- fallback ---------------------------------------------------------

    def _serp_fallback(self, query: str, locality: Locality, category: Category) -> Iterator[Lead]:
        serp_query = f'site:indiamart.com "{query}" {locality.primary_term}'
        try:
            results = SerpClient(self.ctx).search(serp_query, pages=1, per_page=20)
        except Exception as exc:
            self.fail("serp_fallback_failed", query=serp_query, locality=locality.name,
                      category=category.name, detail=repr(exc))
            return

        from ...sources.google_search import business_name_from_title

        for result in results:
            name = business_name_from_title(
                result.title, locality.name, tuple(category.indiamart_queries or category.queries),
                self.place_words,
            )
            if not name:
                continue
            blob = f"{result.title} {result.snippet}"
            yield self.make_lead(
                business_name=name,
                category=category.name,
                locality=locality.name,
                phones=extract_phones(blob, self.region)[:2],
                source_urls=[result.url],
                notes=["discovered via IndiaMART search-engine fallback"],
            )

    def _render(self, url: str) -> str:
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait("dir.indiamart.com")
            page.goto(url, wait_until="domcontentloaded")
            self.ctx.browser.check_blocked(page)
            # IndiaMART lazy-loads cards on scroll.
            for _ in range(3):
                try:
                    page.mouse.wheel(0, 2500)
                except Exception:
                    break
                self.ctx.browser.human_pause(0.6, 1.4)
            return page.content()
