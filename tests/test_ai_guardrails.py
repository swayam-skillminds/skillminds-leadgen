"""Grounding guardrails (Phase 5.5 acceptance): AI never adds a fact the page didn't publish.

* Hallucinated phones and emails -- absent from the fetched page -- are dropped
  from AI extraction, however well-formed they are (the acceptance check).
* The web-search source keeps an organisation only with a source URL the
  provider's search returned, a page we fetched ourselves, and its name on it.
* Signal interpretation needs a verbatim evidence quote; summaries reject
  outreach copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from leadscraper.ai.client import AIClient
from leadscraper.ai.grounding import SourceText, ground_contacts
from leadscraper.ai.settings import resolve
from leadscraper.config.legacy import Config
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.leadtypes.local_business.pipeline import Pipeline
from leadscraper.sources.base import Enricher, Source

from ai_fakes import FakeHTTP, install_http, llm, local_provider, openai_response

PAGES = Path(__file__).parent / "fixtures" / "pages"


# --- the rule ----------------------------------------------------------------------------------

def test_ground_contacts_keeps_only_what_the_page_carries():
    page = SourceText("Call +91 98200 00417 or write events [at] saffronleaf [dot] example. "
                      "Kitchen: Shop 4, Baner Road, Baner, Pune 411045. "
                      "instagram.com/saffronleaf.example", "IN")
    kept = ground_contacts({
        "phones": ["98200-00417", "+91 98200 99999"],              # one real, one invented
        "emails": ["events@saffronleaf.example", "sales@saffronleaf.example"],
        "address": "shop 4, baner road, baner, pune 411045",
        "instagram": "@saffronleaf.example"}, page)
    assert kept.phones == ["+919820000417"]
    assert kept.emails == ["events@saffronleaf.example"]       # de-obfuscated on the page
    assert kept.address and kept.instagram == "saffronleaf.example"
    assert any("98200 99999" in d for d in kept.dropped)
    assert any("sales@" in d for d in kept.dropped)
    invented = ground_contacts({"address": "12 MG Road, Pune"}, page)
    assert invented.empty() and invented.dropped


# --- AI fallback extraction through the pipeline (mocked HTTP) --------------------------------

class SiteSource(Source):
    name = "stub"

    def discover(self, locality, category):
        yield self.make_lead(business_name="Saffron Leaf Caterers", category=category.name,
                             locality=locality.name, website="https://saffronleaf.example",
                             sources=["google_maps"])


class FixturePageEnricher(Enricher):
    """Fetched the caterer's contact page; its regexes found nothing (a redesign)."""

    name = "stub_enricher"

    def targets(self, store):
        return store.all_with_ids()

    def enrich(self, lead):
        from leadscraper.ai.pageprep import prepare

        page = prepare((PAGES / "caterer_contact.html").read_text(encoding="utf-8"),
                       "https://saffronleaf.example/contact")
        self.pending_text, self.pending_source = page.markdown, page.source_text
        return None


def pipeline_config(tmp_path, **ai):
    from test_pipeline import make_config

    config = make_config(tmp_path)
    config.set("ai.enabled", True)
    config.set("ai.providers", [local_provider()])
    config.set("ai.features", ["extraction_fallback"])
    config.set("ai.max_cost_usd", 1.0)
    config.set("runtime.ai_cache", str(tmp_path / "ai.sqlite3"))
    for key, value in ai.items():
        config.set(f"ai.{key}", value)
    return config


def test_hallucinated_phones_and_emails_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": SiteSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES",
                        {"stub_enricher": FixturePageEnricher})

    def model(system, user):
        page_id = json.loads(user)[0]["id"]
        return [{"id": page_id, "business_name": "Saffron Leaf Caterers",
                 "phones": ["+91 98200 00417", "+91 98200 31337"],     # second: invented
                 "emails": ["events@saffronleaf.example", "ceo@saffronleaf.example"],
                 "address": "Shop 4, Baner Road, Baner, Pune 411045",
                 "instagram": "saffron_leaf_official"}]                # invented handle

    http = llm(monkeypatch, model)
    config = pipeline_config(tmp_path)
    pipe = Pipeline(config, localities=config.localities, categories=config.categories,
                    source_names=["stub"], enricher_names=["stub_enricher"])
    pipe.run()

    lead = pipe.leads[0]
    assert lead.phones == ["+919820000417"]
    assert lead.emails == ["events@saffronleaf.example"]
    assert lead.instagram is None
    assert lead.address == "Shop 4, Baner Road, Baner, Pune 411045"
    assert "ai_extract" in lead.sources
    dropped = [r for r in pipe.failures.records if r["reason"] == "ai_ungrounded_dropped"]
    assert dropped and "98200 31337" in dropped[0]["detail"]
    assert "ceo@saffronleaf.example" in dropped[0]["detail"]
    # The model got the token-lean Markdown, not the raw page.
    sent = http.requests[0].body["messages"][1]["content"]
    assert "Saffron Leaf" in sent and "gtm.js" not in sent and len(sent) < 6000


# --- the web-search discovery source ------------------------------------------------------

class FakeFetch:
    """The run's HttpClient for the source's own page fetches."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched: list[str] = []

    def get(self, url, rate_key=None, **kw):
        self.fetched.append(url)
        if url not in self.pages:
            raise ConnectionError("no such page")
        return SimpleNamespace(text=self.pages[url], url=url, status_code=200)


def search_context(tmp_path, monkeypatch, findings, sources):
    from leadscraper.engine.failures import FailureLog
    from leadscraper.net.ratelimit import RateLimiter
    from leadscraper.sources.base import SourceContext

    monkeypatch.setenv("LEADSCRAPER_SECRET_OPENAI_API_KEY", "sk-test")
    install_http(monkeypatch, FakeHTTP(lambda r: (200, openai_response(
        json.dumps({"items": findings}), sources=tuple(sources)))))
    config = Config({"country": "US", "city": "Austin",
                     "localities": [{"name": "Austin"}],
                     "categories": [{"name": "Dentists", "queries": ["dentist"]}],
                     "runtime": {"robots_txt": False, "failure_log": str(tmp_path / "f.jsonl")},
                     "sources": {"website": {"min_delay": 0, "max_delay": 0},
                                 "ai_web_search": {"min_delay": 0, "max_delay": 0}},
                     "ai": {"enabled": True, "providers": ["openai"],
                            "features": ["web_search_discovery"]}})
    client = AIClient(resolve(config))
    fetch = FakeFetch({"https://lakeshoredental.example/":
                       (PAGES / "clinic_home.html").read_text(encoding="utf-8")})
    failures = FailureLog(tmp_path / "f.jsonl")
    ctx = SourceContext(config=config, http=fetch, limiter=RateLimiter(), failures=failures,
                        browser=None, store=None, ai=client)
    return ctx, fetch, failures


def test_web_search_keeps_only_sourced_fetched_grounded_organisations(tmp_path, monkeypatch):
    from leadscraper.sources.ai_web_search import AIWebSearchSource

    findings = [
        {"name": "Lakeshore Family Dental", "source_url": "https://lakeshoredental.example/",
         "website": "https://lakeshoredental.example/", "phone": "(512) 555-0142",
         "email": "dr.smith@lakeshoredental.example",                     # not on the page
         "address": "4100 Lakeshore Blvd, Suite 210, Austin, TX 78701"},
        {"name": "Invented Dental Group", "source_url": "https://invented.example/",
         "phone": "(512) 555-0199"},                                      # not searched
        {"name": "Ghost Orthodontics", "source_url": "https://lakeshoredental.example/",
         "phone": "(512) 555-0142"},                                      # not on the page
        {"name": "Unreachable Smiles", "source_url": "https://down.example/"},  # fetch fails
        {"name": "No URL Dental", "source_url": "not a url"},
    ]
    sources = ["https://lakeshoredental.example/", "https://down.example/"]
    ctx, fetch, failures = search_context(tmp_path, monkeypatch, findings, sources)
    source = AIWebSearchSource(ctx)
    locality, category = ctx.config.localities[0], ctx.config.categories[0]
    leads = list(source.discover(locality, category))

    assert [lead.business_name for lead in leads] == ["Lakeshore Family Dental"]
    lead = leads[0]
    assert lead.phones == ["+15125550142"] and lead.emails == []
    assert lead.source_urls == ["https://lakeshoredental.example/"]
    assert lead.address.startswith("4100 Lakeshore Blvd")
    reasons = [r["reason"] for r in failures.records]
    assert reasons.count("ai_unsourced") == 2                   # invented URL, no URL
    assert "ai_ungrounded" in reasons and "fetch_failed" in reasons
    assert "ai_ungrounded_dropped" in reasons                    # the invented email
    assert "https://invented.example/" not in fetch.fetched     # never fetched


def test_web_search_does_nothing_without_a_searching_provider(tmp_path, monkeypatch):
    from leadscraper.engine.failures import FailureLog
    from leadscraper.net.ratelimit import RateLimiter
    from leadscraper.sources.ai_web_search import AIWebSearchSource
    from leadscraper.sources.base import SourceContext

    http = llm(monkeypatch, lambda s, u: {"items": []})
    config = Config({"country": "US", "city": "Austin", "localities": [{"name": "Austin"}],
                     "categories": [{"name": "Dentists"}],
                     "ai": {"enabled": True, "providers": [local_provider()],
                            "features": ["web_search_discovery"]}})
    failures = FailureLog(tmp_path / "f.jsonl")
    ctx = SourceContext(config=config, http=None, limiter=RateLimiter(), failures=failures,
                        browser=None, store=None, ai=AIClient(resolve(config)))
    assert list(AIWebSearchSource(ctx).discover(config.localities[0],
                                                config.categories[0])) == []
    assert http.requests == [] and failures.records[0]["reason"] == "ai_no_web_search"


def test_the_ai_sources_are_built_only_with_their_feature():
    from leadscraper.leadtypes.local_business.planning import select_sources
    from leadscraper.sources.registry import DISCOVERY_SOURCES

    base = {"country": "US", "sources": {"google_maps": {"enabled": True}}}
    _, plan = select_sources("source", DISCOVERY_SOURCES, None, Config(base))
    assert "ai_web_search" not in plan.built and "needs AI" in plan.skipped["ai_web_search"]
    on = Config({**base, "ai": {"enabled": True, "features": ["web_search_discovery"]}})
    _, plan = select_sources("source", DISCOVERY_SOURCES, None, on)
    assert "ai_web_search" in plan.built and "ai_directory" not in plan.built
    off = Config({**base, "ai": {"enabled": True, "features": ["web_search_discovery"]},
                  "sources": {"ai_web_search": {"enabled": False}}})
    _, plan = select_sources("source", DISCOVERY_SOURCES, None, off)
    assert plan.skipped["ai_web_search"] == "disabled in config"


# --- signals and summaries ------------------------------------------------------------------

def test_signal_interpretation_needs_a_verbatim_quote_and_a_known_type(monkeypatch):
    from leadscraper.ai.features.signals import interpret
    from leadscraper.leadtypes.base import SignalSpec

    def model(system, user):
        return {"items": [
            {"id": "1", "signal_type": "funding", "strength": 0.9, "summary": "Raised a round",
             "evidence": "raised $4M in seed funding"},
            {"id": "2", "signal_type": "funding", "strength": 0.8, "summary": "made up",
             "evidence": "announced a $50M Series C"},                   # not in the item
            {"id": "3", "signal_type": "weather", "strength": 0.5, "summary": "?",
             "evidence": "sunny skies over the new office"},             # not a known type
            {"id": "4", "signal_type": None, "strength": 0, "summary": "", "evidence": ""}]}

    llm(monkeypatch, model)
    client = AIClient(resolve(Config({"ai": {"enabled": True, "providers": [local_provider()],
                                             "features": ["signal_interpretation"]}})))
    items = [{"id": 1, "title": "Acme Logistics raised $4M in seed funding",
              "text": "Acme Logistics raised $4M in seed funding to build its fleet app.",
              "url": "https://news.example/acme"},
             {"id": 2, "title": "Beta Corp news", "text": "Beta Corp opened a new office."},
             {"id": 3, "title": "Gamma", "text": "Gamma enjoyed sunny skies over the new office."},
             {"id": 4, "title": "Delta", "text": "Nothing to see."}]
    specs = [SignalSpec("funding", "Funding round", 0.8, 90),
             SignalSpec("expansion", "Expansion", 0.6, 120)]
    out = interpret(client, items, specs, lead_type="funding_news")
    assert set(out) == {"1"}
    assert out["1"]["type"] == "funding" and out["1"]["strength"] == 0.9
    assert out["1"]["source_url"] == "https://news.example/acme"


@pytest.mark.parametrize("text,ok", [
    ("Family dental clinic in Baner offering implants and orthodontics; 4.8 stars.", True),
    ("A great fit for you -- reach out to them about a new website!", False),
    ("We can help this clinic modernise its booking.", False),
    ("Contact them now for a demo.", False),
    ("x" * 201, False),
])
def test_summaries_reject_outreach_copy(text, ok):
    from leadscraper.ai.features.summary import acceptable

    assert (acceptable(text) is not None) is ok
