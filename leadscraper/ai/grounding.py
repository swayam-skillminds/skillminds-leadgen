"""Grounding guardrails: AI never adds a contact detail the page didn't publish (5.5).

Every phone, email, address, handle or website a model hands back is checked
against the text we fetched ourselves (``source``) before it is kept:

* **phone** -- the same number must parse out of the source with the run's
  region (the scraper's own phone rules), so "+91 98200 12345" in the answer
  matches "98200-12345" on the page, but a number the page doesn't carry is
  dropped however plausible it looks;
* **email** -- found in the source, plain or in the lightly obfuscated forms the
  scraper already reads ("name [at] domain [dot] com");
* **address / name** -- the text appears contiguously in the source, compared
  case-, punctuation- and whitespace-insensitively;
* **instagram / website** -- the handle / host appears in the source.

What is dropped is reported, so a hallucination is visible in the failure log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

from ..core.normalize import extract_emails, extract_phones, normalize_phone

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def _squash(text: str) -> str:
    return " " + _NON_WORD.sub(" ", (text or "").casefold()).strip() + " "


@dataclass
class SourceText:
    """The fetched text an AI answer must be grounded in (page text plus hrefs)."""

    text: str
    region: str | None = None

    @cached_property
    def phones(self) -> set[str]:
        return set(extract_phones(self.text, self.region))

    @cached_property
    def emails(self) -> set[str]:
        return set(extract_emails(self.text))

    @cached_property
    def squashed(self) -> str:
        return _squash(self.text)

    @cached_property
    def lowered(self) -> str:
        return (self.text or "").casefold()

    def has_phone(self, value: str) -> bool:
        number = normalize_phone(str(value), self.region)
        return bool(number) and number in self.phones

    def has_email(self, value: str) -> bool:
        return str(value).strip().lower() in self.emails

    def has_text(self, value: str, *, min_chars: int = 3) -> bool:
        squashed = _squash(value)
        return len(squashed.strip()) >= min_chars and squashed in self.squashed

    def has_handle(self, handle: str) -> bool:
        """The page links the profile (instagram.com/<handle>) or writes @handle."""
        handle = str(handle or "").lstrip("@").strip().casefold()
        if not re.fullmatch(r"[a-z0-9_.]{2,30}", handle):
            return False
        pattern = (rf"(?:instagram\.com/{re.escape(handle)}|(?<![\w.])@{re.escape(handle)})"
                   rf"(?![\w])")
        return re.search(pattern, self.lowered) is not None

    def has_host(self, url: str) -> bool:
        """The site's host appears as a URL or link on the page, not inside another word."""
        from ..core.normalize import domain_of

        host = (domain_of(url) or "").casefold()
        if not host:
            return False
        pattern = (rf"(?:https?://|//|www\.|^|[\s\"'(<,;@])(?:www\.)?{re.escape(host)}"
                   rf"(?=[/\s\"'?#:)>,;]|$)")
        return re.search(pattern, self.lowered) is not None

    def near(self, name: str, *, window: int = 400) -> "SourceText | None":
        """Only the text around each mention of `name`, or None when it isn't on the page.

        On a page listing many businesses (a directory, a search-result page)
        a contact detail must sit next to *this* business's name: page-level
        grounding would let a model hand business B's phone to business A.
        """
        words = [w for w in _NON_WORD.split((name or "").casefold()) if w]
        if not words:
            return None
        pattern = r"[\W_]+".join(re.escape(w) for w in words)
        spans = [m.span() for m in re.finditer(pattern, self.lowered)]
        if not spans:
            return None
        text = self.text or ""
        pieces = [text[max(0, start - window):end + window] for start, end in spans]
        return SourceText(" … ".join(pieces), self.region)


@dataclass
class Grounded:
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    address: str = ""
    instagram: str | None = None
    website: str | None = None
    #: "phone '+1 555 0100' not in source" ...
    dropped: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.phones or self.emails or self.address or self.instagram
                    or self.website)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)] if str(value).strip() else []


def ground_contacts(record: dict, source: SourceText, *, anchor: str | None = None) -> Grounded:
    """Keep only the contact details of `record` that `source` really carries.

    With `anchor` (the business's name), phones, emails and the address must sit
    near a mention of it (:meth:`SourceText.near`): what a listing page needs.
    The website and handle are links, judged on the whole page.
    """
    out = Grounded()
    whole = source
    if anchor:
        source = source.near(anchor) or SourceText("", source.region)
    for raw in _as_list(record.get("phones")):
        number = normalize_phone(raw, source.region)
        if number and source.has_phone(raw):
            if number not in out.phones:
                out.phones.append(number)
        else:
            out.dropped.append(f"phone {raw[:40]!r} not in source")
    for raw in _as_list(record.get("emails")):
        email = raw.strip().lower()
        if source.has_email(email):
            if email not in out.emails:
                out.emails.append(email)
        else:
            out.dropped.append(f"email {raw[:60]!r} not in source")
    address = " ".join(_as_list(record.get("address"))).strip()
    if address:
        if source.has_text(address, min_chars=6):
            out.address = address[:300]
        else:
            out.dropped.append(f"address {address[:60]!r} not in source")
    handle = " ".join(_as_list(record.get("instagram"))).strip().lstrip("@")
    if handle:
        handle = handle.rstrip("/").rsplit("/", 1)[-1]
        if whole.has_handle(handle):
            out.instagram = handle.lower()
        else:
            out.dropped.append(f"instagram {handle[:40]!r} not in source")
    website = " ".join(_as_list(record.get("website"))).strip()
    if website:
        if whole.has_host(website):
            out.website = _site(website)
        else:
            out.dropped.append(f"website {website[:60]!r} not in source")
    return out


def _site(url: str) -> str:
    """Scheme and host of a grounded website: the model's path is not evidence."""
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"https://{url}")
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    return f"{scheme}://{(parsed.hostname or '').lower()}/"
