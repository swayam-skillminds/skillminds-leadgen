"""AI features through the API, services and CLI (Phase 5.4), with mocked HTTP.

* ``icp_builder`` turns a sample brief into a schema-valid run config, returned
  for confirmation (acceptance check); with no AI it falls back to the offering.
* ``planner`` returns suggestions and a revised (valid) config, never applied.
* ``source_discovery`` stores candidates (policy-filtered); an approved one is
  read by the generic ``ai_directory`` source in a real run.
* Providers report whether a key is configured, never the key; usage and the
  pre-run estimate show AI cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from leadscraper.api.app import PREFIX, create_app
from leadscraper.config.schema import RunConfig

from ai_fakes import install_http, FakeHTTP, llm, local_provider, openai_response

PAGES = Path(__file__).parent / "fixtures" / "pages"


@pytest.fixture
def engine():
    from app_db import fresh_application_db

    engine = fresh_application_db()
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as client:
        client.patch(f"{PREFIX}/settings", json={"ai": {"providers": [local_provider()]}})
        yield client


ICP_ANSWER = {
    "name": "Pune ERP prospects", "country": "IN", "places": ["Pune", "Nashik"],
    "categories": [{"name": "Manufacturing companies",
                    "queries": ["manufacturing company", "auto component maker"]},
                   {"name": "Industrial suppliers", "queries": []}],
    "roles": ["Managing Director", "Plant Head"], "signals": ["new plant", "expansion"],
    "industries": ["manufacturing"], "company_size": "51-200", "technologies": ["Tally"],
    "must_have": ["phone", "fax"], "lead_types": ["local_business", "hiring"],
    "notes": ["mid-size means roughly 50-500 people"]}


def test_icp_builder_turns_a_brief_into_a_schema_valid_config(client, monkeypatch):
    seen: list = []

    def model(system, user):
        seen.append((system, user))
        return ICP_ANSWER

    llm(monkeypatch, model)
    response = client.post(f"{PREFIX}/ai/icp", json={
        "brief": "We implement ERP for mid-size manufacturers in Pune and Nashik that are "
                 "expanding.", "offering": "erp"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True and body["source"] == "ai"
    config = RunConfig.model_validate(body["config"])               # schema-valid
    assert config.geography.country == "IN" and config.geography.places == ["Pune", "Nashik"]
    categories = config.lead_type("local_business").categories
    assert categories[0].queries == ["manufacturing company", "auto component maker"]
    assert config.filters.must_have == ["phone"]                    # "fax" is not a field
    assert config.icp.company_size == "51-200" and config.ai.enabled is False
    # A built lead type the draft names is configured (Phase 6); a later one is only noted.
    assert config.lead_type("hiring").enabled is True
    assert not any("later phases" in note for note in body["notes"])
    assert any("Plant Head" in note for note in body["notes"])
    assert "never run" in body["confirm"].lower() or "review it" in body["confirm"].lower()
    assert "ERP implementation" in seen[0][1]                       # started from the offering
    # Usage is in the ledger, attached to no run.
    usage = client.get(f"{PREFIX}/ai/usage").json()
    assert usage["calls"] == 1 and usage["by_feature"] == {"icp_builder": 0.0}
    # Nothing was saved or run.
    assert client.get(f"{PREFIX}/configs").json()["total"] == 0
    assert client.get(f"{PREFIX}/runs").json()["total"] == 0


def test_icp_builder_asks_again_after_an_invalid_draft(client, monkeypatch):
    answers = iter([{**ICP_ANSWER, "country": "Indiaa"}, ICP_ANSWER])
    seen: list = []

    def model(system, user):
        seen.append(user)
        return next(answers)

    llm(monkeypatch, model)
    body = client.post(f"{PREFIX}/ai/icp", json={"brief": "ERP for factories in Pune"}).json()
    assert body["valid"] is True and len(seen) == 2
    assert "failed validation" in seen[1]


def test_icp_builder_without_ai_uses_the_offering(client, monkeypatch):
    llm(monkeypatch, lambda s, u: (500, {"error": "down"}))
    body = client.post(f"{PREFIX}/ai/icp", json={
        "brief": "Websites for restaurants", "offering": "web_app_dev",
        "places": ["Austin"], "country": "US"}).json()
    assert body["source"] == "preset" and body["valid"] is True
    assert body["config"]["geography"] == {"country": "US", "places": ["Austin"]}
    assert client.post(f"{PREFIX}/ai/icp", json={"brief": "something vague"}).status_code == 422
    offerings = client.get(f"{PREFIX}/ai/offerings").json()["items"]
    assert {o["key"] for o in offerings} == {"custom_software", "web_app_dev", "erp",
                                             "staff_augmentation", "training"}


def test_planner_suggests_and_returns_a_valid_revised_config(client, monkeypatch):
    llm(monkeypatch, lambda s, u: {
        "areas": [{"place": "Pune", "areas": ["Baner", "Kothrud", "Hinjewadi"]}],
        "category_queries": [{"category": "dentists",
                              "queries": ["dental clinic", "Dentists", "orthodontist"]}],
        "role_titles": [{"role": "CTO", "titles": ["Chief Technology Officer"]}],
        "lead_types": ["local_business", "crm_sync"], "notes": []})
    document = {"schema_version": 1, "name": "Pune dentists", "project": "pune-dentists",
                "geography": {"country": "IN", "places": ["Pune"]},
                "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}}}
    body = client.post(f"{PREFIX}/ai/plan", json={"config": document, "roles": ["CTO"]}).json()
    assert body["valid"] is True and body["source"] == "ai"
    revised = RunConfig.model_validate(body["config"])
    assert [a.name for a in revised.geography.areas] == ["Baner", "Kothrud", "Hinjewadi"]
    assert revised.lead_type("local_business").categories[0].queries == \
        ["Dentists", "dental clinic", "orthodontist"]                # no duplicate "Dentists"
    assert body["suggestions"]["lead_types"] == ["local_business"]  # crm_sync doesn't exist
    assert body["suggestions"]["role_titles"][0]["titles"] == ["Chief Technology Officer"]
    assert any(p["key"] == "in" and p["applies"] for p in body["applicable"]["packs"])
    bad = client.post(f"{PREFIX}/ai/plan", json={"config": {"name": "x"}})
    assert bad.status_code == 422


def test_source_discovery_stores_policy_filtered_candidates(client, monkeypatch):
    monkeypatch.setenv("LEADSCRAPER_SECRET_OPENAI_API_KEY", "sk-test")
    client.patch(f"{PREFIX}/settings", json={"ai": {"providers": ["openai"]}})
    answer = {"items": [
        {"name": "Pune Business Pages", "url": "https://punebusinesspages.example/pune/dentists/",
         "category": "Dentists", "place": "Pune", "rationale": "lists clinics with phones"},
        {"name": "Indeed", "url": "https://www.indeed.com/q-dentist-jobs.html"},
        {"name": "Justdial", "url": "https://www.justdial.com/Pune/Dentists"},
        {"name": "Made up", "url": "https://imaginary-directory.example/dentists"},
        {"name": "Not a URL", "url": "ftp://x"}]}
    install_http(monkeypatch, FakeHTTP(lambda r: (200, openai_response(
        json.dumps(answer), sources=("https://punebusinesspages.example/",)))))
    body = client.post(f"{PREFIX}/ai/source-discovery", json={
        "country": "IN", "categories": ["Dentists"], "places": ["Pune"]}).json()
    assert body["searched"] is True
    assert [c["host"] for c in body["candidates"]] == ["punebusinesspages.example"]
    assert body["candidates"][0]["status"] == "proposed"
    reasons = {r["url"]: r["reason"] for r in body["rejected"]}
    assert reasons["https://www.indeed.com/q-dentist-jobs.html"].startswith("excluded")
    assert "dedicated source" in reasons["https://www.justdial.com/Pune/Dentists"]
    assert "web search returned" in reasons["https://imaginary-directory.example/dentists"]
    listed = client.get(f"{PREFIX}/ai/candidate-sources", params={"status": "proposed"}).json()
    assert listed["total"] == 1


def test_an_approved_candidate_is_scraped_by_the_generic_extractor(client, engine, monkeypatch):
    """Approve -> run: ai_directory reads the listing with self-healing selectors."""
    from leadscraper.ai.selectors import EXTRACT_SYSTEM, SELECTOR_SYSTEM
    from leadscraper.config.compile import compile_run_config
    from leadscraper.engine.clock import FakeClock
    from leadscraper.engine.runner import RunRequest, execute
    from leadscraper.net.http import HttpClient

    from test_ai_selectors import V1, read_markdown

    created = client.post(f"{PREFIX}/ai/candidate-sources", json={
        "url": "https://punebusinesspages.example/pune/dentists/?page=1",
        "name": "Pune Business Pages", "country": "IN", "place": "Pune",
        "category": "Dentists"}).json()
    assert created["status"] == "proposed"
    approved = client.post(f"{PREFIX}/ai/candidate-sources/{created['id']}/decision",
                           json={"status": "approved"}).json()
    assert approved["status"] == "approved" and approved["decided_at"]

    pages = {"https://punebusinesspages.example/pune/dentists/?page=1": "directory_v1_page1.html",
             "https://punebusinesspages.example/pune/dentists/?page=2": "directory_v1_page2.html"}

    def fake_get(self, url, *, rate_key=None, **kw):
        name = pages.get(url)
        if name is None:
            raise ConnectionError(url)
        return SimpleNamespace(text=(PAGES / name).read_text(encoding="utf-8"), url=url,
                               status_code=200)

    monkeypatch.setattr(HttpClient, "get", fake_get)
    calls = {"extract": 0, "selectors": 0}

    def model(system, user):
        if system == EXTRACT_SYSTEM:
            calls["extract"] += 1
            return {"items": read_markdown(user)}
        if system == SELECTOR_SYSTEM:
            calls["selectors"] += 1
            return V1
        return {"items": []}

    llm(monkeypatch, model)
    document = {"schema_version": 1, "name": "Pune dentists", "project": "pune-dentists",
                "geography": {"country": "IN", "places": ["Pune"], "center": [18.52, 73.85],
                              "admin_area": "Maharashtra"},
                "lead_types": {"local_business": {
                    "categories": [{"name": "Dentists"}],
                    "sources": {"ai_directory": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0}}}},
                "ai": {"enabled": True, "providers": [local_provider()],
                       "features": ["source_discovery"]},
                "runtime": {"robots_txt": False}, "output": {"excel": False}}
    config = compile_run_config(RunConfig.model_validate(document))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 discovery_workers=1, engine=engine, source_names=["ai_directory"],
                                 enricher_names=[]))
    assert outcome.status == "completed"
    leads = outcome.contexts["local_business"].pipeline.leads
    names = {lead.business_name for lead in leads}
    assert len(leads) == 14 and {"Bright Smile Dental Studio", "Modern Dental Studio"} <= names
    bright = next(lead for lead in leads if lead.business_name == "Bright Smile Dental Studio")
    assert bright.phones == ["+919820000101"] and bright.category == "Dentists"
    assert calls == {"extract": 2, "selectors": 1}             # page 2 read by rule
    listed = client.get(f"{PREFIX}/ai/candidate-sources").json()["items"][0]
    assert listed["last_run_at"] is not None


def test_providers_report_keys_as_booleans_only(client, monkeypatch):
    monkeypatch.setenv("LEADSCRAPER_SECRET_ANTHROPIC_API_KEY", "sk-ant-very-secret")
    body = client.get(f"{PREFIX}/ai/providers").json()
    anthropic = next(p for p in body["providers"] if p["name"] == "anthropic")
    assert anthropic["key_configured"] is True and anthropic["key_source"] == "env"
    assert "sk-ant-very-secret" not in json.dumps(body)
    openai = next(p for p in body["providers"] if p["name"] == "openai")
    assert openai["key_configured"] is False and openai["usable"] is False
    assert body["chain"] == ["local"]
    health = client.get(f"{PREFIX}/health").json()
    assert health["checks"]["keys"]["configured"]["ai_anthropic_key"] is True
    assert "sk-ant-very-secret" not in json.dumps(health)
    # Settings hold secret *names*; a key is refused.
    refused = client.patch(f"{PREFIX}/settings", json={"ai": {"providers": [
        {"name": "openai", "api_key": "sk-nope"}]}})
    assert refused.status_code == 422


def test_connectivity_test_makes_one_tiny_call(client, monkeypatch):
    http = llm(monkeypatch, lambda s, u: {"ok": True})
    body = client.post(f"{PREFIX}/ai/providers/local/test").json()
    assert body["ok"] is True and body["model"] == "test-worker" and len(http.requests) == 1
    assert http.requests[0].body["max_tokens"] == 50
    missing = client.post(f"{PREFIX}/ai/providers/openai/test").json()
    assert missing["ok"] is False and "no key" in missing["error"]


def test_estimate_includes_the_ai_cost(client, monkeypatch):
    document = {"schema_version": 1, "name": "Pune dentists", "project": "pune-dentists",
                "geography": {"country": "IN", "places": ["Pune"], "center": [18.52, 73.85],
                              "admin_area": "Maharashtra"},
                "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}},
                "ai": {"enabled": True, "budget_usd": 0.75,
                       "providers": [{"name": "anthropic", "retries": 0}],
                       "features": ["extraction_fallback", "normalisation", "lead_summary"]}}
    monkeypatch.setenv("LEADSCRAPER_SECRET_ANTHROPIC_API_KEY", "sk-ant-test")
    estimate = client.post(f"{PREFIX}/estimate", json={"config": document}).json()
    assert 0 < estimate["ai"]["cost_usd"] <= 0.75 and estimate["ai"]["cap_usd"] == 0.75
    ai = estimate["ai"]["by_lead_type"]["local_business"]
    assert ai["provider"] == "anthropic" and ai["model"] == "claude-haiku-4-5"
    assert set(ai["by_feature"]) >= {"normalisation", "lead_summary"}
    assert 0 < ai["cost_usd"] <= 0.75 and ai["cap_usd"] == 0.75 and ai["priced"] is True


def test_cli_ai_commands(engine, monkeypatch, tmp_path, capsys):
    from leadscraper.cli import main
    from leadscraper.paths import default_db_url

    db = default_db_url()
    assert main(["ai", "offerings", "--db", db]) == 0
    assert "erp" in capsys.readouterr().out
    assert main(["ai", "providers", "--db", db]) == 0
    assert "claude_code_cli" in capsys.readouterr().out
    out = tmp_path / "draft.yaml"
    # No provider usable in tests: the offering makes the draft.
    assert main(["ai", "icp", "ERP for manufacturers", "--offering", "erp", "--place", "Pune",
                 "-o", str(out), "--db", db]) == 0
    assert RunConfig.model_validate(__import__("yaml").safe_load(out.read_text()))
    assert "nothing was run" in capsys.readouterr().out
    assert main(["ai", "candidates", "list", "--db", db]) == 0
    assert main(["ai", "usage", "--db", db]) == 0
