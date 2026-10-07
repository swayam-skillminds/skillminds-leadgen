"""The run config: one versioned, validated document per run (A.8).

Shape::

    schema_version: 1
    name: "Pune manufacturers"
    project: "pune-manufacturing"
    geography: {country: IN, places: [Pune], areas: [], radius_km: null}
    lead_types:
      local_business: {enabled: true, categories: [{name: Manufacturers}]}
    fields: {include: [phones, emails, website, address, socials, ...]}
    filters: {min_rating: null, must_have: [], exclude_chains: false, suppression_lists: []}
    icp: {industries: [], company_size: null, technologies: []}
    scoring: {weights: {fit: 0.4, intent: 0.4, reach: 0.2}}
    depth: balanced
    time_budget_minutes: null
    ai: {enabled: false, provider: null, features: [], budget_usd: 2.0}
    output: {excel: true, csv: false, json: false, google_sheets: false, sort: auto}

``lead_types.<key>`` is validated against that lead type's own config model
(``leadtypes/registry.py``), so adding a lead type needs no change here.
``run_config_json_schema()`` is what the future UI renders forms from.

A country, one place and one category are enough for a good run: every
other section has a working default.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.country import is_known_region

SCHEMA_VERSION = 1

FieldName = Literal["phones", "emails", "website", "address", "socials", "decision_makers",
                    "ratings", "prices"]
ALL_FIELDS: list[str] = ["phones", "emails", "website", "address", "socials",
                         "decision_makers", "ratings", "prices"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Area(_Strict):
    """A sub-area of a place: a suburb, a district, a dam belt."""

    name: str = Field(..., min_length=1)
    aliases: list[str] = Field(default_factory=list,
                               description="Extra search phrasings, most specific first.")
    keywords: list[str] = Field(default_factory=list,
                                description="Place names that put a listing in this area.")
    center: tuple[float, float] | None = Field(None, description="(lat, lng); geocoded if absent.")


class Geography(_Strict):
    country: str = Field(..., description="ISO 3166-1 alpha-2: phones, currency, packs.")
    places: list[str] = Field(..., min_length=1, description="Cities/regions, free text.")
    areas: list[Area] = Field(default_factory=list,
                              description="Optional sub-areas; empty = each place as a whole.")
    radius_km: float | None = Field(None, gt=0, description="Listings farther out are skipped.")
    center: tuple[float, float] | None = Field(None, description="(lat, lng); geocoded if absent.")
    admin_area: str | None = Field(None, description="State/province; geocoded if absent.")
    timezone: str | None = None
    maps_zoom: int | None = Field(None, ge=1, le=21)
    keywords: list[str] = Field(default_factory=list,
                                description="Weak regional words (the city's own name).")
    exclude_keywords: list[str] = Field(default_factory=list,
                                        description="Unpinned listings naming these are out of region.")
    default_area: str | None = Field(None, description="Area for listings nothing else places.")
    place_noise: list[str] = Field(default_factory=list,
                                   description="Extra place words ignored in business names.")

    @field_validator("country")
    @classmethod
    def _iso_country(cls, value: str) -> str:
        code = (value or "").strip().upper()
        if not is_known_region(code):
            raise ValueError(f"{value!r} is not an ISO 3166-1 alpha-2 country code")
        return code


class Fields(_Strict):
    include: list[FieldName] = Field(default_factory=lambda: list(ALL_FIELDS))


class Filters(_Strict):
    """A view over the project: exports leave out what fails; nothing is deleted."""

    min_rating: float | None = Field(
        None, ge=0, le=5, description="Leave out organisations rated below this (best rating "
                                      "across sources, 5-point scale); unrated ones are kept.")
    must_have: list[Literal["phone", "email", "website"]] = Field(
        default_factory=list, description="Leave out organisations without each of these.")
    exclude_chains: bool = False
    suppression_lists: list[str] = Field(default_factory=list)


class ICP(_Strict):
    industries: list[str] = Field(default_factory=list)
    company_size: str | None = None
    technologies: list[str] = Field(default_factory=list)


class ScoreWeights(_Strict):
    fit: float = Field(0.4, ge=0)
    intent: float = Field(0.4, ge=0)
    reach: float = Field(0.2, ge=0)


class Scoring(_Strict):
    weights: ScoreWeights = Field(default_factory=ScoreWeights)


class AIProviderEntry(BaseModel):
    """One provider in the fallback chain; unset fields come from its kind's defaults."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=1, description="anthropic | openai | gemini | "
                                                      "openai_compatible | claude_code_cli, "
                                                      "or your own name with `kind`.")
    kind: str | None = Field(None, description="The API it speaks, when `name` is your own.")
    worker_model: str | None = Field(None, description="Cheap model for bulk calls.")
    judge_model: str | None = Field(None, description="Stronger model for hard calls.")
    base_url: str | None = None
    key_secret_name: str | None = Field(None, description="Secret holding the API key "
                                                          "(never the key itself).")
    structured_output: bool | None = None
    web_search: bool | None = None
    context_tokens: int | None = Field(None, gt=0)
    timeout: float | None = Field(None, gt=0)
    retries: int | None = Field(None, ge=0, le=5)
    enabled: bool = True

    @model_validator(mode="after")
    def _known_kind(self) -> "AIProviderEntry":
        from ..ai.providers.registry import KINDS, kind_of

        if kind_of(self.kind or self.name) is None:
            raise ValueError(f"unknown AI provider {self.kind or self.name!r} "
                             f"(known: {', '.join(KINDS)}); give `kind` for your own name")
        return self


class AISettings(BaseModel):
    """The optional AI layer (Phase 5). Off unless enabled; every feature opt-in."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    provider: str | None = Field(None, description="One provider (legacy): claude_cli | "
                                                   "anthropic_api | auto | a provider kind.")
    providers: list[str | AIProviderEntry] | None = Field(
        None, description="The fallback chain, first preferred. Default: the workspace's "
                          "providers, else the Claude Code subscription.")
    features: list[str] = Field(
        default_factory=list, description="icp_builder, planner, source_discovery, "
        "web_search_discovery, extraction_fallback, normalisation, venue_classification, "
        "signal_interpretation, lead_summary. Empty: extract, names, venues (legacy).")
    budget_usd: float | None = Field(2.0, ge=0, description="Hard cap per run, USD; null = no "
                                                            "cap.")
    allow_billed_api: bool | None = Field(
        None, description="false: never use a provider that bills separately (an API key), "
                          "even when named.")
    prices: dict[str, dict[str, Any]] | None = Field(
        None, description="Price overrides: {provider: {model: {input, output}}} in USD per "
                          "million tokens.")

    @field_validator("features")
    @classmethod
    def _known_features(cls, value: list[str]) -> list[str]:
        from ..ai.catalog import FEATURES, LEGACY, known

        unknown = [f for f in value if not known(f)]
        if unknown:
            raise ValueError(f"unknown AI feature(s) {unknown} (known: {', '.join(FEATURES)}; "
                             f"legacy: {', '.join(sorted(LEGACY))})")
        return value

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str | None) -> str | None:
        from ..ai.providers.registry import kind_of

        if value is not None and value != "auto" and kind_of(value) is None:
            raise ValueError(f"unknown AI provider {value!r}")
        return value

    @field_validator("providers")
    @classmethod
    def _known_providers(cls, value):
        from ..ai.providers.registry import kind_of

        for entry in value or []:
            if isinstance(entry, str) and kind_of(entry) is None:
                raise ValueError(f"unknown AI provider {entry!r}")
        return value


class SheetsSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    spreadsheet_id: str | None = None
    spreadsheet_name: str | None = None
    worksheet_name: str | None = None
    failures_worksheet_name: str | None = None
    share_with: str | None = None


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    excel: bool = Field(True, description="Write the workbook (Overview + Records) after "
                                           "the run.")
    csv: bool = Field(False, description="Write the legacy-layout CSV (and skipped.csv) as "
                                         "the run goes.")
    # "json" would shadow BaseModel.json(); the document key is still "json".
    json_: bool = Field(False, alias="json", description="Write the records as JSON after "
                                                         "the run.")
    google_sheets: bool = False
    sheets: SheetsSettings | None = None
    sort: Literal["auto", "score", "place", "area", "category", "legacy"] = Field(
        "auto", description="Order of the Records rows. auto: score, highest first, grouped "
                            "by place when the run covers several places. legacy: the "
                            "profile's own order (locality/category, or venue tier).")


class Runtime(BaseModel):
    """Process-level knobs (retries, timeouts, file locations)."""

    model_config = ConfigDict(extra="allow")


class RunConfig(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    name: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1, description="The lead database this run merges into.")
    description: str = ""
    geography: Geography
    lead_types: dict[str, Any] = Field(..., description="lead type key -> its config section.")
    fields: Fields = Field(default_factory=Fields)
    filters: Filters = Field(default_factory=Filters)
    icp: ICP = Field(default_factory=ICP)
    scoring: Scoring = Field(default_factory=Scoring)
    depth: Literal["quick", "balanced", "deep"] = "balanced"
    time_budget_minutes: int | None = Field(None, gt=0)
    ai: AISettings = Field(default_factory=AISettings)
    output: Output = Field(default_factory=Output)
    runtime: Runtime = Field(default_factory=Runtime)

    @field_validator("lead_types")
    @classmethod
    def _validate_lead_types(cls, value: dict[str, Any]) -> dict[str, Any]:
        from ..leadtypes import registry

        validated: dict[str, Any] = {}
        for key, section in (value or {}).items():
            model = registry.config_model(key)
            if model is None:
                known = ", ".join(lt.key for lt in registry.all_lead_types())
                raise ValueError(f"unknown lead type {key!r} (known: {known})")
            validated[key] = (section if isinstance(section, model)
                              else model.model_validate(section or {}))
        # The registry's order, not the document's: Postgres keeps a snapshot as
        # JSONB, which does not preserve key order, so a run resumed (or exported)
        # from it would otherwise see its lead types -- and their columns -- in
        # another order than on SQLite.
        rank = {lt.key: i for i, lt in enumerate(registry.all_lead_types())}
        return dict(sorted(validated.items(), key=lambda item: rank[item[0]]))

    @model_validator(mode="after")
    def _something_enabled(self) -> "RunConfig":
        if not any(getattr(section, "enabled", True) for section in self.lead_types.values()):
            raise ValueError("enable at least one lead type")
        return self

    def lead_type(self, key: str) -> Any | None:
        return self.lead_types.get(key)

    def enabled_lead_types(self) -> list[str]:
        return [key for key, section in self.lead_types.items()
                if getattr(section, "enabled", True)]

    def unapplied(self) -> list[str]:
        """Settings this build accepts but does not act on yet, one note each.

        The schema is the whole A.8 shape so a saved config never has to
        change; the runner logs these at run start and ``config validate``
        prints them, rather than letting a filter or a budget pass silently.
        """
        notes: list[str] = []
        if self.filters.exclude_chains:
            notes.append("filters.exclude_chains is not applied yet: chains are kept")
        if self.icp != ICP():
            notes.append("icp is recorded with the run but does not steer it yet")
        return notes

    def to_document(self) -> dict[str, Any]:
        """A plain dict, every set field kept, ready for YAML.

        Python mode, not JSON mode: YAML keeps integer keys (price bands)
        that JSON would turn into strings.
        """
        data = _plain(self.model_dump(mode="python", exclude_unset=True, exclude={"lead_types"}))
        out: dict[str, Any] = {"schema_version": self.schema_version}
        out.update({k: v for k, v in data.items() if k != "schema_version"})
        out["lead_types"] = {key: _plain(section.model_dump(mode="python", exclude_unset=True))
                             for key, section in self.lead_types.items()}
        # Keep the documented key order: identity, geography, lead types, rest.
        order = ["schema_version", "name", "project", "description", "geography", "lead_types"]
        return {**{k: out[k] for k in order if k in out},
                **{k: v for k, v in out.items() if k not in order}}


def _plain(value: Any) -> Any:
    """Tuples (coordinates) as lists, so YAML and JSON can both hold them."""
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def run_config_json_schema() -> dict[str, Any]:
    """JSON Schema for a run config, with every registered lead type's section."""
    from pydantic import create_model

    from ..leadtypes import registry

    sections = {lt.key: (lt.config_model | None, None) for lt in registry.all_lead_types()}
    lead_types_model = create_model(
        "LeadTypes", __config__=ConfigDict(extra="forbid"), **sections)  # type: ignore[call-overload]
    documented = create_model(
        "RunConfig", __base__=RunConfig,
        lead_types=(lead_types_model, Field(..., description="lead type key -> its config section.")),
    )
    schema = documented.model_json_schema()
    schema["$id"] = f"https://skillminds.in/schemas/leadscraper/run-config/v{SCHEMA_VERSION}.json"
    schema["title"] = "Run config"
    return schema
