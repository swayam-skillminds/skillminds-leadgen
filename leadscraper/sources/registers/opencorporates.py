"""OpenCorporates: companies of most jurisdictions, as a generic incorporations source.

``GET https://api.opencorporates.com/v0.4/companies/search?country_code=gb&
incorporation_date=2026-09-13:2026-10-13&current_status=Active&per_page=100&page=1&
api_token=…`` (API reference, verified 2026-10-13: every request needs a token, a
401 "Invalid Api Token" without one). Date filters take a ``from:to`` range; term
filters a pipe-separated "any of" list. Without a search term the docs don't say
what the search returns, and no token was available to check: the name term is left
out and the date, country and status filters do the work.

Terms (verified 2026-10-13): free tokens are only for "Permitted Users" (journalists,
NGOs, academics; "corporations … are not Permitted Users"); commercial use needs a
paid, non-share-alike key. The default limits are 200 requests a month and 50 a
day. **Off by default**, with the secret ``opencorporates-api-token``.
"""

from __future__ import annotations

from typing import Any

from ...core.geo import USER_AGENT
from .base import Incorporation, as_date

HOST = "api.opencorporates.com"
API = "https://api.opencorporates.com/v0.4/companies/search"


def _redact(text: str, key: str | None) -> str:
    """The token out of an error's text, as typed and as a URL quotes it."""
    if not key:
        return text
    from urllib.parse import quote, quote_plus

    for form in {key, quote(key, safe=""), quote_plus(key)}:
        text = text.replace(form, "***")
    return text


def parse(company: dict[str, Any]) -> Incorporation | None:
    number = str(company.get("company_number") or "").strip()
    name = str(company.get("name") or "").strip()
    jurisdiction = str(company.get("jurisdiction_code") or "").strip()
    if not number or not name or not jurisdiction:
        return None
    address = company.get("registered_address") or {}
    codes = [str((c.get("industry_code") or {}).get("code") or "")
             for c in company.get("industry_codes") or []]
    return Incorporation(
        source="opencorporates", number=number, scheme=f"oc_{jurisdiction}", name=name,
        incorporated_on=as_date(company.get("incorporation_date")),
        url=str(company.get("opencorporates_url") or
                f"https://opencorporates.com/companies/{jurisdiction}/{number}"),
        company_type=str(company.get("company_type") or ""),
        status=str(company.get("current_status") or ""),
        address=str(company.get("registered_address_in_full") or "").strip(),
        locality=str(address.get("locality") or "").strip(),
        postcode=str(address.get("postal_code") or "").strip(),
        region=str(address.get("region") or "").strip(),
        country=jurisdiction.split("_")[0].upper()[:2],
        industry_codes=[c for c in codes if c])


class OpenCorporatesIncorporations:
    name = "opencorporates"
    title = "OpenCorporates (any jurisdiction)"
    host = HOST
    applicable_countries = None
    needs_key = True
    secret_prefix = "opencorporates"
    default_secret = "opencorporates-api-token"
    register_url = ("https://opencorporates.com/api_accounts/new (free only for journalists, "
                    "NGOs and academics; commercial use needs a paid key)")
    default_enabled = False
    by_place = True
    has_officers = False

    def search(self, http, query, *, key: str | None = None,
               place: str | None = None) -> list[Incorporation]:
        if not key:
            raise PermissionError("no OpenCorporates API token")
        params: dict[str, Any] = {"country_code": query.country.lower(),
                                  "current_status": "Active", "per_page": "100",
                                  "api_token": key}
        if query.incorporated_from or query.incorporated_to:
            params["incorporation_date"] = (
                f"{query.incorporated_from.isoformat() if query.incorporated_from else ''}:"
                f"{query.incorporated_to.isoformat() if query.incorporated_to else ''}")
        if place:
            params["registered_address"] = place
        if query.company_types:
            params["company_type"] = "|".join(query.company_types)
        out: list[Incorporation] = []
        for page in range(1, max(1, query.max_pages) + 1):
            params["page"] = str(page)
            try:
                data: Any = http.get(API, rate_key=HOST, params=params, attempts=1,
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "application/json"}).json()
            except Exception as exc:        # requests quotes the URL, token included
                raise RuntimeError(f"{type(exc).__name__}: {_redact(str(exc), key)}") \
                    from None
            results = (data or {}).get("results") or {}
            for row in results.get("companies") or []:
                company = parse(row.get("company") or {})
                if company is not None:
                    out.append(company)
            if page >= int(results.get("total_pages") or 1) or len(out) >= query.max_results:
                break
        return out[:query.max_results]
