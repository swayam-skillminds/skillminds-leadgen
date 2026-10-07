"""A configurable price table (Phase 5.1: prices change, so they are data).

Rates are layered: ``ai/defaults.yaml`` <- workspace ``ai.prices`` <- the run
config's ``ai.prices``, each ``{provider_kind_or_name: {model: {input, output},
web_search: {per_1k}}}`` in USD per million tokens. A model nobody lists costs
its provider's ``"*"`` rate -- a deliberately high guess, so an unknown model
can't slip under the budget cap -- and is reported as ``priced: false``.
A local endpoint (Ollama, LM Studio) costs nothing.
"""

from __future__ import annotations

import copy
from typing import Any

from .providers.base import ProviderSpec, is_local_url
from .providers.registry import defaults

#: When even the provider has no "*" rate.
_FALLBACK = {"input": 5.0, "output": 25.0}


def _merge(base: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class PriceTable:
    def __init__(self, *overrides: dict[str, Any] | None):
        table = defaults().get("prices", {})
        for patch in overrides:
            table = _merge(table, patch or {})
        self.table = table

    def _section(self, spec: ProviderSpec) -> dict[str, Any]:
        return {**self.table.get(spec.kind, {}), **self.table.get(spec.name, {})} \
            if spec.name != spec.kind else self.table.get(spec.kind, {})

    def rates(self, spec: ProviderSpec, model: str) -> tuple[float, float, bool]:
        """(input, output) USD per million tokens, and whether the model is listed."""
        if spec.kind == "openai_compatible" and is_local_url(spec.base_url):
            return 0.0, 0.0, True
        section = self._section(spec)
        listed = section.get(model)
        rate = listed or section.get("*") or _FALLBACK
        return float(rate.get("input", 0.0)), float(rate.get("output", 0.0)), bool(listed)

    def search_rate(self, spec: ProviderSpec) -> float:
        """USD per web search."""
        per_1k = (self._section(spec).get("web_search") or {}).get("per_1k", 0.0)
        return float(per_1k) / 1000.0

    def cost(self, spec: ProviderSpec, model: str, tokens_in: int, tokens_out: int,
             searches: int = 0) -> float:
        rate_in, rate_out, _ = self.rates(spec, model)
        return ((tokens_in * rate_in + tokens_out * rate_out) / 1_000_000
                + searches * self.search_rate(spec))


def estimate_tokens(text: str) -> int:
    """~4 characters a token: good enough to project a call's cost before making it."""
    return max(1, len(text or "") // 4)
