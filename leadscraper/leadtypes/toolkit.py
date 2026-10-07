"""What a lead type needs to fetch politely, built the way ``local_business`` builds it.

Not a lead type: a core helper every lead type may use (A.3: lead types depend
on core interfaces only). One ``Kit`` per session: the run's rate limiter, an
HTTP client bound to the store's workspace-wide dead-domain cache, a failure
log feeding the run's ``failures`` table, and a robots.txt cache that follows
``runtime.robots_txt`` (the workspace setting, on by default). The client is
public-only: these lead types fetch addresses that users (uploads) or third
parties (links in job ads) supplied, so internal hosts are refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..engine.failures import FailureLog
from ..net.http import HttpClient
from ..net.ratelimit import RateLimiter
from ..net.robots import RobotsCache


@dataclass
class Kit:
    failures: FailureLog
    limiter: RateLimiter
    http: HttpClient
    robots: RobotsCache

    @classmethod
    def build(cls, context: Any) -> "Kit":
        config = context.config
        failures = FailureLog(config.path_for("failure_log", "logs/failures.jsonl"),
                              sink=getattr(context, "failure_sink", None))
        # The run's shared limiter (one pace per site across lead types), else its own.
        limiter = getattr(context, "limiter", None) or RateLimiter()
        store = getattr(context, "store", None)
        hooks = {}
        if store is not None and callable(getattr(store, "is_dead_domain", None)):
            hooks = {"dead_domain_check": store.is_dead_domain,
                     "on_dead_domain": store.mark_dead_domain}
        try:
            accept_language = config.country_info.accept_language
        except Exception:
            accept_language = "en-US,en;q=0.9"
        http = HttpClient(limiter, timeout=int(config.get("runtime.request_timeout", 30)),
                          max_retries=int(config.get("runtime.max_retries", 3)),
                          backoff_base=float(config.get("runtime.backoff_base", 2.0)),
                          accept_language=accept_language, public_only=True, **hooks)
        robots = RobotsCache(http, enabled=config.get("runtime.robots_txt") is not False)
        return cls(failures=failures, limiter=limiter, http=http, robots=robots)

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:
            pass


def lead_type_section(config: Any, key: str, model: type) -> Any:
    """``lead_types.<key>`` of a compiled config, or the model's defaults."""
    run_config = getattr(config, "run_config", None)
    section = run_config.lead_type(key) if run_config is not None else None
    return section if section is not None else model()
