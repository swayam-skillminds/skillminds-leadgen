"""Provider conformance (Phase 5 acceptance): every provider, over mocked HTTP.

Each provider builds its real request (URL, auth header, body) and parses a
response in its API's real shape; errors map onto the chain's error types.
Nothing here reaches a network: requests-based providers use a fake transport
adapter, the Anthropic SDK an httpx2 mock transport, the CLI a fake subprocess.
"""

from __future__ import annotations

import json

import pytest

from leadscraper.ai.providers import (
    AIRequest, AuthFailed, ProviderError, QuotaExhausted, Refused, build, make_spec,
)
from leadscraper.ai.providers.anthropic import AnthropicProvider
from leadscraper.ai.providers.claude_code_cli import ClaudeCodeCLIProvider
from leadscraper.ai.schema import strict_schema
from pydantic import BaseModel

from ai_fakes import (
    FakeHTTP, anthropic_client, anthropic_message, chat_response, fake_cli, gemini_response,
    openai_response, session_with,
)


class Answer(BaseModel):
    ok: bool
    note: str | None = None


SCHEMA = strict_schema(Answer)


def request(**over) -> AIRequest:
    base = {"system": "sys", "user": "hello", "model": "m-1", "schema": SCHEMA,
            "schema_name": "answer", "max_tokens": 200}
    base.update(over)
    return AIRequest(**base)


def http_provider(kind: str, responder, **spec) -> tuple:
    adapter = FakeHTTP(responder)
    entry = {"name": kind, **({"base_url": "https://llm.example/v1"}
                              if kind == "openai_compatible" else {}), **spec}
    provider = build(make_spec(entry), http=session_with(adapter), key="sk-test-key",
                     sleep=lambda s: None)
    return provider, adapter


# --- capabilities --------------------------------------------------------------------------

def test_every_provider_declares_its_capabilities():
    expected = {"anthropic": (True, True), "openai": (True, True), "gemini": (True, True),
                "openai_compatible": (False, False), "claude_code_cli": (False, False)}
    for kind, (structured, search) in expected.items():
        spec = make_spec(kind)
        assert spec.capabilities.structured_output is structured, kind
        assert spec.capabilities.web_search is search, kind
        assert spec.capabilities.context_tokens > 0
    # Model ids and prices come from data, not code: ai/defaults.yaml.
    assert make_spec("anthropic").worker_model == "claude-haiku-4-5"
    assert make_spec({"name": "anthropic", "worker_model": "claude-sonnet-5-5"}).worker_model \
        == "claude-sonnet-5-5"
    assert make_spec("claude_cli").kind == "claude_code_cli"          # legacy alias
    with pytest.raises(ValueError):
        make_spec("watson")


# --- OpenAI (Responses API) -----------------------------------------------------------------

def test_openai_request_and_structured_output():
    provider, adapter = http_provider("openai", lambda r: (200, openai_response(
        '{"ok": true, "note": null}', tokens=(321, 12))))
    response = provider.complete(request(model="gpt-6-luna"))
    sent = adapter.requests[0]
    assert sent.url == "https://api.openai.com/v1/responses"
    assert sent.headers["Authorization"] == "Bearer sk-test-key"
    assert sent.body["model"] == "gpt-6-luna" and sent.body["instructions"] == "sys"
    assert sent.body["input"] == "hello" and sent.body["max_output_tokens"] == 200
    assert sent.body["text"]["format"] == {"type": "json_schema", "name": "answer",
                                           "schema": SCHEMA, "strict": True}
    assert json.loads(response.text) == {"ok": True, "note": None}
    assert (response.input_tokens, response.output_tokens) == (321, 12)
    assert response.structured is True


def test_openai_web_search_returns_sources():
    provider, adapter = http_provider("openai", lambda r: (200, openai_response(
        '{"ok": true}', sources=("https://a.example/x",), citations=("https://b.example/y",))))
    response = provider.complete(request(model="gpt-6-luna", web_search=True))
    body = adapter.requests[0].body
    assert body["tools"] == [{"type": "web_search"}]
    assert body["include"] == ["web_search_call.action.sources"]
    assert "text" not in body                    # JSON by instruction while searching
    assert response.sources == ["https://a.example/x", "https://b.example/y"]
    assert response.searches == 1


@pytest.mark.parametrize("status,body,error,attempts", [
    (401, {"error": {"message": "Incorrect API key", "type": "invalid_request_error"}},
     AuthFailed, 1),
    (429, {"error": {"message": "You exceeded your current quota",
                     "code": "insufficient_quota"}}, QuotaExhausted, 1),
    (429, {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}},
     QuotaExhausted, 3),                                     # retried twice first
    (500, {"error": {"message": "server error"}}, ProviderError, 3),
    (400, {"error": {"message": "model not found"}}, ProviderError, 1),
])
def test_openai_errors_map_onto_the_chain(status, body, error, attempts):
    provider, adapter = http_provider("openai", lambda r: (status, body))
    with pytest.raises(error):
        provider.complete(request(model="gpt-6-luna"))
    assert len(adapter.requests) == attempts


def test_openai_refusal_is_refused():
    provider, _ = http_provider("openai", lambda r: (200, openai_response(refusal="no")))
    with pytest.raises(Refused):
        provider.complete(request(model="gpt-6-luna"))


# --- Gemini (generateContent) ---------------------------------------------------------------

def test_gemini_request_and_structured_output():
    provider, adapter = http_provider("gemini", lambda r: (200, gemini_response(
        '{"ok": true, "note": "x"}', tokens=(50, 10, 7))))
    response = provider.complete(request(model="gemini-3.5-flash-lite"))
    sent = adapter.requests[0]
    assert sent.url == ("https://generativelanguage.googleapis.com/v1beta/models/"
                        "gemini-3.5-flash-lite:generateContent")
    assert sent.headers["x-goog-api-key"] == "sk-test-key"
    assert sent.body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert sent.body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    config = sent.body["generationConfig"]
    assert config["responseMimeType"] == "application/json" and config["maxOutputTokens"] == 200
    schema = config["responseSchema"]
    assert schema["type"] == "OBJECT" and "additionalProperties" not in schema
    assert schema["properties"]["note"] == {"type": "STRING", "nullable": True}
    assert response.output_tokens == 17                       # thinking counts as output
    assert json.loads(response.text)["ok"] is True


def test_gemini_grounding_sources_and_blocks():
    provider, adapter = http_provider("gemini", lambda r: (200, gemini_response(
        "[]", chunks=[("https://vertexaisearch.cloud.google.com/redirect/1", "clinic.example")],
        queries=["dentists austin"])))
    response = provider.complete(request(model="g", web_search=True))
    assert adapter.requests[0].body["tools"] == [{"googleSearch": {}}]
    assert "clinic.example" in response.sources and response.searches == 1
    blocked, _ = http_provider("gemini", lambda r: (200, gemini_response(block="SAFETY")))
    with pytest.raises(Refused):
        blocked.complete(request(model="g"))


@pytest.mark.parametrize("status,error", [(403, AuthFailed), (429, QuotaExhausted),
                                          (503, ProviderError)])
def test_gemini_errors(status, error):
    provider, _ = http_provider("gemini", lambda r: (status, {"error": {
        "code": status, "message": "x", "status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(error):
        provider.complete(request(model="g"))


# --- OpenAI-compatible (Chat Completions) ----------------------------------------------------

def test_openai_compatible_request_shape():
    provider, adapter = http_provider("openai_compatible", lambda r: (200, chat_response(
        '{"ok": true}')), worker_model="llama", structured_output=True)
    response = provider.complete(request(model="llama"))
    sent = adapter.requests[0]
    assert sent.url == "https://llm.example/v1/chat/completions"
    assert sent.body["messages"] == [{"role": "system", "content": "sys"},
                                     {"role": "user", "content": "hello"}]
    assert sent.body["response_format"]["type"] == "json_schema"
    assert sent.body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert (response.input_tokens, response.output_tokens) == (80, 15)


def test_openai_compatible_local_needs_no_key_and_bills_nothing():
    adapter = FakeHTTP(lambda r: (200, chat_response('{"ok": true}')))
    spec = make_spec({"name": "ollama", "kind": "openai_compatible",
                      "base_url": "http://localhost:11434/v1", "worker_model": "llama3"})
    provider = build(spec, http=session_with(adapter))
    assert provider.problem() is None and provider.bills_separately is False
    provider.complete(request(model="llama3", schema=None))
    assert "Authorization" not in adapter.requests[0].headers
    assert "response_format" not in adapter.requests[0].body   # not declared: JSON by prompt
    remote = build(make_spec({"name": "groq", "kind": "openai_compatible",
                              "base_url": "https://api.groq.example/v1", "worker_model": "x"}))
    assert remote.problem().startswith("no key") and remote.bills_separately is True


def test_openai_compatible_errors():
    provider, _ = http_provider("openai_compatible", lambda r: (401, {"error": "bad key"}),
                                worker_model="x")
    with pytest.raises(AuthFailed):
        provider.complete(request(model="x"))
    refusing, _ = http_provider("openai_compatible",
                                lambda r: (200, chat_response("", refusal="cannot")),
                                worker_model="x")
    with pytest.raises(Refused):
        refusing.complete(request(model="x"))


# --- Anthropic (official SDK over a mock transport) -----------------------------------------

def anthropic_provider(handler, **spec) -> AnthropicProvider:
    pytest.importorskip("anthropic")
    return AnthropicProvider(make_spec({"name": "anthropic", "retries": 0, **spec}),
                             key="sk-ant-test", http_client=anthropic_client(handler))


def _response(status, json=None):
    """An httpx2 response (imported lazily: the SDK, and httpx2 with it, is optional)."""
    import httpx2

    return httpx2.Response(status, json=json)


def test_anthropic_request_and_structured_output():
    seen = []

    def handler(req):
        seen.append(req)
        return _response(200, json=anthropic_message('{"ok": true, "note": null}',
                                                           tokens=(222, 11)))

    response = anthropic_provider(handler).complete(request(model="claude-haiku-4-5"))
    sent = seen[0]
    body = json.loads(sent.content)
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "sk-ant-test"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-haiku-4-5" and body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert (response.input_tokens, response.output_tokens) == (222, 11)
    assert json.loads(response.text)["ok"] is True


def test_anthropic_web_search_tool_results_and_fallbacks():
    seen = []

    def handler(req):
        seen.append(req)
        return _response(200, json=anthropic_message(
            '{"ok": true}', model="claude-opus-5-5", search_results=["https://a.example/p"],
            searches=2))

    provider = anthropic_provider(handler)
    response = provider.complete(request(model="claude-opus-5-5", web_search=True))
    body = json.loads(seen[0].content)
    assert body["tools"] == [{"type": "web_search_20260209", "name": "web_search",
                              "max_uses": 3}]
    assert "output_config" not in body
    # Claude Opus 5.5 opts into server-side refusal fallbacks ("default" routing).
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert response.sources == ["https://a.example/p"] and response.searches == 2
    # Haiku 4.5 has no dynamic filtering: the basic tool version.
    haiku = []
    anthropic_provider(lambda r: haiku.append(r) or _response(
        200, json=anthropic_message("{}"))).complete(request(model="claude-haiku-4-5",
                                                             web_search=True))
    assert json.loads(haiku[0].content)["tools"][0]["type"] == "web_search_20250305"


@pytest.mark.parametrize("status,kind,error", [
    (401, "authentication_error", AuthFailed),
    (429, "rate_limit_error", QuotaExhausted),
    (529, "overloaded_error", ProviderError),
    (400, "invalid_request_error", ProviderError),
])
def test_anthropic_errors_map_onto_the_chain(status, kind, error):
    provider = anthropic_provider(lambda r: _response(status, json={
        "type": "error", "error": {"type": kind, "message": "x"}}))
    with pytest.raises(error):
        provider.complete(request(model="claude-haiku-4-5"))


def test_anthropic_refusal_and_low_credit():
    refusing = anthropic_provider(lambda r: _response(200, json=anthropic_message(
        stop_reason="refusal")))
    with pytest.raises(Refused):
        refusing.complete(request(model="claude-haiku-4-5"))
    broke = anthropic_provider(lambda r: _response(400, json={
        "type": "error", "error": {"type": "invalid_request_error",
                                   "message": "Your credit balance is too low"}}))
    with pytest.raises(QuotaExhausted):
        broke.complete(request(model="claude-haiku-4-5"))


def test_anthropic_without_a_key_is_not_configured(monkeypatch):
    provider = AnthropicProvider(make_spec("anthropic"))
    assert provider.problem().startswith("no key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")          # the standard variable
    assert provider.problem() is None and provider.key_source() == "env:ANTHROPIC_API_KEY"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("LEADSCRAPER_SECRET_ANTHROPIC_API_KEY", "sk-ant-secret")
    assert provider.key_source() == "env"                         # the secret store


# --- Claude Code CLI (subscription; a fake subprocess) -----------------------------------------

def test_claude_cli_command_and_result():
    calls: list = []
    run = fake_cli({"result": '{"ok": true}', "total_cost_usd": 0.0042, "is_error": False,
                    "usage": {"input_tokens": 40, "cache_read_input_tokens": 10,
                              "output_tokens": 9}}, calls=calls)
    provider = ClaudeCodeCLIProvider(make_spec("claude_code_cli"), run=run)
    assert provider.problem() is None and provider.bills_separately is False
    response = provider.complete(request(model="claude-haiku-4-5"))
    command = calls[0]["command"]
    assert command[:2] == ["claude", "-p"] and "--disallowed-tools" in command
    assert command[command.index("--model") + 1] == "claude-haiku-4-5"
    assert calls[0]["input"] == "hello"                  # the prompt goes via stdin
    assert "--json-schema" not in command                # off unless declared
    assert response.reported_cost == 0.0042 and response.input_tokens == 50


def test_claude_cli_quota_and_errors():
    quota = ClaudeCodeCLIProvider(make_spec("claude_code_cli"), run=fake_cli(
        returncode=1, stderr="5-hour usage limit reached; resets at 9pm"))
    with pytest.raises(QuotaExhausted):
        quota.complete(request(model="m"))
    failing = ClaudeCodeCLIProvider(make_spec("claude_code_cli"), run=fake_cli(
        {"is_error": True, "result": "something broke"}))
    with pytest.raises(ProviderError):
        failing.complete(request(model="m"))
    structured = ClaudeCodeCLIProvider(
        make_spec({"name": "claude_code_cli", "structured_output": True}),
        run=fake_cli({"result": "", "structured_output": {"ok": True}}))
    assert json.loads(structured.complete(request(model="m")).text) == {"ok": True}
