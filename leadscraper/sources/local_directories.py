"""Justdial and Sulekha -- local business directories.

Both block aggressively, so they run at the slowest rates in config.yaml and
are SERP-first: we ask a search engine which listing pages exist, then fetch a
small number of them, rather than crawling their paginated search UI.

Justdial caveat: phone numbers are frequently rendered behind a "Show Number"
interaction and have historically been obfuscated with CSS sprite classes.
We attempt the click, decode what we can, and log a skip for the rest so they
can be chased manually -- we never invent a number.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from ..config import Category, Locality
from ..models import Lead
from ..normalize import (
    clean_text, extract_emails, extract_phones, normalize_url, parse_rating,
    parse_review_count,
)
from ..parsing import businesses_from_page
from ..ratelimit import BlockedError
from .base import Source
from .google_search import SerpClient

log = logging.getLogger(__name__)

# Justdial's legacy digit obfuscation: each class maps to one digit.
_JD_DIGIT_CLASSES = {
    "icon-acc": "+", "icon-fa": "(", "icon-fb": ")", "icon-fc": "-",
    "icon-mobilefone": "", "icon-dc": "0", "icon-ba": "1", "icon-yz": "2",
    "icon-wx": "3", "icon-vu": "4", "icon-ts": "5", "icon-rq": "6",
    "icon-po": "7", "icon-nm": "8", "icon-lk": "9",
}


class LocalDirectorySource(Source):
    """SERP-first crawl of a local directory, with a direct-listing attempt."""

    uses_browser = True

    base_url: str = ""
    listing_template: str = ""
    listing_pattern: re.Pattern[str] = re.compile(r"$^")
    host: str = ""

    def rate_keys(self) -> list[str]:
        return [self.name, self.host, "www.google.com", "duckduckgo.com"]

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        max_pages = int(self.cfg.get("max_pages", 1))
        listing_urls = self._listing_urls(locality, category, max_pages)

        if not listing_urls:
            self.fail("no_listings", locality=locality.name, category=category.name)
            return

        for url in listing_urls:
            if self.ctx.store.already_seen(url):
                continue
            try:
                html = self._render(url)
            except BlockedError as exc:
                self.fail("blocked", url=url, locality=locality.name,
                          category=category.name, detail=str(exc))
                return  # back off entirely; this host is done for now
            except Exception as exc:
                self.fail("fetch_failed", url=url, locality=locality.name,
                          category=category.name, detail=repr(exc))
                continue

            parsed_any = False
            for lead in self._parse(html, url, locality, category):
                parsed_any = True
                yield lead
            if parsed_any:
                self.ctx.store.mark_seen(url, self.name)

    def _listing_urls(self, locality: Locality, category: Category, max_pages: int) -> list[str]:
        urls: list[str] = []
        if self.listing_template:
            for query in category.queries[:2]:
                urls.append(self.listing_template.format(
                    query=quote_plus(query.replace(" ", "-")),
                    locality=quote_plus(locality.primary_term.replace(" ", "-")),
                    city=quote_plus(self.ctx.config.city.replace(" ", "-")),
                    city_lower=quote_plus(self.ctx.config.city.lower().replace(" ", "-")),
                ))

        serp_query = f'site:{self.host} {category.queries[0]} {locality.primary_term}'
        try:
            results = SerpClient(self.ctx).search(serp_query, pages=1, per_page=20)
            pattern = self.listing_pattern_for(self.ctx.config.city)
            for result in results:
                if pattern.search(result.url):
                    clean = normalize_url(result.url)
                    if clean and clean not in urls:
                        urls.append(clean)
        except Exception as exc:
            self.fail("serp_failed", query=serp_query, locality=locality.name,
                      category=category.name, detail=repr(exc))

        return urls[: max(1, max_pages) * 8]

    # --- parsing ----------------------------------------------------------

    def _parse(self, html: str, url: str, locality: Locality, category: Category) -> Iterator[Lead]:
        soup = self.ctx.http.soup(html)
        emitted: set[str] = set()

        for record in businesses_from_page(soup):
            name = record.get("business_name", "")
            if not name or name.lower() in emitted:
                continue
            emitted.add(name.lower())
            lead = self.make_lead(
                business_name=name,
                category=category.name,
                locality=locality.name,
                phones=record.get("phones", [])[:3],
                emails=record.get("emails", [])[:2],
                address=record.get("address", ""),
                source_urls=[record.get("url") or url],
            )
            self._attach_rating(lead, record.get("rating"), record.get("reviews"))
            self.assign_locality(lead, locality)
            yield lead

        for card in self._cards(soup):
            lead = self._lead_from_card(card, url, locality, category)
            if lead and lead.business_name.lower() not in emitted:
                emitted.add(lead.business_name.lower())
                yield lead

        if not emitted:
            self.fail("no_businesses_parsed", url=url, locality=locality.name,
                      category=category.name,
                      detail="page fetched but no structured data or cards matched")

    def _cards(self, soup: BeautifulSoup) -> list:
        for selector in self.card_selectors():
            cards = soup.select(selector)
            if len(cards) >= 2:
                return cards[:40]
        return []

    def card_selectors(self) -> list[str]:
        return ["div[class*='result']", "li[class*='result']", "div[class*='listing']",
                "div[class*='card']"]

    def listing_pattern_for(self, city: str) -> re.Pattern[str]:
        """This directory's listing-URL pattern for the run's city."""
        return self.listing_pattern

    def _lead_from_card(self, card, page_url: str, locality: Locality, category: Category) -> Lead | None:
        name_el = card.select_one("h2 a, h3 a, h2, h3, [class*='name'] a, [class*='title']")
        name = clean_text(name_el.get_text(" ", strip=True)) if name_el else ""
        if not name or len(name) < 3:
            return None

        text = clean_text(card.get_text(" ", strip=True))
        phones = extract_phones(text)[:2]
        if not phones:
            phones = self._decode_obfuscated_phone(card)

        if not phones:
            self.fail("phone_hidden", url=page_url, business=name, locality=locality.name,
                      category=category.name,
                      detail="listing found but number is behind an interaction")

        lead = self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            phones=phones,
            emails=extract_emails(text)[:1],
            address=self._card_address(card),
            source_urls=[page_url],
        )
        self._attach_rating(
            lead,
            parse_rating(self._text_of(card, "[class*='rating'], [class*='star']")),
            parse_review_count(self._text_of(card, "[class*='review'], [class*='vote']")),
        )
        self.assign_locality(lead, locality)
        return lead

    def _attach_rating(self, lead: Lead, rating, reviews) -> None:
        """Record a third-party rating as a note -- only Maps/WMG feed the score."""
        if rating is None and reviews is None:
            return
        parts = [f"{self.name} rating {rating}"] if rating is not None else [f"{self.name}"]
        if reviews:
            parts.append(f"({reviews} reviews)")
        lead.notes.append(" ".join(parts))

    @staticmethod
    def _text_of(card, selector: str) -> str:
        el = card.select_one(selector)
        return clean_text(el.get_text(" ", strip=True)) if el else ""

    @staticmethod
    def _card_address(card) -> str:
        el = card.select_one("[class*='address'], [class*='locality'], address")
        return clean_text(el.get_text(" ", strip=True))[:250] if el else ""

    @staticmethod
    def _decode_obfuscated_phone(card) -> list[str]:
        """Rebuild a number from Justdial-style per-digit sprite classes."""
        digits: list[str] = []
        for span in card.select("span[class*='icon-']"):
            for css_class in span.get("class", []):
                if css_class in _JD_DIGIT_CLASSES:
                    digits.append(_JD_DIGIT_CLASSES[css_class])
                    break
        candidate = "".join(digits)
        return extract_phones(candidate) if len(re.sub(r"\D", "", candidate)) >= 10 else []

    def _render(self, url: str) -> str:
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait(self.host)
            page.goto(url, wait_until="domcontentloaded")
            self.ctx.browser.check_blocked(page)
            self._reveal_numbers(page)
            self.ctx.browser.human_pause(0.8, 2.0)
            return page.content()

    def _reveal_numbers(self, page) -> None:
        """Click any 'Show Number' buttons the listing exposes."""
        for selector in ('span:has-text("Show Number")', 'button:has-text("Show Number")',
                         '[class*="showNumber"]', '[class*="show-number"]'):
            try:
                buttons = page.query_selector_all(selector)
            except Exception:
                continue
            for button in buttons[:15]:
                try:
                    button.click(timeout=2_000)
                    self.ctx.browser.human_pause(0.2, 0.6)
                except Exception:
                    continue


class JustdialSource(LocalDirectorySource):
    name = "justdial"
    base_url = "https://www.justdial.com"
    host = "www.justdial.com"
    listing_template = "https://www.justdial.com/{city}/{query}-in-{locality}"
    listing_pattern = re.compile(r"justdial\.com/Mumbai/", re.I)

    def listing_pattern_for(self, city: str) -> re.Pattern[str]:
        return re.compile(r"justdial\.com/" + re.escape(city.replace(" ", "-")) + "/", re.I)

    def card_selectors(self) -> list[str]:
        return ["div.resultbox", "li.cntanr", "div[class*='resultbox']",
                "div[class*='store-details']", *super().card_selectors()]


class SulekhaSource(LocalDirectorySource):
    name = "sulekha"
    base_url = "https://www.sulekha.com"
    host = "www.sulekha.com"
    listing_template = "https://www.sulekha.com/{query}/{city_lower}"
    listing_pattern = re.compile(r"sulekha\.com/[\w\-]+/mumbai", re.I)

    def listing_pattern_for(self, city: str) -> re.Pattern[str]:
        return re.compile(r"sulekha\.com/[\w\-]+/" + re.escape(city.lower().replace(" ", "-")), re.I)

    def card_selectors(self) -> list[str]:
        return ["div.list-item", "div[class*='vendor-card']", "li[class*='listing']",
                *super().card_selectors()]
