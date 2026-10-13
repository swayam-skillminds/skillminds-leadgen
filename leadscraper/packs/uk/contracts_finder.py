"""Contracts Finder (UK, England): lower-value public contracts, from its search API.

``POST https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json`` with
``{"searchCriteria": {"types": ["Contract", "Pipeline"], "statuses": ["Open"],
"cpvCodes": [...], "publishedFrom": "YYYY-MM-DD"}, "size": 1000}``. No key; the
service's data is published under the Open Government Licence v3.0 (verified live
2026-10-13: 200; a CPV code finds the notices classified under it). A result carries
the buyer's name and postcode, the deadline, the value range and its CPV codes; the
notice page has the rest.
"""

from __future__ import annotations

from typing import Any

from ...sources.tenders.base import API_HEADERS, Buyer, Money, Tender, clean, number, parse_time

HOST = "www.contractsfinder.service.gov.uk"
API = "https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json"
NOTICE = "https://www.contractsfinder.service.gov.uk/Notice/{id}"
MAX_SIZE = 1000


def parse(item: dict[str, Any]) -> Tender | None:
    notice_id = str(item.get("id") or "")
    name = clean(item.get("organisationName"), 300)
    if not notice_id or not name:
        return None
    high, low = number(item.get("valueHigh")), number(item.get("valueLow"))
    value = Money(high, "GBP", low) if high else (Money(low, "GBP") if low else None)
    region = clean(item.get("regionText") or item.get("region"), 80)
    if region.lower() == "any region":
        region = ""
    kind = str(item.get("noticeType") or "")
    return Tender(
        source="contracts_finder", notice_id=notice_id,
        title=clean(item.get("title"), 500) or name, url=NOTICE.format(id=notice_id),
        buyer=Buyer(name=name, postcode=clean(item.get("postcode"), 20).upper(),
                    region=region, country="GB"),
        description=clean(item.get("description"), 2000),
        published_at=parse_time(item.get("publishedDate")),
        deadline=parse_time(item.get("deadlineDate"), end_of_day=True), value=value,
        codes=str(item.get("cpvCodes") or "").split(), scheme="cpv",
        stage="planned" if kind.lower() in ("pipeline", "preprocurement") else "tender",
        notice_type=kind, places=[region] if region else [])


class ContractsFinderTenders:
    name = "contracts_finder"
    title = "Contracts Finder (UK)"
    host = HOST
    applicable_countries = frozenset({"GB"})
    codes = "cpv"
    needs_key = False

    def search(self, http, query, *, key: str | None = None) -> list[Tender]:
        """Open notices published since ``query.posted_from``: by CPV code unless the run
        matches by keywords only (then every open notice, for the words to sort)."""
        criteria: dict[str, Any] = {"types": ["Contract", "Pipeline"], "statuses": ["Open"]}
        if query.posted_from is not None:
            criteria["publishedFrom"] = query.posted_from.strftime("%Y-%m-%d")
        match = getattr(query, "match", "auto")
        if query.cpv and match in ("auto", "codes"):
            criteria["cpvCodes"] = list(dict.fromkeys(query.cpv))
        data: Any = http.post(API, rate_key=HOST, headers=API_HEADERS,
                              json={"searchCriteria": criteria,
                                    "size": max(1, min(MAX_SIZE, query.max_results))}).json()
        out = []
        for hit in (data or {}).get("noticeList") or []:
            tender = parse(hit.get("item") or {})
            if tender is not None:
                out.append(tender)
        return out
