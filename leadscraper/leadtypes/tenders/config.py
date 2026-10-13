"""``lead_types.tenders``: which public tenders count, from which portals.

```yaml
lead_types:
  tenders:
    enabled: true
    cpv: ["72000000", "48000000", ...]     # EU / UK classification (prefixes; default IT)
    naics: ["5415", "518210", "611420"]    # US classification (prefixes; default IT)
    keywords: [software, website, ...]     # where a portal has no codes (India), or always
    match: auto            # auto: codes where the portal classifies, keywords otherwise
                           # codes | keywords | any (codes or keywords)
    min_value: null        # in the tender's own currency; a tender stating none is kept
    max_value: null
    closing_after: null    # ISO date; default: now (only tenders still open)
    posted_within_days: 30
    planned: true          # prior-information / pre-solicitation notices count too
    scope: country         # country | place (the buyer or the work in a run place)
    create_new: true       # a buyer nobody holds becomes an organisation
    sources:
      ted: {enabled: true, max_results: 250}               # EU + EEA
      find_a_tender: {enabled: true, max_pages: 20}        # UK, above threshold
      contracts_finder: {enabled: true, max_results: 1000} # UK, below threshold
      sam_gov: {enabled: true, key_secret_name: sam-gov-api-key, max_requests: 1}  # US
      cppp: {enabled: false, max_pages: 10}                # India: opt-in (terms)
      gem: {enabled: false, max_pages: 2}                  # India: opt-in (terms)
```

A portal applies only where its pack does (TED for EU and EEA countries, SAM.gov
for the US …); in other countries this lead type plans nothing and says why.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: IT services (72), software packages (48), computer equipment (302), IT training
#: (80533). CPV, as prefixes.
DEFAULT_CPV = ["72000000", "48000000", "30200000", "80533000"]
#: Custom programming, systems design, facilities management, other computer services
#: (5415xx), data processing and hosting (518210), computer training (611420).
DEFAULT_NAICS = ["5415", "518210", "611420"]
DEFAULT_KEYWORDS = ["software", "website", "web portal", "web application", "mobile app",
                    "mobile application", "application development", "ERP", "IT services",
                    "digital transformation", "cloud", "data centre", "cyber security",
                    "e-governance", "IT infrastructure", "networking", "server", "computer",
                    "laptop", "AMC of computers", "IT support", "database"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Source(_Strict):
    enabled: bool = True
    min_delay: float = Field(1.5, ge=0, description="Seconds between requests to its host.")
    max_delay: float = Field(3.0, ge=0)


class TedSource(_Source):
    max_results: int = Field(250, ge=1, le=2500, description="Notices read per run.")


class FindATenderSource(_Source):
    max_pages: int = Field(20, ge=1, le=200, description="Pages of 100 releases read per run "
                                                         "(the API can't filter by code).")


class ContractsFinderSource(_Source):
    max_results: int = Field(1000, ge=1, le=1000, description="Notices read per run.")


class SamGovSource(_Source):
    key_secret_name: str = Field("sam-gov-api-key",
                                 description="The secret holding your SAM.gov public API key "
                                             "(SAM.gov → Account Details); without it the "
                                             "source is skipped.")
    max_requests: int = Field(1, ge=1, le=10, description="Requests per run (a personal key "
                                                          "without a SAM.gov role allows 10 a "
                                                          "day).")
    max_results: int = Field(1000, ge=1, le=1000, description="Opportunities per request.")

    @field_validator("key_secret_name")
    @classmethod
    def _own_secret(cls, value: str) -> str:
        # A portal's key is only ever sent to its portal: no other secret can be named.
        if value and not value.startswith("sam-gov"):
            raise ValueError("key_secret_name must name a sam-gov-… secret")
        return value


class CpppSource(_Source):
    enabled: bool = False
    max_pages: int = Field(10, ge=1, le=100, description="Pages of ten active tenders read.")
    min_delay: float = Field(3.0, ge=0)
    max_delay: float = Field(6.0, ge=0)


class GemSource(_Source):
    enabled: bool = False
    max_pages: int = Field(2, ge=1, le=20, description="Pages of ten bids read per keyword.")
    max_keywords: int = Field(8, ge=1, le=40, description="Keywords searched (GeM searches "
                                                          "one at a time).")
    min_delay: float = Field(3.0, ge=0)
    max_delay: float = Field(6.0, ge=0)


class TenderSources(_Strict):
    ted: TedSource = Field(default_factory=TedSource)
    find_a_tender: FindATenderSource = Field(default_factory=FindATenderSource)
    contracts_finder: ContractsFinderSource = Field(default_factory=ContractsFinderSource)
    sam_gov: SamGovSource = Field(default_factory=SamGovSource)
    cppp: CpppSource = Field(default_factory=CpppSource)
    gem: GemSource = Field(default_factory=GemSource)


def _codes(value: list) -> list[str]:
    out = []
    for code in value:
        digits = "".join(ch for ch in str(code).split("-")[0] if ch.isdigit())
        if len(digits) < 2:
            raise ValueError(f"{code!r}: a classification code (at least two digits)")
        out.append(digits)
    return list(dict.fromkeys(out))


class TendersConfig(_Strict):
    enabled: bool = True
    cpv: list[str] = Field(default_factory=lambda: list(DEFAULT_CPV),
                           description="CPV codes (EU and UK portals), as prefixes: 72000000 "
                                       "is every IT service.")
    naics: list[str] = Field(default_factory=lambda: list(DEFAULT_NAICS),
                             description="NAICS codes (SAM.gov), as prefixes: 5415 is every "
                                         "computer systems design service.")
    keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_KEYWORDS),
                                description="Words or phrases in a tender's title or "
                                            "description: used where a portal has no codes "
                                            "(India's), or always with match: keywords / any.")
    match: Literal["auto", "codes", "keywords", "any"] = Field(
        "auto", description="auto: codes where the portal classifies, keywords where it "
                            "doesn't; codes: codes only; keywords: keywords only; any: either.")
    min_value: float | None = Field(None, ge=0, description="Smallest stated value, in the "
                                                            "tender's own currency.")
    max_value: float | None = Field(None, ge=0, description="Largest stated value.")
    closing_after: date | None = Field(None, description="Only tenders closing after this "
                                                         "date (default: still open now).")
    posted_within_days: int = Field(30, ge=1, le=365, description="Notices published "
                                                                  "within this many days.")
    planned: bool = Field(True, description="Planned procurements count too (prior "
                                            "information, pre-solicitation, sources sought).")
    scope: Literal["country", "place"] = Field(
        "country", description="country: every buyer in the run's country; place: only "
                               "buyers (or work) in one of the run's places.")
    create_new: bool = Field(True, description="A buyer the project doesn't hold yet becomes "
                                               "an organisation.")
    sources: TenderSources = Field(default_factory=TenderSources)

    @field_validator("cpv", "naics")
    @classmethod
    def _valid_codes(cls, value: list[str]) -> list[str]:
        return _codes(value)

    @field_validator("keywords")
    @classmethod
    def _words(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(" ".join(str(w).split()) for w in value if str(w).strip()))
