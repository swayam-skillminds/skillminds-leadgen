"""What the catalog says about each source: endpoint, method, terms note, last check.

The machine-readable twin of ``docs/sources.md`` (keep them in step;
``tests/test_catalog.py`` fails when a registered source has no entry here).
``review`` marks the sources A.2.4 asks the project owner to decide on.
"""

from __future__ import annotations

from typing import Any

NOTES: dict[str, dict[str, Any]] = {
    "ai_web_search": {
        "endpoint": "the configured AI provider's web search (Anthropic web_search tool, "
                    "OpenAI web_search tool, Gemini googleSearch), then each result page",
        "method": "provider API, then requests (robots.txt honoured)",
        "terms": "Pages the provider's search returned, fetched once each at the site's "
                 "own pacing; a lead needs its name and contacts on the fetched page. Only "
                 "with the web_search_discovery AI feature.",
        "review": False, "last_verified": "2026-10-07 (provider API docs; no live call)"},
    "ai_directory": {
        "endpoint": "approved candidate sources (directories a user approved after "
                    "source_discovery proposed them)",
        "method": "requests; self-healing CSS selectors, AI only on a new or changed layout",
        "terms": "Each candidate's terms are the approver's call (its terms_note); robots.txt "
                 "honoured; never login walls. Only with the source_discovery AI feature.",
        "review": True, "last_verified": None},
    "google_maps": {
        "endpoint": "https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z",
        "method": "Playwright (headless)",
        "terms": "Google's terms restrict automated access; a paid Places API exists.",
        "review": True, "last_verified": "2026-10-07 (live check 1: one quick run)"},
    "google_search": {
        "endpoint": "https://www.google.com/search, falling back to "
                    "https://html.duckduckgo.com/html/",
        "method": "requests, then rendered, then DuckDuckGo",
        "terms": "Google's terms restrict automated queries; DuckDuckGo throttles with 202.",
        "review": True, "last_verified": None},
    "website": {
        "endpoint": "the business's own site: home, then up to 3 contact/about pages",
        "method": "requests",
        "terms": "The business's public pages; robots.txt honoured when the workspace "
                 "setting is on.",
        "review": False, "last_verified": None},
    "maps_lookup": {
        "endpoint": "Google Maps search by name", "method": "Playwright",
        "terms": "As google_maps.", "review": True, "last_verified": None},
    "justdial": {
        "endpoint": "https://www.justdial.com/{City}/{query}-in-{locality}",
        "method": "Playwright",
        "terms": "Blocks automated clients; recorded as a failure, never evaded.",
        "review": False, "last_verified": "2026-10-06 (blocked)"},
    "sulekha": {
        "endpoint": "https://www.sulekha.com/{query}/{city}", "method": "Playwright",
        "terms": "", "review": False, "last_verified": "2026-10-06"},
    "indiamart": {
        "endpoint": "https://dir.indiamart.com/search.mp?ss={query}&cq={city}",
        "method": "Playwright", "terms": "IndiaMART runs a seller API, not a search API.",
        "review": False, "last_verified": None},
    "wedmegood": {
        "endpoint": "https://www.wedmegood.com/vendors/{city}/{slug}/?page=N",
        "method": "plain GET, then Playwright", "terms": "", "review": False,
        "last_verified": "2026-10-06"},
    "weddingwire": {
        "endpoint": "https://www.weddingwire.in/{slug}/{city}?page=N", "method": "Playwright",
        "terms": "", "review": False, "last_verified": "2026-10-06 (403 to a plain client)"},
    "stayvista": {
        "endpoint": "https://www.stayvista.com/villas-sitemap.xml, then property pages",
        "method": "requests (sitemap)", "terms": "", "review": False, "last_verified": None},
    "saffronstays": {
        "endpoint": "https://www.saffronstays.com/sitemap-properties.xml",
        "method": "requests (sitemap)", "terms": "", "review": False, "last_verified": None},
    "elivaas": {
        "endpoint": "https://www.elivaas.com/sitemap.xml", "method": "requests (sitemap)",
        "terms": "", "review": False, "last_verified": None},
    "airbnb": {
        "endpoint": "https://www.airbnb.co.in search pages", "method": "Playwright",
        "terms": "Airbnb's terms prohibit scraping.", "review": True, "last_verified": None},
    "booking": {
        "endpoint": "https://www.booking.com search results", "method": "Playwright",
        "terms": "Booking.com's terms prohibit scraping; an affiliate API exists.",
        "review": True, "last_verified": None},
    "facebook": {
        "endpoint": "SERP site:facebook.com, then https://mbasic.facebook.com/{page}",
        "method": "requests",
        "terms": "Public pages only; a login wall is recorded, never logged into.",
        "review": True, "last_verified": None},
    "instagram": {
        "endpoint": "https://www.instagram.com/{handle}/", "method": "requests",
        "terms": "Public profiles only, never logged into.", "review": True,
        "last_verified": None},
    "linkedin_serp": {
        "endpoint": "SERP site:linkedin.com/in", "method": "via google_search",
        "terms": "LinkedIn is never fetched: only search-indexed profile URLs are recorded.",
        "review": False, "last_verified": None},
}


def note(name: str) -> dict[str, Any]:
    return dict(NOTES.get(name) or {"endpoint": "", "method": "", "terms": "",
                                    "review": False, "last_verified": None})
