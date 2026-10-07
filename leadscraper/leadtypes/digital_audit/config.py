"""``lead_types.digital_audit``: which organisations to audit, which checks, how hard.

```yaml
lead_types:
  digital_audit:
    enabled: true
    checks: [no_website, unreachable, https, mobile, pagespeed, cms_age,
             copyright, broken_links, contact_form, booking, maps_profile]   # default: all
    audit: run              # run | project | targets: whose organisations besides the uploads
    targets:                # an uploaded list of businesses (or the API's /audit/targets)
      - {name: Bright Smile Dental, website: brightsmile.example, category: Dentists}
    domains: [example-bakery.test]
    targets_file: null      # a CSV/XLSX path (CLI); columns name, website, phone, category, place
    reaudit_after_days: 7   # an organisation audited more recently is left alone (0: always)
    pagespeed: {key_secret_name: pagespeed-api-key, strategy: mobile, threshold: 50,
                max_per_run: 25}
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CheckName = Literal["no_website", "unreachable", "https", "mobile", "pagespeed", "cms_age",
                    "copyright", "broken_links", "contact_form", "booking", "maps_profile"]
ALL_CHECKS: list[str] = ["no_website", "unreachable", "https", "mobile", "pagespeed", "cms_age",
                         "copyright", "broken_links", "contact_form", "booking", "maps_profile"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditTarget(_Strict):
    """One uploaded business to audit."""

    name: str = Field("", description="Business name; the domain stands in when empty.")
    website: str | None = Field(None, description="Its site or bare domain.")
    phone: str | None = None
    category: str = Field("", description="What it is (decides whether online booking or "
                                          "ordering is expected).")
    place: str = Field("", description="Its town or area.")


class PageSpeedSettings(_Strict):
    key_secret_name: str | None = Field(
        "pagespeed-api-key", description="Secret holding a Google API key (never the key "
        "itself). Without one, Google's shared keyless quota is tried and usually refuses.")
    strategy: Literal["mobile", "desktop"] = "mobile"
    threshold: int = Field(50, ge=1, le=100, description="A performance score below this is "
                                                        "a finding.")
    max_per_run: int = Field(25, ge=0, description="PageSpeed calls per run (each takes "
                                                   "10–30 s on Google's side).")
    min_delay: float = Field(1.0, ge=0)
    max_delay: float = Field(2.0, ge=0)


class DigitalAuditConfig(_Strict):
    enabled: bool = True
    checks: list[CheckName] = Field(default_factory=lambda: list(ALL_CHECKS),
                                    description="The checks to run; each finding is a signal.")
    audit: Literal["run", "project", "targets"] = Field(
        "run", description="Besides the uploaded targets, audit the organisations this run "
                           "found (run), every organisation in the project (project), or only "
                           "the uploads (targets).")
    targets: list[AuditTarget] = Field(default_factory=list,
                                       description="An uploaded list of businesses.")
    domains: list[str] = Field(default_factory=list, description="An uploaded list of domains.")
    targets_file: str | None = Field(None, description="A CSV/XLSX of businesses or domains "
                                                       "(read when the run starts).")
    reaudit_after_days: float = Field(7, ge=0, description="Skip organisations audited more "
                                                           "recently than this (0: always "
                                                           "re-audit).")
    max_orgs: int | None = Field(None, gt=0, description="Audit at most this many per run.")
    max_links: int = Field(8, ge=0, le=50, description="Homepage links checked for breakage.")
    cert_expiry_days: int = Field(21, ge=0, description="A certificate expiring sooner is a "
                                                        "finding.")
    stale_copyright_years: int = Field(2, ge=0, description="A copyright year this many years "
                                                            "old (or older) is a finding.")
    min_reviews: int = Field(10, ge=0, description="Fewer Maps reviews than this makes a "
                                                   "profile incomplete.")
    pagespeed: PageSpeedSettings = Field(default_factory=PageSpeedSettings)
    booking_categories: dict[str, list[Literal["booking", "ordering"]]] = Field(
        default_factory=dict, description="Extra category words -> what customers expect to do "
                                          "online (added to the built-in table).")
    outdated: dict[str, str] = Field(default_factory=dict, description="Technology -> the "
                                     "oldest version still current (overrides the "
                                     "fingerprint table).")
    workers: int = Field(6, ge=1, le=32, description="Sites audited at once (one request at a "
                                                    "time per site).")
    min_delay: float = Field(1.0, ge=0, description="Seconds between requests to one site.")
    max_delay: float = Field(3.0, ge=0)

    @field_validator("checks")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))
