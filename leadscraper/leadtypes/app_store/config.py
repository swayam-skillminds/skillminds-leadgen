"""``lead_types.app_store``: which apps, how bad they must be, and where to find them.

```yaml
lead_types:
  app_store:
    enabled: true
    keywords: [clinic, restaurant ordering]   # App Store searches in the run's storefront
    genres: []                  # store categories to keep (Medical, Food & Drink …); empty = any
    exclude_genres: [Games]
    rating_ceiling: 3.5         # rated at or below this: a signal
    min_ratings: 5              # ... from at least this many ratings
    stale_months: 18            # no update for this long: a signal
    min_complaints: 2           # low-star reviews naming a theme (Play's listing shows a few)
    installs: {min: null, max: null}   # Play's download band, e.g. min 1000, max 1000000
    scope: country              # country: the storefront; place: the app or its developer
                                # names a run place
    apps: []                    # apps to check: store links, Apple ids, Play packages
    check_sites: run            # run | project | none: store links on these organisations' sites
    create_new: true            # a developer nobody holds becomes an organisation
    sources:
      itunes: {enabled: true, max_results: 50}
      google_play: {enabled: true, match_ios: true, max_listings: 60}
```
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...sources.apps.base import parse_app_ref


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallRange(_Strict):
    min: int | None = Field(None, ge=0, description="Fewest downloads (Play's band, e.g. 1000).")
    max: int | None = Field(None, ge=0, description="Most downloads (Play's band, e.g. "
                                                    "1000000): leave out the giants.")

    @model_validator(mode="after")
    def _ordered(self) -> "InstallRange":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("installs.min is above installs.max")
        return self


class ITunesSource(_Strict):
    enabled: bool = True
    max_results: int = Field(50, ge=1, le=200, description="Apps read per keyword (Apple's "
                                                           "limit is 200).")
    min_delay: float = Field(3.5, ge=3.0, description="Apple allows about 20 calls a minute.")
    max_delay: float = Field(5.0, ge=3.0)


class GooglePlaySource(_Strict):
    enabled: bool = True
    match_ios: bool = Field(True, description="Look up an App Store app's bundle id on Play; "
                                              "kept only when the listing names the same "
                                              "developer website.")
    max_listings: int = Field(60, ge=0, le=1000, description="Play listings read per run.")
    min_delay: float = Field(2.0, ge=1.0)
    max_delay: float = Field(4.0, ge=1.0)


class AppSources(_Strict):
    itunes: ITunesSource = Field(default_factory=ITunesSource)
    google_play: GooglePlaySource = Field(default_factory=GooglePlaySource)


class AppStoreConfig(_Strict):
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list,
                                description="App Store searches, one unit each (\"clinic\", "
                                            "\"food delivery\"); the run's country is the "
                                            "storefront.")
    genres: list[str] = Field(default_factory=list,
                              description="Store categories to keep (Medical, Food & Drink, "
                                          "Business …); empty = any.")
    exclude_genres: list[str] = Field(default_factory=lambda: ["Games"],
                                      description="Store categories to leave out.")
    rating_ceiling: float = Field(3.5, ge=1.0, le=5.0, description="An app rated at or below "
                                                                   "this is a signal.")
    min_ratings: int = Field(5, ge=1, description="Ratings needed before a low rating counts.")
    stale_months: int = Field(18, ge=1, le=120, description="Months without an update that make "
                                                            "an app stale.")
    min_complaints: int = Field(2, ge=1, le=10, description="Low-star reviews naming a theme "
                                                            "needed for a complaints signal.")
    installs: InstallRange = Field(default_factory=InstallRange)
    scope: Literal["country", "place"] = Field(
        "country", description="country: every app in the storefront; place: only apps whose "
                               "name, description or developer address names a run place.")
    apps: list[str] = Field(default_factory=list,
                            description="Apps to check: store links, Apple ids (id123456789) or "
                                        "Play packages (com.example.app).")
    check_sites: Literal["run", "project", "none"] = Field(
        "run", description="Read App Store / Play links on the own sites of the organisations "
                           "this run found (run), of the whole project (project), or none.")
    max_sites: int = Field(200, ge=0, le=5000, description="Sites read for store links a run.")
    create_new: bool = Field(True, description="A developer the project doesn't hold yet "
                                               "becomes an organisation.")
    workers: int = Field(4, ge=1, le=16, description="Sites read for store links at once.")
    min_delay: float = Field(1.0, ge=0, description="Seconds between requests to one site.")
    max_delay: float = Field(3.0, ge=0)
    sources: AppSources = Field(default_factory=AppSources)

    @field_validator("keywords", "genres", "exclude_genres")
    @classmethod
    def _words(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(" ".join(str(v).split()) for v in value if str(v).strip()))

    @field_validator("apps")
    @classmethod
    def _apps(cls, value: list[str]) -> list[str]:
        for item in value:
            if parse_app_ref(item) is None:
                raise ValueError(f"{item!r}: a store link, an Apple id (id123456789) or a Play "
                                 "package (com.example.app)")
        return list(dict.fromkeys(v.strip() for v in value))
