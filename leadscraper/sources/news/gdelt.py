"""The GDELT DOC 2.0 API: worldwide news coverage, searched by keywords.

``https://api.gdeltproject.org/api/v2/doc/doc?query=…&mode=artlist&format=json``
returns up to 250 articles (title, URL, outlet domain, source country, when
GDELT saw it) from the last three months. Free, no key; GDELT's terms allow
any use, commercial included, with a citation of the GDELT Project, and it
asks for at most one request every 5 seconds (verified 2026-10-08).

GDELT matches the query against the whole article, so its results are only
candidates: the lead type's headline rules decide what is an event.
"""

from __future__ import annotations

import re
from typing import Any

from .base import FEED_HEADERS, NewsItem, feed_time, unspace

HOST = "api.gdeltproject.org"
API = "https://api.gdeltproject.org/api/v2/doc/doc"
#: GDELT's own window.
MAX_DAYS = 90


class GdeltError(Exception):
    """GDELT answered with a message instead of JSON (a bad query, a throttle)."""


def _term(word: str) -> str:
    word = " ".join(word.split())
    return f'"{word}"' if re.search(r"[\s\-&]", word) else word


def build_query(words: list[str], *, place: str | None = None, country: str | None = None,
                language: str | None = None) -> str:
    """``(raises OR funding OR "seed round") Pune sourcecountry:india sourcelang:english``."""
    terms = list(dict.fromkeys(_term(w) for w in words if w and w.strip()))
    parts = []
    if terms:
        parts.append(terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})")
    if place:
        parts.append(_term(place))
    if country:
        parts.append(f"sourcecountry:{re.sub(r'[^a-z]', '', country.lower())}")
    if language:
        parts.append(f"sourcelang:{language.lower()}")
    return " ".join(parts)


def search(http, query: str, *, days: int, max_records: int = 100) -> list[NewsItem]:
    """Articles matching ``query`` in the last ``days`` days, newest first.

    Raises ``GdeltError`` when GDELT answers with text (a bad query or its
    throttle message), and the HTTP client's errors as they are.
    """
    params = {"query": query, "mode": "artlist", "format": "json", "sort": "datedesc",
              "maxrecords": str(max(1, min(250, max_records))),
              "timespan": f"{max(1, min(MAX_DAYS, days))}d"}
    # One attempt: GDELT throttles per IP, and retrying a 429 only digs the hole deeper.
    response = http.get(API, rate_key=HOST, params=params, attempts=1,
                        headers={**FEED_HEADERS, "Accept": "application/json"})
    try:
        data: Any = response.json()
    except ValueError:
        raise GdeltError(" ".join((response.text or "").split())[:300]) from None
    items = []
    for article in (data or {}).get("articles") or []:
        url = str(article.get("url") or "")
        title = unspace(str(article.get("title") or ""))
        if not title or not url.startswith(("http://", "https://")):
            continue
        items.append(NewsItem(title=title, url=url,
                              published_at=feed_time(str(article.get("seendate") or "")),
                              source="gdelt", publisher=str(article.get("domain") or ""),
                              feed=query))
    return items
