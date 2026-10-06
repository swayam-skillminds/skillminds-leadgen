"""Integration tests for the orchestrator, using stub sources (no network).

These cover the reliability promises: a broken source is logged and stepped
over, enrichment merges into the right record, and output is written even
when things go wrong.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.config import Config
from leadscraper.core.lead import Lead
from leadscraper.leadtypes.local_business.pipeline import Pipeline
from leadscraper.sources.base import Enricher, Source


def make_config(tmp_path, **overrides):
    data = {
        "localities": [{"name": "Andheri", "aliases": ["Andheri East Mumbai"]}],
        "categories": [{"name": "Caterers", "queries": ["catering services"],
                        "directory_slugs": {}, "indiamart": False}],
        "sources": {
            "stub": {"enabled": True, "min_delay": 0, "max_delay": 0},
            "broken": {"enabled": True, "min_delay": 0, "max_delay": 0},
            "stub_enricher": {"enabled": True, "min_delay": 0, "max_delay": 0},
        },
        "runtime": {
            "db_path": str(tmp_path / "leads.sqlite3"),
            "failure_log": str(tmp_path / "failures.jsonl"),
            "run_log": str(tmp_path / "run.log"),
            "csv_export": str(tmp_path / "leads.csv"),
            "sheets_flush_every": 1000,
            "max_retries": 1,
            "backoff_base": 0.01,
        },
        "sheets": {"enabled": False},
        "scoring": {},
    }
    data.update(overrides)
    return Config(data)


class StubSource(Source):
    name = "stub"

    def discover(self, locality, category):
        yield self.make_lead(
            business_name="Royal Caterers", category=category.name,
            locality=locality.name, phones=["+919820012345"],
            website="https://royalcaterers.in", gmaps_rating=4.6, gmaps_reviews=310,
        )
        yield self.make_lead(
            business_name="Tiny Tiffin Co", category=category.name,
            locality=locality.name,
        )


class BrokenSource(Source):
    name = "broken"

    def discover(self, locality, category):
        yield self.make_lead(business_name="Good One Before Crash",
                             category=category.name, locality=locality.name)
        raise RuntimeError("simulated source explosion")


class StubEnricher(Enricher):
    name = "stub_enricher"

    def enrich(self, lead):
        if not lead.website:
            return None
        found = Lead(business_name=lead.business_name)
        found.emails = ["hello@royalcaterers.in"]
        # A phone the stored record does not have -- must NOT split the lead.
        found.phones = ["+912226701234"]
        found.sources = [self.name]
        return found


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES",
                        {"stub": StubSource, "broken": BrokenSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES",
                        {"stub_enricher": StubEnricher})
    return make_config(tmp_path)


def build(config, **kwargs):
    return Pipeline(
        config,
        localities=config.localities,
        categories=config.categories,
        **kwargs,
    )


def test_run_stores_dedupes_and_exports(wired, tmp_path):
    stats = build(wired, source_names=["stub"], enricher_names=[]).run()
    assert stats.new_leads == 2
    csv_path = tmp_path / "leads.csv"
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8-sig")
    assert "Royal Caterers" in content and "Tiny Tiffin Co" in content


def test_broken_source_is_logged_not_fatal(wired, tmp_path):
    """A source that raises must not stop the run or lose its earlier yields."""
    pipe = build(wired, source_names=["stub", "broken"], enricher_names=[])
    stats = pipe.run()
    assert stats.combos_done == 1
    names = {lead.business_name for lead in pipe.leads}
    assert "Royal Caterers" in names           # the healthy source still ran
    assert "Good One Before Crash" in names    # yields before the crash survive
    reasons = {r["reason"] for r in pipe.failures.records}
    assert "source_crashed" in reasons


def test_enrichment_merges_in_place_without_splitting(wired):
    """The enricher finds a NEW phone; the lead must not be duplicated."""
    pipe = build(wired, source_names=["stub"], enricher_names=["stub_enricher"])
    pipe.run()
    leads = pipe.leads
    royal = [l for l in leads if l.business_name == "Royal Caterers"]
    assert len(royal) == 1, "enrichment split the lead into duplicates"
    assert "hello@royalcaterers.in" in royal[0].emails
    assert set(royal[0].phones) == {"+919820012345", "+912226701234"}
    assert "stub_enricher" in royal[0].sources


def test_ranking_puts_the_stronger_lead_first(wired):
    pipe = build(wired, source_names=["stub"], enricher_names=[])
    pipe.run()
    ordered = [lead.business_name for lead in pipe.leads]
    assert ordered.index("Royal Caterers") < ordered.index("Tiny Tiffin Co")


def test_low_signal_lead_is_kept_not_dropped(wired):
    """'Tiny Tiffin Co' has no phone, no rating, one source -- still in output."""
    pipe = build(wired, source_names=["stub"], enricher_names=[])
    pipe.run()
    assert any(l.business_name == "Tiny Tiffin Co" for l in pipe.leads)


def test_rerun_merges_instead_of_duplicating(wired):
    """Running twice against the same DB must not double the rows."""
    build(wired, source_names=["stub"], enricher_names=[]).run()
    pipe = build(wired, source_names=["stub"], enricher_names=[])
    pipe.run()
    assert len(pipe.leads) == 2
    assert pipe.stats.new_leads == 0


def test_dry_run_makes_no_db_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": StubSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    config = make_config(tmp_path)
    pipe = build(config, source_names=["stub"], enricher_names=[], dry_run=True)
    pipe.run()
    assert pipe.leads == []
    # Re-open the DB independently: a dry run must not have written anything.
    from leadscraper.db.store import LeadStore
    fresh = LeadStore(tmp_path / "leads.sqlite3")
    assert fresh.count() == 0
    fresh.close()


# --- AI layer (stub backend, no network) -----------------------------------

import json as _json

from leadscraper.ai import AIAssistant, AIBackend, AIResult


class StubBackend(AIBackend):
    """Returns canned JSON so the AI paths are testable offline."""

    name = "stub"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, system, user, *, model, max_tokens=4000):
        self.calls += 1
        return AIResult(text=_json.dumps(self.payload), cost_usd=0.001, model=model)


class ExplodingBackend(AIBackend):
    name = "boom"

    def complete(self, system, user, *, model, max_tokens=4000):
        raise RuntimeError("backend is down")


def ai_config(tmp_path, **over):
    cfg = make_config(tmp_path)
    cfg.set("ai.enabled", True)
    cfg.set("ai.batch_size", 10)
    cfg.set("ai.max_cost_usd", 1.0)
    cfg.set("ai.features", [])
    cfg.set("runtime.ai_cache", str(tmp_path / "ai.sqlite3"))
    for k, v in over.items():
        cfg.set(k, v)
    return cfg


def test_ai_extraction_results_are_revalidated_not_trusted(tmp_path):
    """A hallucinated phone must be rejected by the same normalisers as scraped data."""
    from leadscraper.leadtypes.local_business.pipeline import Pipeline
    record = {"id": "1", "business_name": "X", "phones": ["not-a-number", "9820012345"],
              "emails": ["nope", "real@vendor.in"], "address": "Road, Mumbai", "instagram": "@vend"}
    partial = Pipeline._lead_from_ai_record(record)
    assert partial.phones == ["+919820012345"]      # junk dropped
    assert partial.emails == ["real@vendor.in"]     # junk dropped
    assert partial.instagram == "vend"


def test_ai_record_with_nothing_usable_returns_none():
    from leadscraper.leadtypes.local_business.pipeline import Pipeline
    assert Pipeline._lead_from_ai_record(
        {"id": "1", "phones": [], "emails": [], "address": None, "instagram": None}
    ) is None


def test_ai_backend_failure_degrades_silently(tmp_path):
    ai = AIAssistant(ai_config(tmp_path), backend=ExplodingBackend())
    assert ai.extract_contacts([("1", "some text")]) == {}
    assert ai.stats.failures == 1
    ai.close()


def test_ai_budget_cap_disables_further_calls(tmp_path):
    ai = AIAssistant(ai_config(tmp_path, **{"ai.max_cost_usd": 0.0}),
                     backend=StubBackend([{"id": "1", "business_name": "X"}]))
    assert ai.active is False
    assert ai.extract_contacts([("1", "text")]) == {}
    ai.close()


def test_ai_cache_prevents_paying_twice(tmp_path):
    backend = StubBackend([{"id": "1", "business_name": "Cached Co", "phones": [], "emails": [],
                            "address": "", "instagram": None}])
    ai = AIAssistant(ai_config(tmp_path), backend=backend)
    first = ai.extract_contacts([("1", "text")])
    second = ai.extract_contacts([("1", "text")])
    assert first == second and backend.calls == 1 and ai.stats.cache_hits == 1
    ai.close()


def test_ai_disabled_by_default(tmp_path):
    ai = AIAssistant(make_config(tmp_path))
    assert ai.active is False
    assert ai.clean_names([{"id": "1", "title": "X"}]) == {}


def test_ai_handles_object_wrapped_array(tmp_path):
    """Models sometimes return {"results": [...]} instead of a bare array."""
    backend = StubBackend({"results": [{"id": "7", "business_name": "Wrapped Co",
                                        "is_business": True, "matches_category": True}]})
    ai = AIAssistant(ai_config(tmp_path), backend=backend)
    out = ai.clean_names([{"id": "7", "title": "Wrapped Co | Best in Mumbai"}])
    assert out["7"]["business_name"] == "Wrapped Co"
    ai.close()


def test_json_fence_is_stripped():
    from leadscraper.ai import parse_json
    assert parse_json('```json\n[{"id": "1"}]\n```') == [{"id": "1"}]
    assert parse_json('Here you go: [{"id": "2"}]') == [{"id": "2"}]
    assert parse_json("not json at all") is None


def test_zero_budget_is_respected_not_replaced_by_default(tmp_path):
    """A configured 0 is falsy -- it must not fall through to the default."""
    ai = AIAssistant(ai_config(tmp_path, **{"ai.max_cost_usd": 0}), backend=StubBackend([]))
    assert ai.max_cost == 0.0
    assert ai.active is False


# --- AI wiring through the real pipeline -----------------------------------

class SerpishSource(Source):
    """Mimics a title-derived source: messy name, no google_maps corroboration."""

    name = "stub"

    def discover(self, locality, category):
        yield self.make_lead(
            business_name="Studio Fine Arts | Best Caterers in Andheri Mumbai 2024",
            category=category.name, locality=locality.name,
            website="https://studiofinearts.in", sources=["google_search"],
        )


class SilentWebsiteEnricher(Enricher):
    """Fetches fine but finds nothing -- exactly the AI fallback trigger."""

    name = "stub_enricher"
    pending_text = None

    def targets(self, store):
        return store.all_with_ids()

    def enrich(self, lead):
        self.pending_text = "Call 98200 12345 or write to hello@studiofinearts.in"
        return None


def test_ai_phases_run_through_the_pipeline(tmp_path, monkeypatch):
    """End-to-end: name polish renames the lead, fallback extraction adds contacts."""
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": SerpishSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"stub_enricher": SilentWebsiteEnricher})

    class Router(AIBackend):
        """Answers whichever task it's asked about."""
        name = "router"

        def complete(self, system, user, *, model, max_tokens=4000):
            if "clean up business names" in system:
                payload = [{"id": _json.loads(user)[0]["id"],
                            "business_name": "Studio Fine Arts",
                            "is_business": True, "matches_category": True}]
            else:
                payload = [{"id": _json.loads(user)[0]["id"],
                            "business_name": "Studio Fine Arts",
                            "phones": ["98200 12345"], "emails": ["hello@studiofinearts.in"],
                            "address": "Andheri, Mumbai", "instagram": None}]
            return AIResult(text=_json.dumps(payload), cost_usd=0.001, model=model)

    config = ai_config(tmp_path)
    pipe = Pipeline(config, localities=config.localities, categories=config.categories,
                    source_names=["stub"], enricher_names=["stub_enricher"])
    pipe.ai = AIAssistant(config, pipe.failures, backend=Router())
    pipe.run()

    lead = pipe.leads[0]
    assert lead.business_name == "Studio Fine Arts", "AI name polish did not apply"
    assert lead.phones == ["+919820012345"], "AI fallback extraction did not apply"
    assert "hello@studiofinearts.in" in lead.emails
    assert "ai_extract" in lead.sources
    assert pipe.stats.ai_updates > 0


class KeywordStuffedMapsSource(Source):
    """Two Maps listings: one owner stuffed keywords into the name, one didn't."""

    name = "stub"

    def discover(self, locality, category):
        yield self.make_lead(
            business_name="Events Aura – Event Management Company in Mumbai",
            category=category.name, locality=locality.name, sources=["google_maps"],
        )
        yield self.make_lead(
            business_name="Galaxy Caterers - St. Arnold's Lawns",
            category=category.name, locality=locality.name, sources=["google_maps"],
        )


def test_keyword_stuffed_maps_names_reach_ai_name_polish(tmp_path, monkeypatch):
    """Regression (REBUILD Part 0.6): name polish skipped every google_maps lead,
    but all ~100 SEO-titled names in the Mumbai DB came from Maps."""
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": KeywordStuffedMapsSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    sent: list[str] = []

    class NameCleaner(AIBackend):
        name = "names"

        def complete(self, system, user, *, model, max_tokens=4000):
            records = _json.loads(user)
            sent.extend(record["title"] for record in records)
            payload = [{"id": record["id"], "business_name": record["title"].split(" – ")[0],
                        "is_business": True, "matches_category": True} for record in records]
            return AIResult(text=_json.dumps(payload), cost_usd=0.001, model=model)

    config = ai_config(tmp_path)
    pipe = Pipeline(config, localities=config.localities, categories=config.categories,
                    source_names=["stub"], enricher_names=[])
    pipe.ai = AIAssistant(config, pipe.failures, backend=NameCleaner())
    pipe.run()

    assert sent == ["Events Aura – Event Management Company in Mumbai"]
    names = sorted(lead.business_name for lead in pipe.leads)
    assert names == ["Events Aura", "Galaxy Caterers - St. Arnold's Lawns"]


def test_ai_off_leaves_the_messy_name_untouched(tmp_path, monkeypatch):
    """Control case: without AI the pipeline behaves exactly as before."""
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": SerpishSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"stub_enricher": SilentWebsiteEnricher})
    config = make_config(tmp_path)          # ai.enabled defaults to False
    pipe = Pipeline(config, localities=config.localities, categories=config.categories,
                    source_names=["stub"], enricher_names=["stub_enricher"])
    pipe.run()
    assert pipe.leads[0].business_name.startswith("Studio Fine Arts |")
    assert pipe.leads[0].phones == []


# --- billing safety --------------------------------------------------------

def test_billed_backend_never_selected_even_with_an_api_key(monkeypatch):
    """A stray ANTHROPIC_API_KEY must not silently start charging the user."""
    from leadscraper.ai import pick_backend
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    # "auto" must stay on the subscription backend.
    chosen = pick_backend("auto")
    assert chosen is None or chosen.bills_separately is False

    # Even asked for by name, the billed backend is refused while the
    # allow_billed catch is off.
    assert pick_backend("anthropic_api", allow_billed=False) is None


def test_billed_backend_is_available_when_explicitly_allowed(monkeypatch):
    """The escape hatch still works for someone who does want to pay per call."""
    from leadscraper.ai import pick_backend
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    import importlib.util
    if importlib.util.find_spec("anthropic") is None:
        import pytest as _pytest
        _pytest.skip("anthropic SDK not installed")
    chosen = pick_backend("anthropic_api", allow_billed=True)
    assert chosen is not None and chosen.bills_separately is True


def test_assistant_defaults_to_the_subscription_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    cfg = ai_config(tmp_path)
    cfg.set("ai.backend", "auto")
    ai = AIAssistant(cfg)
    if ai.backend is not None:
        assert ai.backend.bills_separately is False
    ai.close()


# --- externally-credited models are blocked --------------------------------

def test_fable_and_mythos_models_are_refused(tmp_path):
    """Those families bill as external credits, so config must not reach them."""
    from leadscraper.ai import safe_model, DEFAULT_MODEL
    for blocked in ("claude-fable-5-1", "claude-fable-5", "claude-mythos-5-1", "CLAUDE-FABLE-5"):
        assert safe_model(blocked, DEFAULT_MODEL) == DEFAULT_MODEL
    # Normal models pass straight through.
    assert safe_model("claude-haiku-4-5", DEFAULT_MODEL) == "claude-haiku-4-5"
    assert safe_model("claude-opus-5", DEFAULT_MODEL) == "claude-opus-5"


def test_assistant_swaps_out_a_configured_fable_model(tmp_path):
    from leadscraper.ai import DEFAULT_MODEL
    cfg = ai_config(tmp_path)
    cfg.set("ai.model", "claude-fable-5-1")
    cfg.set("ai.judge_model", "claude-mythos-5-1")
    ai = AIAssistant(cfg, backend=StubBackend([]))
    assert ai.model == DEFAULT_MODEL
    assert "fable" not in ai.judge_model and "mythos" not in ai.judge_model
    ai.close()


# --- quota exhaustion falls back to non-AI mode ----------------------------

class QuotaBackend(AIBackend):
    """Fails the way an exhausted subscription does."""

    name = "quota"

    def __init__(self):
        self.calls = 0

    def complete(self, system, user, *, model, max_tokens=4000):
        self.calls += 1
        from leadscraper.ai import QuotaExhausted
        raise QuotaExhausted("5-hour usage limit reached; resets at 9pm")


def test_quota_exhaustion_disables_ai_and_stops_calling(tmp_path):
    backend = QuotaBackend()
    ai = AIAssistant(ai_config(tmp_path), backend=backend)
    assert ai.active is True
    ai.extract_contacts([("1", "text")])
    assert ai.quota_exhausted is True and ai.active is False
    # Further work must not hit the backend again.
    ai.clean_names([{"id": "2", "title": "X"}])
    ai.extract_contacts([("3", "more text")])
    assert backend.calls == 1, "kept calling a backend that is out of allowance"
    assert "allowance" in ai.stats.shutdown_reason
    ai.close()


def test_quota_message_is_recognised_from_a_generic_error(tmp_path):
    from leadscraper.ai import is_quota_error
    assert is_quota_error("Error: usage limit reached, resets at 9pm")
    assert is_quota_error("HTTP 429 Too Many Requests")
    assert not is_quota_error("Error: could not resolve host example.com")


def test_repeated_failures_trip_the_circuit_breaker(tmp_path):
    ai = AIAssistant(ai_config(tmp_path, **{"ai.failure_limit": 2}),
                     backend=ExplodingBackend())
    ai.extract_contacts([("1", "a")])
    ai.extract_contacts([("2", "b")])
    assert ai.quota_exhausted is True, "broken backend was never given up on"
    assert "consecutive" in ai.stats.shutdown_reason
    ai.close()


def test_scrape_still_completes_after_ai_quota_runs_out(tmp_path, monkeypatch):
    """The whole point: losing AI must not lose the run."""
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": SerpishSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"stub_enricher": SilentWebsiteEnricher})
    config = ai_config(tmp_path)
    pipe = Pipeline(config, localities=config.localities, categories=config.categories,
                    source_names=["stub"], enricher_names=["stub_enricher"])
    pipe.ai = AIAssistant(config, pipe.failures, backend=QuotaBackend())
    pipe.run()
    assert len(pipe.leads) == 1, "run did not complete after AI was disabled"
    assert pipe.ai.quota_exhausted is True
    # The lead survives with its un-polished name -- degraded, not lost.
    assert pipe.leads[0].business_name.startswith("Studio Fine Arts")


def test_unlimited_budget_means_no_cap(tmp_path):
    cfg = ai_config(tmp_path)
    cfg.set("ai.max_cost_usd", None)
    ai = AIAssistant(cfg, backend=StubBackend([]))
    assert ai.max_cost == float("inf") and ai.over_budget is False
    ai.close()
