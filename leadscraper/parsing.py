"""Structured-data extraction shared by the directory scrapers.

Most Indian vendor directories ship JSON-LD (`schema.org/LocalBusiness`,
`ItemList`, `AggregateRating`) for SEO. Reading that is far more stable than
chasing their CSS class names, so every directory scraper tries JSON-LD first
and only falls back to DOM heuristics when it's absent.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Iterator

from bs4 import BeautifulSoup

from .normalize import (
    clean_text, extract_emails, extract_phones, normalize_url, parse_rating,
    parse_review_count,
)

log = logging.getLogger(__name__)

# schema.org types that describe a business we'd want as a lead.
BUSINESS_TYPES = {
    "localbusiness", "organization", "store", "restaurant", "eventvenue",
    "professionalservice", "caterer", "photograph", "photographystudio",
    "entertainmentbusiness", "florist", "product", "service", "place",
}


def iter_jsonld(soup: BeautifulSoup) -> Iterator[dict[str, Any]]:
    """Yield every JSON-LD object on the page, flattening @graph and arrays."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        # Some sites emit trailing commas or HTML comments inside the block.
        cleaned = re.sub(r"<!--.*?-->", "", raw, flags=re.S).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", cleaned))
            except json.JSONDecodeError:
                continue
        yield from _flatten(data)


def _flatten(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _flatten(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _flatten(node[key])


def _type_of(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type") or node.get("type") or ""
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).lower() for v in values if v}


def looks_like_business(node: dict[str, Any]) -> bool:
    types = _type_of(node)
    if any(t in BUSINESS_TYPES or t.endswith("business") for t in types):
        return bool(node.get("name"))
    # Untyped nodes with a name + address/phone are still useful.
    return bool(node.get("name")) and bool(
        node.get("telephone") or node.get("address") or node.get("aggregateRating")
    )


def _address_string(value: Any) -> str:
    """Flatten a PostalAddress (or plain string) into one line."""
    if not value:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return _address_string(value[0]) if value else ""
    if isinstance(value, dict):
        parts = [
            value.get("streetAddress"), value.get("addressLocality"),
            value.get("addressRegion"), value.get("postalCode"),
        ]
        return clean_text(", ".join(str(p) for p in parts if p))
    return ""


def rating_from_node(node: dict[str, Any]) -> tuple[float | None, int | None]:
    """Pull (rating, review_count) out of an aggregateRating block."""
    agg = node.get("aggregateRating") or node.get("review") or {}
    if isinstance(agg, list):
        agg = agg[0] if agg else {}
    if not isinstance(agg, dict):
        return None, None
    rating = parse_rating(agg.get("ratingValue"))
    count = parse_review_count(agg.get("reviewCount") or agg.get("ratingCount"))
    return rating, count


def business_from_jsonld(node: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one JSON-LD business node into a plain dict of lead fields."""
    if not looks_like_business(node):
        return None
    name = clean_text(node.get("name"))
    if not name:
        return None

    phones: list[str] = []
    for key in ("telephone", "phone", "contactPoint"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("telephone")
        if isinstance(value, list):
            for item in value:
                phones.extend(extract_phones(str(item.get("telephone") if isinstance(item, dict) else item)))
        elif value:
            phones.extend(extract_phones(str(value)))

    emails = extract_emails(str(node.get("email") or ""))
    rating, reviews = rating_from_node(node)

    url = node.get("url") or node.get("@id")
    if isinstance(url, dict):
        url = url.get("@id") or url.get("url")

    return {
        "business_name": name,
        "phones": phones,
        "emails": emails,
        "address": _address_string(node.get("address")),
        "rating": rating,
        "reviews": reviews,
        "url": normalize_url(url) if isinstance(url, str) else None,
        "description": clean_text(node.get("description"))[:500],
    }


def businesses_from_page(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Every distinct business described by JSON-LD on this page."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in iter_jsonld(soup):
        record = business_from_jsonld(node)
        if not record:
            continue
        key = record["business_name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    """First matching <meta> content, checked against name= and property=."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def visible_text(soup: BeautifulSoup, limit: int = 20_000) -> str:
    """Page text with script/style stripped -- the input to regex extractors.

    Works on a copy: callers reuse their soup after this, so decomposing tags
    in place would quietly corrupt their later selectors.
    """
    try:
        clone = copy.copy(soup)
    except Exception:
        clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return clean_text(clone.get_text(" ", strip=True))[:limit]
