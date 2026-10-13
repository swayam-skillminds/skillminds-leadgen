"""TED (Tenders Electronic Daily): the EU's public-procurement notices, from its Search API.

``POST https://api.ted.europa.eu/v3/notices/search`` with an expert query
(``buyer-country=IRL AND classification-cpv IN (72000000) AND publication-date>=…``),
the fields to return, ``limit`` and ``page``. Anonymous: the API is "primarily
targeted at data reusers and does not require authentication" (TED developer
docs, verified live 2026-10-13: 200 with notices; an unknown field is a 400 that
lists every supported one). Titles come in every EU language (English is always
there); a buyer's name, city and contacts in its own.

A CPV query is hierarchical: ``72000000`` also finds ``72268000``.
"""

from __future__ import annotations

from typing import Any

from ...sources.tenders.base import (API_HEADERS, Buyer, Money, Tender, clean, first, number,
                                      own_site, parse_time, text_of)

HOST = "api.ted.europa.eu"
API = "https://api.ted.europa.eu/v3/notices/search"
NOTICE = "https://ted.europa.eu/en/notice/-/detail/{number}"

#: EU member states and the EEA countries that publish on TED: ISO alpha-2 -> TED's alpha-3.
COUNTRIES = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "HR": "HRV", "CY": "CYP", "CZ": "CZE", "DK": "DNK",
    "EE": "EST", "FI": "FIN", "FR": "FRA", "DE": "DEU", "GR": "GRC", "HU": "HUN", "IE": "IRL",
    "IT": "ITA", "LV": "LVA", "LT": "LTU", "LU": "LUX", "MT": "MLT", "NL": "NLD", "PL": "POL",
    "PT": "PRT", "RO": "ROU", "SK": "SVK", "SI": "SVN", "ES": "ESP", "SE": "SWE",
    "IS": "ISL", "LI": "LIE", "NO": "NOR",
}
#: Notice types (eForms) that invite bids now, and those that announce one to come.
OPEN_TYPES = ("cn-standard", "cn-social", "cn-desg", "pin-cfc-standard", "pin-cfc-social",
              "qu-sy", "subco")
PLANNED_TYPES = ("pin-only", "pin-buyer")
FIELDS = [
    "publication-number", "publication-date", "notice-type", "notice-title", "title-proc",
    "description-lot", "classification-cpv", "deadline-receipt-tender-date-lot",
    "deadline-receipt-request-date-lot", "estimated-value-proc", "estimated-value-cur-proc",
    "estimated-value-lot", "estimated-value-cur-lot", "document-url-lot",
    "place-of-performance-city-lot", "buyer-name", "buyer-city", "buyer-country",
    "buyer-email", "buyer-internet-address", "organisation-tel-buyer",
    "organisation-post-code-buyer", "organisation-street-buyer",
    "organisation-identifier-buyer",
]


def _term(word: str) -> str:
    word = " ".join(word.replace('"', " ").split())
    return f'"{word}"' if " " in word else word


def build_query(query) -> str:
    """The expert query for a run: its country, its codes or keywords, recent notices."""
    parts = [f"buyer-country={COUNTRIES[query.country.upper()]}",
             f"notice-type IN ({' '.join(OPEN_TYPES + PLANNED_TYPES)})"]
    if query.posted_from is not None:
        parts.append(f"publication-date>={query.posted_from:%Y%m%d}")
    match = getattr(query, "match", "auto")
    cpv = " ".join(dict.fromkeys(c for c in query.cpv if c))
    words = " OR ".join(_term(w) for w in query.keywords if w and w.strip())
    by_code = f"classification-cpv IN ({cpv})" if cpv else ""
    by_word = f"FT~({words})" if words else ""
    if match == "keywords" and by_word:
        parts.append(by_word)
    elif match == "any" and by_code and by_word:
        parts.append(f"({by_code} OR {by_word})")
    elif by_code and match != "keywords":
        parts.append(by_code)
    elif by_word:
        parts.append(by_word)
    return " AND ".join(parts) + " SORT BY publication-date DESC"


def _value(notice: dict[str, Any]) -> Money | None:
    amount = number(first(notice.get("estimated-value-proc")))
    currency = first(notice.get("estimated-value-cur-proc"))
    if amount is None:
        lots = [v for v in (number(x) for x in notice.get("estimated-value-lot") or []) if v]
        amount = max(lots) if lots else None
        currency = first(notice.get("estimated-value-cur-lot"))
    return Money(amount, str(currency or "EUR")) if amount else None


def _deadline(notice: dict[str, Any]):
    dates = [parse_time(d, end_of_day=True) for d in
             (notice.get("deadline-receipt-tender-date-lot")
              or notice.get("deadline-receipt-request-date-lot") or [])]
    dates = [d for d in dates if d is not None]
    return max(dates) if dates else None        # open while any lot still is


def _places(notice: dict[str, Any]) -> list[str]:
    value = notice.get("place-of-performance-city-lot")
    if isinstance(value, dict):
        value = [v for vs in value.values() for v in (vs if isinstance(vs, list) else [vs])]
    return list(dict.fromkeys(clean(v, 100) for v in (value or []) if v))


def buyer_id(value: Any, country: str) -> str:
    """The buyer's national identifier, written one way: buyers type it into each notice
    ("20110042", "IE 20110042", "ie-20110042"), and it is an identity key. Letters and
    digits only, upper case, without a leading country code; "" for none ("NA")."""
    text = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    if country and text.startswith(country.upper()) and len(text) > len(country) + 2 \
            and text[len(country)].isdigit():
        text = text[len(country):]
    return "" if text in ("", "NA", "NONE", "0") else text


def parse(notice: dict[str, Any], country: str) -> Tender | None:
    number_ = str(notice.get("publication-number") or "").strip()
    buyer_name = clean(text_of(notice.get("buyer-name")), 300)
    if not number_ or not buyer_name:
        return None
    title = clean(text_of(notice.get("title-proc")) or text_of(notice.get("notice-title")), 500)
    identifier = buyer_id(first(notice.get("organisation-identifier-buyer")), country)
    buyer = Buyer(
        name=buyer_name,
        identifier=f"{country}:{identifier}" if identifier else "",
        scheme="ted_buyer" if identifier else "",
        address=clean(first(notice.get("organisation-street-buyer")), 300),
        locality=clean(text_of(notice.get("buyer-city")), 100),
        postcode=clean(first(notice.get("organisation-post-code-buyer")), 20),
        country=country,
        website=own_site(first(notice.get("buyer-internet-address"))),
        email=str(first(notice.get("buyer-email")) or "") or None,
        phone=str(first(notice.get("organisation-tel-buyer")) or "") or None)
    links = notice.get("links") or {}
    url = str(((links.get("html") or {}).get("ENG")) or NOTICE.format(number=number_))
    notice_type = str(notice.get("notice-type") or "")
    return Tender(
        source="ted", notice_id=number_, title=title or buyer_name, url=url, buyer=buyer,
        description=clean(text_of(notice.get("description-lot")), 2000),
        published_at=parse_time(notice.get("publication-date")),
        deadline=_deadline(notice), value=_value(notice),
        codes=list(dict.fromkeys(str(c) for c in notice.get("classification-cpv") or [])),
        scheme="cpv", stage="planned" if notice_type in PLANNED_TYPES else "tender",
        notice_type=notice_type,
        documents_url=str(first(notice.get("document-url-lot")) or "") or None,
        places=_places(notice))


class TedTenders:
    name = "ted"
    title = "TED (EU Tenders Electronic Daily)"
    host = HOST
    applicable_countries = frozenset(COUNTRIES)
    codes = "cpv"
    needs_key = False

    def search(self, http, query, *, key: str | None = None) -> list[Tender]:
        """Notices for the run, newest first, at most ``query.max_results`` of them."""
        expert = build_query(query)
        per_page = max(1, min(250, query.max_results))
        out: list[Tender] = []
        for page in range(1, max(1, query.max_pages) + 1):
            data: Any = http.post(API, rate_key=HOST, headers=API_HEADERS,
                                  json={"query": expert, "fields": FIELDS, "limit": per_page,
                                        "page": page, "paginationMode": "PAGE_NUMBER"}).json()
            notices = (data or {}).get("notices") or []
            for notice in notices:
                tender = parse(notice, query.country.upper())
                if tender is not None:
                    out.append(tender)
            total = int((data or {}).get("totalNoticeCount") or 0)
            if len(notices) < per_page or len(out) >= query.max_results \
                    or page * per_page >= total:
                break
        return out[:query.max_results]
