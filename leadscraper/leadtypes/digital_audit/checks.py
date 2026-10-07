"""The audit's checks as pure functions: page facts in, plain-language findings out.

Nothing here touches the network (``auditor.py`` fetches); every check is a
function of what was fetched, so each one is tested on fixture pages.

A finding is one signal (``FINDINGS`` are the lead type's signal specs):
its ``penalty`` comes off the audit score (100 = nothing found), its
``strength`` is how strong a reason to buy it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ...core.normalize import domain_of
from .fingerprint import Detected, detect


@dataclass(frozen=True)
class FindingType:
    type: str
    #: The config check that produces it.
    check: str
    title: str
    strength: float
    #: Points off the audit score.
    penalty: int
    description: str


FINDINGS: dict[str, FindingType] = {f.type: f for f in (
    FindingType("no_website", "no_website", "No website", 0.8, 60,
                "No website of its own (a social profile or listing only)."),
    FindingType("site_unreachable", "unreachable", "Website unreachable", 0.75, 50,
                "Its website did not load: it doesn't resolve, refuses connections or errors."),
    FindingType("no_https", "https", "No HTTPS", 0.6, 20,
                "The site doesn't load over HTTPS, or its certificate is invalid."),
    FindingType("cert_expiring", "https", "Certificate expiring", 0.5, 10,
                "The HTTPS certificate expires soon."),
    FindingType("not_mobile_friendly", "mobile", "Not mobile-friendly", 0.55, 15,
                "No responsive viewport: the page renders at desktop width on phones."),
    FindingType("slow_site", "pagespeed", "Slow site", 0.45, 10,
                "Google PageSpeed Insights scores its performance poorly."),
    FindingType("outdated_tech", "cms_age", "Outdated technology", 0.6, 15,
                "A CMS, framework or server version that is past its end of life."),
    FindingType("stale_copyright", "copyright", "Stale copyright year", 0.3, 5,
                "The footer's copyright year is years old: the site is likely unmaintained."),
    FindingType("broken_links", "broken_links", "Broken links", 0.35, 8,
                "Links on the homepage lead to missing pages."),
    FindingType("no_contact_form", "contact_form", "No contact form", 0.3, 5,
                "Neither the homepage nor its contact page has an enquiry form."),
    FindingType("no_online_booking", "booking", "No online booking or ordering", 0.5, 10,
                "Its category expects customers to book or order online; the site offers "
                "neither."),
    FindingType("incomplete_maps_profile", "maps_profile", "Incomplete Maps profile", 0.35, 7,
                "Its Google Maps listing lacks a website, phone or address, or has few "
                "reviews."),
)}


@dataclass
class Finding:
    type: str
    #: One plain-language sentence.
    text: str
    #: Extra facts for the signal payload (versions, counts, scores).
    data: dict[str, Any] = field(default_factory=dict)
    url: str | None = None

    @property
    def spec(self) -> FindingType:
        return FINDINGS[self.type]


def audit_score(findings: Iterable[Finding | str]) -> int:
    """100 minus each distinct finding type's penalty, floored at 0."""
    types = {f if isinstance(f, str) else f.type for f in findings}
    return max(0, 100 - sum(FINDINGS[t].penalty for t in types if t in FINDINGS))


# --- what a page shows -------------------------------------------------------------------------

@dataclass
class PageFacts:
    url: str
    viewport: str | None = None
    generators: list[str] = field(default_factory=list)
    asset_urls: list[str] = field(default_factory=list)
    copyright_year: int | None = None
    internal_links: list[str] = field(default_factory=list)
    contact_links: list[str] = field(default_factory=list)
    has_contact_form: bool = False
    booking: list[str] = field(default_factory=list)
    ordering: list[str] = field(default_factory=list)


_COPYRIGHT = re.compile(r"(?:©|&copy;|\(c\)|copyright)", re.I)
_YEAR = re.compile(r"\b(19[89]\d|20\d\d)\b")
_CONTACT_LINK = re.compile(r"contact|enquir|inquir|get[-_ ]in[-_ ]touch|reach[-_ ]us|kontakt",
                           re.I)
_SKIP_LINK = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|mp4|mp3|docx?|xlsx?)$", re.I)
#: Embedded form services that count as a contact form.
_FORM_EMBEDS = re.compile(r"typeform\.com|docs\.google\.com/forms|forms\.gle|jotform|"
                          r"hsforms|wufoo\.com|formspree\.io|tally\.so|wpcf7|contact-form-7|"
                          r"wpforms|gravityforms|ninja-forms|forms\.office\.com", re.I)

#: Online booking: platforms (hosts) and phrases.
BOOKING_HOSTS = ("calendly.com", "fresha.com", "booksy.com", "setmore.com", "simplybook.",
                 "acuityscheduling.com", "squareup.com/appointments", "square.site",
                 "mindbodyonline.com", "zocdoc.com", "practo.com", "vagaro.com",
                 "opentable.", "resy.com", "sevenrooms.com", "tablein.com", "eazydiner.com",
                 "dineout", "cloudbeds.com", "synxis.com", "ezeereservation", "ezeeabsolute",
                 "staah.", "siteminder.com", "booking.com", "airbnb.", "bookeo.com",
                 "appointy.com", "picktime.com", "zenoti.com", "youcanbook.me", "cal.com/",
                 "10to8.com", "doctolib.")
BOOKING_PHRASES = re.compile(
    r"book (?:now|online|an? appointment|a table|a session|your (?:stay|visit|room|table))|"
    r"schedule (?:an? )?(?:appointment|visit|consultation)|reserve (?:a table|now|online)|"
    r"make a reservation|check availability|online booking|appointment booking", re.I)
ORDERING_HOSTS = ("swiggy.com", "zomato.com", "ubereats.com", "doordash.com", "grubhub.com",
                  "deliveroo.", "toasttab.com", "order.online", "chownow.com", "foodpanda.",
                  "talabat.com", "just-eat.", "menulog.", "petpooja", "dotpe.")
ORDERING_PHRASES = re.compile(
    r"order (?:now|online|food|here|for delivery|for pickup)|add to (?:cart|basket|bag)|"
    r"shop now|buy now|checkout|online store", re.I)

#: Category words -> what customers expect to do online. Matched as whole words
#: (plural allowed) in the organisation's category.
EXPECTATIONS: dict[str, tuple[str, ...]] = {
    "restaurant": ("booking", "ordering"), "cafe": ("ordering", "booking"),
    "bakery": ("ordering",), "cake": ("ordering",), "pizza": ("ordering",),
    "caterer": ("ordering", "booking"), "catering": ("ordering", "booking"),
    "florist": ("ordering",), "sweets": ("ordering",), "grocery": ("ordering",),
    "pharmacy": ("ordering",), "boutique": ("ordering",),
    "salon": ("booking",), "spa": ("booking",), "barber": ("booking",),
    "beauty": ("booking",), "clinic": ("booking",), "dentist": ("booking",),
    "dental": ("booking",), "doctor": ("booking",), "physiotherapy": ("booking",),
    "physiotherapist": ("booking",), "dermatologist": ("booking",), "hotel": ("booking",),
    "resort": ("booking",), "homestay": ("booking",), "villa": ("booking",),
    "guest house": ("booking",), "venue": ("booking",), "banquet": ("booking",),
    "gym": ("booking",), "fitness": ("booking",), "yoga": ("booking",),
    "tutor": ("booking",), "coaching": ("booking",), "driving school": ("booking",),
    "photographer": ("booking",), "car service": ("booking",), "veterinary": ("booking",),
}


def _words(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').casefold()).strip()} "


def expected_online(category: str, extra: dict[str, list[str]] | None = None) -> tuple[str, ...]:
    """What customers of this category expect to do online: booking, ordering, both or none."""
    table = {**EXPECTATIONS, **{k.casefold(): tuple(v) for k, v in (extra or {}).items()}}
    text = _words(category)
    kinds: list[str] = []
    for word, wanted in table.items():
        stem = _words(word).strip()
        if stem and re.search(rf" {re.escape(stem)}(?:s|es)? ", text):
            kinds.extend(k for k in wanted if k not in kinds)
    return tuple(kinds)


def same_site(url: str, base: str) -> bool:
    host, home = domain_of(url) or "", domain_of(base) or ""
    return bool(host and home) and (host == home or host.endswith("." + home)
                                    or home.endswith("." + host))


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def page_facts(html: str, url: str) -> PageFacts:
    """Everything the checks read from one page."""
    soup = soup_of(html)
    facts = PageFacts(url=url)
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").strip().lower()
        if name == "viewport":
            facts.viewport = (meta.get("content") or "").strip()
        elif name == "generator" and meta.get("content"):
            facts.generators.append(meta["content"].strip())
    for tag in soup.find_all(["script", "link", "iframe"]):
        ref = tag.get("src") or (tag.get("href") if tag.name == "link" else None)
        if ref:
            absolute = urljoin(url, ref)
            facts.asset_urls.append(absolute)
            lowered = absolute.lower()
            if any(h in lowered for h in BOOKING_HOSTS):
                facts.booking.append(absolute)
            if any(h in lowered for h in ORDERING_HOSTS):
                facts.ordering.append(absolute)
    text = soup.get_text(" ", strip=True)
    facts.copyright_year = copyright_year(text)

    home = url.split("#", 1)[0].rstrip("/")
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        absolute = urljoin(url, href)
        lowered = absolute.lower()
        if any(h in lowered for h in BOOKING_HOSTS):
            facts.booking.append(absolute)
        if any(h in lowered for h in ORDERING_HOSTS):
            facts.ordering.append(absolute)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not same_site(absolute, url):
            continue
        clean = absolute.split("#", 1)[0]
        label = f"{parts.path} {anchor.get_text(' ', strip=True)}"
        if _CONTACT_LINK.search(label) and clean not in facts.contact_links:
            facts.contact_links.append(clean)
        if clean in seen or clean.rstrip("/") == home or _SKIP_LINK.search(parts.path):
            continue
        seen.add(clean)
        facts.internal_links.append(clean)
    for pattern, bucket in ((BOOKING_PHRASES, facts.booking), (ORDERING_PHRASES, facts.ordering)):
        match = pattern.search(text)
        if match:
            bucket.append("phrase: " + match.group(0))
    facts.has_contact_form = has_contact_form(soup, html or "")
    return facts


def copyright_year(text: str) -> int | None:
    """The latest year written next to a copyright mark, if any."""
    years = []
    for match in _COPYRIGHT.finditer(text or ""):
        years.extend(int(y) for y in _YEAR.findall(text[match.end():match.end() + 40]))
        years.extend(int(y) for y in _YEAR.findall(text[max(0, match.start() - 12):
                                                         match.start()]))
    return max(years) if years else None


def has_contact_form(soup: BeautifulSoup, html: str) -> bool:
    """A form someone can send an enquiry through (not a search box or newsletter field)."""
    for form in soup.find_all("form"):
        fields = form.find_all(["input", "textarea", "select"])
        visible = [(f.name, (f.get("type") or "text").lower(), (f.get("name") or "").lower())
                   for f in fields]
        visible = [k for k in visible if k[1] not in ("hidden", "submit", "button", "search")]
        if any(name == "textarea" for name, _, _ in visible):
            return True
        has_contact_field = any(t in ("email", "tel") or "mail" in n or "phone" in n
                                for _, t, n in visible)
        if has_contact_field and len(visible) >= 3:
            return True
    return bool(_FORM_EMBEDS.search(html))


# --- the checks ----------------------------------------------------------------------------------

def check_mobile(facts: PageFacts) -> Finding | None:
    viewport = (facts.viewport or "").replace(" ", "").lower()
    if "width=device-width" in viewport:
        return None
    if facts.viewport is None:
        return Finding("not_mobile_friendly", "Not mobile-friendly: the homepage has no "
                       "viewport tag, so phones show the desktop layout shrunk.", url=facts.url)
    return Finding("not_mobile_friendly", f"Not mobile-friendly: its viewport "
                   f"({facts.viewport[:60]}) doesn't follow the screen width.",
                   {"viewport": facts.viewport[:200]}, url=facts.url)


def check_tech(facts: PageFacts, html: str, headers: dict[str, str],
               thresholds: dict[str, str] | None = None) -> tuple[list[Detected], Finding | None]:
    found = detect(html, headers, generators=facts.generators, script_urls=facts.asset_urls,
                   thresholds=thresholds)
    old = [d for d in found if d.outdated]
    if not old:
        return found, None
    # The reasons (end-of-life dates) travel in the payload; the sentence stays short.
    text = "Outdated technology: " + ", ".join(d.label for d in old) + "."
    return found, Finding("outdated_tech", text, {"technologies": [
        {"name": d.name, "version": d.version, "category": d.category, "why": d.why,
         "evidence": d.evidence} for d in old]}, url=facts.url)


def check_copyright(facts: PageFacts, now: datetime, years: int) -> Finding | None:
    year = facts.copyright_year
    if year is None or year > now.year + 1 or now.year - year < max(1, years):
        return None
    return Finding("stale_copyright", f"Stale copyright: the footer still says {year}, "
                   f"{now.year - year} years ago.", {"year": year}, url=facts.url)


def check_contact_form(pages: list[PageFacts]) -> Finding | None:
    if not pages or any(p.has_contact_form for p in pages):
        return None
    where = "the homepage" + (" or its contact page" if len(pages) > 1 else "")
    return Finding("no_contact_form", f"No contact form on {where}: visitors can only call "
                   "or email.", url=pages[0].url)


def check_booking(pages: list[PageFacts], category: str,
                  extra: dict[str, list[str]] | None = None) -> Finding | None:
    expected = expected_online(category, extra)
    if not expected or not pages:
        return None
    has = {"booking": any(p.booking for p in pages), "ordering": any(p.ordering for p in pages)}
    if any(has[k] for k in expected):
        return None
    wanted = " or ".join({"booking": "book", "ordering": "order"}[k] for k in expected)
    return Finding("no_online_booking", f"No online booking or ordering: customers of "
                   f"{category.strip().lower()} expect to {wanted} online, and the site "
                   "offers no way to.", {"expected": list(expected)}, url=pages[0].url)


def check_broken_links(results: dict[str, str]) -> Finding | None:
    """``results``: link -> "ok" or why it is broken ("HTTP 404", "unreachable")."""
    broken = {u: why for u, why in results.items() if why != "ok"}
    if not broken:
        return None
    sample = "; ".join(f"{u} ({why})" for u, why in list(broken.items())[:3])
    plural = "s" if len(broken) > 1 else ""
    return Finding("broken_links", f"{len(broken)} broken link{plural} on the homepage "
                   f"(of {len(results)} checked): {sample}.",
                   {"broken": len(broken), "checked": len(results),
                    "links": [{"url": u, "status": w} for u, w in list(broken.items())[:10]]})


def check_maps_profile(lead: Any, min_reviews: int) -> Finding | None:
    """Only for organisations with a Maps listing: what that listing lacks."""
    if not getattr(lead, "gmaps_url", None):
        return None
    missing = []
    if not lead.website:
        missing.append("website")
    if not lead.phones:
        missing.append("phone")
    if not (lead.address or "").strip():
        missing.append("address")
    reviews = lead.gmaps_reviews or 0
    if lead.gmaps_rating is None:
        missing.append("rating")
    elif reviews < min_reviews:
        missing.append(f"reviews (only {reviews})")
    if not missing:
        return None
    return Finding("incomplete_maps_profile", "Incomplete Maps profile: no "
                   + ", no ".join(missing) + ".", {"missing": missing}, url=lead.gmaps_url)
