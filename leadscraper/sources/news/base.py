"""News items, and reading RSS / Atom feeds safely.

A ``NewsItem`` is one headline as a news source publishes it: title, link,
date, publisher and (for feeds) a short summary. Press-release feeds also name
the issuing organisation (GlobeNewswire's ``dc:contributor``), which beats
reading it out of the headline.

Feeds are parsed with lxml, entities unresolved and no network access, so a
hostile feed can neither expand entities nor make us fetch anything.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from lxml import etree

from ...core.geo import USER_AGENT
from ..jobs.base import parse_time

#: News APIs and feeds are machine endpoints: we say who we are (GlobeNewswire refuses a
#: browser User-Agent outright, verified 2026-10-08).
FEED_HEADERS = {"User-Agent": USER_AGENT}


@dataclass
class NewsItem:
    title: str
    url: str
    published_at: datetime | None = None
    #: The source name: gdelt, google_news, press_releases, feeds.
    source: str = ""
    #: The outlet ("Economic Times"), or the wire for a press release.
    publisher: str = ""
    #: Plain-text summary (feeds); GDELT gives none.
    summary: str = ""
    #: The organisation the item is from, when the feed says (a press release's issuer).
    company: str = ""
    #: Links in the summary (a press release often names the issuer's site).
    links: list[str] = field(default_factory=list)
    #: The query or feed it came from.
    feed: str = ""

    @property
    def key(self) -> str:
        return self.url.split("#", 1)[0]

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}".strip()


_SPACED = (
    (re.compile(r"(\d) , (\d{3})\b"), r"\1,\2"),           # "1 , 000" -> "1,000"
    (re.compile(r"(\d) \. (\d)"), r"\1.\2"),                # "1 . 5" -> "1.5"
    (re.compile(r"(\w) - (\w)"), r"\1-\2"),                 # "pre - series" -> "pre-series"
    (re.compile(r"(\w) ' (s\b)"), r"\1'\2"),                # "Acme ' s" -> "Acme's"
    (re.compile(r"\s+([,.;:!?%)\]])"), r"\1"),             # "funding , led" -> "funding, led"
    (re.compile(r"([(\[$₹€£])\s+"), r"\1"),                 # "( Acme" / "₹ 10" -> "(Acme" / "₹10"
)


def unspace(title: str) -> str:
    """GDELT's titles carry spaces around punctuation ("raises ₹1 , 000 crores"); undo it."""
    text = " ".join((title or "").split())
    for pattern, repl in _SPACED:
        text = pattern.sub(repl, text)
    return text


def plain(markup: str | None) -> tuple[str, list[str]]:
    """Plain text and http(s) links of a feed summary (HTML or text)."""
    if not markup:
        return "", []
    soup = BeautifulSoup(markup, "html.parser")
    links = [a.get("href") or "" for a in soup.find_all("a")]
    text = " ".join(soup.get_text(" ").split())
    return html.unescape(text), [u for u in links if u.startswith(("http://", "https://"))]


def feed_time(value: str | None) -> datetime | None:
    """RFC 822 (RSS), ISO 8601 (Atom), or GDELT's ``20261002T083000Z``."""
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d{8}T\d{6}Z", value):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return parse_time(value)


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False,
                           recover=True)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _children(node) -> dict[str, list]:
    out: dict[str, list] = {}
    for child in node:
        out.setdefault(_local(child.tag), []).append(child)
    return out


def _text(nodes: list | None) -> str:
    if not nodes:
        return ""
    return "".join(nodes[0].itertext()).strip()


def parse_feed(content: bytes | str, *, source: str, feed: str = "") -> list[NewsItem]:
    """The items of an RSS 2.0 or Atom feed, in feed order. Never raises: junk gives []."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    try:
        root = etree.fromstring(content, _parser())
    except (etree.XMLSyntaxError, ValueError):
        return []
    if root is None:
        return []
    items: list[NewsItem] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        parts = _children(node)
        title = " ".join(_text(parts.get("title")).split())
        link = ""
        for candidate in parts.get("link") or []:
            href = candidate.get("href")
            if href and candidate.get("rel") in (None, "alternate"):
                link = href
                break
            if not href and (candidate.text or "").strip():
                link = candidate.text.strip()
                break
        if not link:
            guid = _text(parts.get("guid"))
            link = guid if guid.startswith(("http://", "https://")) else ""
        if not title or not link.startswith(("http://", "https://")):
            continue
        summary, links = plain(_text(parts.get("description") or parts.get("summary")
                                     or parts.get("content")))
        published = feed_time(_text(parts.get("pubdate") or parts.get("published")
                                    or parts.get("updated") or parts.get("date")))
        source_node = (parts.get("source") or [None])[0]
        publisher = " ".join(("".join(source_node.itertext()) if source_node is not None
                              else "").split())
        company = _text(parts.get("contributor"))
        items.append(NewsItem(title=title, url=link, published_at=published, source=source,
                              publisher=publisher, summary=summary[:2000], company=company,
                              links=links[:20], feed=feed))
    return items
