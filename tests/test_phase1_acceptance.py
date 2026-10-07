"""Phase 1 acceptance (APP-BUILD-PROMPT): a non-Indian run, lead-type
isolation, and plan() agreeing with what the pipeline executes. All offline."""

from __future__ import annotations

from typing import Iterator

import pytest
from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy import select

from leadscraper.config import Config
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import ALL_FIELDS, RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Contact, Evidence, FailureRow, Organization, Run, RunOrganization
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes import registry
from leadscraper.leadtypes.base import LeadType, LeadTypeResult
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.leadtypes.local_business.planning import select_sources
from leadscraper.leadtypes.local_business.plugin import LOCAL_BUSINESS
from leadscraper.sources import registry as source_registry
from leadscraper.sources.base import Source
from leadscraper.sources.google_search import SERP_HEALTH

INDIA_AND_VERTICAL_PACK_SOURCES = {"justdial", "sulekha", "indiamart", "wedmegood", "weddingwire",
                                   "stayvista", "saffronstays", "elivaas", "airbnb", "booking"}
CORE_AND_SOCIAL = {"google_maps", "google_search", "facebook", "linkedin_serp"}


def austin(**lead_type_overrides) -> dict:
    local_business = {"categories": [{"name": "Dentists"}]}
    local_business.update(lead_type_overrides)
    return {
        "name": "Austin dentists", "project": "austin-dentists",
        "geography": {"country": "US", "places": ["Austin"], "center": [30.2672, -97.7431],
                      "admin_area": "Texas"},
        "lead_types": {"local_business": local_business},
        "ai": {"enabled": False},
    }


def compiled(document: dict) -> Config:
    return compile_run_config(RunConfig.model_validate(document))


# --- "dentists in Austin, US" --------------------------------------------------------

def test_us_run_activates_only_core_and_social_sources():
    config = compiled(austin())
    chosen = LOCAL_BUSINESS.sources(config)
    assert set(chosen["discovery"]) == CORE_AND_SOCIAL
    assert set(chosen["enrichment"]) == {"website", "instagram"}
    planned = {unit.source for unit in LOCAL_BUSINESS.plan(config)}
    assert planned == CORE_AND_SOCIAL
    assert not planned & INDIA_AND_VERTICAL_PACK_SOURCES



def test_unchecked_fields_switch_off_the_sources_that_only_fill_them():
    # A.8: a run that wants no decision-makers skips the LinkedIn search; the
    # enrichers that also fill phones keep running.
    phones_only = compiled({**austin(), "fields": {"include": ["phones"]}})
    chosen = LOCAL_BUSINESS.sources(phones_only)
    assert set(chosen["discovery"]) == CORE_AND_SOCIAL - {"linkedin_serp"}
    assert set(chosen["enrichment"]) == {"website", "instagram"}
    assert "linkedin_serp" not in {unit.source for unit in LOCAL_BUSINESS.plan(phones_only)}

    # Only decision-makers: every enricher is idle, businesses are still found.
    people_only = compiled({**austin(), "fields": {"include": ["decision_makers"]}})
    chosen = LOCAL_BUSINESS.sources(people_only)
    assert set(chosen["discovery"]) == CORE_AND_SOCIAL
    assert chosen["enrichment"] == []
    _, plan = select_sources("enricher", source_registry.ENRICHMENT_SOURCES, None, people_only)
    assert plan.skipped["website"].startswith("not needed -- it only fills address, emails,")
    assert plan.skipped["website"].endswith("fields.include asks for decision_makers")


def test_a_run_config_without_fields_keeps_every_source():
    config = compiled(austin())
    assert config.included_fields == set(ALL_FIELDS)
    legacy = Config.load("presets/mumbai-event-vendors.yaml")
    assert legacy.included_fields == set(ALL_FIELDS)


GOOGLE_SERP = """
<html><body><div id="search">
  <div class="g"><div>
    <a href="https://www.brightsmiledental.test/"><h3>Bright Smile Dental | Family Dentist</h3></a>
    <div>Bright Smile Dental in Austin, Texas. Call (201) 555-0123 to book a cleaning or
    a same-day emergency appointment with our family dentists.</div>
  </div></div>
</div></body></html>
"""

DENTIST_SITE = """
<html><head><title>Bright Smile Dental</title></head><body>
  <h1>Bright Smile Dental</h1>
  <p>Call us on <a href="tel:+12015550123">(201) 555-0123</a> or write to
     <a href="mailto:frontdesk@brightsmiledental.test">frontdesk@brightsmiledental.test</a>.</p>
  <p>Visit us: Bright Smile Dental, 1200 Congress Avenue, Austin, Texas 78701</p>
</body></html>
"""


class _Response:
    def __init__(self, text: str, url: str):
        self.text, self.url, self.status_code = text, url, 200
        self.content = text.encode()


class FixtureHttp:
    """Stands in for HttpClient: Google SERP and the dentist's site, nothing else."""

    calls: list[str] = []

    def __init__(self, limiter, **_kwargs):
        self.limiter = limiter

    def get(self, url, params=None, rate_key=None, headers=None, **_kwargs):
        FixtureHttp.calls.append(url)
        if "google.com/search" in url:
            return _Response(GOOGLE_SERP, url)
        if "brightsmiledental.test" in url:
            return _Response(DENTIST_SITE, url)
        raise ConnectionError(f"offline test: no fixture for {url}")

    @staticmethod
    def soup(markup):
        return BeautifulSoup(markup, "lxml")

    def close(self):
        pass


class NoBrowser:
    def __init__(self, **_kwargs):
        pass

    def page(self):
        raise RuntimeError("no browser in the offline test")

    def close(self):
        pass


def no_delays() -> dict:
    return {name: {"enabled": True, "min_delay": 0, "max_delay": 0}
            for name in ("google_maps", "google_search", "website", "facebook", "linkedin_serp",
                         "instagram", "justdial", "sulekha", "indiamart", "wedmegood",
                         "weddingwire")}


def org_rows(config: Config, model):
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    table = model.__table__
    try:
        query = select(table)
        if "project_id" in table.c:
            query = query.where(table.c.project_id == store.project_id)
        return [dict(r) for r in store._conn.execute(query).mappings().all()]
    finally:
        store.close()


def test_us_run_end_to_end_parses_us_phones_without_india_pack_failures(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "HttpClient", FixtureHttp)
    monkeypatch.setattr(pipeline_mod, "BrowserSession", NoBrowser)
    SERP_HEALTH.reset()
    FixtureHttp.calls = []
    # Every source switched on, India's included: the packs must still say no.
    config = compiled(austin(sources=no_delays()))

    outcome = execute(RunRequest(config=config, install_signals=False))

    assert outcome.ok, outcome.results
    pipeline = outcome.contexts["local_business"].pipeline
    assert set(pipeline.source_plan.built) == CORE_AND_SOCIAL
    for name in INDIA_AND_VERTICAL_PACK_SOURCES & set(no_delays()):    # enabled, yet skipped
        assert pipeline.source_plan.skipped[name].startswith("not applicable"), name

    [dentist] = [lead for lead in pipeline.leads if lead.business_name == "Bright Smile Dental"]
    assert dentist.phones == ["+12015550123"]                 # parsed as a US number
    assert dentist.emails == ["frontdesk@brightsmiledental.test"]
    assert dentist.address.endswith("Austin, Texas 78701")    # ZIP, not a 6-digit PIN
    assert "+1 201-555-0123" in dentist.to_row()["Phones"]

    failures = org_rows(config, FailureRow)
    assert failures, "Maps (no browser here) should have recorded its failure"
    assert {f["source"] for f in failures} <= CORE_AND_SOCIAL | {"website", "instagram"}
    assert not {f["source"] for f in failures} & INDIA_AND_VERTICAL_PACK_SOURCES
    assert not any("justdial" in url or "sulekha" in url or "indiamart" in url
                   for url in FixtureHttp.calls)

    [run] = org_rows(config, Run)
    assert run["status"] == "completed" and run["config_snapshot"]["project"] == "austin-dentists"
    orgs = org_rows(config, Organization)
    assert {o["country"] for o in orgs} == {"US"}
    phones = [c for c in org_rows(config, Contact) if c["channel"] == "phone"]
    assert {c["value"] for c in phones} == {"+12015550123"}
    membership = [m for m in org_rows(config, RunOrganization) if m["run_id"] == run["id"]]
    assert {m["action"] for m in membership} == {"new"}


# --- lead-type isolation ------------------------------------------------------------------

class StubSource(Source):
    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Royal Caterers", locality=locality.name,
                             category=category.name, phones=["+919820012345"],
                             gmaps_rating=4.4, gmaps_reviews=120)
        yield self.make_lead(business_name="Sharma Decorators", locality=locality.name,
                             category=category.name, emails=["hi@sharma.in"])


class EchoConfig(BaseModel):
    enabled: bool = True
    names: list[str] = ["Echo Analytics", "Echo Ventures"]
    crash: bool = False


class EchoLeadType(LeadType):
    """A minimal second lead type: it finds two organisations of its own."""

    key = "echo"
    title = "Echo (test)"
    config_model = EchoConfig

    def sources(self, cfg, geo=None):
        return {"discovery": ["echo_source"], "enrichment": []}

    def plan(self, cfg, geo=None, depth="balanced"):
        return []

    def signal_specs(self):
        return []

    def columns(self, cfg):
        return []

    def score(self, org, signals, cfg):
        from leadscraper.core.scoring import ScoreParts

        return ScoreParts(lead_type=self.key, intent=0.5, reasons=["echo"])

    def run(self, context) -> LeadTypeResult:
        section = context.config.run_config.lead_type(self.key)
        result = LeadTypeResult(lead_type=self.key)
        try:
            if section.crash:
                raise RuntimeError("echo exploded")
            for i, name in enumerate(section.names):
                _, action, _ = context.store.upsert(Lead(
                    business_name=name, locality="Pune", phones=[f"+91982001{i:04d}"],
                    sources=["echo_source"]))
                result.new += action == "new"
        except Exception as exc:
            result.failed, result.error = True, repr(exc)
        return result


@pytest.fixture
def echo_registered():
    registry.register(EchoLeadType())
    yield
    registry.unregister("echo")


def local_business_output(config: Config) -> tuple[list, list]:
    """local_business's own output: its rows (layout + values) and score parts."""
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        orgs = [o for o in store._conn.execute(select(Organization.__table__)).mappings()
                if "local_business" in o["lead_types"]]
        rows, parts = [], []
        for org in sorted(orgs, key=lambda o: o["name"]):
            row = Lead.from_dict(org["record"]).to_row()
            for column in ("First Seen", "Last Updated"):
                row.pop(column)
            rows.append(row)
            parts.append((org["name"], org["score"], org["fit"], org["reach"],
                          org["score_reasons"]))
        evidence_sources = {e["source"] for e in store._conn.execute(
            select(Evidence.__table__)).mappings()
            if e["organization_id"] in {o["id"] for o in orgs}}
        return rows, parts + [sorted(evidence_sources)]
    finally:
        store.close()


def isolation_config(with_echo: dict | None, db_url: str | None = None) -> Config:
    document = {
        "name": "Isolation", "project": "isolation",
        "geography": {"country": "IN", "places": ["Mumbai"],
                      "areas": [{"name": "Andheri"}, {"name": "Dadar"}]},
        "lead_types": {"local_business": {
            "categories": [{"name": "Caterers", "queries": ["caterers"]}],
            "sources": {"stub": {"enabled": True, "min_delay": 0, "max_delay": 0}}}},
        "scoring": {"weights": {"fit": 0.4, "intent": 0.4, "reach": 0.2}},
    }
    if db_url:
        document["runtime"] = {"db_url": db_url}
    if with_echo is not None:
        document["lead_types"]["echo"] = with_echo
    return compiled(document)


def run_isolated(tmp_path, monkeypatch, name: str, with_echo: dict | None):
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": StubSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    (tmp_path / name).mkdir(exist_ok=True)
    config = isolation_config(with_echo, db_url=f"sqlite:///{tmp_path / name / 'app.sqlite3'}")
    outcome = execute(RunRequest(config=config, install_signals=False))
    return config, outcome


def test_enabling_or_disabling_a_lead_type_leaves_local_business_unchanged(
        tmp_path, monkeypatch, echo_registered):
    alone_cfg, alone = run_isolated(tmp_path, monkeypatch, "alone", None)
    with_cfg, together = run_isolated(tmp_path, monkeypatch, "together", {"enabled": True})
    off_cfg, disabled = run_isolated(tmp_path, monkeypatch, "disabled", {"enabled": False})

    assert set(together.results) == {"local_business", "echo"}
    assert together.results["echo"].new == 2
    assert set(disabled.results) == {"local_business"}

    baseline = local_business_output(alone_cfg)
    assert baseline[0], "local_business found nothing; the comparison would be empty"
    assert local_business_output(with_cfg) == baseline
    assert local_business_output(off_cfg) == baseline

    # The other lead type's organisations carry only its own tag.
    target, kwargs = store_target(with_cfg)
    store = LeadStore(target, **kwargs)
    tags = {o["name"]: o["lead_types"] for o in store._conn.execute(
        select(Organization.__table__)).mappings()}
    store.close()
    assert tags["Echo Analytics"] == ["echo"]
    assert tags["Royal Caterers"] == ["local_business"]


def test_a_crashing_lead_type_is_contained(tmp_path, monkeypatch, echo_registered):
    alone_cfg, _ = run_isolated(tmp_path, monkeypatch, "alone", None)
    crash_cfg, outcome = run_isolated(tmp_path, monkeypatch, "crash", {"crash": True})
    assert outcome.results["echo"].failed
    assert "echo exploded" in outcome.results["echo"].error
    assert outcome.status == "completed"          # local_business still delivered
    assert local_business_output(crash_cfg) == local_business_output(alone_cfg)


# --- plan() is what the pipeline runs -----------------------------------------------------

def make_recording_source(name: str, scope: str, calls: list):
    def discover(self, locality, category):
        calls.append((name, locality.name if locality else None,
                      category.name if category else None))
        return iter(())
    return type(f"Rec_{name}", (Source,), {"name": name, "scope": scope, "discover": discover})


def test_plan_lists_the_units_the_pipeline_executes_in_order(tmp_path, monkeypatch):
    calls: list = []
    registry_ = {
        "sitemap": make_recording_source("sitemap", "run", calls),
        "area_search": make_recording_source("area_search", "locality", calls),
        "maps": make_recording_source("maps", "combo", calls),
        "citywide": make_recording_source("citywide", "combo", calls),
    }
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", registry_)
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    monkeypatch.setattr(source_registry, "DISCOVERY_SOURCES", registry_)
    sources = {name: {"enabled": True, "min_delay": 0, "max_delay": 0} for name in registry_}
    sources["citywide"]["city_level"] = True
    config = compiled({
        "name": "Plan", "project": "plan",
        "geography": {"country": "IN", "places": ["Pune"],
                      "areas": [{"name": "Baner"}, {"name": "Kothrud"}]},
        "lead_types": {"local_business": {
            "categories": [{"name": "Caterers"},
                           {"name": "Venues", "only_areas": ["Kothrud"]},
                           {"name": "Florists", "sources": ["maps"]}],
            "sources": sources}},
    })
    planned = [(u.source, u.area, u.category) for u in LOCAL_BUSINESS.plan(config)]
    # One worker: the engine runs units in plan order (the original order).
    execute(RunRequest(config=config, install_signals=False, discovery_workers=1))
    assert calls == planned
    # Parallel across hosts (Phase 2): the same units, each exactly once.
    calls.clear()
    execute(RunRequest(config=config, install_signals=False, discovery_workers=3))
    assert sorted(calls, key=repr) == sorted(planned, key=repr)
    assert calls[0] == ("sitemap", None, None)          # whole-region units still go first
    assert planned[0] == ("sitemap", None, None)
    # City-level: once per category, at the first locality it runs in.
    assert [p for p in planned if p[0] == "citywide"] == [
        ("citywide", "Baner", "Caterers"), ("citywide", "Kothrud", "Venues")]
    assert ("citywide", "Baner", "Florists") not in planned


def test_legacy_presets_plan_their_whole_run():
    mumbai = Config.load(__import__("leadscraper.paths", fromlist=["x"]).PROJECT_ROOT
                         / "presets" / "mumbai-event-vendors.yaml")
    units = LOCAL_BUSINESS.plan(mumbai)
    # 9 localities x 8 categories, every enabled India + events + social source.
    assert {u.source for u in units} == {"google_maps", "google_search", "wedmegood",
                                         "weddingwire", "indiamart", "justdial", "sulekha",
                                         "facebook", "linkedin_serp"}
    assert len([u for u in units if u.source == "google_maps"]) == 72
