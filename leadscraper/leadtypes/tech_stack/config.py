"""``lead_types.tech_stack``: which technologies, on whose sites, found how.

```yaml
lead_types:
  tech_stack:
    enabled: true
    technologies: [WordPress, Magento 1, Shopify]   # names from the fingerprint table;
                                                    # empty = any technology, outdated only
    outdated_only: false        # true: only outdated versions of those technologies count
    thresholds: {WordPress: "6.6"}   # technology -> oldest version still current (overrides
                                     # the table)
    check: run                  # run | project | targets: whose organisations besides uploads
    targets: [{name: Example Shop, website: shop.example}]   # an uploaded list (same shape as
    domains: [old-site.example]                              # POST /audit/targets returns)
    targets_file: null          # a CSV/XLSX path (CLI only)
    recheck_after_days: 30      # a site checked more recently is left alone (0: always)
    create_new: true            # a site a web search found becomes an organisation
    sources:
      footprints: {enabled: false, max_results: 20, max_sites: 15,
                   phrases: {Shopify: ['"powered by Shopify"']}}   # opt-in web search
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...sources.web.fingerprint import names, rule


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TechTarget(_Strict):
    """One uploaded business whose site to check (the shape ``/audit/targets`` returns)."""

    name: str = Field("", description="Business name; the domain stands in when empty.")
    website: str | None = Field(None, description="Its site or bare domain.")
    phone: str | None = None
    category: str = ""
    place: str = Field("", description="Its town or area.")


def _known(name: str) -> str:
    found = rule(name)
    if found is None:
        raise ValueError(f"{name!r} is not a technology the fingerprint rules know "
                         f"(known: {', '.join(names())})")
    return found.name


class FootprintSource(_Strict):
    enabled: bool = Field(False, description="A web search per technology and place for the "
                                             "phrases its users' sites show (\"powered by "
                                             "Shopify\"); every site found is then fingerprinted. "
                                             "Off by default: Google's terms restrict automated "
                                             "queries.")
    max_results: int = Field(20, ge=1, le=50, description="Search results read per query.")
    max_sites: int = Field(15, ge=1, le=100, description="Sites fingerprinted per query.")
    phrases: dict[str, list[str]] = Field(default_factory=dict,
                                          description="Extra footprint phrases per technology "
                                                      "(added to the table's).")
    min_delay: float = Field(12.0, ge=0, description="Seconds between searches.")
    max_delay: float = Field(28.0, ge=0)

    @field_validator("phrases")
    @classmethod
    def _phrase_keys(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {_known(k): [p.strip() for p in v if p and p.strip()] for k, v in value.items()}


class TechStackSources(_Strict):
    footprints: FootprintSource = Field(default_factory=FootprintSource)


class TechStackConfig(_Strict):
    enabled: bool = True
    technologies: list[str] = Field(default_factory=list,
                                    description="Technologies to look for, by name (the "
                                                "fingerprint table: WordPress, Shopify, Magento "
                                                "1, Odoo …); empty = any technology, but only "
                                                "outdated versions.")
    outdated_only: bool = Field(False, description="Only outdated versions of the named "
                                                   "technologies count.")
    thresholds: dict[str, str] = Field(default_factory=dict,
                                       description="Technology -> the oldest version still "
                                                   "current (overrides the table).")
    check: Literal["run", "project", "targets"] = Field(
        "run", description="Besides the uploads, check the sites of the organisations this "
                           "run found (run), of every organisation in the project (project), "
                           "or only the uploads (targets).")
    targets: list[TechTarget] = Field(default_factory=list,
                                      description="An uploaded list of businesses.")
    domains: list[str] = Field(default_factory=list, description="An uploaded list of domains.")
    targets_file: str | None = Field(None, description="A CSV/XLSX of businesses or domains "
                                                       "(read when the run starts; CLI only).")
    recheck_after_days: float = Field(30, ge=0, description="Skip sites checked more recently "
                                                            "than this (0: always re-check).")
    max_orgs: int | None = Field(None, gt=0, description="Check at most this many sites a run.")
    create_new: bool = Field(True, description="A site a web search found, running a wanted "
                                               "technology, becomes an organisation.")
    workers: int = Field(6, ge=1, le=32, description="Sites checked at once (one request at a "
                                                    "time per site).")
    min_delay: float = Field(1.0, ge=0, description="Seconds between requests to one site.")
    max_delay: float = Field(3.0, ge=0)
    sources: TechStackSources = Field(default_factory=TechStackSources)

    @field_validator("technologies")
    @classmethod
    def _technologies(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(_known(v) for v in value if str(v).strip()))

    @field_validator("thresholds")
    @classmethod
    def _thresholds(cls, value: dict[str, str]) -> dict[str, str]:
        out = {}
        for name, version in value.items():
            version = str(version).strip()
            if not version or not version[0].isdigit():
                raise ValueError(f"{name}: a version such as \"6.4\"")
            out[_known(name)] = version
        return out
