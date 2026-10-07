"""One interface for every AI provider (Phase 5.1).

A provider turns an :class:`AIRequest` into an :class:`AIResponse` or raises one
of the errors below. Everything above it -- the fallback chain, budget, cache,
schema validation and grounding -- lives in :mod:`leadscraper.ai.client`, so a
provider is only the wire format of one API.

Errors say what the chain should do next:

=====================  =========================================================
``NotConfigured``      no key, no package, no binary: skip this provider, quietly
``AuthFailed``         the key was refused (401/403): drop it for the run
``QuotaExhausted``     out of credit or rate-limited after retries: drop it for
                       the run (the original "subscription allowance" case)
``Refused``            the model declined this request: skip the item, keep the
                       provider
``ProviderError``      anything else (5xx, timeouts, bad model id): try the next
                       provider for this call
=====================  =========================================================
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A call failed; another provider may still answer it."""


class NotConfigured(ProviderError):
    """The provider can't be used here at all (no key, package or binary)."""


class AuthFailed(ProviderError):
    """The provider refused our credentials."""


class QuotaExhausted(ProviderError):
    """Out of credit or allowance (or rate-limited past our retries)."""


class Refused(ProviderError):
    """The model declined this request (safety); the provider itself is fine."""


@dataclass(frozen=True)
class Capabilities:
    structured_output: bool = False
    tool_use: bool = False
    web_search: bool = False
    context_tokens: int = 32000

    def as_dict(self) -> dict[str, Any]:
        return {"structured_output": self.structured_output, "tool_use": self.tool_use,
                "web_search": self.web_search, "context_tokens": self.context_tokens}


@dataclass
class ProviderSpec:
    """One configured provider: which API, which models, where its key is."""

    #: The entry's name ("anthropic", or "groq" for an openai_compatible entry).
    name: str
    #: The API it speaks: anthropic | openai | gemini | openai_compatible | claude_code_cli.
    kind: str
    worker_model: str | None = None
    judge_model: str | None = None
    base_url: str | None = None
    #: The secret holding the key (``core.secrets``: env, secret file, keychain).
    key_secret_name: str | None = None
    #: Standard environment variables also accepted for the key (ANTHROPIC_API_KEY).
    key_env: list[str] = field(default_factory=list)
    capabilities: Capabilities = field(default_factory=Capabilities)
    timeout: float = 120.0
    #: Retries on 429/5xx inside one call (with back-off), before giving up on it.
    retries: int = 2
    enabled: bool = True
    #: Anything provider-specific (the CLI's binary name).
    options: dict[str, Any] = field(default_factory=dict)

    def model_for(self, tier: str) -> str | None:
        if tier == "judge":
            return self.judge_model or self.worker_model
        return self.worker_model or self.judge_model

    def public(self) -> dict[str, Any]:
        """What the API may show: never a key, only where one would come from."""
        return {"name": self.name, "kind": self.kind, "worker_model": self.worker_model,
                "judge_model": self.judge_model, "base_url": self.base_url,
                "key_secret_name": self.key_secret_name, "enabled": self.enabled,
                "capabilities": self.capabilities.as_dict()}


@dataclass
class AIRequest:
    system: str
    user: str
    model: str
    #: JSON Schema the output must satisfy (strict form, see ``ai.schema``);
    #: None = free text.
    schema: dict[str, Any] | None = None
    schema_name: str = "result"
    max_tokens: int = 4000
    #: Let the model search the web (providers that declare ``web_search``).
    web_search: bool = False
    max_searches: int = 3


@dataclass
class AIResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: Web searches the provider ran (billed per search).
    searches: int = 0
    #: URLs (or, for Gemini, source domains) the provider's web search used, in order.
    sources: list[str] = field(default_factory=list)
    #: Cost the provider reported itself (the CLI does), else computed from prices.
    reported_cost: float | None = None
    #: True when the provider's native structured-output mode produced ``text``.
    structured: bool = False


def is_local_url(url: str | None) -> bool:
    """A base URL on this machine or a private network (Ollama, LM Studio)."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class Provider(ABC):
    """The wire format of one AI API."""

    kind: str = "provider"
    #: True when calls produce a bill of their own (API keys); False for the
    #: Claude Code subscription and local endpoints.
    bills_separately: bool = True

    def __init__(self, spec: ProviderSpec, *, key: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.spec = spec
        self._key = key
        self._sleep = sleep

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def capabilities(self) -> Capabilities:
        return self.spec.capabilities

    # --- configuration ------------------------------------------------------------

    def api_key(self) -> str | None:
        """The key, from the secret store or a standard env variable; never logged."""
        if self._key:
            return self._key
        from ...core.secrets import get_secret

        if self.spec.key_secret_name:
            value = get_secret(self.spec.key_secret_name)
            if value:
                return value
        for env in self.spec.key_env:
            value = os.environ.get(env)
            if value:
                return value
        return None

    def key_source(self) -> str | None:
        """Where the key is configured (env / file / keyring / env:NAME), or None."""
        from ...core.secrets import secret_source

        if self._key:
            return "given"
        if self.spec.key_secret_name:
            source = secret_source(self.spec.key_secret_name)
            if source:
                return source
        for env in self.spec.key_env:
            if os.environ.get(env):
                return f"env:{env}"
        return None

    def needs_key(self) -> bool:
        return True

    def problem(self) -> str | None:
        """Why this provider can't be used here, or None when it can."""
        if not self.spec.enabled:
            return "disabled in config"
        if not self.spec.worker_model and not self.spec.judge_model:
            return "no model configured (worker_model / judge_model)"
        if self.needs_key() and not self.api_key():
            where = self.spec.key_secret_name
            return (f"no key: set secret {where!r} (leadscraper secrets set {where})"
                    if where else "no key configured (key_secret_name)")
        return None

    # --- the call -----------------------------------------------------------------------

    @abstractmethod
    def complete(self, request: AIRequest) -> AIResponse:
        """One completion. Raises one of this module's errors on failure."""

    def _with_retries(self, send: Callable[[], Any],
                      retryable: Callable[[Exception], float | None]) -> Any:
        """Run `send`, retrying while `retryable(exc)` gives a delay (429/5xx)."""
        attempt = 0
        while True:
            try:
                return send()
            except Exception as exc:
                delay = retryable(exc)
                if delay is None or attempt >= self.spec.retries:
                    raise
                attempt += 1
                self._sleep(min(30.0, delay * attempt))


def retry_after(headers: Any, default: float = 2.0) -> float:
    try:
        return max(0.0, float(headers.get("retry-after")))
    except (TypeError, ValueError, AttributeError):
        return default


def describe(status: int, body: Any) -> str:
    """A short error text for logs and failure records: never a key, never a prompt."""
    message = ""
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("status") or error.get("type") or "")
        elif isinstance(error, str):
            message = error
    elif isinstance(body, str):
        message = body
    return f"HTTP {status}: {message[:200]}".strip()
