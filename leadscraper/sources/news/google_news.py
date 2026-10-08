"""Google News search as RSS (opt-in: its feed terms forbid our use without permission).

``https://news.google.com/rss/search?q=…&hl=en-IN&gl=IN&ceid=IN:en`` lists up to
100 headlines, each "Headline - Publisher", with a news.google.com link and the
outlet in ``<source>``. The feed itself says (verified 2026-10-08) it "is made
available solely for the purpose of rendering Google News results within a
personal feed reader for personal, non-commercial use. Any other use of the
feed is expressly prohibited." So the source is off unless a config switches
it on, which is the project owner's call (A.2.4).
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .base import FEED_HEADERS, NewsItem, parse_feed

HOST = "news.google.com"
SEARCH = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"


def build_query(words: list[str], *, place: str | None = None, days: int = 30) -> str:
    terms = list(dict.fromkeys(f'"{w}"' if " " in w else w for w in words if w.strip()))
    parts = [f"({' OR '.join(terms)})"] if terms else []
    if place:
        parts.append(f'"{place}"')
    parts.append(f"when:{max(1, days)}d")
    return " ".join(parts)


def search(http, query: str, *, locale: str = "en-US", country: str = "US") -> list[NewsItem]:
    lang = (locale or "en-US").split("-")[0]
    url = SEARCH.format(q=quote_plus(query), hl=locale, gl=(country or "US").upper(), lang=lang)
    response = http.get(url, rate_key=HOST, headers=FEED_HEADERS)
    items = parse_feed(response.content, source="google_news", feed=query)
    for item in items:
        # "Acme raises $5M - Economic Times": the outlet is in <source>, drop it from the title.
        if item.publisher and item.title.endswith(f" - {item.publisher}"):
            item.title = item.title[: -len(item.publisher) - 3].strip()
        item.summary = ""                 # Google's summary is the same headline as a link
        item.links = []
    return items
