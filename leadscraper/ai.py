"""Optional AI layer.

Two backends, chosen automatically:

  * ``claude_cli``  -- shells out to the Claude Code CLI in ``--print`` mode.
    Uses your Claude Code SUBSCRIPTION, so no API key is needed. Each
    invocation spawns a process (~10s), so this backend is only economic in
    batches -- see ``batch_size`` in config.yaml.
  * ``anthropic_api`` -- the official SDK, used when ANTHROPIC_API_KEY is set.
    Far faster and cheaper per call; preferred when a key is available.

Design rules, because this bolts onto a scraper that must not crash:
  * AI is OFF by default and every call degrades to None on failure.
  * Results are cached in SQLite, so re-runs never pay twice.
  * A hard USD budget disables the layer once exceeded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Any

log = logging.getLogger(__name__)

# Bulk extraction/classification is exactly the "worker" role Haiku is for.
DEFAULT_MODEL = "claude-haiku-4-5"
# Used for the few genuinely judgement-heavy calls (duplicate adjudication).
DEFAULT_JUDGE_MODEL = "claude-opus-5"

# Model families that are billed as EXTRA credits rather than drawn from a
# normal Claude Code subscription allowance. Never selected, whatever config says.
BLOCKED_MODEL_FAMILIES = ("fable", "mythos")

# Text that means "you are out of allowance", not "this request was bad".
_QUOTA_MARKERS = (
    "usage limit", "usage_limit", "rate limit", "rate_limit", "quota",
    "limit reached", "limit exceeded", "out of credits", "insufficient credits",
    "too many requests", "upgrade your plan", "resets at", "try again later",
    "429",
)


class QuotaExhausted(RuntimeError):
    """The subscription allowance is spent. Stop asking; fall back to non-AI."""


def is_quota_error(*texts: str) -> bool:
    """True when any of `texts` looks like an allowance/rate-limit message."""
    blob = " ".join(t for t in texts if t).lower()
    return any(marker in blob for marker in _QUOTA_MARKERS)


def safe_model(requested: str, fallback: str) -> str:
    """Refuse externally-credited model families, whatever the config says."""
    name = (requested or "").strip().lower()
    if not name:
        return fallback
    if any(family in name for family in BLOCKED_MODEL_FAMILIES):
        log.warning(
            "model %r is in an externally-credited family (%s) -- using %r instead",
            requested, "/".join(BLOCKED_MODEL_FAMILIES), fallback,
        )
        return fallback
    return requested


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def _as_number(value, default, cast):
    """Cast a config value, treating only None/blank/invalid as 'use default'.

    Deliberately NOT `value or default` -- a legitimate 0 must survive.
    """
    if value is None or value == "":
        return cast(default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        return cast(default)


def strip_json_fence(text: str) -> str:
    """Models wrap JSON in markdown fences even when told not to."""
    return _FENCE.sub("", text or "").strip()


def parse_json(text: str) -> Any | None:
    """Best-effort JSON parse of a model response. None if it isn't JSON."""
    cleaned = strip_json_fence(text)
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} or [...] span.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


# --- backends --------------------------------------------------------------

@dataclass
class AIResult:
    text: str
    cost_usd: float = 0.0
    model: str = ""


class AIBackend(ABC):
    name = "backend"
    #: True when using this backend produces a bill SEPARATE from any
    #: Claude Code subscription. Checked before every run.
    bills_separately = False

    @abstractmethod
    def complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        """Run one completion. Raises on failure; callers handle it."""

    @property
    def available(self) -> bool:
        return True


class ClaudeCLIBackend(AIBackend):
    """Claude Code CLI in --print mode. Subscription auth, no API key.

    Consumes your Claude Code subscription allowance -- the same bucket your
    interactive sessions use. It does NOT create a separate invoice.
    """

    name = "claude_cli"
    bills_separately = False

    def __init__(self, timeout: int = 180, binary: str = "claude"):
        self.timeout = timeout
        self.binary = binary

    @property
    def available(self) -> bool:
        from shutil import which
        return which(self.binary) is not None

    def complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        command = [
            self.binary, "-p",
            "--system-prompt", system,
            "--model", model,
            "--output-format", "json",
            # Strip Claude Code's own tooling: without these each call drags in
            # ~30k tokens of tools/skills context and costs ~8x more.
            "--disallowed-tools", "*",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--exclude-dynamic-system-prompt-sections",
        ]
        completed = subprocess.run(
            command,
            input=user,              # via stdin: page text can exceed argv limits
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if completed.returncode != 0:
            if is_quota_error(completed.stderr, completed.stdout):
                raise QuotaExhausted(completed.stderr.strip()[:200] or "usage limit reached")
            raise RuntimeError(
                f"claude CLI exited {completed.returncode}: {completed.stderr[:300]}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            if is_quota_error(completed.stdout, completed.stderr):
                raise QuotaExhausted("usage limit reported by the CLI")
            raise RuntimeError(f"claude CLI returned non-JSON: {completed.stdout[:200]}")
        if payload.get("is_error"):
            detail = str(payload.get("result") or payload)[:300]
            if is_quota_error(detail, payload.get("subtype", "")):
                raise QuotaExhausted(detail)
            raise RuntimeError(f"claude CLI reported an error: {detail}")
        return AIResult(
            text=payload.get("result", "") or "",
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            model=model,
        )


class AnthropicAPIBackend(AIBackend):
    """Official SDK. Used when ANTHROPIC_API_KEY (or an auth token) is present.

    This is pay-as-you-go API billing, SEPARATE from a Claude Code
    subscription. It is never selected unless explicitly configured.
    """

    name = "anthropic_api"
    bills_separately = True

    # Published rates, $ per million tokens, for local cost accounting.
    PRICES = {
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-sonnet-5": (2.0, 10.0),
        "claude-opus-5": (5.0, 25.0),
    }

    def __init__(self):
        self._client = None

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

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        import anthropic

        try:
            return self._complete(system, user, model=model, max_tokens=max_tokens)
        except anthropic.RateLimitError as exc:
            raise QuotaExhausted(str(exc)[:200]) from exc

    def _complete(self, system: str, user: str, *, model: str, max_tokens: int = 4000) -> AIResult:
        response = self._get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        rate_in, rate_out = self.PRICES.get(model, (0.0, 0.0))
        cost = (response.usage.input_tokens * rate_in
                + response.usage.output_tokens * rate_out) / 1_000_000
        return AIResult(text=text, cost_usd=cost, model=model)


def pick_backend(
    preferred: str = "claude_cli",
    timeout: int = 180,
    *,
    allow_billed: bool = False,
) -> AIBackend | None:
    """Choose a backend.

    `allow_billed` is the safety catch: unless it is explicitly True, a
    backend that bills separately from the Claude Code subscription is never
    selected -- not even by "auto", and not even when an API key happens to
    be sitting in the environment. Spending money must be an explicit choice.
    """
    api, cli = AnthropicAPIBackend(), ClaudeCLIBackend(timeout=timeout)
    candidates = {"anthropic_api": api, "claude_cli": cli}

    if preferred in candidates:
        backend = candidates[preferred]
        if backend.bills_separately and not allow_billed:
            log.warning(
                "backend %r bills separately from your Claude Code subscription; "
                "refusing to use it because ai.allow_billed_api is false", preferred
            )
            return None
        return backend if backend.available else None

    # "auto": subscription first, and only fall through to the billed API
    # when the user has explicitly allowed it.
    order = (cli, api) if not allow_billed else (api, cli)
    for backend in order:
        if backend.bills_separately and not allow_billed:
            continue
        if backend.available:
            return backend
    return None


# --- cache -----------------------------------------------------------------

class ResponseCache:
    """Content-addressed cache so repeated runs never pay for the same call."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_cache ("
            " key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        self._conn.commit()
        self.hits = 0

    @staticmethod
    def key_for(system: str, user: str, model: str) -> str:
        digest = hashlib.sha256(f"{model}\x00{system}\x00{user}".encode()).hexdigest()
        return digest[:48]

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response FROM ai_cache WHERE key = ?", (key,)
            ).fetchone()
        if row:
            self.hits += 1
            return row[0]
        return None

    def put(self, key: str, response: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_cache (key, response, created_at) VALUES (?, ?, ?)",
                (key, response, time.time()),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- assistant -------------------------------------------------------------

@dataclass
class AIStats:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    per_task: dict[str, int] = field(default_factory=dict)
    shutdown_reason: str = ""


class AIAssistant:
    """High-level AI operations for the scraper.

    Every public method returns a plain value or None -- never raises -- so
    callers can treat AI as a bonus rather than a dependency.
    """

    def __init__(self, config, failures=None, backend: AIBackend | None = None):
        self.config = config
        self.failures = failures
        self.enabled = bool(config.get("ai.enabled", False))
        self.model = safe_model(config.get("ai.model", DEFAULT_MODEL), DEFAULT_MODEL)
        self.judge_model = safe_model(
            config.get("ai.judge_model", DEFAULT_JUDGE_MODEL), DEFAULT_JUDGE_MODEL
        )
        # `or <default>` is wrong for numerics: a configured 0 is falsy, so
        # `--ai-budget 0` would silently become the default and spend money.
        self.batch_size = max(1, _as_number(config.get("ai.batch_size"), 12, int))
        # None / "unlimited" -> no cap: run until the subscription allowance
        # itself runs out, which the quota detector then handles.
        raw_budget = config.get("ai.max_cost_usd", 5.0)
        if raw_budget is None or str(raw_budget).strip().lower() in ("", "none", "unlimited"):
            self.max_cost = float("inf")
        else:
            self.max_cost = max(0.0, _as_number(raw_budget, 5.0, float))
        self.features = set(config.get("ai.features", []) or [])
        self.stats = AIStats()
        self._budget_warned = False
        self.allow_billed = bool(config.get("ai.allow_billed_api", False))
        #: Set once the subscription allowance is spent -- AI then stays off
        #: for the rest of the run and the scraper continues without it.
        self.quota_exhausted = False
        self._consecutive_failures = 0
        #: Give up on AI entirely after this many back-to-back failures, so a
        #: broken backend can't cost hours of process spawns.
        self._failure_limit = _as_number(config.get("ai.failure_limit"), 4, int)

        self.backend = backend
        if self.enabled and self.backend is None:
            self.allow_billed = bool(config.get("ai.allow_billed_api", False))
            self.backend = pick_backend(
                config.get("ai.backend", "claude_cli") or "claude_cli",
                timeout=_as_number(config.get("ai.timeout"), 180, int),
                allow_billed=self.allow_billed,
            )
            if self.backend is None:
                log.warning(
                    "AI is enabled but no backend is usable: install the `claude` CLI "
                    "or set ANTHROPIC_API_KEY. Continuing without AI."
                )
                self.enabled = False

        self.cache = ResponseCache(config.path_for("ai_cache", "data/ai_cache.sqlite3")) \
            if self.enabled else None

    # --- plumbing ---------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(
            self.enabled
            and self.backend is not None
            and not self.over_budget
            and not self.quota_exhausted
        )

    @property
    def over_budget(self) -> bool:
        return self.stats.cost_usd >= self.max_cost

    def uses(self, feature: str) -> bool:
        return self.active and (not self.features or feature in self.features)

    def _prompt(self, template: str) -> str:
        """A system prompt for this run's city and profile.

        The templates are written for the Mumbai vendor run and come back
        byte-identical for it, so that run's response cache stays valid.
        """
        city = str(self.config.get("city", "Mumbai") or "Mumbai")
        text = template.replace("Mumbai", city)
        if str(self.config.get("profile", "vendors") or "vendors").lower() == "venues":
            text = (text.replace("event-industry lead list", "high-end venue list")
                        .replace("event-vendor lead list", "high-end venue list"))
        return text

    def _call(self, system: str, user: str, *, task: str, model: str | None = None) -> Any | None:
        """One cached, budgeted, failure-tolerant completion returning parsed JSON."""
        if not self.active:
            if self.over_budget and not self._budget_warned:
                self._budget_warned = True
                log.warning("AI budget of $%.2f reached -- AI features disabled for the rest "
                            "of this run", self.max_cost)
            return None

        model = model or self.model
        key = ResponseCache.key_for(system, user, model)
        cached = self.cache.get(key) if self.cache else None
        if cached is not None:
            self.stats.cache_hits += 1
            return parse_json(cached)

        started = time.time()
        try:
            result = self.backend.complete(system, user, model=model)
        except QuotaExhausted as exc:
            self._shutdown(f"subscription allowance reached ({str(exc)[:120]})")
            return None
        except subprocess.TimeoutExpired:
            self._fail(task, "ai_timeout", "backend timed out")
            self._note_failure(task)
            return None
        except Exception as exc:
            # Some CLI/network failures only reveal the quota in their text.
            if is_quota_error(repr(exc)):
                self._shutdown(f"subscription allowance reached ({str(exc)[:120]})")
                return None
            self._fail(task, "ai_call_failed", repr(exc))
            self._note_failure(task)
            return None
        finally:
            self.stats.seconds += time.time() - started

        self._consecutive_failures = 0
        self.stats.calls += 1
        self.stats.cost_usd += result.cost_usd
        self.stats.per_task[task] = self.stats.per_task.get(task, 0) + 1

        parsed = parse_json(result.text)
        if parsed is None:
            self._fail(task, "ai_bad_json", (result.text or "")[:200])
            return None
        if self.cache:
            self.cache.put(key, result.text)
        return parsed

    def _shutdown(self, why: str) -> None:
        """Turn AI off for the rest of the run and carry on without it."""
        if self.quota_exhausted:
            return
        self.quota_exhausted = True
        self.stats.shutdown_reason = why
        log.warning("AI disabled for the rest of this run: %s. "
                    "Scraping continues in non-AI mode.", why)
        if self.failures is not None:
            self.failures.record("ai", "ai_disabled", detail=why[:300])

    def _note_failure(self, task: str) -> None:
        """Trip the circuit breaker after repeated back-to-back failures."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_limit:
            self._shutdown(
                f"{self._consecutive_failures} consecutive AI failures (last task: {task})"
            )

    def _fail(self, task: str, reason: str, detail: str) -> None:
        self.stats.failures += 1
        log.debug("AI %s failed (%s): %s", task, reason, detail[:150])
        if self.failures is not None:
            self.failures.record("ai", reason, detail=f"{task}: {detail}"[:400])

    def close(self) -> None:
        if self.cache:
            self.cache.close()

    # --- task 1: contact extraction from raw page text --------------------

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
        the model could read; ids it couldn't handle are simply absent.
        """
        if not self.uses("extract") or not pages:
            return {}

        out: dict[str, dict] = {}
        for chunk in self._chunks(pages):
            payload = [
                {"id": page_id, "text": (text or "")[:6000]}
                for page_id, text in chunk
            ]
            parsed = self._call(
                self._prompt(self.EXTRACT_SYSTEM),
                json.dumps(payload, ensure_ascii=False),
                task="extract",
            )
            for record in self._as_records(parsed):
                record_id = str(record.get("id", ""))
                if record_id:
                    out[record_id] = record
        return out

    # --- task 2: business name + category sanity ---------------------------

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
            parsed = self._call(
                self._prompt(self.NAME_SYSTEM),
                json.dumps(chunk, ensure_ascii=False),
                task="names",
            )
            for record in self._as_records(parsed):
                record_id = str(record.get("id", ""))
                if record_id:
                    out[record_id] = record
        return out

    # --- task 3: venue classification ---------------------------------------

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
            parsed = self._call(system, json.dumps(chunk, ensure_ascii=False), task="venues")
            for record in self._as_records(parsed):
                record_id = str(record.get("id", ""))
                if record_id:
                    out[record_id] = record
        return out

    # --- task 4: duplicate adjudication -----------------------------------

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
            parsed = self._call(
                self.DEDUP_SYSTEM,
                json.dumps(chunk, ensure_ascii=False),
                task="dedup",
                model=self.judge_model,
            )
            for record in self._as_records(parsed):
                record_id = str(record.get("id", ""))
                if record_id:
                    out[record_id] = record
        return out

    # --- helpers ----------------------------------------------------------

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
        stats = self.stats
        if not (stats.calls or stats.cache_hits):
            return ""
        billed = self.backend.bills_separately if self.backend else False
        basis = ("billed to your API account" if billed
                 else "drawn from your Claude Code subscription -- not a separate bill")
        lines = [
            f"  AI calls               : {stats.calls}"
            f" (cache hits {stats.cache_hits}, failures {stats.failures})",
            f"  AI usage               : ${stats.cost_usd:.3f} of "
            + ("no cap" if self.max_cost == float("inf") else f"${self.max_cost:.2f} budget")
            + f" ({basis})",
            f"  AI time                : {stats.seconds / 60:.1f} min"
            f" via {self.backend.name if self.backend else 'n/a'}",
        ]
        if stats.per_task:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(stats.per_task.items()))
            lines.append(f"  AI calls by task       : {detail}")
        if stats.shutdown_reason:
            lines.append(f"  AI stopped early       : {stats.shutdown_reason}")
            lines.append("                           (scraping continued without AI)")
        return "\n".join(lines)
