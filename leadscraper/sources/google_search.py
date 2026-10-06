"""Google SERP scraping, plus the shared `serp_search` helper.

Google Search is the CAPTCHA-iest source here, so it gets the longest delays
in config.yaml and an automatic DuckDuckGo fallback: when Google blocks us,
we keep the run productive instead of losing the query entirely.

Three sources ride on this module -- plain vendor discovery, the LinkedIn
`site:` probe, and the Facebook page probe -- so they all share one pacing
budget on google.com rather than each pacing itself in isolation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import parse_qs, urlparse

from ..config import Category, Locality
from ..core.lead import Lead
from ..core.normalize import (
    clean_text, domain_of, extract_emails, extract_phones, is_vendor_website,
    normalize_url,
)
from ..net.ratelimit import BlockedError
from .base import Source

log = logging.getLogger(__name__)

GOOGLE_SEARCH = "https://www.google.com/search"
DDG_SEARCH = "https://html.duckduckgo.com/html/"

# Directory domains: a hit here is corroboration, not a vendor website.
KNOWN_DIRECTORIES = {
    "justdial.com": "justdial",
    "sulekha.com": "sulekha",
    "indiamart.com": "indiamart",
    "wedmegood.com": "wedmegood",
    "weddingwire.in": "weddingwire",
    "weddingz.in": "weddingz",
    "venuelook.com": "venuelook",
    "bookeventz.com": "bookeventz",
    "eventjuicer.com": "eventjuicer",
}

# Booking platforms: a hit is a LISTING of the venue, recorded as a platform.
KNOWN_PLATFORMS = {
    "stayvista.com": "stayvista",
    "vistarooms.com": "stayvista",
    "saffronstays.com": "saffronstays",
    "elivaas.com": "elivaas",
    "lohono.com": "lohono",
    "isprava.com": "isprava",
    "luxunlock.com": "luxunlock",
    "airbnb.co.in": "airbnb",
    "airbnb.com": "airbnb",
    "booking.com": "booking",
    "makemytrip.com": "makemytrip",
    "goibibo.com": "goibibo",
    "agoda.com": "agoda",
    "tripadvisor.in": "tripadvisor",
    "tripadvisor.com": "tripadvisor",
}

# Title separators used by virtually every Indian vendor site.
_TITLE_SPLIT = re.compile(r"\s+[\|\-–—:·]\s+")
_TITLE_NOISE = re.compile(
    r"\b(best|top\s*\d*|no\.?\s*1|leading|professional|affordable|cheap|"
    r"hire|book|list of|\d{4})\b", re.I
)


class _SerpHealth:
    """Circuit breaker for the whole search-engine path.

    When an IP is CAPTCHA'd by Google *and* throttled by DuckDuckGo, every
    SERP call still costs the full retry ladder plus an escalating rate-limit
    penalty -- minutes per query, for results that will never arrive. Five
    sources fall back to search, so that compounds into a run that cannot
    finish. After `threshold` consecutive total failures we stop calling out
    entirely and let those sources fail fast on their direct paths instead.
    """

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.consecutive_failures = 0
        self.disabled = False

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if not self.disabled and self.consecutive_failures >= self.threshold:
            self.disabled = True
            log.warning(
                "search engines unreachable after %d consecutive failures "
                "(Google CAPTCHA + DuckDuckGo throttling) -- disabling the SERP "
                "fallback for the rest of this run so sources fail fast",
                self.consecutive_failures,
            )

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.disabled = False


#: Process-wide, deliberately shared by every SerpClient instance.
SERP_HEALTH = _SerpHealth()


@dataclass
class SerpResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = "google"

    @property
    def domain(self) -> str | None:
        return domain_of(self.url)


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<encoded>."""
    if "duckduckgo.com/l/" not in href and not href.startswith("/l/"):
        return href
    query = parse_qs(urlparse(href).query)
    target = query.get("uddg", [None])[0]
    return target or href


def _decode_google_url(href: str) -> str | None:
    """Google sometimes emits /url?q=<target>&sa=..."""
    if href.startswith("/url?"):
        query = parse_qs(urlparse(href).query)
        return query.get("q", [None])[0]
    if href.startswith("http"):
        return href
    return None


class SerpClient:
    """Paced SERP fetcher with a DuckDuckGo fallback."""

    def __init__(self, ctx, *, fallback: bool = True):
        self.ctx = ctx
        self.fallback = fallback

    def search(self, query: str, *, pages: int = 1, per_page: int = 20) -> list[SerpResult]:
        """Run `query` against Google, then a rendered Google, then DuckDuckGo.

        Google serves a JavaScript-only shell to plain HTTP clients, so the
        cheap requests tier usually yields nothing and we escalate to the
        headless browser. The requests tier is kept because it's an order of
        magnitude cheaper on the runs where it does work.
        """
        if SERP_HEALTH.disabled:
            return []

        results: list[SerpResult] = []
        google_blocked = False

        for page_index in range(pages):
            try:
                results.extend(self._google(query, page_index, per_page))
            except BlockedError as exc:
                log.warning("Google SERP blocked for %r: %s", query, exc)
                google_blocked = True
                break
            except Exception as exc:
                log.info("Google SERP failed for %r: %s", query, exc)
                break

        if not results and not google_blocked and self._can_render():
            log.info("Google returned no parseable results for %r; rendering it", query)
            for page_index in range(pages):
                try:
                    results.extend(self._google_rendered(query, page_index, per_page))
                except BlockedError as exc:
                    log.warning("Rendered Google blocked for %r: %s", query, exc)
                    google_blocked = True
                    break
                except Exception as exc:
                    log.info("Rendered Google failed for %r: %s", query, exc)
                    break

        if not results and self.fallback:
            reason = "google blocked" if google_blocked else "google returned nothing"
            log.info("Falling back to DuckDuckGo for %r (%s)", query, reason)
            try:
                results.extend(self._duckduckgo(query))
            except Exception as exc:
                log.info("DuckDuckGo fallback failed for %r: %s", query, exc)

        # Feed the circuit breaker before returning.
        if results:
            SERP_HEALTH.record_success()
        else:
            SERP_HEALTH.record_failure()

        # Dedup by URL, preserving rank order.
        seen: set[str] = set()
        unique: list[SerpResult] = []
        for result in results:
            key = normalize_url(result.url) or result.url
            if key not in seen:
                seen.add(key)
                unique.append(result)
        return unique

    def _google(self, query: str, page_index: int, per_page: int) -> list[SerpResult]:
        params = {
            "q": query,
            "num": str(min(per_page, 20)),
            "hl": "en",
            "gl": "in",
            "start": str(page_index * per_page),
            # `pws=0` disables personalisation, which makes results reproducible.
            "pws": "0",
        }
        response = self.ctx.http.get(
            GOOGLE_SEARCH, params=params, rate_key="www.google.com",
            headers={"Referer": "https://www.google.com/"},
        )
        return self._parse_google(response.text)

    def _can_render(self) -> bool:
        return getattr(self.ctx, "browser", None) is not None

    def _google_rendered(self, query: str, page_index: int, per_page: int) -> list[SerpResult]:
        """Same query, run through the headless browser so Google's JS runs."""
        from urllib.parse import urlencode

        params = urlencode({
            "q": query, "num": str(min(per_page, 20)), "hl": "en", "gl": "in",
            "start": str(page_index * per_page), "pws": "0",
        })
        url = f"{GOOGLE_SEARCH}?{params}"
        with self.ctx.browser.page() as page:
            self.ctx.limiter.wait("www.google.com")
            page.goto(url, wait_until="domcontentloaded")
            self.ctx.browser.dismiss_google_consent(page)
            self.ctx.browser.check_blocked(page)
            try:
                page.wait_for_selector("h3", timeout=12_000)
            except Exception:
                pass  # a genuinely empty SERP has no h3 either
            self.ctx.browser.human_pause(0.5, 1.5)
            return self._parse_google(page.content())

    def _parse_google(self, markup: str) -> list[SerpResult]:
        soup = self.ctx.http.soup(markup)
        results: list[SerpResult] = []
        for heading in soup.select("h3"):
            anchor = heading.find_parent("a")
            if anchor is None:
                continue
            href = _decode_google_url(anchor.get("href", ""))
            if not href or "google.com" in (domain_of(href) or ""):
                continue
            # The snippet is the nearest following text block.
            container = heading.find_parent("div", recursive=True)
            snippet = ""
            for _ in range(4):
                if container is None:
                    break
                text = clean_text(container.get_text(" ", strip=True))
                if len(text) > len(heading.get_text(strip=True)) + 40:
                    snippet = text
                    break
                container = container.parent
            results.append(SerpResult(
                title=clean_text(heading.get_text(" ", strip=True)),
                url=href, snippet=snippet, engine="google",
            ))
        return results

    def _duckduckgo(self, query: str) -> list[SerpResult]:
        response = self.ctx.http.get(
            DDG_SEARCH, params={"q": query, "kl": "in-en"},
            rate_key="duckduckgo.com",
            headers={"Referer": "https://duckduckgo.com/"},
            # 202 + an empty body is how DDG says "slow down".
            blocked_statuses=(202,),
        )
        soup = self.ctx.http.soup(response.text)
        results: list[SerpResult] = []
        for anchor in soup.select("a.result__a"):
            href = _decode_ddg_url(anchor.get("href", ""))
            if not href or not href.startswith("http"):
                continue
            block = anchor.find_parent(class_="result")
            snippet = ""
            if block is not None:
                snippet_el = block.select_one(".result__snippet")
                if snippet_el:
                    snippet = clean_text(snippet_el.get_text(" ", strip=True))
            results.append(SerpResult(
                title=clean_text(anchor.get_text(" ", strip=True)),
                url=href, snippet=snippet, engine="duckduckgo",
            ))
        return results


# Generic words that never distinguish one vendor from another.
_GENERIC_TOKENS = {
    "mumbai", "india", "maharashtra", "near", "me", "in", "the", "and", "for",
    "of", "services", "service", "company", "co", "best", "top", "professional",
    "corporate", "verified", "affordable", "cheap", "hire", "book", "booking",
    "list", "price", "prices", "cost", "packages", "package", "contact",
    "number", "phone", "online", "reviews", "review", "rating", "photos",
}


def business_name_from_title(
    title: str, locality: str = "", category_terms: tuple[str, ...] = ()
) -> str:
    """Turn a SERP title into a plausible business name, or "" if it isn't one.

    'Royal Caterers in Andheri | Best Wedding Caterers Mumbai 2024'
        -> 'Royal Caterers'

    Search results are full of category pages whose titles are pure keywords
    ("Event Photographer in Mumbai"). Those are not businesses, so anything
    with no distinctive token left after removing the category, the locality
    and generic filler is rejected -- and logged, so nothing vanishes silently.
    """
    if not title:
        return ""
    first = _TITLE_SPLIT.split(title)[0]
    if locality:
        first = re.sub(r"\s+in\s+" + re.escape(locality) + r"\b.*$", "", first, flags=re.I)
    first = _TITLE_NOISE.sub(" ", first)
    first = re.sub(r"\s{2,}", " ", first).strip(" -|,:·&")
    if len(first) < 3 or len(first.split()) > 9:
        return ""

    if not _has_distinctive_token(first, locality, category_terms):
        return ""
    return first


def _has_distinctive_token(name: str, locality: str, category_terms: tuple[str, ...]) -> bool:
    """True when `name` contains a word that isn't category/locality/filler."""
    noise = set(_GENERIC_TOKENS)
    for term in (*category_terms, locality):
        for word in re.split(r"\W+", (term or "").lower()):
            if word:
                noise.add(word)
                # "photographers" shouldn't survive because the query said
                # "photographer" -- match the singular/plural pair too.
                noise.add(word.rstrip("s"))
                noise.add(word + "s")

    for word in re.split(r"\W+", name.lower()):
        if not word or word.isdigit():
            continue
        if word in noise or word.rstrip("s") in noise:
            continue
        return True
    return False


class GoogleSearchSource(Source):
    """Finds vendor sites and directory listings that Maps missed."""

    name = "google_search"
    priority = 20

    def rate_keys(self) -> list[str]:
        return ["www.google.com", "duckduckgo.com"]

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        pages = int(self.cfg.get("pages", 2))
        per_page = int(self.cfg.get("results_per_query", 20))
        client = SerpClient(self.ctx, fallback=bool(self.cfg.get("fallback_to_duckduckgo", True)))

        suffix = self.cfg.get("query_suffix", "contact number")
        for query_term in category.queries[:3]:  # SERP is expensive; cap the fan-out
            query = f'"{query_term}" {locality.primary_term} {suffix}'.strip()
            try:
                results = client.search(query, pages=pages, per_page=per_page)
            except Exception as exc:
                self.fail("search_failed", query=query, locality=locality.name,
                          category=category.name, detail=repr(exc))
                continue

            if not results:
                self.fail("no_results", query=query, locality=locality.name,
                          category=category.name)
                continue

            for result in results:
                lead = self._to_lead(result, locality, category)
                if lead:
                    yield lead

    def _to_lead(self, result: SerpResult, locality: Locality, category: Category) -> Lead | None:
        city = self.ctx.config.city
        name = business_name_from_title(
            result.title, locality.name, (*category.queries, city, *locality.aliases)
        )
        if not name:
            self.fail("generic_or_unparseable_title", url=result.url, locality=locality.name,
                      category=category.name, detail=result.title[:200])
            return None

        host = result.domain or ""
        directory = next(
            (label for dom, label in KNOWN_DIRECTORIES.items()
             if host == dom or host.endswith("." + dom)),
            None,
        )

        lead = self.make_lead(
            business_name=name,
            category=category.name,
            locality=locality.name,
            source_urls=[result.url],
        )
        # Snippets often carry a phone or email straight in the SERP text.
        blob = f"{result.title} {result.snippet}"
        lead.phones = extract_phones(blob)[:2]
        lead.emails = extract_emails(blob)[:2]

        platform = next(
            (label for dom, label in KNOWN_PLATFORMS.items()
             if host == dom or host.endswith("." + dom)),
            None,
        )

        if platform:
            lead.platforms.append(platform)
            lead.listing_urls.append(normalize_url(result.url) or result.url)
            lead.notes.append(f"listed on {platform} (search result)")
        elif directory:
            # A directory hit is corroboration; don't record it as their website.
            lead.sources.append(directory)
            lead.notes.append(f"found via {directory} listing in search results")
        elif is_vendor_website(result.url):
            lead.website = normalize_url(result.url)
        else:
            # Social/aggregator result with no directory mapping -- keep the URL
            # only, so the enrichers can still use it.
            lead.notes.append(f"search result: {host}")
        if self.ctx.config.is_venue_run:
            lead.description = result.snippet
            self.assign_locality(lead, locality)
        return lead
