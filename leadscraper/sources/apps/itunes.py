"""Apple's iTunes Search API: apps by keyword in one storefront, and lookups by id.

``GET https://itunes.apple.com/search?term=…&country=in&entity=software&limit=…``
and ``GET https://itunes.apple.com/lookup?id=…&country=in`` (verified live
2026-10-13): JSON with ``trackId``, ``trackName``, ``sellerName``, ``sellerUrl``
(the developer's site, often), ``averageUserRating``, ``userRatingCount``,
``currentVersionReleaseDate``, ``releaseDate``, ``version``, ``bundleId``,
``primaryGenreName``, ``description``. No key. Apple limits it to "approximately
20 calls per minute", so calls are paced 3.5–5 s apart. Only the API: Apple's
site terms forbid page-scraping its web pages, and robots.txt disallows the
review feeds, so App Store reviews are not read.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ...core.geo import USER_AGENT
from .base import APP_STORE, AppListing, iso_date

API = "https://itunes.apple.com"
HOST = "itunes.apple.com"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


def search_url(term: str, country: str, limit: int) -> str:
    return f"{API}/search?" + urlencode({"term": term, "country": country.lower(),
                                         "entity": "software", "limit": max(1, min(200, limit))})


def lookup_url(ids: list[str], country: str) -> str:
    return f"{API}/lookup?" + urlencode({"id": ",".join(ids), "country": country.lower(),
                                         "entity": "software"})


def parse(item: dict[str, Any]) -> AppListing | None:
    """One result, or None for anything that isn't an app."""
    if item.get("wrapperType") != "software" and item.get("kind") != "software":
        return None
    track = item.get("trackId")
    name = str(item.get("trackName") or "").strip()
    if not track or not name:
        return None
    rating = item.get("averageUserRating")
    count = item.get("userRatingCount")
    return AppListing(
        store=APP_STORE, app_id=str(track), name=name,
        url=str(item.get("trackViewUrl") or "").split("?uo=", 1)[0],
        developer=str(item.get("sellerName") or item.get("artistName") or "").strip(),
        developer_website=str(item.get("sellerUrl") or "").strip() or None,
        bundle_id=str(item.get("bundleId") or ""),
        rating=round(float(rating), 2) if isinstance(rating, (int, float)) else None,
        ratings=int(count) if isinstance(count, (int, float)) else None,
        updated=iso_date(item.get("currentVersionReleaseDate")),
        released=iso_date(item.get("releaseDate")),
        version=str(item.get("version") or ""),
        genre=str(item.get("primaryGenreName") or ""),
        description=str(item.get("description") or "")[:4000])


def _results(http, url: str) -> list[AppListing]:
    response = http.get(url, rate_key=HOST, headers=HEADERS)
    data = response.json() or {}
    out = []
    for item in data.get("results") or []:
        listing = parse(item) if isinstance(item, dict) else None
        if listing is not None:
            out.append(listing)
    return out


def search(http, term: str, country: str, limit: int = 50) -> list[AppListing]:
    """Apps matching ``term`` in the ``country`` storefront, as Apple ranks them."""
    return _results(http, search_url(term, country, limit))


def lookup(http, ids: list[str], country: str) -> list[AppListing]:
    """Apps by track id (up to 100 a call: Apple accepts a comma-separated list)."""
    out: list[AppListing] = []
    for start in range(0, len(ids), 100):
        out.extend(_results(http, lookup_url(ids[start:start + 100], country)))
    return out
