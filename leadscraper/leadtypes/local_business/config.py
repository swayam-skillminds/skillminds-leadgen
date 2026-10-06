"""``lead_types.local_business`` -- the config model for this lead type.

Everything the original scraper read from the top level of ``config.yaml``
that is specific to finding businesses by category lives here, namespaced:
its categories, its sources' settings, its legacy score weights, its dedup
knobs, and the hospitality vertical's brands and tiers.

Pack-specific keys a source understands (``indiamart``, ``city_level``,
``serp_fallback``, ...) are allowed through as extra fields, so a pack can
grow a setting without a schema change here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KNOWN_VERTICALS = ("events", "hospitality")


class SourceSettings(BaseModel):
    """One source's settings. Unknown keys are the source's own options."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(False, description="Run this source.")
    min_delay: float | None = Field(None, ge=0, description="Seconds between requests (lower bound).")
    max_delay: float | None = Field(None, ge=0, description="Seconds between requests (upper bound).")


class CategorySpec(BaseModel):
    """What to search for: a category name and the phrases that find it."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=1, description="Shown in the output, e.g. 'Dentists'.")
    queries: list[str] = Field(default_factory=list,
                               description="Search phrases; defaults to the name.")
    directory_slugs: dict[str, str | None] = Field(
        default_factory=dict, description="Per-directory category slug hints.")
    sources: list[str] = Field(default_factory=list,
                               description="Only these sources run this category (empty = all).")
    only_areas: list[str] = Field(default_factory=list,
                                  description="Only these areas run this category (empty = all).")


class LegacyScoring(BaseModel):
    """Component weights of the original legitimacy / venue score (the fit part)."""

    model_config = ConfigDict(extra="allow")

    weights: dict[str, float] | None = None
    reviews_saturation: float | None = None
    sources_saturation: float | None = None
    wedmegood_featured_bonus: float | None = None
    completeness_fields: dict[str, float] | None = None


class DedupSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    phone_match_min_similarity: int | None = Field(
        None, ge=0, le=100, description="A shared phone also needs this name similarity.")
    geo_match_radius_m: float | None = Field(
        None, ge=0, description="Pins this close are merge candidates.")
    geo_match_min_similarity: int | None = Field(None, ge=0, le=100)
    cross_locality_names: bool | None = None
    extra_name_noise: list[str] = Field(default_factory=list,
                                        description="Words ignored when comparing names.")
    domain_match: bool | None = Field(None, description="Merge records sharing a website domain.")
    domain_match_min_similarity: int | None = Field(None, ge=0, le=100)


class BrandSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    aliases: list[str] = Field(default_factory=list)
    tier: Literal["premium", "budget", "neutral"] = "premium"
    website: str | None = None
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    instagram: str | None = None


class HospitalitySettings(BaseModel):
    """The hospitality vertical: operators, tier thresholds, platform search places."""

    model_config = ConfigDict(extra="allow")

    brands: list[BrandSpec] = Field(default_factory=list)
    venue_tiers: dict[str, Any] = Field(default_factory=dict)
    premium_platforms: list[str] | None = None
    hotel_chains: list[str] | None = None
    premium_hotel_chains: list[str] | None = None
    platform_searches: dict[str, str] = Field(default_factory=dict)


class LocalBusinessConfig(BaseModel):
    """Businesses by category and location."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    verticals: list[Literal["events", "hospitality"]] = Field(
        default_factory=list,
        description="Industry verticals; they switch on vertical packs and the venue layout.")
    categories: list[CategorySpec] = Field(default_factory=list)
    sources: dict[str, SourceSettings] = Field(
        default_factory=dict,
        description="Per-source settings. Leave out to use every applicable source "
                    "at polite default delays.")
    scoring: LegacyScoring | None = None
    dedup: DedupSettings | None = None
    hospitality: HospitalitySettings | None = None
    #: Top-level keys of a legacy config this schema has no home for, carried
    #: through unchanged so an import is lossless.
    legacy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _needs_a_category(self) -> "LocalBusinessConfig":
        if self.enabled and not self.categories:
            raise ValueError("local_business needs at least one category")
        return self

    @property
    def is_venue_run(self) -> bool:
        return "hospitality" in self.verticals


#: Settings a source gets when a config leaves `sources` out entirely.
#: Delays are the ones the original runs settled on; sources whose pack does
#: not apply to the run's country / verticals are skipped regardless.
DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
    "google_maps": {"enabled": True, "min_delay": 2.5, "max_delay": 6.0,
                    "max_results_per_query": 20, "scroll_rounds": 6},
    "google_search": {"enabled": True, "min_delay": 12.0, "max_delay": 28.0,
                      "results_per_query": 20, "pages": 1, "fallback_to_duckduckgo": True},
    "website": {"enabled": True, "min_delay": 1.5, "max_delay": 4.0, "max_pages_per_site": 4},
    "maps_lookup": {"enabled": False, "min_delay": 2.5, "max_delay": 6.0},
    "facebook": {"enabled": True, "min_delay": 6.0, "max_delay": 14.0},
    "linkedin_serp": {"enabled": True, "min_delay": 15.0, "max_delay": 30.0},
    "instagram": {"enabled": True, "min_delay": 8.0, "max_delay": 20.0,
                  "max_profiles_per_run": 150},
    "justdial": {"enabled": True, "min_delay": 10.0, "max_delay": 22.0, "max_pages": 1},
    "sulekha": {"enabled": True, "min_delay": 8.0, "max_delay": 18.0, "max_pages": 1},
    "indiamart": {"enabled": True, "min_delay": 5.0, "max_delay": 11.0, "max_pages": 2},
    "wedmegood": {"enabled": True, "min_delay": 4.0, "max_delay": 9.0, "max_pages": 3},
    "weddingwire": {"enabled": True, "min_delay": 4.0, "max_delay": 9.0, "max_pages": 3},
    "stayvista": {"enabled": True, "min_delay": 2.0, "max_delay": 5.0, "max_properties": 120},
    "saffronstays": {"enabled": True, "min_delay": 2.0, "max_delay": 5.0, "max_properties": 120},
    "elivaas": {"enabled": True, "min_delay": 2.0, "max_delay": 5.0, "max_properties": 80},
    "airbnb": {"enabled": True, "min_delay": 8.0, "max_delay": 16.0, "max_pages": 2},
    "booking": {"enabled": True, "min_delay": 8.0, "max_delay": 16.0, "max_pages": 1},
}
