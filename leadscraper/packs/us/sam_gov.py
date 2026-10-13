"""SAM.gov (US): federal contract opportunities, from the Get Opportunities Public API v2.

``GET https://api.sam.gov/opportunities/v2/search?api_key=…&postedFrom=MM/dd/yyyy&
postedTo=MM/dd/yyyy&ptype=o&ptype=k&ptype=p&ptype=r&limit=1000`` (GSA Open Technology
docs, verified 2026-10-13). A free personal API key is required (SAM.gov →
Account Details); without one this source is skipped and says so. A key of a
non-federal user with no SAM.gov role allows **10 requests a day**, so a run makes
one request by default (``max_requests``) and sorts NAICS codes itself. The docs
list 404 as "No data found"; live, a request without a valid key also answered an
empty 404, so a 404 is reported as "no results (or a key SAM.gov doesn't accept)".

The key goes in the query string, as the API requires: it never appears in a
failure record or log line (``_redact``), and a request is never retried (no retry
log, and the daily quota is small).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ...net.ratelimit import BlockedError, PermanentError
from ...sources.tenders.base import API_HEADERS, Buyer, Money, Tender, clean, number, parse_time

HOST = "api.sam.gov"
API = "https://api.sam.gov/opportunities/v2/search"
REGISTER_URL = "https://sam.gov (sign in, then Account Details → Public API Key)"
#: Procurement types: solicitation, combined synopsis/solicitation (open now);
#: presolicitation, sources sought (on their way).
OPEN_TYPES = ("o", "k")
PLANNED_TYPES = ("p", "r")
_PLANNED_NAMES = ("presolicitation", "sources sought", "special notice")
#: SAM.gov allows a year between postedFrom and postedTo.
MAX_DAYS = 364


#: Place-of-performance filter: a state's name (the run's geocoded admin area) -> its code.
STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}


def state_code(region: str | None) -> str | None:
    """"Texas" -> "TX"; "TX" -> "TX"; anything else -> None."""
    text = " ".join(str(region or "").split())
    if len(text) == 2 and text.upper() in STATES.values():
        return text.upper()
    return STATES.get(text.lower())


class SamKeyRejected(Exception):
    """SAM.gov refused the key (401)."""


def _redact(text: str, key: str | None) -> str:
    """The key out of an error's text, as typed and as a URL quotes it."""
    if not key:
        return text
    from urllib.parse import quote, quote_plus

    for form in {key, quote(key, safe=""), quote_plus(key)}:
        text = text.replace(form, "***")
    return text


def _buyer(item: dict[str, Any]) -> Buyer | None:
    path = [p.strip() for p in str(item.get("fullParentPathName") or "").split(".") if p.strip()]
    if not path:
        path = [str(item.get(k) or "").strip() for k in ("department", "subTier", "office")]
        path = [p for p in path if p]
    if not path:
        return None
    office = item.get("officeAddress") or {}
    contacts = item.get("pointOfContact") or []
    primary = next((c for c in contacts if (c or {}).get("type") == "primary"),
                   contacts[0] if contacts else {}) or {}
    code = str(item.get("fullParentPathCode") or "").strip()
    return Buyer(name=clean(path[-1], 300), identifier=code,
                 scheme="us_sam_office" if code else "",
                 locality=clean(str(office.get("city") or "").title(), 100),
                 postcode=clean(office.get("zipcode"), 20),
                 region=clean(office.get("state"), 10), country="US",
                 email=primary.get("email") or None, phone=primary.get("phone") or None,
                 contact=clean(primary.get("fullName"), 120))


def parse(item: dict[str, Any]) -> Tender | None:
    notice_id = str(item.get("noticeId") or "")
    buyer = _buyer(item)
    if not notice_id or buyer is None:
        return None
    kind = str(item.get("type") or item.get("baseType") or "")
    place = item.get("placeOfPerformance") or {}
    where = [clean((place.get("city") or {}).get("name"), 100),
             clean((place.get("state") or {}).get("code"), 10)]
    award = number((item.get("award") or {}).get("amount"))
    links = [str(link) for link in item.get("resourceLinks") or [] if link]
    ui = str(item.get("uiLink") or "")
    path = str(item.get("fullParentPathName") or "")
    return Tender(
        source="sam_gov", notice_id=notice_id,
        title=clean(item.get("title"), 500) or buyer.name,
        url=ui if ui.startswith("http") else f"https://sam.gov/opp/{notice_id}/view",
        buyer=buyer, description=clean(path.replace(".", " › "), 500),
        published_at=parse_time(item.get("postedDate")),
        deadline=parse_time(item.get("responseDeadLine"), end_of_day=True),
        value=Money(award, "USD") if award else None,
        codes=[str(item["naicsCode"])] if item.get("naicsCode") else [], scheme="naics",
        stage="planned" if kind.lower() in _PLANNED_NAMES else "tender",
        notice_type=kind, documents_url=links[0] if links else None,
        places=[w for w in where if w])


class SamGovTenders:
    name = "sam_gov"
    title = "SAM.gov (US federal opportunities)"
    host = HOST
    applicable_countries = frozenset({"US"})
    codes = "naics"
    needs_key = True
    #: Its secret's name must start with this (a key is never sent elsewhere).
    secret_prefix = "sam-gov"
    default_secret = "sam-gov-api-key"
    register_url = REGISTER_URL

    def search(self, http, query, *, key: str | None = None) -> list[Tender]:
        """Opportunities posted since ``query.posted_from`` (one request per page, at most
        ``query.max_pages``: the daily quota is small). Raises ``SamKeyRejected`` when the
        key is refused, ``PermanentError`` for SAM.gov's 404 ("no data")."""
        if not key:
            raise SamKeyRejected("no SAM.gov API key")
        now = datetime.now(timezone.utc)
        since = query.posted_from or now - timedelta(days=30)
        since = max(since, now - timedelta(days=MAX_DAYS))
        limit = max(1, min(1000, query.max_results))
        params: dict[str, Any] = {"api_key": key, "postedFrom": since.strftime("%m/%d/%Y"),
                                  "postedTo": now.strftime("%m/%d/%Y"),
                                  "ptype": list(OPEN_TYPES + PLANNED_TYPES),
                                  "limit": str(limit)}
        state = state_code(query.region)
        if state:
            params["state"] = state
        out: list[Tender] = []
        for page in range(max(1, query.max_pages)):
            params["offset"] = str(page * limit)
            try:
                response = http.get(API, rate_key=HOST, params=params, headers=API_HEADERS,
                                    attempts=1)
            except PermanentError:
                if out:
                    break
                raise
            except BlockedError as exc:
                raise BlockedError(_redact(str(exc), key) + ": SAM.gov refuses a key it doesn't "
                                   "accept and one whose daily quota is spent") from None
            except Exception as exc:
                # requests' errors quote the whole URL, key included.
                text = _redact(str(exc), key)
                if "401" in text:
                    raise SamKeyRejected(text) from None
                raise RuntimeError(f"{type(exc).__name__}: {text}") from None
            data: Any = response.json()
            items = (data or {}).get("opportunitiesData") or []
            out.extend(t for t in (parse(i) for i in items) if t is not None)
            total = int((data or {}).get("totalRecords") or 0)
            if len(items) < limit or len(out) >= total:
                break
        return out
