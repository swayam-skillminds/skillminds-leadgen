"""What a procurement portal tells us about one notice, and the rules that pick notices.

Every portal (``Pack.tenders``) reads its notices into ``Tender``: the notice's
own id and page, the buyer (a public body, with the contact details the notice
publishes), the closing date, the value as stated, and its classification codes
(CPV in the EU and the UK, NAICS in the US; none in India). The ``tenders`` lead
type then decides which notices count: codes where the portal classifies,
keywords where it doesn't (``code_matches``, ``keyword_hits``).

A portal class has:

* ``name`` (its source name in configs, failures and the catalog), ``title``,
  ``host`` (its rate key) and ``applicable_countries``;
* ``codes``: ``"cpv"``, ``"naics"`` or None -- what its notices are classified by;
* ``needs_key`` with ``secret_prefix`` / ``default_secret`` / ``register_url`` when
  it only answers with an API key (SAM.gov);
* ``search(http, query, *, key=None) -> list[Tender]``: may raise the HTTP client's
  errors (the session records them); never invents a field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone, tzinfo
from typing import Any, Iterable

from ...core.geo import USER_AGENT

#: Government APIs get to know who is asking (an identifying User-Agent).
API_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

#: Currency signs for display; anything else is written as its code.
_SIGNS = {"EUR": "€", "GBP": "£", "USD": "$", "INR": "₹"}


@dataclass
class Buyer:
    """The public body that runs the procurement, as the notice names it."""

    name: str
    #: The portal's id for the buyer, and the ``registry_ids`` scheme it is filed under
    #: ("gb_fts" -> "3524"): the same buyer next month is the same organisation.
    identifier: str = ""
    scheme: str = ""
    address: str = ""
    locality: str = ""
    postcode: str = ""
    region: str = ""
    #: ISO 3166-1 alpha-2.
    country: str = ""
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    #: A named contact point, as published ("Procurement Department", "Briony Fox").
    contact: str = ""


def format_amount(amount: float, currency: str) -> str:
    """"€5,300,000", "£150,000", "SEK 2,000,000": a plain grouping everywhere."""
    sign = _SIGNS.get((currency or "").upper(), "")
    figure = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"{sign}{figure}" if sign else f"{(currency or '').upper()} {figure}".strip()


@dataclass
class Money:
    """A value as the notice states it: an amount (or a range) in its own currency."""

    amount: float | None = None
    currency: str = ""
    #: The lower end of a stated range ("£50,000–£150,000"); None for a single figure.
    low: float | None = None

    @property
    def text(self) -> str:
        if self.amount is None:
            return ""
        top = format_amount(self.amount, self.currency)
        if self.low is not None and 0 < self.low < self.amount:
            return f"{format_amount(self.low, self.currency)}–{top}"
        return top

    def as_dict(self) -> dict[str, Any]:
        return {"amount": self.amount, "low": self.low, "currency": self.currency,
                "text": self.text}


@dataclass
class Tender:
    """One procurement notice."""

    #: The portal (its source name).
    source: str
    #: The portal's own id for the notice ("674231-2026", "GEM/2026/B/8027178").
    notice_id: str
    title: str
    #: The notice on the portal (a page a person can open).
    url: str
    buyer: Buyer
    description: str = ""
    published_at: datetime | None = None
    #: When submissions close (aware). None: the notice states none (planning notices).
    deadline: datetime | None = None
    value: Money | None = None
    #: Classification codes as published (CPV "72268000", NAICS "541511").
    codes: list[str] = field(default_factory=list)
    #: "cpv" | "naics" | "" -- what ``codes`` are.
    scheme: str = ""
    #: "tender" (open for bids) or "planned" (a prior-information / pre-solicitation /
    #: sources-sought notice: a procurement on its way).
    stage: str = "tender"
    #: The portal's notice type ("cn-standard", "Solicitation", "Open").
    notice_type: str = ""
    #: Where the tender documents are, when the notice links them.
    documents_url: str | None = None
    #: Place of performance, as stated (cities, regions, codes).
    places: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.notice_id}"

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.description}"


@dataclass
class TenderQuery:
    """What a run asks every portal for."""

    #: ISO 3166-1 alpha-2, and its name.
    country: str
    country_name: str = ""
    #: The run's places and areas (for the portals that can narrow by place).
    places: list[str] = field(default_factory=list)
    #: The run's state / region ("TX", "Maharashtra"), when known.
    region: str | None = None
    cpv: list[str] = field(default_factory=list)
    naics: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    #: Notices published since then (aware).
    posted_from: datetime | None = None
    #: Only notices still open after this moment (aware).
    closing_after: datetime | None = None
    #: How many notices a portal may return, and how many pages it may read.
    max_results: int = 250
    max_pages: int = 10


# --- matching -------------------------------------------------------------------------------

def _digits(code: str) -> str:
    """"72268000-5" -> "72268000"; "541511" -> "541511"."""
    return re.sub(r"\D", "", str(code or "").split("-")[0])


def code_prefix(code: str) -> str:
    """A wanted code as a prefix: CPV "72000000" means the whole 72 division, NAICS "5415"
    every code under it. Trailing zeros are the hierarchy's "any"; at least two digits."""
    digits = _digits(code)
    trimmed = digits.rstrip("0")
    return trimmed if len(trimmed) >= 2 else digits[:2]


def code_matches(codes: Iterable[str], wanted: Iterable[str]) -> list[str]:
    """The notice's codes that fall under a wanted one."""
    prefixes = [p for p in (code_prefix(w) for w in wanted) if p]
    return [c for c in dict.fromkeys(_digits(c) for c in codes)
            if c and any(c.startswith(p) for p in prefixes)]


def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    """The keywords (whole words or phrases, any case) the text contains."""
    hits = []
    for word in keywords:
        word = " ".join(str(word or "").split())
        if not word:
            continue
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in word.split()) + r"(?!\w)"
        if re.search(pattern, text or "", flags=re.IGNORECASE):
            hits.append(word)
    return hits


# --- reading portals' values ----------------------------------------------------------------

_DATE_ZONE = re.compile(r"(\d{4}-\d{2}-\d{2})(Z|[+-]\d{2}:\d{2})?")
_FORMATS = ("%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y",
            "%m/%d/%Y", "%d/%m/%Y")


def parse_time(value: Any, *, end_of_day: bool = False,
               zone: tzinfo = timezone.utc) -> datetime | None:
    """An aware datetime from what portals write: ISO with or without a zone or time
    ("2026-11-04+01:00", "2026-11-04Z", "2026-10-13T12:00:00Z"), "13-Oct-2026 12:10 PM",
    "10/31/2026". A bare date is the end of that day for a deadline (``end_of_day``);
    a time without a zone is in ``zone`` (the portal's own: IST for India's)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=zone)
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59, 59) if end_of_day else time(0),
                                tzinfo=zone)
    text = str(value).strip()
    match = _DATE_ZONE.fullmatch(text)
    if match:                        # TED's "2026-11-04+01:00": a date with its zone
        when = datetime.combine(date.fromisoformat(match.group(1)),
                                time(23, 59, 59) if end_of_day else time(0))
        offset = match.group(2)
        if offset and offset != "Z":
            return datetime.fromisoformat(when.isoformat() + offset)
        return when.replace(tzinfo=timezone.utc if offset == "Z" else zone)
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return when if when.tzinfo else when.replace(tzinfo=zone)
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            when = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end_of_day and "%H" not in fmt and "%I" not in fmt:
            when = when.replace(hour=23, minute=59, second=59)
        return when.replace(tzinfo=zone)
    return None


def number(value: Any) -> float | None:
    """A plain amount from "5300000", 5300000, "350567.00", "1,20,000"; None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = re.sub(r"[,\s]", "", str(value))
    try:
        amount = float(text)
    except ValueError:
        return None
    return amount if amount > 0 else None


def first(value: Any) -> Any:
    """The first item of a list (TED repeats a lot's field once per lot), else the value."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def text_of(value: Any, languages: Iterable[str] = ("eng",)) -> str:
    """A TED-style multilingual field ({"eng": "…", "ces": ["…"]}) in English when it is
    there, else in the first language it has."""
    if isinstance(value, dict):
        for lang in (*languages, *value.keys()):
            if value.get(lang):
                return str(first(value[lang]) or "").strip()
        return ""
    if isinstance(value, list):
        return str(first(value) or "").strip()
    return str(value or "").strip()


#: E-procurement platforms and portals: a notice often gives one as a buyer's address, but
#: hundreds of buyers share it, so it is never a buyer's own site (or identity).
_PORTAL_HOSTS = ("publiccontractsscotland", "publictendersscotland", "etenders",
                 "delta-esourcing", "in-tendhost", "proactis", "bravosolution", "jaggaer",
                 "sell2wales", "mytenders", "eu-supply", "ted.europa.eu", "find-tender",
                 "contractsfinder", "tendersdirect", "achilles", "supplying2nhs", "sam.gov",
                 "eprocure.gov.in", "gem.gov.in", "e-tendersni", "atamis", "tenderlake")


def own_site(url: Any) -> str | None:
    """A buyer's own website, or None for a procurement portal or a non-link.

    ``"http://https://example"`` (a typo some notices carry) is repaired first."""
    text = str(url or "").strip().replace("http://https://", "https://")
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}" if "." in text and " " not in text else ""
    lowered = text.lower()
    if not text or any(host in lowered for host in _PORTAL_HOSTS):
        return None
    return text


def clean(text: Any, limit: int = 2000) -> str:
    """Whitespace collapsed, HTML entities a portal left in decoded, cut to ``limit``."""
    import html

    return " ".join(html.unescape(str(text or "")).split())[:limit]
