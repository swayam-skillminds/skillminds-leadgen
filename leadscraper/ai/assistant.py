"""The local_business AI tasks, over the Phase 5 client (``ai.client``).

Before Phase 5 this module was the whole AI layer: two backends, a cache and a
budget. Those now live in ``ai/providers``, ``ai/cache.py`` and ``ai/client.py``;
what stays here is :class:`AIAssistant`, the four batch tasks the
``local_business`` pipeline has always called (contact extraction, name
polish, venue classification, duplicate adjudication), with their prompts
unchanged byte for byte -- so the Mumbai and Nashik response caches stay valid.

The pre-Phase-5 names (``AIBackend``, ``ClaudeCLIBackend``, ``pick_backend``,
``safe_model``, ``QuotaExhausted`` ...) are kept: an ``AIBackend`` passed to
:class:`AIAssistant` becomes a provider of its own (tests use stub backends).

Design rules, unchanged:
  * AI is OFF by default and every call degrades to None/{} on failure.
  * Results are cached, so re-runs never pay twice.
  * A hard USD budget disables the layer once reached.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable, Union

from pydantic import BaseModel, Field

from .cache import ResponseCache  # noqa: F401  (re-exported)
from .client import AIClient, ClientStats
from .providers.base import AIRequest, AIResponse, Provider, ProviderError, ProviderSpec
from .providers.base import QuotaExhausted  # noqa: F401  (re-exported)
from .providers.claude_code_cli import QUOTA_MARKERS as _QUOTA_MARKERS  # noqa: F401
from .providers.claude_code_cli import is_quota_error
from .schema import parse_json as _parse_json
from .settings import (  # noqa: F401  (re-exported)
    BLOCKED_MODEL_FAMILIES, DEFAULT_JUDGE_MODEL, DEFAULT_MODEL, ResolvedAI, resolve, safe_model,
)

log = logging.getLogger(__name__)

#: Kept for callers that used it as the AIStats type.
AIStats = ClientStats


def strip_json_fence(text: str) -> str:
    import re

    return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text or "", flags=re.M).strip()


def parse_json(text: str) -> Any | None:
    """Best-effort JSON parse of a model response. None if it isn't JSON."""
    return _parse_json(text)


# --- the pre-Phase-5 backend interface -------------------------------------------

@dataclass
class AIResult:
    text: str
    cost_usd: float = 0.0
    model: str = ""


class AIBackend(ABC):
    """A completion backend of the original layer: ``complete(system, user, model=)``."""

    name = "backend"
    bills_separately = False

    @abstractmethod
    def complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        """Run one completion. Raises on failure; callers handle it."""

    @property
    def available(self) -> bool:
        return True


class LegacyBackendProvider(Provider):
    """An ``AIBackend`` speaking the Phase 5 provider interface."""

    kind = "legacy"

    def __init__(self, backend: AIBackend, *, worker_model: str = DEFAULT_MODEL,
                 judge_model: str = DEFAULT_JUDGE_MODEL):
        super().__init__(ProviderSpec(name=getattr(backend, "name", "backend"), kind="legacy",
                                      worker_model=worker_model, judge_model=judge_model))
        self.backend = backend
        self.bills_separately = bool(getattr(backend, "bills_separately", False))

    def problem(self) -> str | None:
        return None if self.backend.available else f"backend {self.name} unavailable"

    def complete(self, request: AIRequest) -> AIResponse:
        try:
            result = self.backend.complete(request.system, request.user, model=request.model,
                                           max_tokens=request.max_tokens)
        except QuotaExhausted:
            raise
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("backend timed out") from exc
        except Exception as exc:
            # Some CLI/network failures only reveal the quota in their text.
            if is_quota_error(repr(exc)):
                raise QuotaExhausted(f"allowance reached ({str(exc)[:120]})") from exc
            raise ProviderError(repr(exc)[:300]) from exc
        return AIResponse(text=result.text or "", model=result.model or request.model,
                          reported_cost=float(result.cost_usd or 0.0))


class ClaudeCLIBackend(AIBackend):
    """The Claude Code CLI as an ``AIBackend`` (now ``providers.claude_code_cli``)."""

    name = "claude_cli"
    bills_separately = False

    def __init__(self, timeout: int = 180, binary: str = "claude"):
        from .providers.claude_code_cli import ClaudeCodeCLIProvider
        from .providers.registry import make_spec

        spec = make_spec({"name": "claude_code_cli", "timeout": timeout, "binary": binary})
        self._provider = ClaudeCodeCLIProvider(spec)
        self.binary = binary

    @property
    def available(self) -> bool:
        from shutil import which

        return which(self.binary) is not None

    def complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        response = self._provider.complete(AIRequest(system=system, user=user, model=model,
                                                     max_tokens=max_tokens))
        return AIResult(text=response.text, cost_usd=response.reported_cost or 0.0, model=model)


class AnthropicAPIBackend(AIBackend):
    """The Anthropic API as an ``AIBackend`` (now ``providers.anthropic``)."""

    name = "anthropic_api"
    bills_separately = True

    @property
    def available(self) -> bool:
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            log.info("ANTHROPIC_API_KEY is set but the `anthropic` package isn't installed")
            return False
        return True

    def complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        from .prices import PriceTable
        from .providers.anthropic import AnthropicProvider
        from .providers.registry import make_spec

        spec = make_spec("anthropic")
        response = AnthropicProvider(spec).complete(
            AIRequest(system=system, user=user, model=model, max_tokens=max_tokens))
        cost = PriceTable().cost(spec, model, response.input_tokens, response.output_tokens)
        return AIResult(text=response.text, cost_usd=cost, model=model)


def pick_backend(preferred: str = "claude_cli", timeout: int = 180, *,
                 allow_billed: bool = False) -> AIBackend | None:
    """Choose a pre-Phase-5 backend. A billed one only with ``allow_billed``."""
    api, cli = AnthropicAPIBackend(), ClaudeCLIBackend(timeout=timeout)
    candidates = {"anthropic_api": api, "claude_cli": cli}
    if preferred in candidates:
        backend = candidates[preferred]
        if backend.bills_separately and not allow_billed:
            log.warning("backend %r bills separately from your Claude Code subscription; "
                        "refusing to use it because ai.allow_billed_api is false", preferred)
            return None
        return backend if backend.available else None
    order = (cli, api) if not allow_billed else (api, cli)
    for backend in order:
        if backend.bills_separately and not allow_billed:
            continue
        if backend.available:
            return backend
    return None


# --- what the tasks return (validated; ids may come back as numbers) -------------

Id = Union[str, int]


class ExtractedPage(BaseModel):
    id: Id
    business_name: str | None = None
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    address: str | None = None
    instagram: str | None = None


class ExtractBatch(BaseModel):
    items: list[ExtractedPage] = Field(default_factory=list)


class NameVerdict(BaseModel):
    id: Id
    business_name: str | None = None
    is_business: bool | None = None
    matches_category: bool | None = None


class NameBatch(BaseModel):
    items: list[NameVerdict] = Field(default_factory=list)


class VenueVerdict(BaseModel):
    id: Id
    is_venue: bool | None = None
    venue_type: str | None = None
    managed_by: str | None = None
    event_friendly: bool | None = None


class VenueBatch(BaseModel):
    items: list[VenueVerdict] = Field(default_factory=list)


class DuplicateVerdict(BaseModel):
    id: Id
    same_business: bool = False
    confidence: float = 0.0
    reason: str = ""


class DuplicateBatch(BaseModel):
    items: list[DuplicateVerdict] = Field(default_factory=list)


# --- the assistant ---------------------------------------------------------------

class AIAssistant:
    """High-level AI operations for the local_business pipeline.

    Every public method returns a plain value -- never raises -- so callers can
    treat AI as a bonus rather than a dependency.
    """

    def __init__(self, config, failures=None, backend: AIBackend | None = None, *,
                 client: AIClient | None = None):
        self.config = config
        self.failures = failures
        if client is not None:
            self.client = client
            self._owns_client = False
            if client.failures is None:        # the run's client: report into our log
                client.failures = failures
        else:
            settings = resolve(config)
            providers = None
            if backend is not None:
                first = settings.specs[0] if settings.specs else None
                providers = [LegacyBackendProvider(
                    backend,
                    worker_model=safe_model(config.get("ai.model") or
                                            (first.worker_model if first else None),
                                            DEFAULT_MODEL),
                    judge_model=safe_model(config.get("ai.judge_model") or
                                           (first.judge_model if first else None),
                                           DEFAULT_JUDGE_MODEL))]
            cache_path = (config.path_for("ai_cache", "data/ai_cache.sqlite3")
                          if settings.enabled else None)
            self.client = AIClient(settings, failures=failures, cache_path=cache_path,
                                   providers=providers)
            self._owns_client = True
        self.batch_size = self.client.settings.batch_size
        if self.client.settings.enabled and not self.client.enabled:
            log.warning("AI is enabled but no provider is usable (%s). Continuing without AI.",
                        "; ".join(f"{k}: {v}" for k, v in self.client.unavailable.items())
                        or "none configured")

    # --- the client's state, under the pre-Phase-5 names ----------------------------

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    @property
    def active(self) -> bool:
        return self.client.active

    @property
    def backend(self) -> Provider | None:
        return self.client.first()

    @property
    def model(self) -> str:
        first = self.client.first()
        if first is not None and first.spec.worker_model:
            return first.spec.worker_model
        return safe_model(self.config.get("ai.model"), DEFAULT_MODEL)

    @property
    def judge_model(self) -> str:
        first = self.client.first()
        if first is not None and first.spec.judge_model:
            return first.spec.judge_model
        return safe_model(self.config.get("ai.judge_model"), DEFAULT_JUDGE_MODEL)

    @property
    def max_cost(self) -> float:
        return self.client.cap

    @property
    def over_budget(self) -> bool:
        return self.client.over_budget

    @property
    def features(self) -> set[str]:
        return set(self.client.settings.features)

    @property
    def stats(self) -> ClientStats:
        return self.client.stats

    @property
    def quota_exhausted(self) -> bool:
        return self.client.shut_down

    def uses(self, feature: str) -> bool:
        return self.client.uses(feature)

    def _prompt(self, template: str) -> str:
        """A system prompt for this run's city and profile.

        The templates are written for the Mumbai vendor run and come back
        byte-identical for it, so that run's response cache stays valid.
        """
        from ..core.country import country

        info = country(str(self.config.get("country") or "IN"))
        city = str(self.config.get("city") or info.name)
        text = template.replace("Mumbai", city)
        if info.code != "IN":
            text = text.replace(
                "Indian phone numbers may be written with or without +91.",
                f"Phone numbers in {info.name} may be written with or without "
                f"+{info.calling_code}.")
        if str(self.config.get("profile", "vendors") or "vendors").lower() == "venues":
            text = (text.replace("event-industry lead list", "high-end venue list")
                        .replace("event-vendor lead list", "high-end venue list"))
        return text

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    # --- task 1: contact extraction from raw page text --------------------------------

    EXTRACT_SYSTEM = (
        "You extract business contact details from scraped web page text for a "
        "Mumbai event-industry lead list.\n"
        "Input is a JSON array of objects with `id` and `text`.\n"
        "For EACH input object return one output object with keys: id, business_name, "
        "phones (array of strings), emails (array), address (string), instagram "
        "(handle without @, or null).\n"
        "Rules: copy values verbatim from the text -- never invent, complete or guess "
        "a phone number or email. De-obfuscate patterns like 'name AT domain DOT com'. "
        "Use null or an empty array when a field is genuinely absent. Indian phone "
        "numbers may be written with or without +91.\n"
        "Return ONLY a JSON array, no prose and no markdown fence."
    )

    def extract_contacts(self, pages: list[tuple[str, str]]) -> dict[str, dict]:
        """Pull contacts out of page text the selectors couldn't parse.

        `pages` is [(id, page_text), ...]. Returns {id: {fields}} for whatever
        the model could read; ids it couldn't handle are simply absent. The
        caller grounds every value against the page (``ai.grounding``).
        """
        if not self.uses("extract") or not pages:
            return {}
        out: dict[str, dict] = {}
        for chunk in self._chunks(pages):
            payload = [{"id": page_id, "text": (text or "")[:6000]} for page_id, text in chunk]
            result = self.client.call("extract", system=self._prompt(self.EXTRACT_SYSTEM),
                                      user=json.dumps(payload, ensure_ascii=False),
                                      output=ExtractBatch)
            for record in (result.data.items if result else []):
                out[str(record.id)] = {**record.model_dump(), "id": str(record.id)}
        return out

    # --- task 2: business name + category sanity --------------------------------------

    NAME_SYSTEM = (
        "You clean up business names harvested from search-engine result titles "
        "for a Mumbai event-vendor lead list.\n"
        "Input is a JSON array of objects with `id`, `title`, `category`, `locality`.\n"
        "For EACH input return one object with keys: id, business_name, is_business, "
        "matches_category.\n"
        "`business_name`: the actual company/brand name only -- strip taglines, "
        "locality suffixes, SEO filler ('Best', 'Top 10', years, 'Contact Number').\n"
        "`is_business`: false when the title is a generic category or listicle page "
        "rather than one named business (e.g. 'Event Photographer in Mumbai', "
        "'10 Best Caterers') -- in that case set business_name to null.\n"
        "`matches_category`: whether the business plausibly belongs to the given "
        "category.\n"
        "Return ONLY a JSON array, no prose and no markdown fence."
    )

    def clean_names(self, items: list[dict]) -> dict[str, dict]:
        """`items` is [{id, title, category, locality}, ...] -> {id: verdict}."""
        if not self.uses("names") or not items:
            return {}
        out: dict[str, dict] = {}
        for chunk in self._chunks(items):
            result = self.client.call("names", system=self._prompt(self.NAME_SYSTEM),
                                      user=json.dumps(chunk, ensure_ascii=False),
                                      output=NameBatch)
            for record in (result.data.items if result else []):
                out[str(record.id)] = {**record.model_dump(), "id": str(record.id)}
        return out

    # --- task 3: venue classification -------------------------------------------------

    VENUE_SYSTEM = (
        "You classify scraped listings for a high-end venue list around Mumbai: villas, "
        "farmhouses, resorts, hotels and event venues that host stays and functions.\n"
        "Input is a JSON array of objects with `id`, `name`, `source_category`, "
        "`description`, `amenities`, `locality`.\n"
        "For EACH input return one object with keys: id, is_venue, venue_type, "
        "managed_by, event_friendly.\n"
        "`is_venue`: false ONLY when the text shows the listing is something other than "
        "a bookable property or event venue (a travel agent, restaurant, shop, temple, "
        "real-estate project, generic listicle). Too little text to tell is NOT a reason "
        "for false: answer true with venue_type \"Unclassified\".\n"
        "`venue_type`: exactly one of {types}.\n"
        "`managed_by`: the operator brand when the text names one (e.g. StayVista, "
        "SaffronStays, Elivaas, EKOSTAY), else null.\n"
        "`event_friendly`: true if the text says events, parties, weddings or "
        "celebrations are hosted; false if it says they are not; else null.\n"
        "Judge only from the given text -- never invent facts.\n"
        "Return ONLY a JSON array, no prose and no markdown fence."
    )

    def classify_venues(self, items: list[dict], venue_types: Iterable[str]) -> dict[str, dict]:
        """`items` is [{id, name, source_category, description, amenities, locality}]."""
        if not self.uses("venues") or not items:
            return {}
        system = self._prompt(self.VENUE_SYSTEM).replace(
            "{types}", ", ".join(f'"{t}"' for t in venue_types))
        out: dict[str, dict] = {}
        for chunk in self._chunks(items):
            result = self.client.call("venues", system=system,
                                      user=json.dumps(chunk, ensure_ascii=False),
                                      output=VenueBatch)
            for record in (result.data.items if result else []):
                out[str(record.id)] = {**record.model_dump(), "id": str(record.id)}
        return out

    # --- task 4: duplicate adjudication (judge tier) ---------------------------------

    DEDUP_SYSTEM = (
        "You decide whether two scraped listings describe the SAME real business.\n"
        "Input is a JSON array of objects with `id`, `a`, `b` (each a business record).\n"
        "For EACH pair return an object with keys: id, same_business (boolean), "
        "confidence (0-1), reason (short).\n"
        "Two branches of one chain at different addresses are NOT the same business. "
        "The same business listed under slightly different names IS. When genuinely "
        "unsure, answer false -- a wrong merge destroys data, a missed merge only "
        "leaves a duplicate.\n"
        "Return ONLY a JSON array, no prose and no markdown fence."
    )

    def judge_duplicates(self, pairs: list[dict]) -> dict[str, dict]:
        """`pairs` is [{id, a, b}, ...] -> {id: {same_business, confidence, reason}}."""
        if not self.uses("dedup") or not pairs:
            return {}
        out: dict[str, dict] = {}
        for chunk in self._chunks(pairs):
            result = self.client.call("dedup", system=self.DEDUP_SYSTEM,
                                      user=json.dumps(chunk, ensure_ascii=False),
                                      output=DuplicateBatch, tier="judge", max_tokens=16000)
            for record in (result.data.items if result else []):
                out[str(record.id)] = {**record.model_dump(), "id": str(record.id)}
        return out

    # --- helpers ------------------------------------------------------------------

    def _chunks(self, items: list):
        for start in range(0, len(items), self.batch_size):
            yield items[start:start + self.batch_size]

    @staticmethod
    def _as_records(parsed: Any) -> list[dict]:
        """Models sometimes wrap the array in an object -- accept both."""
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, list):
                    return [r for r in value if isinstance(r, dict)]
            return [parsed]
        return []

    def report(self) -> str:
        stats = self.client.stats
        if not (stats.calls or stats.cache_hits):
            return ""
        billed = self.client.billed()
        basis = ("billed to your API account(s)" if billed
                 else "drawn from your Claude Code subscription or a local model -- "
                      "not a separate bill")
        cap = self.client.cap
        lines = [
            f"  AI calls               : {stats.calls}"
            f" (cache hits {stats.cache_hits}, failures {stats.failures})",
            f"  AI usage               : ${stats.cost_usd:.3f} of "
            + ("no cap" if cap == float("inf") else f"${cap:.2f} budget") + f" ({basis})",
            f"  AI time                : {stats.seconds / 60:.1f} min",
        ]
        by_provider = self.client.ledger.by("provider")
        if by_provider:
            lines.append("  AI by provider         : " + ", ".join(
                f"{name} ${row['cost_usd']:.3f} ({row['calls']} calls)"
                for name, row in by_provider.items()))
        if stats.per_task:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(stats.per_task.items()))
            lines.append(f"  AI calls by task       : {detail}")
        if stats.shutdown_reason:
            lines.append(f"  AI stopped early       : {stats.shutdown_reason}")
            lines.append("                           (scraping continued without AI)")
        return "\n".join(lines)
