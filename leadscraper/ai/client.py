"""The run's AI client: fallback chain, budget, cache, ledger, schema validation.

One :class:`AIClient` serves a whole run (every lead type shares its budget).
Every feature calls :meth:`AIClient.call` with a task name, a prompt and the
pydantic model the answer must satisfy, and gets a :class:`CallResult` -- or
None, in which case it carries on exactly as it would without AI.

What a call goes through:

1. **Gate.** AI on, the task's feature switched on, the client not shut down.
2. **Cache.** Provider kind + model + schema + prompt hash; hits cost nothing.
3. **Budget.** The call's worst case (prompt, the answer at its ``max_tokens``
   -- clamped to what is left -- and its searches) is held against the run's
   hard cap before the call, under a lock; when it can't fit, AI stops for the
   rest of the run. Spend is the providers' reported usage at the table's rates.
4. **Chain.** Providers in order. One out of credit, with refused credentials or
   unconfigured is dropped for the run; one that errors is skipped for this call;
   a safety refusal skips the item. When none is left, AI stops and the run
   continues without it.
5. **Validation.** The answer is parsed into the pydantic model. Invalid output
   is retried once (same provider, told what was wrong), then skipped.

Nothing here raises to the caller.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from .cache import ResponseCache
from .catalog import feature_of
from .ledger import UsageLedger
from .prices import estimate_tokens
from .providers.base import (
    AIRequest, AIResponse, AuthFailed, NotConfigured, Provider, ProviderError, QuotaExhausted,
    Refused,
)
from .providers.registry import build
from .schema import strict_schema, validate
from .settings import ResolvedAI, bills_separately

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

#: The smallest answer worth asking for: below it, the budget is spent.
_MIN_ANSWER = 256
#: Tokens a provider adds to every call (roles, formatting).
_CALL_OVERHEAD = 50

_RETRY_NOTE = ("\n\nYour previous answer was not valid ({why}). Answer again with ONLY the "
               "JSON object the instructions describe -- no prose, no markdown fence.")


@dataclass
class CallResult(Generic[M]):
    data: M
    text: str
    provider: str
    model: str
    sources: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    cached: bool = False


@dataclass
class ClientStats:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    per_task: dict[str, int] = field(default_factory=dict)
    shutdown_reason: str = ""


class AIClient:
    def __init__(self, settings: ResolvedAI, *, failures: Any = None,
                 cache_path: str | Path | None = None,
                 providers: list[Provider] | None = None,
                 provider_kwargs: dict[str, Any] | None = None,
                 on_spend: Callable[[dict[str, Any]], None] | None = None):
        self.settings = settings
        self.failures = failures
        self.enabled = bool(settings.enabled)
        self.tasks = settings.tasks
        self.cap = settings.cap
        self.prices = settings.prices
        self.ledger = UsageLedger()
        self.stats = ClientStats()
        self.on_spend = on_spend
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        #: Spend held by calls in flight (see _reserve).
        self._reserved = 0.0
        self._budget_warned = False
        self.shut_down = False
        #: Providers left out of the chain, and why (shown by `ai_status`).
        self.unavailable: dict[str, str] = dict(settings.skipped)
        self.chain: list[Provider] = []
        if self.enabled:
            candidates = providers if providers is not None else [
                build(spec, **(provider_kwargs or {}).get(spec.kind, {}))
                for spec in settings.specs]
            for provider in candidates:
                why = provider.problem()
                if why:
                    self.unavailable[provider.name] = why
                    log.info("AI provider %s not used: %s", provider.name, why)
                else:
                    self.chain.append(provider)
            if not self.chain:
                self.enabled = False
        self.cache = ResponseCache(cache_path) if (self.enabled and cache_path) else None

    # --- state ------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.enabled and not self.shut_down and bool(self.chain) and not self.over_budget

    @property
    def spent(self) -> float:
        return self.stats.cost_usd

    @property
    def over_budget(self) -> bool:
        return self.stats.cost_usd >= self.cap

    def uses(self, task: str) -> bool:
        return self.active and task in self.tasks

    def wants(self, task: str) -> bool:
        """The task is switched on (whether or not AI is usable right now)."""
        return self.enabled and task in self.tasks

    def first(self) -> Provider | None:
        return self.chain[0] if self.chain else None

    def can_search(self) -> bool:
        return any(p.capabilities.web_search for p in self.chain)

    def spend(self) -> dict[str, Any]:
        cap = None if self.cap == float("inf") else self.cap
        return {"cost_usd": round(self.stats.cost_usd, 4), "cap_usd": cap,
                "calls": self.stats.calls}

    # --- the call --------------------------------------------------------------------------

    def call(self, task: str, *, system: str, user: str, output: type[M],
             tier: str = "worker", web_search: bool = False, max_tokens: int = 4000,
             max_searches: int = 3) -> CallResult[M] | None:
        if not self.uses(task):
            self._warn_budget()
            return None
        schema = strict_schema(output)
        schema_name = output.__name__.lower()
        chain = [p for p in self.chain if p.capabilities.web_search] if web_search \
            else list(self.chain)
        if not chain:
            return None

        if self.cache is not None and not web_search:
            first = chain[0]
            model = first.spec.model_for(tier) or ""
            cached = self.cache.lookup(first.spec.kind, model, system, user, schema_name)
            if cached is not None:
                data, _ = validate(output, cached)
                if data is not None:
                    with self._lock:
                        self.stats.cache_hits += 1
                    self.ledger.add(first.name, model, feature_of(task), cache_hits=1)
                    return CallResult(data=data, text=cached, provider=first.name,
                                      model=model, cached=True)

        answered_by_none = True
        for provider in chain:
            if provider not in self.chain:      # dropped by another thread meanwhile
                continue
            model = provider.spec.model_for(tier)
            if not model:
                continue
            request = AIRequest(system=system, user=user, model=model, schema=schema,
                                schema_name=schema_name, max_tokens=max_tokens,
                                web_search=web_search, max_searches=max_searches)
            outcome = self._attempt(task, provider, request, output)
            if outcome is _BUDGET:
                return None
            if outcome is _DROPPED or outcome is _SKIP:
                continue
            answered_by_none = False
            if outcome is _REFUSED or outcome is _INVALID:
                return None
            data, response, cost = outcome
            if self.cache is not None and not web_search:
                key = self.cache.key_for(provider.spec.kind, model, system, user, schema_name)
                self.cache.put(key, response.text)
            return CallResult(data=data, text=response.text, provider=provider.name,
                              model=response.model or model, sources=list(response.sources),
                              cost_usd=cost)
        if answered_by_none:
            self._note_failure(task)
        if not self.chain:
            self._shutdown("every AI provider is out of use (quota or allowance reached, "
                           "or credentials refused)")
        return None

    def _attempt(self, task: str, provider: Provider, request: AIRequest, output):
        """(data, response, cost), or a sentinel: _DROPPED, _SKIP, _REFUSED, _INVALID, _BUDGET."""
        feature = feature_of(task)
        why = ""
        for attempt in (1, 2):
            reserved = self._reserve(provider, request)
            if reserved is None:
                return _BUDGET if attempt == 1 else _INVALID
            request, held = reserved
            started = time.time()
            try:
                response = provider.complete(request)
            except (NotConfigured, AuthFailed, QuotaExhausted) as exc:
                self._drop(provider, task, exc)
                return _DROPPED
            except Refused as exc:
                self._fail(task, "ai_refused", f"{provider.name}: {exc}")
                self.ledger.add(provider.name, request.model, feature, failures=1)
                return _REFUSED
            except ProviderError as exc:
                self._fail(task, "ai_call_failed", f"{provider.name}: {exc}")
                self.ledger.add(provider.name, request.model, feature, failures=1)
                return _SKIP
            except Exception as exc:              # a provider bug: contained like an error
                self._fail(task, "ai_call_failed", f"{provider.name}: {exc!r}")
                self.ledger.add(provider.name, request.model, feature, failures=1)
                return _SKIP
            finally:
                with self._lock:
                    self.stats.seconds += time.time() - started
                    self._reserved -= held
            cost = self._account(task, provider, request, response)
            data, why = validate(output, response.text)
            if data is not None:
                with self._lock:
                    self._consecutive_failures = 0
                return data, response, cost
            if attempt == 1 and self.uses(task):
                request = AIRequest(**{**request.__dict__,
                                       "user": request.user + _RETRY_NOTE.format(why=why)})
                continue
            break
        self._fail(task, "ai_invalid_output", f"{provider.name}: {why}")
        self.ledger.add(provider.name, request.model, feature, failures=1)
        self._note_failure(task)                  # a model that never answers validly
        return _INVALID

    def _account(self, task: str, provider: Provider, request: AIRequest,
                 response: AIResponse) -> float:
        cost = response.reported_cost if response.reported_cost is not None else \
            self.prices.cost(provider.spec, request.model, response.input_tokens,
                             response.output_tokens, response.searches)
        with self._lock:
            self.stats.calls += 1
            self.stats.cost_usd += cost
            self.stats.per_task[task] = self.stats.per_task.get(task, 0) + 1
        self.ledger.add(provider.name, request.model, feature_of(task), calls=1,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        searches=response.searches, cost_usd=cost)
        if self.on_spend is not None:
            try:
                self.on_spend(self.spend())
            except Exception as exc:            # a progress sink never breaks a call
                log.debug("AI spend sink failed: %s", exc)
        return cost

    def _reserve(self, provider: Provider, request: AIRequest) -> tuple[AIRequest, float] | None:
        """Hold the call's worst case against the cap, or stop AI when it can't fit.

        Worst case = the prompt (estimated, with a margin) + the answer at its
        ``max_tokens`` + its web searches. The hold is taken under the lock, so
        calls on several worker threads can't all pass against the same spend;
        an answer the budget can't cover in full gets a smaller ``max_tokens``
        (but never below a useful minimum). Actual cost replaces the hold.
        """
        if self.cap == float("inf"):
            return request, 0.0
        rate_in, rate_out, _ = self.prices.rates(provider.spec, request.model)
        # The prompt, the schema (providers bill it as input) and a per-call
        # overhead, with a 30 % margin over the ~4-characters-a-token estimate.
        schema = json.dumps(request.schema) if request.schema else ""
        tokens_in = int((estimate_tokens(request.system) + estimate_tokens(request.user)
                         + estimate_tokens(schema) + _CALL_OVERHEAD) * 1.3)
        searches = request.max_searches if request.web_search else 0
        fixed = tokens_in * rate_in / 1_000_000 + searches * self.prices.search_rate(provider.spec)
        floor = min(_MIN_ANSWER, request.max_tokens)
        with self._lock:
            room = self.cap - self.stats.cost_usd - self._reserved - fixed
            answer = request.max_tokens if rate_out <= 0 else \
                min(request.max_tokens, int(max(0.0, room) * 1_000_000 / rate_out))
            fits = room >= 0 and answer >= floor
            if fits:
                held = fixed + answer * rate_out / 1_000_000
                self._reserved += held
        if not fits:
            self._shutdown(f"budget of ${self.cap:.2f} reached (spent "
                           f"${self.stats.cost_usd:.3f}; the next call could cost "
                           f"~${fixed + floor * rate_out / 1_000_000:.3f})")
            return None
        if answer < request.max_tokens:
            request = AIRequest(**{**request.__dict__, "max_tokens": answer})
        return request, held

    # --- failure handling -----------------------------------------------------------

    def _drop(self, provider: Provider, task: str, exc: Exception) -> None:
        reason = {NotConfigured: "not configured", AuthFailed: "credentials refused",
                  QuotaExhausted: "quota or allowance reached"}.get(type(exc), "unusable")
        with self._lock:
            if provider in self.chain:
                self.chain.remove(provider)
            self.unavailable[provider.name] = f"{reason}: {str(exc)[:150]}"
        log.warning("AI provider %s dropped for this run (%s): %s", provider.name, reason,
                    str(exc)[:150])
        if self.failures is not None:
            self.failures.record("ai", "ai_provider_dropped",
                                 detail=f"{provider.name}: {reason}: {str(exc)[:200]}")

    def _note_failure(self, task: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            tripped = self._consecutive_failures >= self.settings.failure_limit
        if tripped:
            self._shutdown(f"{self._consecutive_failures} consecutive AI failures "
                           f"(last task: {task})")

    def _fail(self, task: str, reason: str, detail: str) -> None:
        with self._lock:
            self.stats.failures += 1
        log.debug("AI %s failed (%s): %s", task, reason, detail[:150])
        if self.failures is not None:
            self.failures.record("ai", reason, detail=f"{task}: {detail}"[:400])

    def _shutdown(self, why: str) -> None:
        with self._lock:
            if self.shut_down:
                return
            self.shut_down = True
            self.stats.shutdown_reason = why
        log.warning("AI disabled for the rest of this run: %s. Continuing without AI.", why)
        if self.failures is not None:
            self.failures.record("ai", "ai_disabled", detail=why[:300])

    def _warn_budget(self) -> None:
        if self.enabled and self.over_budget and not self._budget_warned:
            self._budget_warned = True
            log.warning("AI budget of $%.2f reached -- AI features off for the rest of this "
                        "run", self.cap)

    # --- reporting ---------------------------------------------------------------------

    def status(self) -> str:
        """Whether AI is on, and if not exactly why: logged at INFO every run."""
        if self.active:
            first = self.chain[0]
            cap = "no cap" if self.cap == float("inf") else f"${self.cap:.2f} cap"
            names = " -> ".join(p.name for p in self.chain)
            return (f"active via {names} (worker {first.spec.worker_model}, judge "
                    f"{first.spec.judge_model}; features: "
                    f"{', '.join(self.settings.features) or 'extract, names, venues'}; {cap})")
        if not self.settings.enabled:
            return "off -- ai.enabled is false in config"
        if self.stats.shutdown_reason:
            return f"off -- {self.stats.shutdown_reason}"
        if not self.chain:
            why = "; ".join(f"{k}: {v}" for k, v in self.unavailable.items()) or \
                "no provider configured"
            return f"off -- no AI provider is usable ({why}); continuing as a non-AI run"
        if self.over_budget:
            return f"off -- the ${self.cap:.2f} budget is already spent"
        return "off"

    def billed(self) -> bool:
        return any(bills_separately(p.spec) and p.bills_separately for p in self.chain)

    def close(self) -> None:
        if self.cache is not None:
            try:
                self.cache.close()
            except Exception:
                pass


class _Sentinel:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


_DROPPED, _SKIP, _REFUSED, _INVALID, _BUDGET = (
    _Sentinel("dropped"), _Sentinel("skip"), _Sentinel("refused"), _Sentinel("invalid"),
    _Sentinel("budget"))
