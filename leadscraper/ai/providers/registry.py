"""Provider kinds, their defaults (``ai/defaults.yaml``), and specs from config.

A provider entry in config is either a kind's name (``anthropic``) or a mapping::

    {name: groq, kind: openai_compatible, base_url: https://api.groq.com/openai/v1,
     worker_model: ..., judge_model: ..., key_secret_name: groq-api-key,
     structured_output: true}

Missing fields come from the kind's defaults. The legacy backend names
(``claude_cli``, ``anthropic_api``) are aliases.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .anthropic import AnthropicProvider
from .base import Capabilities, Provider, ProviderSpec
from .claude_code_cli import ClaudeCodeCLIProvider
from .gemini import GeminiProvider
from .openai import OpenAIProvider
from .openai_compatible import OpenAICompatibleProvider

KINDS: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "claude_code_cli": ClaudeCodeCLIProvider,
}
ALIASES = {"claude_cli": "claude_code_cli", "anthropic_api": "anthropic",
           "claude": "anthropic", "google": "gemini"}
_CAPABILITY_KEYS = ("structured_output", "tool_use", "web_search", "context_tokens")
_SPEC_KEYS = ("worker_model", "judge_model", "base_url", "key_secret_name", "key_env",
              "timeout", "retries", "enabled")
DEFAULTS_FILE = Path(__file__).resolve().parent.parent / "defaults.yaml"


@lru_cache(maxsize=1)
def _defaults() -> dict[str, Any]:
    with open(DEFAULTS_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def defaults() -> dict[str, Any]:
    return copy.deepcopy(_defaults())


def kind_of(name: str | None) -> str | None:
    key = str(name or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in KINDS else None


def make_spec(entry: str | dict[str, Any]) -> ProviderSpec:
    """A full spec from a config entry, filled from the kind's defaults."""
    data = {"name": entry} if isinstance(entry, str) else dict(entry)
    kind = kind_of(data.get("kind") or data.get("name"))
    if kind is None:
        known = ", ".join(KINDS)
        raise ValueError(f"unknown AI provider {data.get('kind') or data.get('name')!r} "
                         f"(known: {known}; aliases: {', '.join(ALIASES)})")
    base = defaults().get("providers", {}).get(kind, {})
    name = str(data.get("name") or kind)
    if kind_of(name) == kind:
        name = kind                          # "claude_cli" is the claude_code_cli entry
    merged = {k: base.get(k) for k in _SPEC_KEYS if base.get(k) is not None}
    merged.update({k: data[k] for k in _SPEC_KEYS if data.get(k) is not None})
    capabilities = dict(base.get("capabilities") or {})
    capabilities.update({k: data[k] for k in _CAPABILITY_KEYS if data.get(k) is not None})
    options = {k: v for k, v in data.items()
               if k not in (*_SPEC_KEYS, *_CAPABILITY_KEYS, "name", "kind")}
    return ProviderSpec(
        name=name, kind=kind,
        worker_model=merged.get("worker_model"), judge_model=merged.get("judge_model"),
        base_url=merged.get("base_url"), key_secret_name=merged.get("key_secret_name"),
        key_env=list(merged.get("key_env") or []),
        capabilities=Capabilities(**{k: capabilities[k] for k in _CAPABILITY_KEYS
                                     if k in capabilities}),
        timeout=float(merged.get("timeout") or 120.0),
        retries=int(merged["retries"]) if merged.get("retries") is not None else 2,
        enabled=bool(merged.get("enabled", True)), options=options)


def build(spec: ProviderSpec, **kwargs: Any) -> Provider:
    return KINDS[spec.kind](spec, **kwargs)
