"""Normalisation helpers.

Every scraper hands raw, messy strings to the store. This module is the single
place where they get turned into comparable, canonical values -- which is what
makes dedup (name + phone) and scoring reliable.
"""

from __future__ import annotations

import html
import re
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import phonenumbers

# --- phones ----------------------------------------------------------------

DEFAULT_REGION = "IN"

# Matches things like "+91 98200 12345", "022-2345 6789", "9820012345".
_PHONE_CANDIDATE = re.compile(r"(?:\+?91[\-\s.]?)?(?:\(?0?\d{2,5}\)?[\-\s.]?)?\d{3,5}[\-\s.]?\d{3,5}")

# Strings that look like phone numbers but never are.
_PHONE_BLACKLIST = {
    "0000000000",
    "1234567890",
    "9999999999",
    "1111111111",
}


def normalize_phone(raw: str, region: str = DEFAULT_REGION) -> str | None:
    """Return an E.164 phone number, or None if `raw` isn't a usable number."""
    if not raw:
        return None
    raw = str(raw).strip()
    # phonenumbers chokes on stray unicode separators that Indian sites love.
    cleaned = re.sub(r"[^\d+]", "", raw.replace(" ", " "))
    if not cleaned:
        return None
    if cleaned.strip("+").lstrip("0") in _PHONE_BLACKLIST:
        return None
    for attempt in (cleaned, "+" + cleaned.lstrip("+")):
        try:
            parsed = phonenumbers.parse(attempt, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return None


def extract_phones(text: str, region: str = DEFAULT_REGION) -> list[str]:
    """Pull every valid phone number out of a blob of text, in order, deduped.

    Uses phonenumbers' own matcher first (high precision) and falls back to a
    permissive regex for numbers written without a country code.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    for match in phonenumbers.PhoneNumberMatcher(text, region):
        if phonenumbers.is_valid_number(match.number):
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            if e164 not in seen:
                seen.add(e164)
                found.append(e164)

    for candidate in _PHONE_CANDIDATE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        # Indian mobiles are 10 digits; landlines with STD are 10-11.
        if not (10 <= len(digits) <= 12):
            continue
        e164 = normalize_phone(candidate, region)
        if e164 and e164 not in seen:
            seen.add(e164)
            found.append(e164)
    return found


def phone_display(e164: str) -> str:
    """Human-friendly rendering for the spreadsheet."""
    try:
        parsed = phonenumbers.parse(e164, DEFAULT_REGION)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return e164


# --- emails ----------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Obfuscations seen in the wild: "name [at] domain [dot] com".
_DEOBFUSCATE = [
    (re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I), "@"),
    (re.compile(r"\s+at\s+(?=[A-Za-z0-9.\-]+\s*(?:\[|\(|\{)?\s*dot)", re.I), "@"),
    (re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I), "."),
    (re.compile(r"\s+dot\s+", re.I), "."),
]

# Junk that shows up in page source but is never a lead's address.
_EMAIL_BLACKLIST_PREFIX = ("noreply", "no-reply", "donotreply", "example", "your@", "email@example")
_EMAIL_BLACKLIST_DOMAIN = (
    "sentry.io", "wixpress.com", "example.com", "domain.com", "yoursite.com",
    "godaddy.com", "w3.org", "schema.org", "googlemail.com.png",
)
_IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")


def extract_emails(text: str) -> list[str]:
    """Pull every plausible email out of text, including lightly obfuscated ones."""
    if not text:
        return []
    text = html.unescape(text)
    deobfuscated = text
    for pattern, repl in _DEOBFUSCATE:
        deobfuscated = pattern.sub(repl, deobfuscated)

    found: list[str] = []
    seen: set[str] = set()
    for blob in (text, deobfuscated):
        for raw in _EMAIL.findall(blob):
            email = raw.strip(" .,;:").lower()
            if email in seen:
                continue
            if email.endswith(_IMAGE_SUFFIX):
                continue
            if email.startswith(_EMAIL_BLACKLIST_PREFIX):
                continue
            domain = email.rsplit("@", 1)[-1]
            if any(domain.endswith(bad) for bad in _EMAIL_BLACKLIST_DOMAIN):
                continue
            # "2x@3x" style asset names.
            if re.fullmatch(r"\d+x?", email.split("@")[0]):
                continue
            seen.add(email)
            found.append(email)
    return found


# --- business names --------------------------------------------------------

# Legal / marketing suffixes that shouldn't stop two records matching.
_NAME_NOISE = re.compile(
    r"\b(pvt\.?|private|ltd\.?|limited|llp|inc\.?|co\.?|company|"
    r"and|&|the|mumbai|india|maharashtra|"
    r"services?|service|solutions?|enterprises?|events?|"
    r"studios?|studio|photography|caterers?|catering|decorators?|decoration)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_name(raw: str) -> str:
    """Canonical form of a business name, used as a dedup key component."""
    if not raw:
        return ""
    name = html.unescape(str(raw)).lower()
    name = _PUNCT.sub(" ", name)
    name = _WS.sub(" ", name).strip()
    return name


# Run-specific noise on top of `_NAME_NOISE`: the configured city, and for a
# venue run words like "villa" / "resort" that every listing carries. Set once
# per run from config via `set_extra_name_noise`; empty means Mumbai behaviour.
_EXTRA_NAME_NOISE: re.Pattern[str] | None = None


def set_extra_name_noise(words: Iterable[str]) -> None:
    """Treat `words` as dedup noise in `name_key`, in addition to the defaults."""
    global _EXTRA_NAME_NOISE
    cleaned = sorted({normalize_name(w) for w in words if w and normalize_name(w)},
                     key=len, reverse=True)
    _EXTRA_NAME_NOISE = (
        re.compile(r"\b(" + "|".join(re.escape(w) for w in cleaned) + r")\b", re.I)
        if cleaned else None
    )


def name_key(raw: str) -> str:
    """Aggressive form of `normalize_name` with legal/category noise stripped.

    "Sharma Caterers Pvt Ltd" and "Sharma Catering Services" collapse to the
    same key, which is what we want when merging across directories.
    """
    name = normalize_name(raw)
    name = _NAME_NOISE.sub(" ", name)
    if _EXTRA_NAME_NOISE is not None:
        name = _EXTRA_NAME_NOISE.sub(" ", name)
    name = _WS.sub(" ", name).strip()
    return name or normalize_name(raw)


def clean_text(raw: str | None) -> str:
    """Collapse whitespace and unescape entities; safe on None."""
    if not raw:
        return ""
    return _WS.sub(" ", html.unescape(str(raw))).strip()


# --- urls / handles --------------------------------------------------------

_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_", "_ga")


def normalize_url(raw: str | None) -> str | None:
    """Strip tracking junk and normalise scheme/host so URLs compare equal."""
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw or raw.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    if not raw.startswith(("http://", "https://")):
        if raw.startswith("//"):
            raw = "https:" + raw
        elif "." in raw.split("/")[0]:
            raw = "https://" + raw
        else:
            return None
    try:
        parts = urlparse(raw)
    except ValueError:
        return None
    if not parts.netloc:
        return None
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = "&".join(
        kv for kv in parts.query.split("&")
        if kv and not kv.lower().startswith(_TRACKING_PARAMS)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunparse((parts.scheme.lower(), netloc, path, "", query, ""))


def domain_of(url: str | None) -> str | None:
    """Bare registrable-ish host, used for per-domain rate limiting."""
    normalized = normalize_url(url)
    if not normalized:
        return None
    return urlparse(normalized).netloc or None


# Aggregators / socials that must never be recorded as a vendor's own website.
NON_VENDOR_DOMAINS = {
    "facebook.com", "m.facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "youtu.be", "pinterest.com", "wa.me", "api.whatsapp.com",
    "google.com", "goo.gl", "maps.google.com", "maps.app.goo.gl", "justdial.com",
    "sulekha.com", "indiamart.com", "wedmegood.com", "weddingwire.in", "shaadisaga.com",
    "tripadvisor.in", "tripadvisor.com", "zomato.com", "swiggy.com", "yellowpages.in",
    "sitemaps.org", "wordpress.org", "blogspot.com", "wixsite.com",
    # Villa / hotel marketplaces: a listing there is a platform page, never the
    # property's own site.
    "stayvista.com", "vistarooms.com", "saffronstays.com", "elivaas.com", "lohono.com",
    "isprava.com", "luxunlock.com", "airbnb.com", "airbnb.co.in", "booking.com",
    "makemytrip.com", "goibibo.com", "agoda.com", "expedia.com", "expedia.co.in",
    "hotels.com", "trivago.in", "oyorooms.com", "treebo.com", "yatra.com",
    "cleartrip.com", "venuelook.com", "weddingz.in", "bookeventz.com",
}


def is_vendor_website(url: str | None) -> bool:
    """True when a URL looks like the business's own site, not a profile on an aggregator."""
    host = domain_of(url)
    if not host:
        return False
    return not any(host == bad or host.endswith("." + bad) for bad in NON_VENDOR_DOMAINS)


# A Maps place URL carries the feature id as "!1s0x<cell>:0x<cid>"; share links
# and older exports carry the same place as "?cid=<decimal>". Both reduce to
# the cid, so one place compares equal however its URL was written.
_MAPS_FTID = re.compile(r"!1s0x[0-9a-f]+:(0x[0-9a-f]+)", re.I)
_MAPS_CID = re.compile(r"[?&]cid=(\d+)")


def maps_place_key(url: str | None) -> str | None:
    """Stable identity of a Google Maps place ("cid:<decimal>"), or None.

    Maps returns the same place for searches run from neighbouring localities,
    so this is what tells two locality-tagged records they are one business.
    """
    if not url:
        return None
    match = _MAPS_FTID.search(url)
    if match:
        return f"cid:{int(match.group(1), 16)}"
    match = _MAPS_CID.search(url)
    if match:
        return f"cid:{int(match.group(1))}"
    return None


_IG_HANDLE = re.compile(r"(?:instagram\.com|instagr\.am)/(?!p/|reel/|explore/|stories/)([A-Za-z0-9_.]{2,30})", re.I)
_IG_BARE = re.compile(r"(?:^|\s)@([A-Za-z0-9_.]{3,30})(?=\s|$)")
_IG_RESERVED = {"about", "legal", "explore", "developer", "accounts", "directory", "privacy", "help"}


def extract_instagram(text: str | None) -> str | None:
    """Return a bare Instagram handle (no @) found in a URL or bio text."""
    if not text:
        return None
    match = _IG_HANDLE.search(str(text))
    if match:
        handle = match.group(1).strip(".").lower()
        if handle and handle not in _IG_RESERVED:
            return handle
    match = _IG_BARE.search(str(text))
    if match:
        return match.group(1).strip(".").lower()
    return None


_LINKEDIN = re.compile(r"linkedin\.com/(?:in|company)/[A-Za-z0-9\-_%]+", re.I)


def extract_linkedin(text: str | None) -> str | None:
    """Return a canonical public LinkedIn profile/company URL found in text."""
    if not text:
        return None
    match = _LINKEDIN.search(str(text))
    if not match:
        return None
    return "https://www." + match.group(0).lower().lstrip("/")


_FACEBOOK = re.compile(r"(?:facebook\.com|fb\.com)/(?!sharer|plugins|tr\?|dialog)([A-Za-z0-9.\-]{3,})", re.I)


def extract_facebook(text: str | None) -> str | None:
    """Return a canonical Facebook page URL found in text."""
    if not text:
        return None
    match = _FACEBOOK.search(str(text))
    if not match:
        return None
    slug = match.group(1).split("?")[0].strip("/")
    if not slug or slug.lower() in {"profile.php", "pages"}:
        return None
    return f"https://www.facebook.com/{slug}"


_WHATSAPP = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|web\.whatsapp\.com/send\?phone=)(\+?\d{8,15})", re.I)


def extract_whatsapp(text: str | None) -> str | None:
    """Return a normalised wa.me link if the text contains a WhatsApp CTA."""
    if not text:
        return None
    match = _WHATSAPP.search(str(text))
    if not match:
        return None
    phone = normalize_phone(match.group(1))
    if not phone:
        return None
    return f"https://wa.me/{phone.lstrip('+')}"


# --- numbers ---------------------------------------------------------------

_RATING = re.compile(r"(\d(?:[.,]\d)?)\s*(?:/\s*5|out of 5|star)?", re.I)
_REVIEW_COUNT = re.compile(r"([\d,]+(?:\.\d+)?)\s*([KkLl]?)\s*(?:reviews?|ratings?)", re.I)


def parse_rating(raw: str | float | None) -> float | None:
    """Parse '4.5', '4,5', '4.5/5', '4.5 stars' -> 4.5. Rejects out-of-range values."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if 0 < value <= 5 else None
    text = str(raw)
    # "1,234 reviews" would otherwise yield a rating of 1.0 -- a thousands
    # separator means we were handed a review count, not a rating.
    if re.search(r"\d,\d{3}", text):
        return None
    match = _RATING.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if 0 < value <= 5 else None


def parse_review_count(raw: str | int | None) -> int | None:
    """Parse '1,234 reviews', '(89)', '2.1K reviews', '1.2 Lakh ratings' -> int."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw)
    match = _REVIEW_COUNT.search(text)
    if match:
        number, suffix = match.group(1), match.group(2).lower()
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            return None
        if suffix == "k":
            value *= 1_000
        elif suffix == "l":
            value *= 100_000
        return int(value)
    # Bare "(1,234)" as Google Maps renders it next to the rating. Kept
    # deliberately strict: a loose match would read "4.5 stars" as 4 reviews.
    bare = re.fullmatch(r"\s*\(?\s*([\d,]{1,12})\s*\)?\s*", text)
    if bare:
        try:
            return int(bare.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    """Order-preserving dedup for list-valued lead fields."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
