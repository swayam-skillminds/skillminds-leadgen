"""The run's AI client: fallback chain, budget cap, cache, validation, ledger, settings.

Providers are OpenAI-compatible endpoints answered by a fake transport
(``ai_fakes``): real requests, mocked HTTP.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from leadscraper.ai.client import AIClient
from leadscraper.ai.settings import resolve
from leadscraper.config.legacy import Config

from ai_fakes import FakeHTTP, chat_response, install_http, local_provider


class Verdict(BaseModel):
    ok: bool


def remote(name: str, **extra) -> dict:
    """A remote (billed) OpenAI-compatible provider whose key is an env secret."""
    return {"name": name, "kind": "openai_compatible", "base_url": f"https://api.{name}.com/v1",
            "worker_model": f"{name}-worker", "judge_model": f"{name}-judge",
            "key_secret_name": f"ai-{name}-key", "retries": 0, **extra}


def config(*providers, **ai) -> Config:
    return Config({"ai": {"enabled": True, "providers": list(providers),
                          "features": ["extract", "names", "lead_summary"],
                          "max_cost_usd": 5.0, **ai}})


class Recorder:
    """Records failures like a FailureLog."""

    def __init__(self):
        self.entries: list[tuple[str, str, str]] = []

    def record(self, source, reason, **kw):
        self.entries.append((source, reason, kw.get("detail", "")))

    def reasons(self) -> list[str]:
        return [r for _, r, _ in self.entries]


def by_host(answers: dict):
    """Responder: answers[host] is a callable(request) or a fixed (status, payload)."""
    def responder(req):
        host = req.url.split("/")[2]
        answer = answers[host]
        return answer(req) if callable(answer) else answer
    return responder


def ok(text: str = '{"ok": true}', tokens=(1000, 100)):
    return 200, chat_response(text, tokens=tokens)


@pytest.fixture
def keys(monkeypatch):
    for name in ("first", "second", "pricey"):
        monkeypatch.setenv(f"LEADSCRAPER_SECRET_AI_{name.upper()}_KEY", f"sk-{name}")


def calls_to(adapter, host: str) -> int:
    return sum(1 for r in adapter.requests if host in r.url)


# --- the chain -----------------------------------------------------------------------------

def test_a_failing_provider_falls_back_to_the_next(monkeypatch, keys):
    install_http(monkeypatch, FakeHTTP(by_host({
        "api.first.com": (500, {"error": "down"}), "api.second.com": ok()})))
    failures = Recorder()
    client = AIClient(resolve(config(remote("first"), remote("second"))), failures=failures)
    result = client.call("summary", system="s", user="u", output=Verdict)
    assert result is not None and result.data.ok and result.provider == "second"
    assert "ai_call_failed" in failures.reasons()
    rows = {(r["provider"], r["feature"]): r for r in client.ledger.rows()}
    assert rows[("first", "lead_summary")]["failures"] == 1
    assert rows[("second", "lead_summary")]["calls"] == 1
    # An erroring provider stays in the chain for the next call (it may recover).
    assert [p.name for p in client.chain] == ["first", "second"]


def test_quota_drops_a_provider_for_the_rest_of_the_run(monkeypatch, keys):
    adapter = install_http(monkeypatch, FakeHTTP(by_host({
        "api.first.com": (402, {"error": {"message": "insufficient credits"}}),
        "api.second.com": ok()})))
    failures = Recorder()
    client = AIClient(resolve(config(remote("first"), remote("second"))), failures=failures)
    for _ in range(3):
        assert client.call("summary", system="s", user="u2", output=Verdict) is not None
    assert calls_to(adapter, "api.first.com") == 1, "kept calling a provider out of credit"
    assert "ai_provider_dropped" in failures.reasons()
    assert "quota" in client.unavailable["first"]


def test_when_every_provider_is_exhausted_ai_stops_and_says_why(monkeypatch, keys):
    install_http(monkeypatch, FakeHTTP(lambda r: (401, {"error": "bad key"})))
    failures = Recorder()
    client = AIClient(resolve(config(remote("first"), remote("second"))), failures=failures)
    assert client.call("summary", system="s", user="u", output=Verdict) is None
    assert client.shut_down and not client.active
    assert "ai_disabled" in failures.reasons()
    assert "every AI provider is out of use" in client.status()


def test_repeated_errors_trip_the_circuit_breaker(monkeypatch, keys):
    adapter = install_http(monkeypatch, FakeHTTP(lambda r: (503, {"error": "busy"})))
    client = AIClient(resolve(config(remote("first"), failure_limit=2)))
    client.call("summary", system="s", user="a", output=Verdict)
    client.call("summary", system="s", user="b", output=Verdict)
    assert client.shut_down and "consecutive" in client.stats.shutdown_reason
    before = len(adapter.requests)
    assert client.call("summary", system="s", user="c", output=Verdict) is None
    assert len(adapter.requests) == before


def test_a_refusal_skips_the_item_not_the_provider(monkeypatch, keys):
    answers = iter([chat_response("", refusal="no"), chat_response('{"ok": true}')])
    install_http(monkeypatch, FakeHTTP(lambda r: (200, next(answers))))
    failures = Recorder()
    client = AIClient(resolve(config(remote("first"))), failures=failures)
    assert client.call("summary", system="s", user="a", output=Verdict) is None
    assert client.call("summary", system="s", user="b", output=Verdict) is not None
    assert "ai_refused" in failures.reasons() and client.active


# --- schema validation ---------------------------------------------------------------------

def test_invalid_output_is_retried_once_then_skipped(monkeypatch, keys):
    adapter = install_http(monkeypatch, FakeHTTP(lambda r: (200, chat_response(
        '{"ok": "maybe-ish"}'))))
    failures = Recorder()
    client = AIClient(resolve(config(remote("first"))), failures=failures)
    assert client.call("summary", system="s", user="u", output=Verdict) is None
    assert len(adapter.requests) == 2                         # one retry, no more
    retry_prompt = adapter.requests[1].body["messages"][1]["content"]
    assert "previous answer was not valid" in retry_prompt
    assert failures.reasons() == ["ai_invalid_output"]


def test_invalid_then_valid_output_is_used(monkeypatch, keys):
    answers = iter(["not json at all", '{"ok": false}'])
    install_http(monkeypatch, FakeHTTP(lambda r: (200, chat_response(next(answers)))))
    client = AIClient(resolve(config(remote("first"))))
    result = client.call("summary", system="s", user="u", output=Verdict)
    assert result is not None and result.data.ok is False


# --- budget ------------------------------------------------------------------------------------

def test_the_budget_cap_stops_ai_before_a_call_would_cross_it(monkeypatch, keys):
    adapter = install_http(monkeypatch, FakeHTTP(lambda r: ok(tokens=(1000, 100))))
    prices = {"pricey": {"pricey-worker": {"input": 10.0, "output": 50.0}}}
    failures = Recorder()
    # Each call: 1000 in x $10/M + 100 out x $50/M = $0.015. Cap: $0.04.
    client = AIClient(resolve(config(remote("pricey"), max_cost_usd=0.04, prices=prices)),
                      failures=failures)
    answered = [client.call("summary", system="s" * 40, user=f"u{i}", output=Verdict)
                for i in range(10)]
    made = sum(1 for a in answered if a is not None)
    assert 1 <= made < 10 and len(adapter.requests) == made
    assert client.stats.cost_usd <= 0.04 + 1e-9, "spent past the hard cap"
    assert client.shut_down and "budget" in client.stats.shutdown_reason
    assert "ai_disabled" in failures.reasons()


def test_unknown_models_are_priced_high_not_free(keys):
    settings = resolve(config({"name": "openai", "worker_model": "gpt-unknown-9"}))
    from leadscraper.ai.providers import make_spec

    rate_in, rate_out, listed = settings.prices.rates(make_spec("openai"), "gpt-unknown-9")
    assert not listed and rate_in >= 1.0 and rate_out >= 1.0
    local = make_spec(local_provider())
    assert settings.prices.rates(local, "anything")[:2] == (0.0, 0.0)   # local = free


def test_prices_are_configurable(keys):
    from leadscraper.ai.providers import make_spec

    settings = resolve(config(prices={"anthropic": {"claude-haiku-4-5": {"input": 7,
                                                                          "output": 9}}}))
    assert settings.prices.rates(make_spec("anthropic"), "claude-haiku-4-5")[:2] == (7.0, 9.0)
    assert settings.prices.cost(make_spec("anthropic"), "claude-haiku-4-5",
                                1_000_000, 0, searches=1000) == pytest.approx(7.0 + 10.0)


# --- cache ------------------------------------------------------------------------------------

def test_the_cache_answers_repeated_calls_for_free(monkeypatch, keys, tmp_path):
    adapter = install_http(monkeypatch, FakeHTTP(lambda r: ok()))
    client = AIClient(resolve(config(remote("first"))), cache_path=tmp_path / "c.sqlite3")
    first = client.call("summary", system="s", user="same", output=Verdict)
    second = client.call("summary", system="s", user="same", output=Verdict)
    assert first.data == second.data and second.cached
    assert len(adapter.requests) == 1 and client.stats.cache_hits == 1
    row = client.ledger.rows()[0]
    assert row["calls"] == 1 and row["cache_hits"] == 1
    client.close()
    # Keyed by provider kind + model + schema + prompt: another model asks again.
    other = AIClient(resolve(config(remote("first", worker_model="other"))),
                     cache_path=tmp_path / "c.sqlite3")
    other.call("summary", system="s", user="same", output=Verdict)
    assert len(adapter.requests) == 2
    other.close()


def test_the_pre_phase_5_cache_still_answers_claude_prompts(tmp_path, monkeypatch):
    """Legacy cache keys (model + prompt) stay valid for the Claude providers."""
    from leadscraper.ai.cache import ResponseCache
    from leadscraper.ai.providers.claude_code_cli import ClaudeCodeCLIProvider
    from leadscraper.ai.providers import make_spec

    from ai_fakes import fake_cli

    path = tmp_path / "ai_cache.sqlite3"
    old = ResponseCache(path)
    old.put(ResponseCache.legacy_key("s", "u", "claude-haiku-4-5"), '{"ok": true}')
    old.close()
    calls: list = []
    provider = ClaudeCodeCLIProvider(make_spec("claude_code_cli"), run=fake_cli({}, calls=calls))
    client = AIClient(resolve(config("claude_code_cli")), cache_path=path, providers=[provider])
    result = client.call("summary", system="s", user="u", output=Verdict)
    assert result is not None and result.cached and calls == []
    client.close()


# --- ledger ------------------------------------------------------------------------------------

def test_the_ledger_writes_deltas_per_provider_model_feature(monkeypatch, keys):
    from sqlalchemy import select

    from app_db import fresh_application_db
    from leadscraper.db.models import AIUsage
    from leadscraper.services.workspace import default_workspace

    engine = fresh_application_db()
    ws = default_workspace(engine)
    install_http(monkeypatch, FakeHTTP(lambda r: ok(tokens=(500, 50))))
    client = AIClient(resolve(config(remote("first"))))
    client.call("summary", system="s", user="a", output=Verdict)
    client.call("names", system="s", user="b", output=Verdict)
    assert client.ledger.flush(engine, ws.id, None, unattached=True) == 2
    assert client.ledger.flush(engine, ws.id, None, unattached=True) == 0   # nothing new
    client.call("summary", system="s", user="c", output=Verdict)
    client.ledger.flush(engine, ws.id, None, unattached=True)
    with engine.connect() as conn:
        rows = conn.execute(select(AIUsage.__table__)).mappings().all()
    totals: dict = {}
    for row in rows:
        key = (row["provider"], row["model"], row["feature"])
        totals[key] = totals.get(key, 0) + row["calls"]
        assert row["input_tokens"] > 0 and row["cost_usd"] > 0
    assert totals == {("first", "first-worker", "lead_summary"): 2,
                      ("first", "first-worker", "normalisation"): 1}
    engine.dispose()


# --- settings: where the chain comes from, and the safety catches ----------------------------

def test_the_legacy_presets_keep_the_subscription_backend():
    from leadscraper.config.legacy import Config as Loaded

    for preset in ("presets/mumbai-event-vendors.yaml", "presets/nashik-venues.yaml"):
        cfg = Loaded.load(preset)
        settings = resolve(cfg)
        assert [s.kind for s in settings.specs] == ["claude_code_cli"], preset
        assert settings.specs[0].worker_model == "claude-haiku-4-5"
        assert settings.cap == 5.0
        assert "names" in settings.tasks and "extract" in settings.tasks
        assert "summary" not in settings.tasks                 # new features stay opt-in


def test_a_stray_api_key_never_starts_billing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray")
    settings = resolve(Config({"ai": {"enabled": True, "backend": "auto"}}))
    assert [s.kind for s in settings.specs] == ["claude_code_cli"]
    # Named, but with the legacy safety catch off: refused, and why.
    named = resolve(Config({"ai": {"enabled": True, "providers": ["anthropic"],
                                   "allow_billed_api": False}}))
    assert named.specs == [] and "bills separately" in named.skipped["anthropic"]


def test_the_workspace_chain_applies_when_a_config_names_none():
    settings = resolve(Config({"ai": {"enabled": True}}),
                       {"providers": ["gemini", "openai"]})
    assert [s.name for s in settings.specs] == ["gemini", "openai"]
    assert settings.chain_source == "workspace"
    own = resolve(Config({"ai": {"enabled": True, "providers": ["anthropic"]}}),
                  {"providers": ["gemini"]})
    assert [s.name for s in own.specs] == ["anthropic"]


def test_externally_credited_models_are_never_selected():
    settings = resolve(Config({"ai": {"enabled": True, "providers": [
        {"name": "anthropic", "worker_model": "claude-fable-5-1",
         "judge_model": "claude-mythos-5-1"}]}}))
    spec = settings.specs[0]
    assert "fable" not in spec.worker_model and "mythos" not in spec.judge_model


def test_run_config_schema_validates_ai_settings():
    from pydantic import ValidationError

    from leadscraper.config.schema import AISettings

    AISettings.model_validate({"enabled": True, "features": ["normalisation", "extract"],
                               "providers": ["anthropic", {"name": "groq",
                                                           "kind": "openai_compatible",
                                                           "base_url": "https://g.example"}]})
    with pytest.raises(ValidationError):
        AISettings.model_validate({"features": ["mind_reading"]})
    with pytest.raises(ValidationError):
        AISettings.model_validate({"providers": ["watson"]})
    with pytest.raises(ValidationError):
        AISettings.model_validate({"providers": [{"name": "mine"}]})   # no kind


def test_keys_never_reach_the_workspace_settings():
    from leadscraper.services.settings import WorkspaceAI

    with pytest.raises(ValueError):
        WorkspaceAI.model_validate({"providers": [{"name": "openai", "api_key": "sk-x"}]})
    WorkspaceAI.model_validate({"providers": [{"name": "openai",
                                               "key_secret_name": "openai-api-key"}]})


def test_web_search_calls_go_only_to_providers_that_search(monkeypatch, keys):
    adapter = install_http(monkeypatch, FakeHTTP(lambda r: ok()))
    client = AIClient(resolve(config(remote("first"))))           # compatible: no search
    assert client.call("summary", system="s", user="u", output=Verdict,
                       web_search=True) is None
    assert adapter.requests == []
    assert json.dumps(client.spend())                              # spend is serialisable


def test_a_key_is_never_sent_to_an_endpoint_it_does_not_belong_to():
    """No login (A.2.2): a provider entry can't borrow another endpoint's key."""
    from leadscraper.ai.providers import make_spec

    attacks = [
        {"name": "x", "kind": "openai_compatible", "base_url": "https://evil.example/v1",
         "key_secret_name": "anthropic-api-key"},
        {"name": "anthropic", "base_url": "https://evil.example", },     # default key, new URL
        {"name": "x", "kind": "openai_compatible", "base_url": "https://evil.example/v1",
         "key_secret_name": "notify-smtp-password"},
    ]
    for entry in attacks:
        with pytest.raises(ValueError):
            make_spec(entry)
    # A custom secret is bound to its endpoint by name: ai-<site>-...
    with pytest.raises(ValueError):
        make_spec({"name": "x", "kind": "openai_compatible", "base_url": "https://evil.com/v1",
                   "key_secret_name": "ai-groq-key"})        # groq's key, evil's URL
    with pytest.raises(ValueError):
        make_spec({"name": "x", "kind": "openai_compatible", "base_url": "https://groq.evil.com",
                   "key_secret_name": "ai-groq-key"})        # the site is "evil"
    # Env variables come from the kind's defaults only, never from config.
    spec = make_spec({"name": "x", "kind": "openai_compatible", "base_url": "https://e.com",
                      "key_env": ["ANTHROPIC_API_KEY"], "key_secret_name": "ai-e-key"})
    assert spec.key_env == []
    # The legitimate cases still work.
    assert make_spec("anthropic").key_secret_name == "anthropic-api-key"
    assert make_spec({"name": "groq", "kind": "openai_compatible",
                      "base_url": "https://api.groq.com/openai/v1",
                      "key_secret_name": "ai-groq-key"}).key_secret_name == "ai-groq-key"
    proxied = make_spec({"name": "anthropic", "base_url": "https://claude-proxy.com",
                         "key_secret_name": "ai-claude-proxy-key"})
    assert proxied.base_url and proxied.key_env == []            # not ANTHROPIC_API_KEY
    assert make_spec("anthropic").key_env == ["ANTHROPIC_API_KEY"]


def test_concurrent_calls_cannot_overshoot_the_cap(monkeypatch, keys):
    """Review finding: the cap check and the spend were not atomic across threads."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    def slow(req):
        """Usage as an API reports it: the prompt's real size, a full answer."""
        time.sleep(0.05)
        prompt = len(json.dumps(req.body["messages"])) // 4 + 20
        return ok(tokens=(prompt, req.body["max_tokens"]))

    install_http(monkeypatch, FakeHTTP(slow))
    prices = {"pricey": {"pricey-worker": {"input": 10.0, "output": 50.0}}}
    client = AIClient(resolve(config(remote("pricey"), max_cost_usd=0.06, prices=prices)))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: client.call("summary", system="s", user=f"u{i}",
                                            output=Verdict, max_tokens=200), range(16)))
    assert client.stats.cost_usd <= 0.06 + 1e-9, client.stats.cost_usd


def test_a_nearly_spent_budget_shortens_the_answer(monkeypatch, keys):
    adapter = install_http(monkeypatch, FakeHTTP(lambda r: ok(tokens=(10, 10))))
    prices = {"pricey": {"pricey-worker": {"input": 0.0, "output": 1000.0}}}
    client = AIClient(resolve(config(remote("pricey"), max_cost_usd=1.0, prices=prices)))
    client.call("summary", system="s", user="u", output=Verdict, max_tokens=4000)
    # $1 at $1000 per million output tokens buys 1000 tokens, not 4000.
    assert adapter.requests[0].body["max_tokens"] == 1000


def test_invalid_output_counts_towards_the_circuit_breaker(monkeypatch, keys):
    install_http(monkeypatch, FakeHTTP(lambda r: (200, chat_response("nonsense"))))
    client = AIClient(resolve(config(remote("first"), failure_limit=2)))
    client.call("summary", system="s", user="a", output=Verdict)
    client.call("summary", system="s", user="b", output=Verdict)
    assert client.shut_down and "consecutive" in client.stats.shutdown_reason
