"""Google Maps discovery via Playwright.

Maps is the highest-value source: it's the only one that reliably gives us
rating + review count + phone + website + full address in one place.

Strategy per locality x category:
  1. Open /maps/search/<query>
  2. Scroll the results feed to load more cards
  3. Collect place URLs from the feed
  4. Open each place panel and extract the detail fields

The DOM extraction runs as a single in-page script with several fallback
selectors per field, because Maps' generated class names change often. When
a field can't be found we log a skip rather than guessing.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import quote_plus

from ..config import Category, Locality
from ..models import Lead
from ..normalize import (
    clean_text, extract_instagram, extract_phones, normalize_url, parse_rating,
    parse_review_count,
)
from ..ratelimit import BlockedError, with_retry
from ..venues import coords_from_maps_url, extract_amenities, nightly_prices, set_price
from .base import Source

log = logging.getLogger(__name__)

MAPS_SEARCH = "https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z?hl=en&gl=IN"

# Runs inside the place panel. Returns whatever it can find; missing keys are
# handled on the Python side.
_DETAIL_SCRIPT = """
() => {
  const pick = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el) {
        const v = (el.getAttribute('aria-label') || el.textContent || '').trim();
        if (v) return v;
      }
    }
    return null;
  };
  const attr = (sel, name) => {
    const el = document.querySelector(sel);
    return el ? el.getAttribute(name) : null;
  };

  // Phone lives in data-item-id="phone:tel:+912212345678"
  let phone = null;
  const phoneBtn = document.querySelector('button[data-item-id^="phone:tel:"], a[data-item-id^="phone:tel:"]');
  if (phoneBtn) {
    const id = phoneBtn.getAttribute('data-item-id') || '';
    phone = id.replace('phone:tel:', '') || (phoneBtn.getAttribute('aria-label') || '');
  }

  // Website: the "authority" item is the business's own link.
  let website = attr('a[data-item-id="authority"]', 'href')
             || attr('a[data-item-id^="authority"]', 'href');
  if (!website) {
    const wsBtn = document.querySelector('a[aria-label^="Website"], a[data-tooltip="Open website"]');
    if (wsBtn) website = wsBtn.getAttribute('href');
  }

  const name = pick(['h1.DUwDvf', 'h1[class*="fontHeadlineLarge"]', 'h1']);

  const address = (() => {
    const el = document.querySelector('button[data-item-id="address"]');
    if (el) return (el.getAttribute('aria-label') || el.textContent || '').replace(/^Address:\\s*/i, '').trim();
    return null;
  })();

  // Rating + review count sit together in the header block.
  let rating = null, reviews = null;
  const rEl = document.querySelector('div.F7nice span[aria-hidden="true"], span.ceNzKf[aria-label], div.F7nice');
  if (rEl) rating = (rEl.getAttribute('aria-label') || rEl.textContent || '').trim();
  const revEl = document.querySelector('div.F7nice span[aria-label*="review"], button[aria-label*="review"], span[aria-label*="review"]');
  if (revEl) reviews = (revEl.getAttribute('aria-label') || revEl.textContent || '').trim();

  const categoryEl = document.querySelector('button[jsaction*="category"], div.LBgpqf button');
  const category = categoryEl ? categoryEl.textContent.trim() : null;

  // Hotel panels carry a star class ("4-star hotel") next to the category.
  const starEl = Array.from(document.querySelectorAll('span, div'))
    .find(el => el.childElementCount === 0 && /^[1-5]-star hotel$/i.test((el.textContent || '').trim()));
  const hotelClass = starEl ? starEl.textContent.trim() : null;

  // Plus-code / secondary text sometimes carries an alternate phone.
  const bodyText = (document.querySelector('div[role="main"]') || document.body).innerText || '';

  return {name, phone, website, address, rating, reviews, category, hotelClass,
          bodyText: bodyText.slice(0, 6000), url: location.href};
}
"""

_FEED_LINKS_SCRIPT = """
() => Array.from(document.querySelectorAll('a[href*="/maps/place/"]'))
        .map(a => a.href)
        .filter((v, i, arr) => arr.indexOf(v) === i)
"""


class GoogleMapsSource(Source):
    name = "google_maps"
    uses_browser = True

    @property
    def pace_key(self) -> str:
        """Limiter key every Maps request waits on.

        Defaults to the google.com budget shared with Google Search. A venue
        run sets `rate_key: google_maps` so Maps paces on its own delays: Maps
        and SERP throttle separately, and sharing meant a SERP CAPTCHA penalty
        (+135s) plus SERP's 12-28s range slowed every Maps page to ~2.5 min.
        """
        return str(self.cfg.get("rate_key", "www.google.com"))

    def rate_keys(self) -> list[str]:
        keys = ["google_maps", self.pace_key]
        return list(dict.fromkeys(keys))

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        max_results = int(self.cfg.get("max_results_per_query", 25))
        scroll_rounds = int(self.cfg.get("scroll_rounds", 8))

        seen_places: set[str] = set()
        phrasings = int(self.cfg.get("locality_phrasings", 2))
        for query_term in category.queries:
            for locality_term in locality.search_terms[:phrasings]:
                query = f"{query_term} in {locality_term}"
                try:
                    place_urls = self._search(query, scroll_rounds, max_results)
                except BlockedError as exc:
                    self.fail("blocked", query=query, locality=locality.name,
                              category=category.name, detail=str(exc))
                    return  # Maps is angry; stop this combo entirely
                except Exception as exc:
                    self.fail("search_failed", query=query, locality=locality.name,
                              category=category.name, detail=repr(exc))
                    continue

                if not place_urls:
                    self.fail("no_results", query=query, locality=locality.name,
                              category=category.name)
                    continue

                for place_url in place_urls:
                    if len(seen_places) >= max_results:
                        break
                    key = self._place_key(place_url)
                    if key in seen_places:
                        continue
                    seen_places.add(key)
                    lead = self._fetch_place(place_url, locality, category)
                    if lead:
                        yield lead

    # --- steps ------------------------------------------------------------

    def _search(self, query: str, scroll_rounds: int, max_results: int) -> list[str]:
        """Return place URLs from the results feed for one query."""
        lat, lng = self.ctx.config.geo_center
        zoom = int(self.ctx.config.get("geo.maps_zoom", 12) or 12)
        url = MAPS_SEARCH.format(query=quote_plus(query), lat=f"{lat:.4f}", lng=f"{lng:.4f}", zoom=zoom)
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait(self.pace_key)
            page.goto(url, wait_until="domcontentloaded")
            self.ctx.browser.dismiss_google_consent(page)
            self.ctx.browser.check_blocked(page)

            # A single strong match redirects straight to a place page.
            if "/maps/place/" in page.url:
                return [page.url]

            try:
                page.wait_for_selector('div[role="feed"], a[href*="/maps/place/"]', timeout=15_000)
            except Exception:
                return []

            links = self._scroll_feed(page, scroll_rounds, max_results)
            return links[:max_results]

    def _scroll_feed(self, page, rounds: int, max_results: int) -> list[str]:
        """Scroll the left-hand feed until it stops yielding new results."""
        links: list[str] = []
        feed = page.query_selector('div[role="feed"]')
        stale_rounds = 0
        for _ in range(rounds):
            try:
                found = page.evaluate(_FEED_LINKS_SCRIPT) or []
            except Exception:
                found = []
            before = len(links)
            for href in found:
                if href not in links:
                    links.append(href)
            if len(links) >= max_results:
                break
            # No growth twice in a row means we've hit the end of the list.
            stale_rounds = stale_rounds + 1 if len(links) == before else 0
            if stale_rounds >= 2:
                break
            try:
                if feed:
                    page.evaluate("(el) => el.scrollBy(0, el.scrollHeight)", feed)
                else:
                    page.mouse.wheel(0, 3000)
            except Exception:
                break
            self.ctx.browser.human_pause(1.2, 2.8)
        return links

    def _fetch_place(self, place_url: str, locality: Locality, category: Category) -> Lead | None:
        """Open one place panel and build a Lead from it."""
        try:
            data = with_retry(
                lambda: self._read_place(place_url),
                attempts=int(self.ctx.config.get("runtime.max_retries", 3)),
                base=float(self.ctx.config.get("runtime.backoff_base", 2.0)),
                label=f"maps place {place_url[:60]}",
                give_up_on=(BlockedError,),
            )
        except BlockedError as exc:
            self.fail("blocked", url=place_url, locality=locality.name,
                      category=category.name, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("place_fetch_failed", url=place_url, locality=locality.name,
                      category=category.name, detail=repr(exc))
            return None

        name = clean_text(data.get("name"))
        if not name:
            self.fail("no_name", url=place_url, locality=locality.name, category=category.name)
            return None

        body = data.get("bodyText") or ""
        phones = extract_phones(data.get("phone") or "")
        if not phones:
            phones = extract_phones(body)[:1]  # body text is noisier; take the first

        website = data.get("website")
        website = normalize_url(website) if website else None

        lead = self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            phones=phones,
            website=website,
            address=clean_text(data.get("address")),
            gmaps_url=data.get("url") or place_url,
            gmaps_rating=parse_rating(data.get("rating")),
            gmaps_reviews=parse_review_count(data.get("reviews")),
            instagram=extract_instagram(body),
            source_urls=[place_url],
        )
        if data.get("category"):
            maps_category = clean_text(data["category"])
            if maps_category:
                lead.notes.append(f"Maps category: {maps_category}")
                lead.property_type = maps_category
        if self.ctx.config.is_venue_run:
            self._venue_details(lead, data, body, locality)
            if self.out_of_region(lead):
                self.fail("out_of_region", url=place_url, business=name,
                          locality=locality.name, category=category.name,
                          detail=f"pin is beyond geo.radius_km of {self.ctx.config.city}")
                return None
        return lead

    def _venue_details(self, lead: Lead, data: dict, body: str, locality: Locality) -> None:
        """Pin, star class, nightly price and amenities from the place panel."""
        point = coords_from_maps_url(data.get("url")) or coords_from_maps_url(lead.gmaps_url)
        if point:
            lead.latitude, lead.longitude = point
        stars = re.match(r"([1-5])", data.get("hotelClass") or "")
        if stars:
            lead.hotel_class = int(stars.group(1))
        # Hotel panels list booking-site prices ("₹7,450 ... per night"); the
        # panel text is short enough that the first clean one is this place's.
        prices = nightly_prices(body)
        if prices:
            set_price(lead, min(prices), max(prices), "per night", "google_maps")
        lead.amenities = extract_amenities(body)
        if {"Private Pool", "Infinity Pool"} & set(lead.amenities):
            lead.has_pool = True
        self.assign_locality(lead, locality)

    def _read_place(self, place_url: str) -> dict:
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait(self.pace_key)
            page.goto(place_url, wait_until="domcontentloaded")
            self.ctx.browser.dismiss_google_consent(page)
            self.ctx.browser.check_blocked(page)
            try:
                page.wait_for_selector("h1", timeout=12_000)
            except Exception:
                pass
            self.ctx.browser.human_pause(0.6, 1.5)
            return page.evaluate(_DETAIL_SCRIPT) or {}

    @staticmethod
    def _place_key(place_url: str) -> str:
        """Stable identity for a place URL (its CID / feature id when present)."""
        match = re.search(r"!1s([^!?]+)", place_url) or re.search(r"/maps/place/([^/@?]+)", place_url)
        return match.group(1) if match else place_url
