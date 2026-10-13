"""``lead_types.registrations``: which newly incorporated companies, from which registers.

```yaml
lead_types:
  registrations:
    enabled: true
    incorporated_within_days: 30
    industry_codes: []          # prefixes of the register's codes (UK SIC: "62", "6201",
                                # "62012"); empty = every industry
    exclude_codes: ["99999", "74990"]   # dormant and non-trading companies
    company_types: []           # the register's types (Companies House: ltd, llp, plc …)
    scope: place                # place: registered office in a run place; country: anywhere
    create_new: true            # a company nobody holds becomes an organisation
    directors: true             # current directors become people (one request each)
    max_director_lookups: 100
    sources:
      companies_house: {enabled: true, key_secret_name: companies-house-api-key,
                        max_results: 500}            # UK; free key
      opencorporates: {enabled: false, token_secret_name: opencorporates-api-token}
                                # any country; free tokens only for public-benefit users
```

A register applies only where its pack does (Companies House for GB);
OpenCorporates applies anywhere but is opt-in. Elsewhere this lead type plans
nothing and says why.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Companies House's own codes for companies that won't trade.
DEFAULT_EXCLUDE = ["99999", "74990"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompaniesHouseSource(_Strict):
    enabled: bool = True
    key_secret_name: str = Field("companies-house-api-key",
                                 description="The secret holding your Companies House API "
                                             "key; without it the register is skipped.")
    max_results: int = Field(500, ge=1, le=5000, description="Companies read per place.")
    min_delay: float = Field(0.6, ge=0.5, description="600 requests per 5 minutes allowed.")
    max_delay: float = Field(1.2, ge=0.5)

    @field_validator("key_secret_name")
    @classmethod
    def _own_secret(cls, value: str) -> str:
        # A register's key is only ever sent to its register: no other secret can be named.
        if value and not value.startswith("companies-house"):
            raise ValueError("key_secret_name must name a companies-house-… secret")
        return value


class OpenCorporatesSource(_Strict):
    enabled: bool = False
    token_secret_name: str = Field("opencorporates-api-token",
                                   description="The secret holding your OpenCorporates API "
                                               "token (free only for public-benefit users).")
    max_results: int = Field(200, ge=1, le=1000, description="Companies read per place.")
    max_pages: int = Field(2, ge=1, le=10, description="Pages of 100 (the default plan "
                                                       "allows 50 requests a day).")
    min_delay: float = Field(2.0, ge=1.0)
    max_delay: float = Field(4.0, ge=1.0)

    @field_validator("token_secret_name")
    @classmethod
    def _own_secret(cls, value: str) -> str:
        if value and not value.startswith("opencorporates"):
            raise ValueError("token_secret_name must name an opencorporates-… secret")
        return value


class RegisterSources(_Strict):
    companies_house: CompaniesHouseSource = Field(default_factory=CompaniesHouseSource)
    opencorporates: OpenCorporatesSource = Field(default_factory=OpenCorporatesSource)


def _codes(value: list) -> list[str]:
    out = []
    for code in value:
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if not digits:
            raise ValueError(f"{code!r}: an industry code (digits)")
        out.append(digits)
    return list(dict.fromkeys(out))


class RegistrationsConfig(_Strict):
    enabled: bool = True
    incorporated_within_days: int = Field(30, ge=1, le=365,
                                          description="Companies incorporated within this "
                                                      "many days.")
    industry_codes: list[str] = Field(default_factory=list,
                                      description="Industry code prefixes to keep (UK SIC "
                                                  "2007: 62 = computer programming and "
                                                  "consultancy); empty = every industry.")
    exclude_codes: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE),
                                     description="Industry code prefixes to leave out "
                                                 "(99999 dormant, 74990 non-trading).")
    company_types: list[str] = Field(default_factory=list,
                                     description="The register's company types (Companies "
                                                 "House: ltd, llp, plc …); empty = all.")
    scope: Literal["place", "country"] = Field(
        "place", description="place: companies whose registered office is in one of the "
                             "run's places; country: anywhere in the country.")
    create_new: bool = Field(True, description="A company the project doesn't hold yet "
                                               "becomes an organisation.")
    directors: bool = Field(True, description="Current directors become people (one request "
                                              "per company).")
    max_director_lookups: int = Field(100, ge=0, le=5000,
                                      description="Companies whose directors are read per "
                                                  "run.")
    sources: RegisterSources = Field(default_factory=RegisterSources)

    @field_validator("industry_codes", "exclude_codes")
    @classmethod
    def _valid_codes(cls, value: list[str]) -> list[str]:
        return _codes(value)

    @field_validator("company_types")
    @classmethod
    def _types(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(t).strip().lower() for t in value if str(t).strip()))
