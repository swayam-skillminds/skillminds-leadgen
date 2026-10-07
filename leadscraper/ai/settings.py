"""From config (and workspace settings) to the run's AI set-up (Phase 5.1–5.3).

The provider chain, in order of preference:

1. the run config's ``ai.providers`` (a list of names or entries);
2. else the legacy single ``ai.provider`` (``claude_cli`` / ``anthropic_api``);
3. else the workspace's ``ai.providers`` (``PATCH /settings``);
4. else the Claude Code subscription (``claude_code_cli``) -- and the billed
   Anthropic API before it only when ``ai.allow_billed_api: true``, exactly the
   old "auto".

Spending money stays an explicit choice: a provider that bills separately is
used only when a config or the workspace names it, and never while
``ai.allow_billed_api`` is explicitly ``false`` (the legacy presets set that).
A stray API key in the environment changes nothing.

Model families billed as extra credits (Fable, Mythos) are never selected,
whatever a config says.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .catalog import CONFIG_TIME, tasks_for
from .prices import PriceTable
from .providers.base import ProviderSpec
from .providers.registry import KINDS, make_spec

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_JUDGE_MODEL = "claude-opus-5-5"
#: Billed as EXTRA credits rather than drawn from a normal allowance: never selected.
BLOCKED_MODEL_FAMILIES = ("fable", "mythos")
_CLAUDE_KINDS = ("anthropic", "claude_code_cli")
DEFAULT_ASSIST_BUDGET = 0.50


def safe_model(requested: str | None, fallback: str) -> str:
    name = (requested or "").strip().lower()
    if not name:
        return fallback
    if any(family in name for family in BLOCKED_MODEL_FAMILIES):
        log.warning("model %r is in an externally-credited family (%s) -- using %r instead",
                    requested, "/".join(BLOCKED_MODEL_FAMILIES), fallback)
        return fallback
    return str(requested)


def _number(value: Any, default: float, cast=float):
    """Only None/blank/invalid mean "default": a configured 0 must survive."""
    if value is None or value == "":
        return cast(default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        return cast(default)


def budget_cap(value: Any, default: float | None = 2.0) -> float:
    """USD cap; None / "unlimited" = no cap (until the provider's own quota stops it)."""
    if value is None or str(value).strip().lower() in ("", "none", "unlimited", "inf"):
        return float("inf")
    return max(0.0, _number(value, default if default is not None else 0.0))


def bills_separately(spec: ProviderSpec) -> bool:
    from .providers.base import is_local_url

    if spec.kind == "claude_code_cli":
        return False
    if spec.kind == "openai_compatible":
        return not is_local_url(spec.base_url)
    return True


@dataclass
class ResolvedAI:
    enabled: bool
    specs: list[ProviderSpec] = field(default_factory=list)
    #: Providers named but left out of the chain, and why.
    skipped: dict[str, str] = field(default_factory=dict)
    features: list[str] = field(default_factory=list)
    tasks: frozenset[str] = frozenset()
    cap: float = 2.0
    prices: PriceTable = field(default_factory=PriceTable)
    batch_size: int = 12
    failure_limit: int = 4
    #: Where the chain came from: config | legacy | workspace | default.
    chain_source: str = "default"

    def public(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "providers": [s.public() for s in self.specs],
                "skipped": self.skipped, "features": self.features,
                "cap_usd": None if self.cap == float("inf") else self.cap,
                "chain_source": self.chain_source}


def _get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        node: Any = config
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    return config.get(key, default)


def _chain(config: Any, workspace_ai: dict[str, Any]) -> tuple[list[Any], str]:
    entries = _get(config, "ai.providers")
    if entries:
        return list(entries), "config"
    backend = _get(config, "ai.backend") or _get(config, "ai.provider")
    if backend and str(backend).lower() != "auto":
        return [str(backend)], "legacy"
    if workspace_ai.get("providers"):
        return list(workspace_ai["providers"]), "workspace"
    if _get(config, "ai.allow_billed_api") is True:
        return ["anthropic", "claude_code_cli"], "default"
    return ["claude_code_cli"], "default"


def resolve(config: Any, workspace_ai: dict[str, Any] | None = None) -> ResolvedAI:
    """The run's AI set-up. Never raises: a bad entry is reported in ``skipped``."""
    workspace_ai = dict(workspace_ai or {})
    enabled = bool(_get(config, "ai.enabled", False))
    entries, source = _chain(config, workspace_ai)
    allow_billed = _get(config, "ai.allow_billed_api")
    model = _get(config, "ai.model")
    judge = _get(config, "ai.judge_model")
    timeout = _get(config, "ai.timeout")
    specs: list[ProviderSpec] = []
    skipped: dict[str, str] = {}
    for entry in entries:
        try:
            spec = make_spec(entry)
        except (ValueError, TypeError) as exc:
            skipped[str(entry if isinstance(entry, str) else entry.get("name"))] = str(exc)
            continue
        if spec.kind in _CLAUDE_KINDS:
            if model and (isinstance(entry, str) or not entry.get("worker_model")):
                spec.worker_model = model
            if judge and (isinstance(entry, str) or not entry.get("judge_model")):
                spec.judge_model = judge
            spec.worker_model = safe_model(spec.worker_model, DEFAULT_MODEL)
            spec.judge_model = safe_model(spec.judge_model, DEFAULT_JUDGE_MODEL)
        if timeout is not None and (isinstance(entry, str) or entry.get("timeout") is None):
            spec.timeout = _number(timeout, 120.0)
        if allow_billed is False and bills_separately(spec):
            skipped[spec.name] = ("bills separately from your subscription and "
                                  "ai.allow_billed_api is false")
            continue
        if any(s.name == spec.name for s in specs):
            continue
        specs.append(spec)
    features = [str(f) for f in (_get(config, "ai.features") or [])]
    prices = PriceTable(workspace_ai.get("prices"), _get(config, "ai.prices"))
    return ResolvedAI(
        enabled=enabled, specs=specs, skipped=skipped, features=features,
        tasks=tasks_for(features),
        cap=budget_cap(_get(config, "ai.max_cost_usd", _get(config, "ai.budget_usd", 2.0))),
        prices=prices,
        batch_size=max(1, _number(_get(config, "ai.batch_size"), 12, int)),
        failure_limit=max(1, _number(_get(config, "ai.failure_limit"), 4, int)),
        chain_source=source)


def resolve_assist(workspace_ai: dict[str, Any] | None = None,
                   overrides: dict[str, Any] | None = None) -> ResolvedAI:
    """AI for setting a config up (ICP builder, planner, source discovery).

    The workspace's providers, its ``assist_budget_usd`` per request, and its
    ``assist_features`` (all three by default).
    """
    workspace_ai = dict(workspace_ai or {})
    ai: dict[str, Any] = {"enabled": True,
                          "max_cost_usd": workspace_ai.get("assist_budget_usd",
                                                           DEFAULT_ASSIST_BUDGET),
                          "features": workspace_ai.get("assist_features") or list(CONFIG_TIME)}
    ai.update(overrides or {})
    resolved = resolve({"ai": ai}, workspace_ai)
    return resolved


def known_kinds() -> list[str]:
    return list(KINDS)
