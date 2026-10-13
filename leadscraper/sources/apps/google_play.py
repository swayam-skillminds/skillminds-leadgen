"""Google Play's public listing page: one app's rating, downloads, last update,
developer contacts and the few reviews it shows.

``GET https://play.google.com/store/apps/details?id={package}&hl=en&gl={country}``
(verified live 2026-10-13). robots.txt allows ``/store/apps/details`` and
disallows ``/store/search``, and Google's terms forbid automated access "in
violation of the machine-readable instructions on our web pages (for example,
robots.txt files…)": so listings are read one by one (packages come from store
links on organisations' own sites, the config, or an iOS bundle id), and Play's
search is never used. A package that isn't listed answers 404.

What the page carries, read without depending on its generated class names:
JSON-LD ``SoftwareApplication`` (name, category, rating, rating count, the
developer's site as ``author.url``), "Updated on" + a date, "<band> Downloads",
the "App support" section (website, support email, phone; "About the developer":
legal name, email, address, phone) and a few reviews (stars, date, text; the
reviewer's name is never kept).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ...core.normalize import domain_of
from .base import GOOGLE_PLAY, AppListing, Review

BASE = "https://play.google.com"
HOST = "play.google.com"
_STARS = re.compile(r"^Rated (\d) stars? out of five stars$")
_DATE = re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, \d{4})\b")
_INSTALLS = re.compile(r">\s*([\d.,]+\s*[KMB]?\+)\s*</div>\s*<div[^>]*>\s*Downloads\s*<", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"^\+?[\d\s()./-]{7,}$")
_MULTIPLIER = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def listing_url(package: str, country: str, lang: str = "en") -> str:
    return f"{BASE}/store/apps/details?" + urlencode({"id": package, "hl": lang,
                                                       "gl": country.lower()})


def public_url(package: str) -> str:
    """The listing's link as the workbook shows it (no language or country)."""
    return f"{BASE}/store/apps/details?" + urlencode({"id": package})


def _date(text: str) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def installs_min(band: str) -> int | None:
    """The lower bound of a download band: "10K+" -> 10000, "1,000+" -> 1000."""
    match = re.match(r"([\d.,]+)\s*([KMB]?)\+?", (band or "").strip(), re.I)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(number * _MULTIPLIER.get(match.group(2).upper(), 1))


def _json_ld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "{}")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "SoftwareApplication":
                return item
    return {}


def _support(soup: BeautifulSoup) -> dict[str, str]:
    """The "App support" section: website, email, phone, and "About the developer"."""
    out: dict[str, str] = {}
    label = soup.find(string=re.compile(r"^\s*App support\s*$"))
    section = label.find_parent(lambda t: t.name in ("section", "div") and
                                "About the developer" in t.get_text()) if label else None
    if section is None:
        return out
    for link in section.find_all("a", href=True):
        href, text = link["href"].strip(), link.get_text(" ", strip=True)
        if href.startswith("mailto:"):
            out.setdefault("email", href[7:].split("?", 1)[0].strip())
        elif href.startswith(("http://", "https://")) and "Website" in text \
                and "website" not in out:
            out["website"] = href
    tokens = [t.strip() for t in section.get_text("\n", strip=True).split("\n") if t.strip()]
    if "Phone number" in tokens:
        after = tokens[tokens.index("Phone number") + 1:tokens.index("Phone number") + 2]
        if after and _PHONE.match(after[0]):
            out["phone"] = after[0]
    if "About the developer" in tokens:
        tail = tokens[tokens.index("About the developer") + 1:]
        if tail and not _EMAIL.fullmatch(tail[0]) and not _PHONE.match(tail[0]):
            out["legal_name"] = tail[0]
            tail = tail[1:]
        address = []
        for token in tail:
            if _EMAIL.fullmatch(token):
                out.setdefault("email", token)
            elif _PHONE.match(token):
                out.setdefault("phone", token)
            elif token.lower().startswith(("more by", "similar apps")):
                break
            else:
                address.append(token)
        if address:
            out["address"] = ", ".join(" ".join(a.split()).strip(" ,") for a in address)[:300]
    return out


#: Text a review's box carries besides the review: menus, flags, helpfulness votes.
_REVIEW_CHROME = re.compile(r"^(?:more_vert|Flag inappropriate|Show review history|"
                            r"Did you find this helpful\??|Yes|No|"
                            r"[\d,]+ (?:people|person) found this review helpful)$", re.I)


def _review_box(label):
    """The review's own element: the largest ancestor that holds this one star rating and
    no other (its parent is the list of reviews)."""
    box = None
    node = label.parent
    for _ in range(10):
        if node is None or node.name in ("body", "html"):
            break
        if len(node.find_all(attrs={"aria-label": _STARS})) > 1:
            break
        box, node = node, node.parent
    return box


def _reviews(soup: BeautifulSoup) -> list[Review]:
    """Each review's stars, date and text. The text is what follows the review's date in
    its own box (the reviewer's name comes before it and is never read)."""
    out: list[Review] = []
    for label in soup.find_all(attrs={"aria-label": _STARS}):
        box = _review_box(label)
        if box is None:
            continue
        lines = [t.strip() for t in box.get_text("\n", strip=True).split("\n") if t.strip()]
        dated = next((i for i, line in enumerate(lines) if _DATE.fullmatch(line)), None)
        if dated is None:
            continue
        body = next((line for line in lines[dated + 1:] if not _REVIEW_CHROME.match(line)), "")
        if not body:
            continue
        stars = int(_STARS.match(label["aria-label"]).group(1))
        out.append(Review(rating=stars, text=body[:1200], date=_date(lines[dated])))
    return out


def parse(html: str, package: str, *, url: str | None = None) -> AppListing | None:
    """The listing, or None when the page isn't an app's listing."""
    soup = BeautifulSoup(html or "", "lxml")
    data = _json_ld(soup)
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    rating = (data.get("aggregateRating") or {})
    author = data.get("author") or {}
    support = _support(soup)
    updated = None
    label = soup.find(string=re.compile(r"^\s*Updated on\s*$"))
    if label is not None:
        holder = label.find_parent()
        sibling = holder.find_next_sibling() if holder is not None else None
        updated = _date(sibling.get_text(" ", strip=True)) if sibling is not None else None
    installs = _INSTALLS.search(html or "")
    band = " ".join(installs.group(1).split()) if installs else ""
    try:
        value = round(float(rating.get("ratingValue")), 2) if rating.get("ratingValue") \
            else None
    except (TypeError, ValueError):
        value = None
    try:
        count = int(str(rating.get("ratingCount") or "").replace(",", "")) \
            if rating.get("ratingCount") else None
    except ValueError:
        count = None
    website = support.get("website") or (str(author.get("url") or "").strip() or None)
    if website and domain_of(website) in ("play.google.com", "google.com"):
        website = None
    return AppListing(
        store=GOOGLE_PLAY, app_id=package, name=name, url=url or public_url(package),
        developer=support.get("legal_name") or str(author.get("name") or "").strip(),
        developer_website=website, developer_email=support.get("email"),
        developer_phone=support.get("phone"), developer_address=support.get("address", ""),
        rating=value, ratings=count, installs=band, installs_min=installs_min(band),
        updated=updated, genre=str(data.get("applicationCategory") or "").replace("_", " ")
        .title(), description=str(data.get("description") or "")[:4000],
        reviews=_reviews(soup))


def fetch(http, package: str, country: str) -> AppListing | None:
    """One package's listing in a country's store; None if it isn't listed there.
    Raises what ``HttpClient.get`` raises otherwise (blocked, failed)."""
    from ...net.ratelimit import PermanentError

    try:
        response = http.get(listing_url(package, country), rate_key=HOST)
    except PermanentError:
        return None
    return parse(response.text or "", package)
