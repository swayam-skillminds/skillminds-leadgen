"""One tiny live call per AI provider -- opt-in only (``pytest -m live``).

Each test skips unless its provider is configured:

* anthropic / openai / gemini: a key in the secret store or the standard env
  variable (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GEMINI_API_KEY``);
* openai_compatible: ``LEADSCRAPER_LIVE_COMPATIBLE_URL`` (+ ``..._MODEL``,
  optional ``LEADSCRAPER_SECRET_LIVE_COMPATIBLE_KEY``);
* claude_code_cli: ``LEADSCRAPER_LIVE_CLAUDE_CLI=1`` (draws on your subscription).

Each call asks for ``{"ok": true}`` with a 50-token answer cap on the worker
model: well under a cent each, so the whole file stays far below $0.10. The
running total is asserted.
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from leadscraper.ai.client import AIClient
from leadscraper.ai.providers import build, make_spec
from leadscraper.ai.settings import resolve
from leadscraper.config.legacy import Config

pytestmark = pytest.mark.live

BUDGET_USD = 0.10
SPENT = {"usd": 0.0}


class Ping(BaseModel):
    ok: bool


def ping(entry) -> None:
    spec = make_spec(entry)
    provider = build(spec)
    why = provider.problem()
    if why:
        pytest.skip(f"{spec.name}: {why}")
    settings = resolve(Config({"ai": {"enabled": True, "providers": [entry],
                                      "max_cost_usd": BUDGET_USD - SPENT["usd"]}}))
    settings.tasks = frozenset({"connectivity"})
    client = AIClient(settings, providers=[provider])
    result = client.call("connectivity", system="You answer connectivity checks.",
                         user='Reply with exactly {"ok": true}', output=Ping, max_tokens=50)
    SPENT["usd"] += client.stats.cost_usd
    assert result is not None, client.status()
    assert result.data.ok is True
    assert SPENT["usd"] < BUDGET_USD


def test_anthropic_live():
    ping("anthropic")


def test_openai_live():
    ping("openai")


def test_gemini_live():
    ping("gemini")


def test_openai_compatible_live():
    url = os.environ.get("LEADSCRAPER_LIVE_COMPATIBLE_URL")
    if not url:
        pytest.skip("set LEADSCRAPER_LIVE_COMPATIBLE_URL (and _MODEL) to run")
    ping({"name": "live-compatible", "kind": "openai_compatible", "base_url": url,
          "worker_model": os.environ.get("LEADSCRAPER_LIVE_COMPATIBLE_MODEL", "llama3.2"),
          "key_secret_name": "live-compatible-key"})


def test_claude_code_cli_live():
    if os.environ.get("LEADSCRAPER_LIVE_CLAUDE_CLI") != "1":
        pytest.skip("set LEADSCRAPER_LIVE_CLAUDE_CLI=1 to use your subscription")
    ping("claude_code_cli")
