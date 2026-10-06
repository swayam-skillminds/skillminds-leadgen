"""Wedding-vendor directories: WedMeGood and WeddingWire India.

These sites are JS-rendered and their URL taxonomies change, so nothing about
the URL is hardcoded any more. Both the city segment and the category slug are
resolved against the live site before a crawl starts:

  1. City slug      -- the config city is slugified and probed; "delhi" 404s
                       while "delhi-ncr" and "new-delhi" both work, so a few
                       variants are tried and the first live one is kept.
  2. Category slug  -- harvested from the directory's own listing-page hrefs,
                       then matched to the configured category. A slug written
                       in config.yaml is only used if the live site still has
                       it; it used to say "caterers" while WedMeGood served
                       "wedding-catering", and every request 404'd.
  3. `site:` SERP    -- opt-in only (`serp_fallback: true`). It used to run
                       whenever a listing came back empty, which -- with every
                       listing URL broken -- turned the whole run into a search
                       scraper and earned a CAPTCHA within two minutes.

WedMeGood additionally carries the rating / review-count / "Featured" signals
that feed the directory component of the legitimacy score.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterator

from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

from ...config import Category, Locality
from ...core.lead import Lead
from ...core.normalize import (
    clean_text, dedupe_preserving_order, domain_of, extract_emails,
    extract_instagram, extract_phones, normalize_url, parse_rating,
    parse_review_count,
)
from ...core.parsing import businesses_from_page, meta_content, visible_text
from ...net.ratelimit import BlockedError
from ..hospitality.venues import enrich_from_text
from ...sources.base import Source
from ...sources.google_search import SerpClient

log = logging.getLogger(__name__)

# Badge wording each directory uses for a paid/vetted listing.
_FEATURED_WORDS = re.compile(r"\b(featured|handpicked|premium|verified|wmg\s*pick|top\s*rated)\b", re.I)

# WedMeGood's live vendor taxonomy at the time of writing. This is ONLY a
# fallback for when runtime discovery fails -- a hardcoded table is what broke
# this source in the first place, so a run that leans on it says so loudly.
WEDMEGOOD_STATIC_SLUGS = frozenset({
    "bartenders-wedding", "beauty-and-wellness", "bridal-makeup", "bridal-wear",
    "djs", "family-makeup", "groom-wear", "mehendi-artists", "pre-wedding-shoot",
    "pre-wedding-shoot-locations", "sangeet-choreographers", "trousseau-packers",
    "wedding-accessories", "wedding-cakes", "wedding-cards", "wedding-catering",
    "wedding-decorators", "wedding-entertainment", "wedding-favors",
    "wedding-jewellery", "wedding-pandits-priests", "wedding-photographers",
    "wedding-planners", "wedding-venues",
})

# Below this a "200" is a bot wall or an error shell, not a listing page:
# weddingwire.in answers our probes with a 404-byte body.
_MIN_LISTING_BYTES = 2_000

# rapidfuzz score under which we would rather skip a category than guess at it.
# A wrong slug costs a page of 404s and silently mislabels whatever it does
# return; a skip costs one log line.
_FUZZ_THRESHOLD = 80

# Memoised for the process lifetime, per (source, city): neither a site's
# taxonomy nor its city routing changes mid-run, and every miss is a page fetch
# behind a multi-second rate limit.
_SLUG_CACHE: dict[tuple[str, str], frozenset[str]] = {}
_CITY_CACHE: dict[tuple[str, str], str | None] = {}

# Plural/gerund endings, longest first. Crude on purpose: it only has to bring
# "caterers" and "catering" to the same stem so fuzzy matching can see them as
# the same category.
_SUFFIXES = ("ings", "ing", "ers", "ors", "es", "s")


def _slugify(value: str) -> str:
    """Casefold, strip accents, and collapse anything else into single dashes."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", stripped).strip("-")


def _stem(token: str) -> str:
    for suffix in _SUFFIXES:
        # Keep at least three characters so "djs" survives intact.
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _stem_slug(slug: str) -> str:
    return "-".join(_stem(token) for token in slug.split("-"))


def _city_variants(base: str) -> list[str]:
    """Plausible spellings of a city segment, best guess first.

    Indian metros are routed inconsistently: WedMeGood serves "delhi-ncr" and
    "new-delhi" but 404s on plain "delhi", so the derived slug alone is not
    enough to decide a city is unsupported.
    """
    variants = [base]
    if base.startswith("new-"):
        variants.append(base[len("new-"):])
    else:
        variants.append(f"new-{base}")
    variants.append(f"{base}-ncr")
    head = base.split("-")[0]
    if head != base:
        variants.append(head)
    variants.append(base.replace("-", ""))
    return dedupe_preserving_order(variants)


class DirectorySource(Source):
    """Shared listing -> profile crawl for the wedding directories."""

    uses_browser = True

    #: e.g. "https://www.wedmegood.com"
    base_url: str = ""
    #: Formatted with {city}, {slug} and {page}.
    listing_template: str = ""
    #: Matches this directory's vendor-profile URLs.
    profile_pattern: re.Pattern[str] = re.compile(r"$^")
    #: Which key in category.directory_slugs to use.
    slug_key: str = ""
    #: Category slug used to fetch a listing page when probing or harvesting.
    seed_slug: str = ""
    #: Captures category slugs out of listing-page hrefs; {city} is filled in.
    slug_href_template: str = ""
    #: Last-resort taxonomy for when runtime discovery fails.
    static_slugs: frozenset[str] = frozenset()
    #: True only for WedMeGood, whose signals are scored separately.
    is_wedmegood: bool = False

    #: Google/DuckDuckGo are only hit by the opt-in `serp_fallback`.
    serp_is_fallback = True

    def rate_keys(self) -> list[str]:
        return [self.name, self.host, "www.google.com", "duckduckgo.com"]

    @property
    def host(self) -> str:
        return domain_of(self.base_url) or self.name

    # --- entry point ------------------------------------------------------

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        city = self.ctx.config.city
        city_slug = self.resolve_city_slug(city)
        if not city_slug:
            log.info("%s: no live city slug for %r; skipping %s/%s",
                     self.name, city, locality.name, category.name)
            return

        slug = self.resolve_slug(category, city_slug)
        if not slug:
            return  # resolve_slug has already said why

        profile_urls = self._collect_profile_urls(slug, city_slug, locality, category)
        if not profile_urls:
            self.fail("no_listings", locality=locality.name, category=category.name,
                      detail=f"no profiles found for slug {slug!r}")
            return

        for profile_url in profile_urls:
            if self.ctx.store.already_seen(profile_url):
                # Debug, not info: this fires once per URL per re-run and is
                # the memo working, not a source giving up.
                log.debug("%s: %s already seen; skipping", self.name, profile_url)
                continue
            lead = self._read_profile(profile_url, locality, category)
            if lead:
                # Only memo on success: marking a failed fetch as seen would
                # make the failure permanent across re-runs.
                self.ctx.store.mark_seen(profile_url, self.name)
                yield lead

    # --- city and category slug resolution --------------------------------

    def city_slug(self, city: str) -> str:
        """Config city -> URL segment: the pack's override for this site, else its slug."""
        from .. import city_slug

        return city_slug(type(self), city)

    def resolve_city_slug(self, city: str) -> str | None:
        key = (self.name, city)
        if key in _CITY_CACHE:
            return _CITY_CACHE[key]
        resolved = self._probe_city_slug(city)
        _CITY_CACHE[key] = resolved
        return resolved

    def _probe_city_slug(self, city: str) -> str | None:
        for candidate in _city_variants(self.city_slug(city)):
            url = self.listing_template.format(city=candidate, slug=self.seed_slug, page=1)
            try:
                response = self.ctx.http.get(url, rate_key=self.host)
            except BlockedError as exc:
                # The host is refusing us, not the city. No other variant will
                # fare better and each one costs a rate-limiter penalty, so stop.
                log.info("%s: city probe blocked at %r (%s); skipping this source",
                         self.name, candidate, exc)
                return None
            except Exception as exc:
                log.info("%s: city slug %r did not resolve (%r)", self.name, candidate, exc)
                continue

            if response.status_code == 200 and len(response.content) >= _MIN_LISTING_BYTES:
                log.info("%s: city %r resolved to %r", self.name, city, candidate)
                return candidate
            log.info("%s: city slug %r answered HTTP %s with %d bytes; trying the next variant",
                     self.name, candidate, response.status_code, len(response.content))

        log.info("%s: no city slug worked for %r; skipping this source", self.name, city)
        return None

    def discovered_slugs(self, city_slug: str) -> frozenset[str]:
        """The category slugs this directory actually serves for a city."""
        key = (self.name, city_slug)
        cached = _SLUG_CACHE.get(key)
        if cached is not None:
            return cached

        slugs = self._harvest_slugs(city_slug)
        if not slugs:
            log.warning("%s: slug discovery FAILED for city %r; falling back to the static "
                        "list of %d slugs, which will be wrong as soon as the site changes",
                        self.name, city_slug, len(self.static_slugs))
            slugs = self.static_slugs
        _SLUG_CACHE[key] = slugs
        return slugs

    def _harvest_slugs(self, city_slug: str) -> frozenset[str]:
        url = self.listing_template.format(city=city_slug, slug=self.seed_slug, page=1)
        pattern = re.compile(self.slug_href_template.format(city=re.escape(city_slug)), re.I)

        # The slug links are in the served HTML, so a plain GET is enough and is
        # an order of magnitude cheaper than a browser tab. Playwright is the
        # fallback, not the default.
        try:
            html = self.ctx.http.get(url, rate_key=self.host).text
        except Exception as exc:
            log.info("%s: plain GET of %s failed (%r); retrying with the browser",
                     self.name, url, exc)
            try:
                html = self._render(url)
            except Exception as render_exc:
                log.info("%s: browser fetch of %s also failed (%r)",
                         self.name, url, render_exc)
                return frozenset()

        return frozenset(match.lower() for match in pattern.findall(html))

    def resolve_slug(self, category: Category, city_slug: str) -> str | None:
        """Pick the live slug for a category, or None to skip this source."""
        discovered = self.discovered_slugs(city_slug)
        configured = category.directory_slugs.get(self.slug_key)

        if not discovered:
            # Nothing discovered and no static list to fall back on. Trusting
            # config here is a guess, but it beats silently disabling a source.
            if configured:
                log.info("%s: nothing discovered for %r; falling back to the unverified "
                         "configured slug %r for %s",
                         self.name, city_slug, configured, category.name)
            else:
                log.info("%s: nothing discovered for %r and no configured slug for %s; skipping",
                         self.name, city_slug, category.name)
            return configured

        if configured and configured in discovered:
            return configured
        if configured:
            log.info("%s: configured slug %r is not a live category on %s; resolving %s by name",
                     self.name, configured, self.host, category.name)

        derived = _slugify(category.name)
        if derived in discovered:
            return derived

        # Also match on stems, so "Caterers" can reach "wedding-catering":
        # character-for-character that pair scores 56, well under any threshold
        # worth having, while their stems ("cater" / "wedd-cater") reach 90.
        # rapidfuzz scores a mapping's VALUES and hands back the key, so the
        # stems do the comparing and the live slug is what comes out.
        by_slug = {slug: slug for slug in sorted(discovered)}
        by_stem = {slug: _stem_slug(slug) for slug in by_slug}

        best: tuple[str, float] | None = None
        for query, choices in ((derived, by_slug), (_stem_slug(derived), by_stem)):
            match = process.extractOne(query, choices, scorer=fuzz.WRatio,
                                       score_cutoff=_FUZZ_THRESHOLD)
            if match and (best is None or match[1] > best[1]):
                best = (match[2], match[1])
        if best:
            log.info("%s: matched category %s to slug %r (score %.0f)",
                     self.name, category.name, best[0], best[1])
            return best[0]

        log.info("no slug for category %s on %s", category.name, self.name)
        return None

    # --- discovery of profile URLs ---------------------------------------

    def _collect_profile_urls(self, slug: str, city_slug: str, locality: Locality,
                              category: Category) -> list[str]:
        max_pages = int(self.cfg.get("max_pages", 3))
        urls: list[str] = []

        for page_num in range(1, max_pages + 1):
            listing_url = self.listing_template.format(city=city_slug, slug=slug, page=page_num)
            try:
                html = self._fetch(listing_url)
            except BlockedError as exc:
                self.fail("blocked", url=listing_url, locality=locality.name,
                          category=category.name, detail=str(exc))
                break
            except Exception as exc:
                self.fail("listing_failed", url=listing_url, locality=locality.name,
                          category=category.name, detail=repr(exc))
                break

            found = self._profile_links(html)
            new = [u for u in found if u not in urls]
            urls.extend(new)
            # No new profiles on this page means pagination has run out.
            if not new:
                log.info("%s: page %d of %s added no new profiles; pagination exhausted",
                         self.name, page_num, listing_url)
                break

        if not urls:
            if self.cfg.get("serp_fallback", False):
                urls = self._serp_fallback(slug, locality, category)
            else:
                log.info("%s: listing for %s/%s (slug %r) yielded no profiles and "
                         "serp_fallback is off; set sources.%s.serp_fallback: true to "
                         "search instead of crawling",
                         self.name, locality.name, category.name, slug, self.name)

        return urls

    def _profile_links(self, html: str) -> list[str]:
        soup = self.ctx.http.soup(html)
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.startswith("/"):
                href = self.base_url.rstrip("/") + href
            if self.profile_pattern.search(href):
                clean = normalize_url(href)
                if clean and clean not in links:
                    links.append(clean)
        return links

    def _serp_fallback(self, slug: str, locality: Locality, category: Category) -> list[str]:
        """Ask a search engine for profile URLs. Opt-in: see `serp_fallback`."""
        query = f'site:{self.host} {category.name} {locality.primary_term}'
        client = SerpClient(self.ctx, fallback=True)
        try:
            results = client.search(query, pages=1, per_page=20)
        except Exception as exc:
            self.fail("serp_fallback_failed", query=query, locality=locality.name,
                      category=category.name, detail=repr(exc))
            return []
        urls = [r.url for r in results if self.profile_pattern.search(r.url)]
        if urls:
            log.info("%s: recovered %d profiles for %s/%s via SERP fallback",
                     self.name, len(urls), locality.name, category.name)
        else:
            log.info("%s: SERP fallback returned no %s profiles for %s/%s",
                     self.name, self.host, locality.name, category.name)
        return urls

    # --- profile parsing --------------------------------------------------

    def _read_profile(self, url: str, locality: Locality, category: Category) -> Lead | None:
        try:
            html = self._fetch(url)
        except BlockedError as exc:
            self.fail("blocked", url=url, locality=locality.name,
                      category=category.name, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("profile_failed", url=url, locality=locality.name,
                      category=category.name, detail=repr(exc))
            return None

        soup = self.ctx.http.soup(html)
        text = visible_text(soup, limit=12_000)

        record = self._best_business_record(soup)
        name = record.get("business_name") if record else ""
        if not name:
            name = meta_content(soup, "og:title", "twitter:title")
            name = re.split(r"\s+[\|\-–]\s+", name)[0].strip() if name else ""
        if not name:
            heading = soup.find("h1")
            name = clean_text(heading.get_text(" ", strip=True)) if heading else ""
        if not name:
            self.fail("no_name", url=url, locality=locality.name, category=category.name)
            return None

        phones = (record.get("phones") if record else []) or extract_phones(text, self.region)[:3]
        emails = (record.get("emails") if record else []) or extract_emails(text)[:3]
        rating = record.get("rating") if record else None
        reviews = record.get("reviews") if record else None
        if rating is None:
            rating = self._dom_rating(soup)
        if reviews is None:
            reviews = self._dom_reviews(soup, text)

        address = (record.get("address") if record else "") or self._dom_address(soup)
        featured = bool(_FEATURED_WORDS.search(self._badge_text(soup)))

        lead = self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            phones=phones,
            emails=emails,
            address=address,
            instagram=extract_instagram(html),
            source_urls=[url],
        )

        if self.ctx.config.is_venue_run:
            description = (record.get("description") if record else "") or meta_content(
                soup, "og:description", "description")
            lead.description = description or ""
            # A venue run only crawls a directory's venue category, so that is
            # the listing's type unless its name says hotel / resort / farm.
            lead.property_type = lead.property_type or "Wedding venue"
            enrich_from_text(lead, text, price_source=self.name, currency=self.currency)
            lead.platforms.append(self.name)
            lead.listing_urls.append(url)
            # A city listing spills into neighbouring districts (WedMeGood's
            # "Nashik" venues include Silvassa resorts).
            if self.out_of_region(lead):
                self.fail("out_of_region", url=url, business=name, locality=locality.name,
                          category=category.name, detail=lead.address[:200])
                return None
            self.assign_locality(lead, locality)

        if self.is_wedmegood:
            lead.wmg_rating = rating
            lead.wmg_reviews = reviews
            lead.wmg_featured = featured
            lead.wmg_url = url
        else:
            # Non-WedMeGood directory ratings still signal quality; record them
            # as a note so they inform a human without inflating the WMG score.
            if rating is not None:
                lead.notes.append(f"{self.name} rating {rating}"
                                  + (f" ({reviews} reviews)" if reviews else ""))
            if featured:
                lead.notes.append(f"{self.name}: featured/premium listing")
        return lead

    def _best_business_record(self, soup: BeautifulSoup) -> dict | None:
        """Pick the JSON-LD node that describes the profile's own business."""
        # The directory describes itself in JSON-LD too, and with more fields
        # filled in than any vendor -- it must never win "richest record".
        own = {self.name.lower(), (self.host or "").lower(), "wedmegood", "weddingwire"}
        records = [r for r in businesses_from_page(soup, self.region)
                   if r.get("business_name", "").lower().replace(" ", "") not in own]
        if not records:
            log.debug("%s: no JSON-LD business record; falling back to DOM parsing", self.name)
            return None
        # The richest record (most populated fields) is almost always the page subject.
        return max(records, key=lambda r: sum(1 for v in r.values() if v))

    @staticmethod
    def _dom_rating(soup: BeautifulSoup) -> float | None:
        for selector in ('[itemprop="ratingValue"]', '[class*="rating"]', '[class*="Rating"]',
                         '[data-rating]'):
            for el in soup.select(selector)[:4]:
                value = el.get("content") or el.get("data-rating") or el.get_text(" ", strip=True)
                rating = parse_rating(value)
                if rating is not None:
                    return rating
        return None

    @staticmethod
    def _dom_reviews(soup: BeautifulSoup, text: str) -> int | None:
        for selector in ('[itemprop="reviewCount"]', '[itemprop="ratingCount"]',
                         '[class*="review-count"]', '[class*="reviewCount"]'):
            for el in soup.select(selector)[:4]:
                value = el.get("content") or el.get_text(" ", strip=True)
                count = parse_review_count(value)
                if count is not None:
                    return count
        match = re.search(r"([\d,]+)\s+reviews?", text, re.I)
        return parse_review_count(match.group(0)) if match else None

    @staticmethod
    def _dom_address(soup: BeautifulSoup) -> str:
        for selector in ('[itemprop="address"]', '[class*="address"]', '[class*="Address"]',
                         'address'):
            el = soup.select_one(selector)
            if el:
                value = clean_text(el.get_text(" ", strip=True))
                if len(value) > 10:
                    return value[:300]
        return ""

    @staticmethod
    def _badge_text(soup: BeautifulSoup) -> str:
        """Concatenate the small badge-ish elements where 'Featured' appears."""
        chunks: list[str] = []
        for selector in ('[class*="badge"]', '[class*="Badge"]', '[class*="tag"]',
                         '[class*="featured"]', '[class*="Featured"]', '[class*="label"]'):
            for el in soup.select(selector)[:20]:
                chunks.append(el.get_text(" ", strip=True))
        return " ".join(chunks)[:2000]

    # --- fetching ---------------------------------------------------------

    def _fetch(self, url: str) -> str:
        """Plain GET when `plain_http_first` is on and it returns a real page,
        else the browser.

        WedMeGood serves complete, server-rendered HTML to a plain client but a
        Cloudflare block page to headless Chromium, so for it the cheap path is
        also the only one that works.
        """
        if self.cfg.get("plain_http_first", False):
            try:
                response = self.ctx.http.get(url, rate_key=self.host)
                if response.status_code == 200 and len(response.content) >= _MIN_LISTING_BYTES:
                    return response.text
                log.info("%s: plain GET of %s gave HTTP %s / %d bytes; rendering instead",
                         self.name, url, response.status_code, len(response.content))
            except BlockedError:
                raise
            except Exception as exc:
                log.info("%s: plain GET of %s failed (%r); rendering instead", self.name, url, exc)
        return self._render(url)

    def _render(self, url: str) -> str:
        """Fetch a URL with Playwright so client-side content is present."""
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait(self.host)
            page.goto(url, wait_until="domcontentloaded")
            self.ctx.browser.check_blocked(page)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception as exc:
                # networkidle never settles on ad-heavy pages; not fatal
                log.debug("%s: networkidle never settled for %s (%r)", self.name, url, exc)
            self.ctx.browser.human_pause(0.5, 1.4)
            return page.content()


class WedMeGoodSource(DirectorySource):
    name = "wedmegood"
    priority = 30
    base_url = "https://www.wedmegood.com"
    listing_template = "https://www.wedmegood.com/vendors/{city}/{slug}/?page={page}"
    # Vendors live under /profile/, venues under /wedding-venues/<Name>-<id>;
    # the anchored id keeps /wedding-venues/<x>/reviews/... sub-pages out.
    profile_pattern = re.compile(
        r"wedmegood\.com/(?:profile/[\w\-]+|wedding-venues/[\w\-]+-\d+(?:$|[?#]))", re.I)
    slug_key = "wedmegood"
    seed_slug = "wedding-photographers"
    slug_href_template = r"/vendors/{city}/([a-z0-9\-]+)/"
    static_slugs = WEDMEGOOD_STATIC_SLUGS
    is_wedmegood = True


class WeddingWireSource(DirectorySource):
    name = "weddingwire"
    priority = 31
    base_url = "https://www.weddingwire.in"
    listing_template = "https://www.weddingwire.in/{slug}/{city}?page={page}"
    profile_pattern = re.compile(r"weddingwire\.in/[\w\-]+/[\w\-]+--e\d+", re.I)
    slug_key = "weddingwire"
    seed_slug = "wedding-photographers"
    slug_href_template = r"/([a-z0-9\-]+)/{city}(?:[/?#]|$)"
