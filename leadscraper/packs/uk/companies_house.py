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
from typing import Any
from urllib.parse import quote

from ...sources.people.roles import person_name, same_company
from ...sources.people.team_pages import Found

HOST = "api.company-information.service.gov.uk"
API = "https://api.company-information.service.gov.uk"
PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}/officers"
#: Officer roles that run the company (secretaries, nominees and corporate officers don't
#: name a person).
ROLES = {"director": "Director", "llp-member": "LLP member",
         "llp-designated-member": "LLP designated member",
         "managing-officer": "Managing officer"}


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
    host = HOST

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        token = base64.b64encode(f"{key}:".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def find(self, http, company: str, *, key: str) -> tuple[list[Found], str | None]:
        """(current directors, why not): ``registry_no_match`` when no active company
        with this name is registered. HTTP errors are raised as they are."""
        headers = self._headers(key)
        data: Any = http.get(f"{API}/search/companies?q={quote(company)}&items_per_page=5",
                             rate_key=HOST, headers=headers).json()
        best = None
        for item in (data or {}).get("items") or []:
            if str(item.get("company_status") or "active") != "active":
                continue
            score = same_company(str(item.get("title") or ""), company)
            if score >= 90 and (best is None or score > best[0]):
                best = (score, item)
        if best is None:
            return [], "registry_no_match"
        number = str(best[1].get("company_number") or "")
        officers: Any = http.get(f"{API}/company/{quote(number)}/officers?items_per_page=50",
                                 rate_key=HOST, headers=headers).json()
        out = []
        for officer in (officers or {}).get("items") or []:
            role = ROLES.get(str(officer.get("officer_role") or ""))
            if not role or officer.get("resigned_on"):
                continue
            name = person_name(display_name(str(officer.get("name") or "")))
            if name:
                out.append(Found(name, role, PAGE.format(number=number), "registry"))
        return out, None
