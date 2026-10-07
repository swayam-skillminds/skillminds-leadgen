"""Request and response models of the HTTP API, each with an example.

The OpenAPI document is generated from these, so a UI (or its generated
client) gets field names, types, enums and an example for every endpoint.
``tests/test_api.py::test_openapi_covers_every_endpoint_with_examples``
fails if an endpoint is added without one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

NOW = "2026-10-07T09:30:00Z"
EXAMPLE_CONFIG = {
    "schema_version": 1, "name": "Pune dentists", "project": "Pune dentists",
    "geography": {"country": "IN", "places": ["Pune"], "areas": [{"name": "Baner"}]},
    "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}},
    "fields": {"include": ["phones", "emails", "website"]},
    "depth": "quick", "time_budget_minutes": 30,
}


def examples(*items: Any, **config: Any) -> ConfigDict:
    return ConfigDict(json_schema_extra={"examples": list(items)}, **config)


def _page_example(schema: dict[str, Any], model: type[BaseModel]) -> None:
    """A page's example: one item (the item model's own example) and the paging fields."""
    from typing import get_args

    item_type = (get_args(model.model_fields["items"].annotation) or (None,))[0]
    extra = (getattr(item_type, "model_config", {}) or {}).get("json_schema_extra") or {}
    items = (extra.get("examples") or [])[:1] if isinstance(extra, dict) else []
    schema["examples"] = [{"items": items, "total": 1, "limit": 50, "offset": 0}]


class Page(BaseModel, Generic[T]):
    """One page of a list. ``total`` counts every match, not just this page."""

    model_config = ConfigDict(json_schema_extra=_page_example)

    items: list[T]
    total: int
    limit: int
    offset: int


class Message(BaseModel):
    model_config = examples({"detail": "no run 42", "errors": []})

    detail: str
    errors: list[dict[str, Any]] = Field(default_factory=list)


# --- system -------------------------------------------------------------------------------------

class Check(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "warn", "fail"]
    detail: str


class Health(BaseModel):
    model_config = examples({
        "status": "degraded", "version": "0.3.0", "time": NOW, "mode": "local",
        "checks": {"database": {"status": "ok", "detail": "sqlite, schema 0003"},
                   "browser": {"status": "ok", "detail": "Playwright 1.63.0, chromium-1200"},
                   "disk": {"status": "ok", "detail": "120.4 GB free at /data"},
                   "keys": {"status": "ok", "detail": "none configured (all optional)"},
                   "worker": {"status": "warn", "detail": "no worker is running"}}})

    status: Literal["ok", "degraded", "down"]
    version: str
    time: datetime
    mode: str
    checks: dict[str, Check]


class RunConfigSchema(BaseModel):
    model_config = examples({
        "schema": {"title": "Run config", "type": "object"},
        "ui": {"geography.country": {"label": "Country", "help": "Drives phone parsing...",
                                     "widget": "text", "default": None, "enum": None,
                                     "required": True, "step": "where", "advanced": False}},
        "example": EXAMPLE_CONFIG,
        "applicability": {"country": "US", "verticals": [],
                          "lead_types": [{"key": "local_business", "applicable": True,
                                          "reason": None}],
                          "sources": [{"name": "justdial", "kind": "discovery", "pack": "in",
                                       "applicable": False,
                                       "reason": "pack 'in' covers IN; this run is in US"}]},
        "events": {"oneOf": []}}, populate_by_name=True, serialize_by_alias=True)

    json_schema: dict[str, Any] = Field(..., alias="schema")
    ui: dict[str, dict[str, Any]]
    example: dict[str, Any]
    applicability: dict[str, Any]
    events: dict[str, Any]


class LeadTypeInfo(BaseModel):
    model_config = examples({
        "key": "local_business", "title": "Local businesses",
        "description": "Businesses by category and location...", "applicable_countries": None,
        "applicable": True, "reason": None, "signals": [],
        "columns": [{"key": "phones", "header": "Phones", "kind": "phone", "field": "phones"}],
        "config_schema": {"title": "LocalBusinessConfig"}})

    key: str
    title: str
    description: str
    applicable_countries: list[str] | None
    applicable: bool
    reason: str | None
    signals: list[dict[str, Any]]
    columns: list[dict[str, Any]]
    config_schema: dict[str, Any]


class SourceInfo(BaseModel):
    model_config = examples({
        "name": "justdial", "kind": "discovery", "pack": "in", "lead_type": "local_business",
        "applicable": False, "reason": "pack 'in' covers IN; this run is in US",
        "applicable_countries": ["IN"], "applicable_verticals": None, "scope": "combo",
        "priority": 30, "uses_browser": True, "fills": None,
        "defaults": {"enabled": True, "min_delay": 10.0, "max_delay": 22.0, "max_pages": 1},
        "endpoint": "https://www.justdial.com/{City}/{query}-in-{locality}",
        "method": "Playwright", "terms": "Blocks automated clients...", "review": False,
        "last_verified": "2026-10-06 (blocked)",
        "health": {"status": "degraded", "runs": 3, "units": 12, "productive": 2, "empty": 4,
                   "failed": 0, "failures": {"blocked": 8}, "blocks": 8, "success_rate": 0.167,
                   "failure_rate": 0.0, "block_rate": 0.4}})

    name: str
    kind: Literal["discovery", "enrichment"]
    pack: str
    lead_type: str
    applicable: bool
    reason: str | None
    applicable_countries: list[str] | None
    applicable_verticals: list[str] | None
    scope: str
    priority: int
    uses_browser: bool
    fills: list[str] | None
    defaults: dict[str, Any]
    endpoint: str
    method: str
    terms: str
    review: bool
    last_verified: str | None
    health: dict[str, Any]


class RunOptions(BaseModel):
    """What to run beyond the config: a slice, a depth, a time budget."""

    model_config = examples({"depth": "quick", "time_budget_minutes": 15, "areas": ["Baner"],
                             "sources": ["google_maps"]}, extra="forbid")

    depth: Literal["quick", "balanced", "deep"] | None = None
    time_budget_minutes: float | None = Field(None, gt=0)
    areas: list[str] | None = Field(None, description="Only these areas (default: all).")
    categories: list[str] | None = None
    sources: list[str] | None = Field(None, description="Discovery sources; [] = none.")
    enrichers: list[str] | None = Field(None, description="Enrichers; [] = no enrichment.")
    lead_types: list[str] | None = None
    force_enrich: bool | None = None
    discovery_workers: int | None = Field(None, ge=1, le=16)


class EstimateRequest(BaseModel):
    model_config = examples({"config": EXAMPLE_CONFIG, "options": {"time_budget_minutes": 15}},
                            {"saved_config_id": 7}, extra="forbid")

    config: dict[str, Any] | None = None
    saved_config_id: int | None = None
    version: int | None = None
    options: RunOptions = Field(default_factory=RunOptions)


class Estimate(BaseModel):
    """The engine's estimate; ``docs/engine.md`` explains every field."""

    model_config = examples({
        "units_queued": 8, "per_lead_type": {"local_business": {"google_maps": 4,
                                                                "google_search": 4}},
        "p50_seconds": 912.4, "p90_seconds": 1388.0, "expected_new": 41.2,
        "finalise_seconds": 15.0, "basis": {"prior": 8},
        "budget": {"seconds": 900, "units_fit": 6, "units_skipped": 2, "p50_seconds": 885.0,
                   "p90_seconds": 990.0}, "ai": None}, extra="allow")

    p50_seconds: float | None = None
    p90_seconds: float | None = None


class PreviewRequest(BaseModel):
    model_config = examples({"config": EXAMPLE_CONFIG, "seconds": 120, "area": "Baner"},
                            extra="forbid")

    config: dict[str, Any] | None = None
    saved_config_id: int | None = None
    version: int | None = None
    seconds: float = Field(120, ge=10, le=900)
    area: str | None = None
    category: str | None = None


# --- projects -------------------------------------------------------------------------------------

class Project(BaseModel):
    model_config = examples({
        "id": 3, "name": "Pune dentists", "slug": "pune-dentists",
        "description": "Clinics for the dental-software pitch", "retention_days": 365,
        "organizations": 412, "runs": 5, "last_run_at": NOW, "created_at": NOW,
        "updated_at": NOW})

    id: int
    name: str
    slug: str
    description: str
    retention_days: int | None
    organizations: int
    runs: int
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


class ProjectCreate(BaseModel):
    model_config = examples({"name": "Pune dentists",
                             "description": "Clinics for the dental-software pitch",
                             "retention_days": 365}, extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    retention_days: int | None = Field(None, ge=1)


class ProjectUpdate(BaseModel):
    model_config = examples({"description": "Renamed after the campaign",
                             "retention_days": None}, extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    retention_days: int | None = Field(None, ge=1)


# --- saved configs ----------------------------------------------------------------------------------

CONFIG_SUMMARY = {
    "id": 7, "name": "Pune dentists", "version": 3, "version_id": 19, "latest_version": 3,
    "versions": 3, "project": "Pune dentists", "project_id": 3, "schema_version": 1,
    "description": "added Kothrud", "created_at": NOW, "updated_at": NOW}


class SavedConfigSummary(BaseModel):
    model_config = examples(CONFIG_SUMMARY)

    id: int
    name: str
    version: int
    version_id: int
    latest_version: int
    versions: int
    project: str | None
    project_id: int | None
    schema_version: int
    description: str
    created_at: datetime
    updated_at: datetime


class SavedConfig(SavedConfigSummary):
    model_config = examples({**CONFIG_SUMMARY, "document": EXAMPLE_CONFIG})

    document: dict[str, Any]


class ConfigVersion(BaseModel):
    model_config = examples({"version": 2, "version_id": 12, "description": "deeper",
                             "created_at": NOW, "project": "Pune dentists"})

    version: int
    version_id: int
    description: str
    created_at: datetime
    project: str | None


class ConfigCreate(BaseModel):
    model_config = examples({"document": EXAMPLE_CONFIG, "description": "first draft"},
                            extra="forbid")

    document: dict[str, Any]
    name: str | None = Field(None, description="Default: the document's name.")
    description: str = ""


class ConfigVersionCreate(BaseModel):
    model_config = examples({"document": EXAMPLE_CONFIG, "description": "added Kothrud"},
                            extra="forbid")

    document: dict[str, Any]
    description: str = ""


class ConfigRename(BaseModel):
    model_config = examples({"name": "Pune dentists (2027)"}, extra="forbid")

    name: str = Field(..., min_length=1)


class ConfigClone(BaseModel):
    model_config = examples({"name": "Nashik dentists", "version": 2}, extra="forbid")

    name: str = Field(..., min_length=1)
    version: int | None = None


class ConfigImport(BaseModel):
    model_config = examples({"text": "schema_version: 1\nname: Pune dentists\n...",
                             "filename": "pune.yaml"}, extra="forbid")

    text: str = Field(..., description="YAML or JSON; a legacy config.yaml is converted.")
    filename: str | None = None
    name: str | None = None


class ConfigValidation(BaseModel):
    model_config = examples({"valid": True, "document": EXAMPLE_CONFIG, "errors": [],
                             "unapplied": ["icp is recorded with the run but does not steer "
                                           "it yet"]})

    valid: bool
    document: dict[str, Any] | None
    errors: list[dict[str, Any]]
    unapplied: list[str]


# --- runs ---------------------------------------------------------------------------------------------

RUN_EXAMPLE = {
    "id": 42, "project_id": 3, "project": "Pune dentists", "saved_config_id": 19,
    "config_name": "Pune dentists", "kind": "run", "status": "completed", "error": "",
    "created_at": NOW, "started_at": NOW, "finished_at": NOW, "heartbeat_at": NOW,
    "worker": None, "attempts": 1, "active_seconds": 842.6, "new": 37, "updated": 12,
    "units_done": 8, "units_total": 8, "estimate_p50_seconds": 912.4,
    "estimate_p90_seconds": 1388.0}

RunStatus = Literal["queued", "running", "pausing", "paused", "stopping", "completed", "failed",
                    "cancelled"]


class RunSummary(BaseModel):
    model_config = examples(RUN_EXAMPLE)

    id: int
    project_id: int
    project: str | None
    saved_config_id: int | None
    config_name: str | None
    kind: str
    status: RunStatus
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    worker: str | None
    attempts: int
    active_seconds: float
    new: int
    updated: int
    units_done: int | None
    units_total: int | None
    estimate_p50_seconds: float | None
    estimate_p90_seconds: float | None


class Run(RunSummary):
    """Everything history keeps about a run."""

    model_config = examples({
        **RUN_EXAMPLE, "config": EXAMPLE_CONFIG,
        "request": {"localities": None, "depth": "quick", "time_budget_minutes": 15},
        "checkpoint": {"phase": "finalise"}, "units": {"done": 8},
        "lead_types": {"local_business": {"new": 37, "merged": 12, "units_done": 8}},
        "per_source": [{"lead_type": "local_business", "source": "google_maps", "units": 4,
                        "done": 4, "failed": 0, "skipped": 0, "found": 60, "new": 30,
                        "merged": 10, "seconds": 520.1, "expected_seconds": 586.0}],
        "failures": {"total": 3, "by_reason": {"blocked": 3},
                     "by_source": {"google_search": {"blocked": 3}}},
        "organizations": {"new": 37, "updated": 12, "unchanged": 5},
        "estimate": {"p50_seconds": 912.4, "p90_seconds": 1388.0},
        "actual": {"elapsed_seconds": 842.6, "new": 37, "merged": 12, "units_done": 8,
                   "units_skipped": 0, "skipped_units": [], "deadline_reached": False},
        "ai_usage": {"cost_usd": 0.0, "calls": 0, "rows": []},
        "files": [{"kind": "csv", "path": "/data/projects/pune-dentists/export.csv",
                   "rows": 412, "at": NOW}],
        "preview": None})

    config: dict[str, Any]
    request: dict[str, Any]
    checkpoint: dict[str, Any]
    units: dict[str, int]
    lead_types: dict[str, Any]
    per_source: list[dict[str, Any]]
    failures: dict[str, Any]
    organizations: dict[str, int]
    estimate: dict[str, Any]
    actual: dict[str, Any]
    ai_usage: dict[str, Any]
    files: list[dict[str, Any]]
    preview: dict[str, Any] | None


class RunCreate(BaseModel):
    model_config = examples({"config": EXAMPLE_CONFIG, "options": {"time_budget_minutes": 30}},
                            {"saved_config_id": 7, "version": 3, "options": {"depth": "deep"}},
                            extra="forbid")

    config: dict[str, Any] | None = Field(None, description="A run-config document, or ...")
    saved_config_id: int | None = Field(None, description="... a saved config (its latest "
                                                          "version unless `version` is given).")
    version: int | None = None
    project_id: int | None = Field(None, description="Run into this project instead of the "
                                                     "config's.")
    options: RunOptions = Field(default_factory=RunOptions)


class RerunRequest(BaseModel):
    model_config = examples({"options": {"depth": "deep"}}, extra="forbid")

    options: RunOptions = Field(default_factory=RunOptions)


class Failure(BaseModel):
    model_config = examples({
        "id": 9, "source": "justdial", "reason": "blocked",
        "url": "https://www.justdial.com/Pune/dentists-in-baner", "query": "dentists",
        "locality": "Baner", "category": "Dentists", "business": None,
        "detail": "HTTP 403", "created_at": NOW})

    id: int
    source: str
    reason: str
    url: str | None
    query: str | None
    locality: str | None
    category: str | None
    business: str | None
    detail: str
    created_at: datetime


class WorkUnit(BaseModel):
    model_config = examples({
        "key": "local_business|google_maps|Baner|Dentists", "lead_type": "local_business",
        "source": "google_maps", "area": "Baner", "category": "Dentists", "scope": "combo",
        "status": "done", "found": 18, "new": 12, "merged": 3, "seconds": 131.2,
        "expected_seconds": 146.5, "priority": 0.033, "skip_reason": "", "error": "",
        "started_at": NOW, "finished_at": NOW})

    key: str
    lead_type: str
    source: str
    area: str | None
    category: str | None
    scope: str
    status: str
    found: int
    new: int
    merged: int
    seconds: float | None
    expected_seconds: float | None
    priority: float | None
    skip_reason: str
    error: str
    started_at: datetime | None
    finished_at: datetime | None


class DiffItem(BaseModel):
    organization_id: int
    name: str
    locality: str | None
    category: str | None
    score: float | None
    change: Literal["new", "updated", "unchanged", "only_in_against"]


class RunDiff(BaseModel):
    model_config = examples({
        "run_id": 42, "against": 37,
        "counts": {"new": 12, "updated": 30, "unchanged": 55, "only_in_against": 4},
        "total": 101, "limit": 50, "offset": 0,
        "items": [{"organization_id": 901, "name": "Smile Dental", "locality": "Baner",
                   "category": "Dentists", "score": 78.5, "change": "new"}]})

    run_id: int
    against: int | None
    counts: dict[str, int]
    total: int
    limit: int
    offset: int
    items: list[DiffItem]


class Event(BaseModel):
    """One run event; ``type`` says which (schemas: ``GET /schema/run-config``, ``events``)."""

    model_config = examples({
        "type": "unit_finished", "seq": 5, "run_id": 42, "at": NOW,
        "unit": "local_business|google_maps|Baner|Dentists", "lead_type": "local_business",
        "source": "google_maps", "status": "done", "found": 18, "new": 12, "merged": 3,
        "seconds": 131.2, "error": ""}, extra="allow")

    type: str
    seq: int
    run_id: int | None
    at: datetime


class SheetsResult(BaseModel):
    model_config = examples({"url": "https://docs.google.com/spreadsheets/d/abc", "rows": 37,
                             "scope": "run"})

    url: str | None
    rows: int
    scope: str


# --- organisations ------------------------------------------------------------------------------------

ORG_EXAMPLE = {
    "id": 901, "project_id": 3, "name": "Smile Dental", "locality": "Baner",
    "category": "Dentists", "country": "IN", "domain": "smiledental.in",
    "lead_types": ["local_business"], "score": 78.5, "fit": 0.74, "intent": None,
    "reach": 0.8, "score_reasons": ["Google rating 4.6 from 210 reviews"],
    "phones": ["+919820012345"], "emails": ["hello@smiledental.in"],
    "website": "https://smiledental.in", "gmaps_url": "https://www.google.com/maps/place/...",
    "sources": ["google_maps", "website"], "latitude": 18.559, "longitude": 73.786,
    "created_at": NOW, "updated_at": NOW}


class OrganizationSummary(BaseModel):
    model_config = examples(ORG_EXAMPLE)

    id: int
    project_id: int
    name: str
    locality: str | None
    category: str | None
    country: str | None
    domain: str | None
    lead_types: list[str]
    score: float
    fit: float | None
    intent: float | None
    reach: float | None
    score_reasons: list[str]
    phones: list[str]
    emails: list[str]
    website: str | None
    gmaps_url: str | None
    sources: list[str]
    latitude: float | None
    longitude: float | None
    created_at: datetime
    updated_at: datetime | None


class Organization(OrganizationSummary):
    model_config = examples({
        **ORG_EXAMPLE, "record": {"business_name": "Smile Dental", "phones": ["+919820012345"]},
        "contacts": [{"channel": "phone", "value": "+919820012345", "kind": "published",
                      "source": "google_maps", "confidence": 1.0, "first_seen": NOW}],
        "people": [{"name": "Dr. Rao", "title": "", "profile_url": None,
                    "source": "google_maps", "first_seen": NOW}],
        "signals": [],
        "evidence": [{"source": "website", "url": "https://smiledental.in/contact",
                      "fields": ["emails"], "lead_type": "local_business", "run_id": 42,
                      "fetched_at": NOW}],
        "identity_keys": [{"kind": "phone", "value": "+919820012345"}],
        "runs": [{"run_id": 42, "action": "new", "lead_type": "local_business", "at": NOW}]})

    record: dict[str, Any]
    contacts: list[dict[str, Any]]
    people: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    identity_keys: list[dict[str, Any]]
    runs: list[dict[str, Any]]


class EraseRequest(BaseModel):
    model_config = examples({"domain": "smiledental.in", "dry_run": True},
                            {"phone": "+91 98200 12345", "suppress": True}, extra="forbid")

    domain: str | None = None
    phone: str | None = Field(None, description="With the country code, or give `region`.")
    region: str | None = Field(None, description="ISO country for a phone without +code.")
    project_id: int | None = Field(None, description="Only in this project (default: all).")
    dry_run: bool = False
    suppress: bool = Field(True, description="Also put the value on the 'Erasure requests' "
                                             "list so it never reappears in an export.")


class EraseResult(BaseModel):
    model_config = examples({"erased": [{"id": 901, "project_id": 3, "name": "Smile Dental"}],
                             "count": 1, "dry_run": False,
                             "suppression_list": "Erasure requests"})

    erased: list[dict[str, Any]]
    count: int
    dry_run: bool
    suppression_list: str | None
    failures_removed: int = 0


# --- suppression -----------------------------------------------------------------------------------

class SuppressionEntryIn(BaseModel):
    kind: Literal["name", "domain", "phone"] | None = Field(
        None, description="Guessed from the value when left out.")
    value: str = Field(..., min_length=1)


class SuppressionListOut(BaseModel):
    model_config = examples({"id": 2, "name": "Existing customers", "mode": "exclude",
                             "entries": 140, "created_at": NOW})

    id: int
    name: str
    mode: Literal["exclude", "flag"]
    entries: int
    created_at: datetime


class SuppressionListCreate(BaseModel):
    model_config = examples({"name": "Existing customers", "mode": "exclude",
                             "entries": [{"kind": "domain", "value": "smiledental.in"},
                                         {"value": "+919820012345"}]}, extra="forbid")

    name: str = Field(..., min_length=1)
    mode: Literal["exclude", "flag"] = "exclude"
    entries: list[SuppressionEntryIn] = Field(default_factory=list)


class SuppressionListUpdate(BaseModel):
    model_config = examples({"mode": "flag"}, extra="forbid")

    name: str | None = None
    mode: Literal["exclude", "flag"] | None = None


class SuppressionEntryOut(BaseModel):
    model_config = examples({"id": 5, "kind": "domain", "value": "smiledental.in",
                             "created_at": NOW})

    id: int
    kind: str
    value: str
    created_at: datetime


class EntriesAdd(BaseModel):
    model_config = examples({"entries": [{"kind": "name", "value": "Smile Dental"}]},
                            extra="forbid")

    entries: list[SuppressionEntryIn]


class EntriesResult(BaseModel):
    model_config = examples({"added": 138, "skipped": 2, "read": 140})

    added: int
    skipped: int
    read: int | None = None


# --- schedules -----------------------------------------------------------------------------------

class ScheduleOut(BaseModel):
    model_config = examples({
        "id": 1, "name": "Monthly Pune dentists", "saved_config_id": 7,
        "config_name": "Pune dentists", "cron": "0 6 1 * *", "timezone": "Asia/Kolkata",
        "enabled": True, "only_new": True, "overrides": {"time_budget_minutes": 120},
        "next_run_at": NOW, "last_run_at": None, "last_run_id": None, "created_at": NOW})

    id: int
    name: str
    saved_config_id: int
    config_name: str | None
    cron: str
    timezone: str
    enabled: bool
    only_new: bool
    overrides: dict[str, Any]
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: int | None
    created_at: datetime


class ScheduleCreate(BaseModel):
    model_config = examples({"saved_config_id": 7, "cron": "0 6 1 * *",
                             "timezone": "Asia/Kolkata", "only_new": True,
                             "name": "Monthly Pune dentists",
                             "overrides": {"time_budget_minutes": 120}}, extra="forbid")

    saved_config_id: int
    cron: str = Field(..., description="5-field cron, or @daily / @weekly / @monthly.")
    timezone: str = "UTC"
    name: str = ""
    only_new: bool = Field(False, description="Exports of its runs default to what is new "
                                              "since the schedule's previous run.")
    enabled: bool = True
    overrides: dict[str, Any] = Field(default_factory=dict,
                                      description="depth and/or time_budget_minutes.")


class ScheduleUpdate(BaseModel):
    model_config = examples({"enabled": False}, {"cron": "0 6 * * mon"}, extra="forbid")

    cron: str | None = None
    timezone: str | None = None
    name: str | None = None
    only_new: bool | None = None
    enabled: bool | None = None
    overrides: dict[str, Any] | None = None


class QueuedRun(BaseModel):
    model_config = examples({"run_id": 57})

    run_id: int


# --- notifications -----------------------------------------------------------------------------------

class NotificationOut(BaseModel):
    model_config = examples({
        "id": 1, "name": "Sales channel", "kind": "webhook",
        "config": {"url": "https://hooks.slack.com/***"},
        "events": ["run_completed", "run_failed", "needs_attention"], "enabled": True,
        "created_at": NOW, "last_sent_at": NOW, "last_error": "", "secret_configured": None})

    id: int
    name: str
    kind: Literal["desktop", "webhook", "email"]
    config: dict[str, Any]
    events: list[str]
    enabled: bool
    created_at: datetime
    last_sent_at: datetime | None
    last_error: str
    secret_configured: bool | None


NotificationEvent = Literal["run_completed", "run_failed", "needs_attention"]


class NotificationCreate(BaseModel):
    model_config = examples(
        {"name": "Sales channel", "kind": "webhook",
         "config": {"url": "https://hooks.slack.com/services/T000/B000/XXXX"},
         "events": ["run_completed", "needs_attention"]},
        {"name": "Ops mail", "kind": "email",
         "config": {"host": "smtp.example.com", "port": 587, "username": "leads",
                    "password_secret_name": "notify-smtp-password", "from": "leads@example.com",
                    "to": ["ops@example.com"]}}, extra="forbid")

    name: str = Field(..., min_length=1)
    kind: Literal["desktop", "webhook", "email"]
    config: dict[str, Any] = Field(default_factory=dict)
    events: list[NotificationEvent] | None = None
    enabled: bool = True


class NotificationUpdate(BaseModel):
    model_config = examples({"enabled": False}, extra="forbid")

    name: str | None = None
    config: dict[str, Any] | None = None
    events: list[NotificationEvent] | None = None
    enabled: bool | None = None


class TestResult(BaseModel):
    model_config = examples({"ok": False, "error": "RuntimeError: webhook answered HTTP 404"})

    ok: bool
    error: str


# --- settings, bundles ---------------------------------------------------------------------------

class SettingsOut(BaseModel):
    model_config = examples({
        "retention": {"logs_days": 30, "events_days": 90, "seen_urls_days": 90},
        "keep_awake": True, "robots_txt": True, "data_dir": "/data",
        "database": "postgresql+psycopg://leads:***@db:5432/leads", "mode": "server"})

    retention: dict[str, Any]
    keep_awake: bool
    robots_txt: bool
    data_dir: str
    database: str
    mode: str


class SettingsUpdate(BaseModel):
    model_config = examples({"retention": {"logs_days": 14}, "robots_txt": True},
                            extra="forbid")

    retention: dict[str, int | None] | None = None
    keep_awake: bool | None = None
    robots_txt: bool | None = None


class PurgeResult(BaseModel):
    model_config = examples({"dry_run": True, "events": 1200, "seen_urls": 300,
                             "log_files": 4, "organizations": {"Pune dentists": 12}})

    dry_run: bool
    events: int
    seen_urls: int
    log_files: int
    organizations: dict[str, int]


class BundleImportResult(BaseModel):
    model_config = examples({
        "manifest": {"format": "leadscraper-bundle", "format_version": 1,
                     "app_version": "0.3.0", "schema_revision": "0003",
                     "created_on": {"os": "Windows"}},
        "saved_configs": {"added": 3, "skipped": 0, "invalid": []},
        "presets": {"added": 0, "skipped": 2}, "suppression_lists": {"lists": 1, "entries": 140},
        "projects": {"created": 1, "existing": 0}, "schedules": {"added": 1, "skipped": 0},
        "organizations": {"pune-dentists": {"mode": "verbatim", "imported": 412}}, "runs": 5})

    manifest: dict[str, Any]
    saved_configs: dict[str, Any]
    presets: dict[str, int]
    suppression_lists: dict[str, int]
    projects: dict[str, int]
    schedules: dict[str, int]
    organizations: dict[str, Any]
    runs: int


class Preset(BaseModel):
    model_config = examples({"file": "nashik-venues.yaml", "source": "shipped",
                             "name": "Nashik venues", "project": "Nashik venues",
                             "country": "IN", "places": ["Nashik"],
                             "document": {"schema_version": 1, "name": "Nashik venues"}})

    file: str
    source: Literal["shipped", "user"]
    name: str | None
    project: str | None
    country: str | None
    places: list[str]
    document: dict[str, Any]
