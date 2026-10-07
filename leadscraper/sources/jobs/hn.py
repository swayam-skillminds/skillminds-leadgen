"""Hacker News "Ask HN: Who is hiring?" through the Algolia HN Search API.

The monthly thread is posted by ``whoishiring``; each top-level comment is one
company's ad, by convention headed ``Company | Role | Location | REMOTE/ONSITE |
URL``. Two requests per thread: the latest threads (``search_by_date``), then
the thread's whole tree (``items/{id}``). Public, no key (verified 2026-10-07).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from ...core.normalize import domain_of, is_vendor_website, normalize_url
from .base import Posting, find_boards, parse_time

HOST = "hn.algolia.com"
THREADS = ("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
           "&hitsPerPage=20")
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
TITLE_PREFIX = "ask hn: who is hiring"
#: Links in an ad that are not the company's own site.
_NOT_SITES = ("news.ycombinator.com", "github.com", "gitlab.com", "docs.google.com",
              "forms.gle", "notion.site", "notion.so", "bit.ly", "goo.gl", "t.co")
_HEAD_SPLIT = re.compile(r"\s+[|•–—]\s+|\s+-\s+|\s*\|\s*")


@dataclass
class Thread:
    id: str
    title: str
    created_at: datetime | None


def threads(http, months: int) -> list[Thread]:
    """The newest ``months`` "Who is hiring?" threads."""
    data = http.get(THREADS, rate_key=HOST, headers={"Accept": "application/json"}).json()
    found = [Thread(str(h.get("objectID")), str(h.get("title") or ""),
                    parse_time(h.get("created_at")))
             for h in data.get("hits") or []
             if str(h.get("title") or "").lower().startswith(TITLE_PREFIX)]
    return found[:months]


def comments(http, thread: Thread) -> list[dict[str, Any]]:
    data = http.get(ITEM.format(id=thread.id), rate_key=HOST,
                    headers={"Accept": "application/json"}).json()
    return [c for c in data.get("children") or [] if c.get("text") and c.get("author")]


def _plain(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    links = [a.get("href") or "" for a in soup.find_all("a")]
    for p in soup.find_all("p"):
        p.insert_before("\n")
    return soup.get_text("").strip(), links


def company_of(headline: str) -> str:
    first = _HEAD_SPLIT.split(headline.strip(), maxsplit=1)[0]
    first = re.sub(r"\s*\([^)]*\)\s*", " ", first).strip(" :,-")
    return first if 0 < len(first) <= 80 else ""


def parse_comment(comment: dict[str, Any], thread: Thread) -> Posting | None:
    """One ad as a Posting (roles are matched later, against the run's families)."""
    text, links = _plain(str(comment.get("text") or ""))
    if not text:
        return None
    headline = text.split("\n", 1)[0].strip()
    company = company_of(headline)
    if not company:
        return None
    site = None
    for link in links:
        url = normalize_url(link)
        host = domain_of(url) or ""
        if url and is_vendor_website(url) and not any(host.endswith(h) for h in _NOT_SITES) \
                and not find_boards([url]):
            site = url
            break
    location_lines = [line for line in text.split("\n")[1:6]
                      if re.match(r"\s*(?:-\s*)?locations?\s*:", line, re.I)]
    location = " ".join([headline, *location_lines])
    return Posting(
        title=headline[:300], url=f"https://news.ycombinator.com/item?id={comment.get('id')}",
        company=company, company_url=site, location=location,
        remote=True if re.search(r"\bremote\b", location, re.I) else None,
        posted_at=parse_time(comment.get("created_at")), board=f"hn:{thread.id}",
        source="hn_whos_hiring", text=text[:4000])
