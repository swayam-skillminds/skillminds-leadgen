"""AI in whole runs (Phase 5 acceptance), through the engine with mocked HTTP:

* a run with AI on but a failing provider completes identically to AI off;
* the budget cap stops AI mid-run and the run continues;
* the run's AI usage lands in the ledger per provider and feature, its spend is
  an event, and AI attributes reach the workbook in labelled columns.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.engine.clock import FakeClock
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.sources.base import Enricher, Source

from ai_fakes import FakeHTTP, chat_response, install_http, local_provider, llm

CONTACT_PAGE = ("Saffron Leaf Caterers -- events team: +91 98200 00417, "
                "events [at] saffronleaf [dot] example. Kitchen: Shop 4, Baner Road, Baner, Pune.")


class TitleSource(Source):
    """Search-result-style names: what AI name polish (or the rules) clean up."""

    name = "stub"

    def discover(self, locality, category):
        for i, title in enumerate(["Royal Caterers | Best Caterers in Mumbai",
                                   "Saffron Leaf Caterers - Wedding Catering in Andheri",
                                   "Spice Route Kitchen", "Annapurna Foods | Top 10 Caterers",
                                   "Golden Spoon Events", "Tiffin Tales"]):
            yield self.make_lead(business_name=title, category=category.name,
                                 locality=locality.name, website=f"https://site{i}.example",
                                 phones=[f"+9198200001{i:02d}"], sources=["google_search"])


class SilentSite(Enricher):
    """Fetched each site; the regexes found nothing; the page goes to AI extraction."""

    name = "stub_enricher"
    fills = None

    def targets(self, store):
        return store.all_with_ids()

    def enrich(self, lead):
        self.pending_text = self.pending_source = CONTACT_PAGE
        return None


@pytest.fixture
def sources(monkeypatch):
    from leadscraper.sources import registry as source_registry

    registry_ = {"stub": TitleSource}
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", registry_)
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"stub_enricher": SilentSite})
    monkeypatch.setattr(source_registry, "DISCOVERY_SOURCES", registry_)


def run_config(project: str, tmp_path, ai: dict | None = None):
    document = {
        "name": project, "project": project,
        "geography": {"country": "IN", "places": ["Mumbai"], "center": [19.07, 72.87],
                      "admin_area": "Maharashtra", "areas": [{"name": "Andheri"}]},
        "lead_types": {"local_business": {
            "categories": [{"name": "Caterers"}],
            "sources": {"stub": {"enabled": True, "min_delay": 0, "max_delay": 0},
                        "stub_enricher": {"enabled": True, "min_delay": 0, "max_delay": 0}}}},
        "output": {"excel": True},
        "runtime": {"ai_cache": str(tmp_path / f"{project}-ai.sqlite3")},
    }
    if ai is not None:
        document["ai"] = ai
    return compile_run_config(RunConfig.model_validate(document))


def run(config, events: list | None = None):
    return execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                              discovery_workers=1,
                              sinks=[events.append] if events is not None else []))


def snapshot(outcome) -> list[dict]:
    """The run's records, minus the times that differ between any two runs."""
    leads = outcome.contexts["local_business"].pipeline.leads
    out = []
    for lead in leads:
        row = dataclasses.asdict(lead)
        row.pop("first_seen"), row.pop("last_updated")
        out.append(row)
    return out


def failure_reasons(engine, run_id: int) -> list[str]:
    from sqlalchemy import select

    from leadscraper.db.models import FailureRow

    with engine.connect() as conn:
        return [r[0] for r in conn.execute(select(FailureRow.__table__.c.reason).where(
            FailureRow.__table__.c.run_id == run_id))]


def test_ai_on_with_a_failing_provider_completes_identically_to_ai_off(
        sources, monkeypatch, tmp_path):
    off = run(run_config("ai-off", tmp_path))
    http = install_http(monkeypatch, FakeHTTP(lambda r: (500, {"error": "provider down"})))
    on = run(run_config("ai-failing", tmp_path, ai={
        "enabled": True, "providers": [local_provider()], "budget_usd": 1.0,
        "features": ["extraction_fallback", "normalisation", "lead_summary"]}))

    assert off.status == on.status == "completed"
    assert http.requests, "the failing provider was never even tried"
    assert snapshot(on) == snapshot(off)
    names = sorted(lead["business_name"] for lead in snapshot(on))
    assert "Royal Caterers" in names                         # the rules still cleaned it
    from leadscraper.db.engine import open_database
    from leadscraper.paths import default_db_url

    engine = open_database(default_db_url())
    reasons = failure_reasons(engine, on.run_id)
    assert "ai_call_failed" in reasons and "ai_disabled" in reasons
    assert "ai_call_failed" not in failure_reasons(engine, off.run_id)
    engine.dispose()


def test_a_runs_ai_usage_is_in_the_ledger_and_the_event_stream(sources, monkeypatch,
                                                                 tmp_path):
    from sqlalchemy import select

    from leadscraper.db.engine import open_database
    from leadscraper.db.models import AIUsage, OrgAttribute
    from leadscraper.paths import default_db_url

    def answer(system, user):
        records = json.loads(user.split("\n\n", 1)[-1])
        return {"items": [{"id": r["id"], "summary": f"{r['name']} is a caterer in "
                                                       f"{r['locality']}."} for r in records]}

    llm(monkeypatch, answer)
    events: list = []
    outcome = run(run_config("ai-ledger", tmp_path, ai={
        "enabled": True, "budget_usd": 0.5, "features": ["lead_summary"], "batch_size": 4,
        "providers": [local_provider()]}), events)
    assert outcome.status == "completed"

    engine = open_database(default_db_url())
    with engine.connect() as conn:
        summaries = conn.execute(select(OrgAttribute.__table__).where(
            OrgAttribute.__table__.c.key == "summary")).mappings().all()
        ledger = conn.execute(select(AIUsage.__table__).where(
            AIUsage.__table__.c.run_id == outcome.run_id)).mappings().all()
    engine.dispose()
    assert len(summaries) == 6 and all(s["origin"] == "ai" for s in summaries)
    assert all(s["producer"] == "local/test-worker" for s in summaries)
    assert [(r["provider"], r["model"], r["feature"]) for r in ledger] == \
        [("local", "test-worker", "lead_summary")]
    assert ledger[0]["calls"] == 2 and ledger[0]["input_tokens"] > 0
    assert ledger[0]["cost_usd"] == 0.0                       # a local model is free
    spend = [e for e in events if e.type == "ai_spend"]
    assert spend and spend[-1].calls == 2 and spend[-1].cap_usd == 0.5


def test_the_budget_cap_with_a_billed_provider(sources, monkeypatch, tmp_path):
    from sqlalchemy import func, select

    from leadscraper.db.engine import open_database
    from leadscraper.db.models import AIUsage, OrgAttribute
    from leadscraper.paths import default_db_url

    monkeypatch.setenv("LEADSCRAPER_SECRET_PRICEY_KEY", "sk-test")

    def responder(req):
        user = req.body["messages"][1]["content"]
        records = json.loads(user.split("\n\n", 1)[-1])
        return 200, chat_response(json.dumps({"items": [
            {"id": r["id"], "summary": f"{r['name']} is a caterer in {r['locality']}."}
            for r in records]}), tokens=(1000, 100))

    install_http(monkeypatch, FakeHTTP(responder))
    events: list = []
    outcome = run(run_config("ai-billed", tmp_path, ai={
        "enabled": True, "budget_usd": 0.04, "features": ["lead_summary"], "batch_size": 1,
        "providers": [{"name": "pricey", "kind": "openai_compatible",
                       "base_url": "https://pricey.example/v1", "worker_model": "w",
                       "key_secret_name": "pricey-key", "retries": 0}],
        "prices": {"pricey": {"w": {"input": 10.0, "output": 50.0}}}}), events)
    assert outcome.status == "completed" and len(snapshot(outcome)) == 6

    engine = open_database(default_db_url())
    with engine.connect() as conn:
        summaries = conn.execute(select(func.count()).select_from(OrgAttribute.__table__).where(
            OrgAttribute.__table__.c.key == "summary")).scalar()
        cost = conn.execute(select(func.sum(AIUsage.__table__.c.cost_usd)).where(
            AIUsage.__table__.c.run_id == outcome.run_id)).scalar()
    assert 1 <= summaries < 6, "AI did not stop mid-run"
    assert cost <= 0.04 + 1e-9, "spent past the cap"
    assert "ai_disabled" in failure_reasons(engine, outcome.run_id)
    engine.dispose()
    spend = [e for e in events if e.type == "ai_spend"]
    assert spend and spend[-1].cap_usd == 0.04 and spend[-1].cost_usd <= 0.04


def test_ai_attributes_reach_the_workbook_labelled(sources, monkeypatch, tmp_path):
    from openpyxl import load_workbook

    def answer(system, user):
        payload = json.loads(user.split("\n\n", 1)[-1] if user.startswith("Search:") else user)
        if "industry tags" in system:
            return {"items": [{"id": r["id"], "industry": ["catering"], "company_size": "11-50"}
                              for r in payload]}
        if "one factual line" in system:
            return {"items": [{"id": r["id"], "summary": f"{r['name']} caters events."}
                              for r in payload]}
        if "extract business contact details" in system:
            return [{"id": r["id"], "phones": ["+91 98200 00417"],
                     "emails": ["events@saffronleaf.example"]} for r in payload]
        return [{"id": r["id"], "business_name": r["title"].split(" |")[0].split(" -")[0],
                 "is_business": True, "matches_category": True} for r in payload]

    llm(monkeypatch, answer)
    outcome = run(run_config("ai-columns", tmp_path, ai={
        "enabled": True, "providers": [local_provider()], "budget_usd": 1.0,
        "features": ["extraction_fallback", "normalisation", "lead_summary"]}))
    assert outcome.status == "completed"
    from leadscraper.db.engine import open_database
    from leadscraper.db.runs import get_run
    from leadscraper.paths import default_db_url

    engine = open_database(default_db_url())
    files = get_run(engine, outcome.run_id)["files"]
    engine.dispose()
    book = load_workbook(next(f["path"] for f in files if f["kind"] == "xlsx"))
    headers = [c.value for c in book["Records"][1]]
    for header in ("Summary (AI)", "Industry (AI)", "Company Size (AI estimate)"):
        assert header in headers
    row = [c.value for c in book["Records"][2]]
    assert row[headers.index("Company Size (AI estimate)")] == "11-50"
    assert row[headers.index("Industry (AI)")] == "catering"
    legend = [c.value for col in book["Overview"].iter_cols() for c in col if c.value]
    assert any("An estimate, not a fact" in str(v) for v in legend)
