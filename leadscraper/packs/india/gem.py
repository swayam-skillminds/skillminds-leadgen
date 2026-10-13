"""GeM (India's Government e-Marketplace): ongoing bids, from the public bid listing.

The listing page ``https://bidplus.gem.gov.in/all-bids`` loads its bids with a form
post to ``/all-bids-data`` carrying the page's own form token (``csrf_bd_gem_nk``)
and a JSON payload: a full-text search, "ongoing bids", a page number. Each bid
has its number, item categories (its title), start and end dates, quantity, and the
buying ministry and department; the bid document is ``/showbidDocument/{id}``.
Verified live 2026-10-13: "software" found 573 ongoing bids, ten a page. The end
times arrive as "2026-10-13T13:00:00Z" for bids closing at 1 pm in India, so they
are read as IST.

Terms (verified 2026-10-13): robots.txt disallows only ``/resources/`` and a few
bank-guarantee paths; GeM's copyright policy: its contents "may not be reproduced
partially or fully without due permission in writing in advance". **Off by default**:
reading bids for our own leads is the owner's call. One search per keyword (the
site's own full-text search), at most ``max_pages`` pages each.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta, timezone
from typing import Any

from ...sources.tenders.base import Buyer, Tender, clean, first, number, parse_time

IST = timezone(timedelta(hours=5, minutes=30), "IST")
HOST = "bidplus.gem.gov.in"
PAGE = "https://bidplus.gem.gov.in/all-bids"
DATA = "https://bidplus.gem.gov.in/all-bids-data"
DOCUMENT = "https://bidplus.gem.gov.in/showbidDocument/{id}"
_TOKEN = re.compile(r"""['"]csrf_bd_gem_nk['"]\s*:\s*['"]([0-9a-f]{16,64})['"]""")


class GemFormChanged(Exception):
    """The listing page no longer carries the form token the data post needs."""


def _ist(value: Any):
    text = str(first(value) or "").strip()
    return parse_time(text[:-1] if text.endswith("Z") else text, zone=IST)


def parse(doc: dict[str, Any]) -> Tender | None:
    bid_id = str(first(doc.get("b_id")) or doc.get("id") or "")
    number_ = str(first(doc.get("b_bid_number")) or "").strip()
    department = clean(first(doc.get("ba_official_details_deptName")), 300)
    ministry = clean(first(doc.get("ba_official_details_minName")), 300)
    buyer = department or ministry
    if not bid_id or not number_ or not buyer:
        return None
    items = clean(first(doc.get("b_category_name")) or first(doc.get("bd_category_name")), 500)
    quantity = number(first(doc.get("b_total_quantity")))
    detail = ", ".join(x for x in (ministry if ministry and ministry != buyer else "",
                                   f"quantity {quantity:,.0f}" if quantity else "") if x)
    return Tender(
        source="gem", notice_id=number_, title=items or number_,
        url=DOCUMENT.format(id=bid_id), buyer=Buyer(name=buyer, country="IN"),
        description=detail, published_at=_ist(doc.get("final_start_date_sort")),
        deadline=_ist(doc.get("final_end_date_sort")),
        notice_type="Bid/RA" if first(doc.get("b_bid_to_ra")) else "Bid",
        documents_url=DOCUMENT.format(id=bid_id))


def payload(keyword: str, page: int) -> str:
    return json.dumps({"page": page, "param": {"searchBid": keyword, "searchType": "fullText"},
                       "filter": {"bidStatusType": "ongoing_bids", "byType": "all",
                                  "highBidValue": "", "byEndDate": {"from": "", "to": ""},
                                  "sort": "Bid-End-Date-Oldest"}})


class GemTenders:
    name = "gem"
    title = "GeM (India Government e-Marketplace)"
    host = HOST
    applicable_countries = frozenset({"IN"})
    codes = None
    needs_key = False
    #: Off unless a config enables it (its copyright policy asks for written permission).
    default_enabled = False

    def search(self, http, query, *, key: str | None = None) -> list[Tender]:
        """Ongoing bids found by GeM's own full-text search, one search per keyword."""
        page_html = http.get(PAGE, rate_key=HOST).text
        match = _TOKEN.search(page_html or "")
        if match is None:
            raise GemFormChanged("the bid listing page has no csrf_bd_gem_nk token")
        token = match.group(1)
        out: list[Tender] = []
        seen: set[str] = set()
        for keyword in [k for k in query.keywords if k and k.strip()]:
            for page in range(1, max(1, query.max_pages) + 1):
                data: Any = http.post(DATA, rate_key=HOST,
                                      headers={"X-Requested-With": "XMLHttpRequest",
                                               "Referer": PAGE},
                                      data={"payload": payload(keyword, page),
                                            "csrf_bd_gem_nk": token}).json()
                body = (((data or {}).get("response") or {}).get("response") or {})
                docs = body.get("docs") or []
                for doc in docs:
                    tender = parse(doc)
                    if tender is not None and tender.notice_id not in seen:
                        seen.add(tender.notice_id)
                        out.append(tender)
                if len(docs) < 10 or page * 10 >= int(body.get("numFound") or 0) \
                        or len(out) >= query.max_results:
                    break
            if len(out) >= query.max_results:
                break
        return out[:query.max_results]
