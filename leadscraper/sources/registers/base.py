"""What a company register tells us about a newly incorporated company.

A register class (``Pack.incorporations``, or a generic one such as OpenCorporates)
has:

* ``name``, ``title``, ``host`` (its rate key), ``applicable_countries`` (None =
  anywhere) and ``default_enabled``;
* ``needs_key`` with ``secret_prefix`` / ``default_secret`` / ``register_url``;
* ``by_place``: True when it can narrow a search to one place (one unit per place);
* ``search(http, query, *, key=None, place=None) -> list[Incorporation]``;
* optionally ``officers(http, number, *, key=None) -> list[Officer]`` (its current
  directors), with ``has_officers = True``.

Registers' errors are the HTTP client's; the session records them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Officer:
    """A current director (or LLP member) as the register lists them."""

    name: str
    role: str
    appointed_on: date | None = None


@dataclass
class Incorporation:
    """One newly incorporated company."""

    source: str
    #: The register's company number, and the ``registry_ids`` scheme it is filed under.
    number: str
    scheme: str
    name: str
    incorporated_on: date | None
    #: The company's page on the register (or the aggregator).
    url: str
    company_type: str = ""
    status: str = ""
    #: The registered office, as the register writes it.
    address: str = ""
    locality: str = ""
    postcode: str = ""
    region: str = ""
    #: ISO 3166-1 alpha-2.
    country: str = ""
    #: Industry codes as registered (UK SIC 2007: "62012").
    industry_codes: list[str] = field(default_factory=list)
    code_scheme: str = ""
    officers: list[Officer] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.scheme}:{self.number}"


@dataclass
class IncorporationQuery:
    """What a run asks every register for."""

    country: str
    country_name: str = ""
    #: Companies incorporated in [incorporated_from, incorporated_to].
    incorporated_from: date | None = None
    incorporated_to: date | None = None
    #: Industry code prefixes to keep ("62", "6201"); empty = every industry.
    industry_codes: list[str] = field(default_factory=list)
    #: Register company types ("ltd", "llp"); empty = every type.
    company_types: list[str] = field(default_factory=list)
    max_results: int = 500
    max_pages: int = 5


def as_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
