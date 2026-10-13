"""Companies House (UK): a company's current directors, from the official public API.

``GET https://api.company-information.service.gov.uk/search/companies?q=…`` finds
the company (an active one whose registered name matches ours closely);
``GET /company/{number}/officers`` lists its officers. Directors and LLP
members who haven't resigned are decision-makers by law. A free API key is
needed (HTTP Basic, the key as user name; without one the API answers 401,
verified 2026-10-08); the limit is 600 requests per 5 minutes. The key is the
secret ``companies-house-api-key`` and never leaves the Authorization header.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

from ...sources.people.roles import person_name, same_company
from ...sources.people.team_pages import Found

HOST = "api.company-information.service.gov.uk"
API = "https://api.company-information.service.gov.uk"
PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}/officers"
COMPANY = "https://find-and-update.company-information.service.gov.uk/company/{number}"
#: Where to get the free key.
REGISTER_URL = ("https://developer.company-information.service.gov.uk (register, then create "
                "an application and a REST API key)")
#: Officer roles that run the company (secretaries, nominees and corporate officers don't
#: name a person).
ROLES = {"director": "Director", "llp-member": "LLP member",
         "llp-designated-member": "LLP designated member",
         "managing-officer": "Managing officer"}


_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)


def _postcodes(address: str | None) -> list[str]:
    return [m.group(0) for m in _POSTCODE.finditer(address or "")]


def display_name(name: str) -> str:
    """"SHAH, Priya Anand" -> "Priya Anand Shah"."""
    surname, _, given = (name or "").partition(",")
    full = f"{given.strip()} {surname.strip()}".strip() if given else surname.strip()
    return " ".join(w.capitalize() if w.isupper() else w for w in full.split())


class CompaniesHouseDirectors:
    name = "companies_house"
    applicable_countries = frozenset({"GB"})
    #: Its secret's name must start with this (a key is never sent elsewhere).
    secret_prefix = "companies-house"
    default_secret = "companies-house-api-key"
    register_url = REGISTER_URL
    host = HOST

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        token = base64.b64encode(f"{key}:".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def find(self, http, company: str, *, key: str, locality: str | None = None,
             address: str | None = None) -> tuple[list[Found], str | None]:
        """(current directors, why not). ``registry_no_match``: no active company with this
        name; ``registry_ambiguous``: several, and the place doesn't single one out (a
        "Phoenix Cafe" in Leeds is not the one in London). HTTP errors are raised."""
        headers = self._headers(key)
        data: Any = http.get(f"{API}/search/companies?q={quote(company)}&items_per_page=10",
                             rate_key=HOST, headers=headers).json()
        candidates = []
        for item in (data or {}).get("items") or []:
            if str(item.get("company_status") or "active") != "active":
                continue
            score = same_company(str(item.get("title") or ""), company)
            if score >= 90:
                candidates.append((score, item))
        if not candidates:
            return [], "registry_no_match"
        if len({str(item.get("company_number")) for _, item in candidates}) > 1:
            places = [p.casefold() for p in (locality, *_postcodes(address)) if p]
            here = [(score, item) for score, item in candidates
                    if any(p in str(item.get("address_snippet") or "").casefold()
                           for p in places)]
            if len({str(item.get("company_number")) for _, item in here}) != 1:
                return [], "registry_ambiguous"
            candidates = here
        best = max(candidates, key=lambda c: c[0])
        number = str(best[1].get("company_number") or "")
        return [Found(name, role, PAGE.format(number=number), "registry")
                for name, role, _ in current_officers(http, number, key=key)], None


def current_officers(http, number: str, *, key: str) -> list[tuple[str, str, str | None]]:
    """(name, role, appointed on) of a company's current directors and LLP members.

    ``GET /company/{number}/officers``: officers who haven't resigned, in a role that
    runs the company (``ROLES``), named as a person. HTTP errors are raised."""
    officers: Any = http.get(f"{API}/company/{quote(number)}/officers?items_per_page=50",
                             rate_key=HOST,
                             headers=CompaniesHouseDirectors._headers(key)).json()
    out = []
    for officer in (officers or {}).get("items") or []:
        role = ROLES.get(str(officer.get("officer_role") or ""))
        if not role or officer.get("resigned_on"):
            continue
        name = person_name(display_name(str(officer.get("name") or "")))
        if name:
            out.append((name, role, officer.get("appointed_on") or None))
    return out


class CompaniesHouseIncorporations:
    """Newly incorporated companies, from the advanced company search (Phase 7).

    ``GET /advanced-search/companies?incorporated_from=…&incorporated_to=…&
    company_status=active&location=<place>&sic_codes=…&size=…&start_index=…`` (API
    reference, verified 2026-10-13; 401 "Empty Authorization header" without a key, 404
    "No companies found"). ``location`` matches the registered office's address, so a run
    asks once per place. Directors come from ``/company/{number}/officers``.
    """

    name = "companies_house"
    title = "Companies House (UK)"
    host = HOST
    applicable_countries = frozenset({"GB"})
    needs_key = True
    secret_prefix = "companies-house"
    default_secret = "companies-house-api-key"
    register_url = REGISTER_URL
    default_enabled = True
    by_place = True
    has_officers = True
    #: The search answers at most this many companies per request.
    PAGE_SIZE = 5000

    def search(self, http, query, *, key: str | None = None,
               place: str | None = None) -> list:
        from ...net.ratelimit import PermanentError
        from ...sources.registers.base import Incorporation, as_date

        if not key:
            raise PermissionError("no Companies House API key")
        size = max(1, min(self.PAGE_SIZE, query.max_results))
        params: dict[str, Any] = {"company_status": "active", "size": str(size)}
        if query.incorporated_from:
            params["incorporated_from"] = query.incorporated_from.isoformat()
        if query.incorporated_to:
            params["incorporated_to"] = query.incorporated_to.isoformat()
        if place:
            params["location"] = place
        if query.industry_codes:
            # The register files five-digit codes; a shorter prefix is matched afterwards.
            exact = [c for c in query.industry_codes if len(c) == 5 and c.isdigit()]
            if exact and len(exact) == len(query.industry_codes):
                params["sic_codes"] = ",".join(exact)
        if query.company_types:
            params["company_type"] = ",".join(query.company_types)
        out = []
        for page in range(max(1, query.max_pages)):
            params["start_index"] = str(page * size)
            try:
                data: Any = http.get(f"{API}/advanced-search/companies", rate_key=HOST,
                                     params=params,
                                     headers=CompaniesHouseDirectors._headers(key)).json()
            except PermanentError:          # 404: "No companies found"
                break
            items = (data or {}).get("items") or []
            for item in items:
                number = str(item.get("company_number") or "").strip()
                name = str(item.get("company_name") or "").strip()
                if not number or not name:
                    continue
                office = item.get("registered_office_address") or {}
                lines = [office.get(k) for k in ("address_line_1", "address_line_2",
                                                 "locality", "region", "postal_code")]
                out.append(Incorporation(
                    source=self.name, number=number, scheme="uk_company", name=name,
                    incorporated_on=as_date(item.get("date_of_creation")),
                    url=COMPANY.format(number=number),
                    company_type=str(item.get("company_type") or ""),
                    status=str(item.get("company_status") or ""),
                    address=", ".join(str(x).strip() for x in lines if x),
                    locality=str(office.get("locality") or "").strip(),
                    postcode=str(office.get("postal_code") or "").strip(),
                    region=str(office.get("region") or "").strip(), country="GB",
                    industry_codes=[str(c) for c in item.get("sic_codes") or []],
                    code_scheme="uk_sic_2007"))
            hits = int((data or {}).get("hits") or 0)
            if len(items) < size or (page + 1) * size >= hits or len(out) >= query.max_results:
                break
        return out[:query.max_results]

    def officers(self, http, number: str, *, key: str | None = None) -> list:
        from ...sources.registers.base import Officer, as_date

        if not key:
            return []
        return [Officer(name, role, as_date(appointed))
                for name, role, appointed in current_officers(http, number, key=key)]
