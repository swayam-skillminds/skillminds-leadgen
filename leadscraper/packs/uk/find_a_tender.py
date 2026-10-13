"""Find a Tender (UK): above-threshold public tenders, from its OCDS API.

``GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?stages=tender&
updatedFrom=…&limit=100`` returns OCDS release packages, newest updates first, with a
``links.next`` cursor. No key; published under the Open Government Licence v3.0
(the package's own ``license``; verified live 2026-10-13). The API can't filter by
classification, so a run reads the window's releases page by page (``max_pages``)
and the lead type keeps the ones whose CPV codes or words it wants.

Each release names its buyer among ``parties`` (role ``buyer``), with the address
and the contact point the notice publishes.
"""

from __future__ import annotations

from typing import Any

from ...sources.tenders.base import (API_HEADERS, Buyer, Money, Tender, clean, number, own_site,
                                      parse_time)

HOST = "www.find-tender.service.gov.uk"
API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
NOTICE = "https://www.find-tender.service.gov.uk/Notice/{id}"


def _cpv(tender: dict[str, Any]) -> list[str]:
    codes = []
    main = tender.get("classification") or {}
    if str(main.get("scheme") or "").upper() == "CPV" and main.get("id"):
        codes.append(str(main["id"]))
    for item in tender.get("items") or []:
        for extra in [item.get("classification") or {}, *(item.get("additionalClassifications")
                                                           or [])]:
            if str(extra.get("scheme") or "").upper() == "CPV" and extra.get("id"):
                codes.append(str(extra["id"]))
    return list(dict.fromkeys(codes))


def _value(tender: dict[str, Any]) -> Money | None:
    value = tender.get("value") or {}
    amount = number(value.get("amount"))
    currency = value.get("currency")
    if amount is None:
        lots = [(number((lot.get("value") or {}).get("amount")),
                 (lot.get("value") or {}).get("currency")) for lot in tender.get("lots") or []]
        lots = [(a, c) for a, c in lots if a]
        if lots:
            amount = sum(a for a, _ in lots)
            currency = lots[0][1]
    low = number((tender.get("minValue") or {}).get("amount"))
    return Money(amount, str(currency or "GBP"), low) if amount else None


def ocds_buyer(release: dict[str, Any], *, scheme: str) -> Buyer | None:
    """The buyer party of an OCDS release (Find a Tender, Contracts Finder)."""
    ref = release.get("buyer") or {}
    party = next((p for p in release.get("parties") or [] if "buyer" in (p.get("roles") or [])),
                 None) or {}
    name = clean(party.get("name") or ref.get("name") or
                 (party.get("identifier") or {}).get("legalName"), 300)
    if not name:
        return None
    address = party.get("address") or {}
    contact = party.get("contactPoint") or {}
    identifier = str(party.get("id") or ref.get("id") or "")
    # The buyer's own site only: a contact point's link is usually the e-tendering portal
    # that hundreds of buyers share.
    return Buyer(name=name, identifier=identifier, scheme=scheme if identifier else "",
                 address=clean(address.get("streetAddress"), 300),
                 locality=clean(address.get("locality"), 100),
                 postcode=clean(address.get("postalCode"), 20),
                 region=clean(address.get("region"), 50), country="GB",
                 website=own_site((party.get("details") or {}).get("url")),
                 email=contact.get("email") or None,
                 phone=contact.get("telephone") or None,
                 contact=clean(contact.get("name"), 120))


def parse(release: dict[str, Any]) -> Tender | None:
    tender = release.get("tender") or {}
    status = str(tender.get("status") or "")
    if status not in ("active", "planned"):
        return None                             # cancelled, complete, withdrawn
    buyer = ocds_buyer(release, scheme="gb_fts")
    release_id = str(release.get("id") or "")
    if buyer is None or not release_id:
        return None
    documents = [d.get("url") for d in tender.get("documents") or [] if d.get("url")]
    submit = str(tender.get("submissionMethodDetails") or "")
    places = [clean((i.get("deliveryLocation") or {}).get("description"), 100)
              for i in tender.get("items") or []]
    return Tender(
        source="find_a_tender", notice_id=release_id,
        title=clean(tender.get("title"), 500) or buyer.name,
        url=NOTICE.format(id=release_id), buyer=buyer,
        description=clean(tender.get("description"), 2000),
        published_at=parse_time(release.get("date")),
        deadline=parse_time((tender.get("tenderPeriod") or {}).get("endDate"), end_of_day=True),
        value=_value(tender), codes=_cpv(tender), scheme="cpv",
        stage="planned" if status == "planned" else "tender",
        notice_type=str(tender.get("procurementMethodDetails") or tender.get("procurementMethod")
                        or ""),
        documents_url=documents[0] if documents else (submit if submit.startswith("http")
                                                      else None),
        places=[p for p in dict.fromkeys(places) if p])


class FindATenderTenders:
    name = "find_a_tender"
    title = "Find a Tender (UK)"
    host = HOST
    applicable_countries = frozenset({"GB"})
    codes = "cpv"
    needs_key = False

    def search(self, http, query, *, key: str | None = None) -> list[Tender]:
        """Tender-stage releases updated since ``query.posted_from``, page by page (at most
        ``query.max_pages``: the API can't filter by code, so the lead type sorts them)."""
        params: dict[str, Any] | None = {"stages": "tender", "limit": "100"}
        if query.posted_from is not None:
            params["updatedFrom"] = query.posted_from.strftime("%Y-%m-%dT%H:%M:%S")
        url, out, seen = API, [], set()
        for _ in range(max(1, query.max_pages)):
            data: Any = http.get(url, rate_key=HOST, params=params, headers=API_HEADERS).json()
            for release in (data or {}).get("releases") or []:
                tender = parse(release)
                if tender is not None and tender.notice_id not in seen:
                    seen.add(tender.notice_id)
                    out.append(tender)
            following = ((data or {}).get("links") or {}).get("next")
            if not following or not str(following).startswith(API):
                break
            url, params = str(following), None      # the cursor carries the query
        return out
