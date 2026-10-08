"""Press-release and other RSS / Atom feeds.

* ``press_releases`` (opt-in): GlobeNewswire's per-country feed of the latest
  releases (``/RssFeed/country/{Country}/…``), each naming its issuer in
  ``dc:contributor``. Its robots.txt allows ``/RssFeed/``; its terms page could
  not be found on 2026-10-08, so the source is off until the owner decides.
* ``feeds``: any RSS / Atom URLs a config lists -- feeds the user is entitled
  to use (an industry body's news, a regional business feed with a licence).
"""

from __future__ import annotations

from urllib.parse import quote

from .base import FEED_HEADERS, NewsItem, parse_feed

GLOBENEWSWIRE_HOST = "www.globenewswire.com"
GLOBENEWSWIRE = ("https://www.globenewswire.com/RssFeed/country/{country}/feedTitle/"
                 "GlobeNewswire%20-%20News%20from%20{country}")
#: The country names GlobeNewswire's feeds use where they differ from ours.
GLOBENEWSWIRE_COUNTRIES = {"GB": "United Kingdom", "US": "United States",
                           "AE": "United Arab Emirates"}


def globenewswire_url(country_code: str, country_name: str) -> str:
    name = GLOBENEWSWIRE_COUNTRIES.get((country_code or "").upper(), country_name)
    return GLOBENEWSWIRE.format(country=quote(name))


def read(http, url: str, *, source: str, rate_key: str | None = None) -> list[NewsItem]:
    response = http.get(url, rate_key=rate_key,
                        headers={**FEED_HEADERS,
                                 "Accept": "application/rss+xml, application/atom+xml, "
                                           "application/xml;q=0.9, */*;q=0.5"})
    return parse_feed(response.content, source=source, feed=url)
