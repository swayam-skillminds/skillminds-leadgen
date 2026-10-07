"""Google PageSpeed Insights API v5: a site's Lighthouse performance score.

``GET https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=…&strategy=mobile
&category=performance[&key=…]``. Free; the key is optional on paper, but the
shared keyless quota is usually spent (verified 2026-10-07: HTTP 429), so a key
(secret ``pagespeed-api-key``) is what makes this check work. One call takes
10–30 s on Google's side; a run makes at most ``max_per_run``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

log = logging.getLogger(__name__)

ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
RATE_KEY = "pagespeed"
TIMEOUT = 90


@dataclass
class PageSpeedResult:
    score: int
    #: Largest Contentful Paint (ms), Cumulative Layout Shift, Total Blocking Time (ms).
    lcp_ms: float | None = None
    cls: float | None = None
    tbt_ms: float | None = None


class PageSpeed:
    """One run's PageSpeed calls: capped, and switched off for the run on quota or key errors."""

    def __init__(self, http, settings, failures, *, key: str | None = None):
        self.http = http
        self.settings = settings
        self.failures = failures
        self._key = key
        self._lock = threading.Lock()
        self.remaining = int(settings.max_per_run)
        #: Why PageSpeed stopped for this run, once it did.
        self.off_reason: str | None = None
        http.limiter.configure(RATE_KEY, settings.min_delay, settings.max_delay)

    def _take(self) -> bool:
        with self._lock:
            if self.off_reason is not None or self.remaining <= 0:
                return False
            self.remaining -= 1
            return True

    def _off(self, reason: str, detail: str) -> None:
        with self._lock:
            if self.off_reason is not None:
                return
            self.off_reason = reason
        log.warning("PageSpeed off for the rest of the run: %s", detail)
        self.failures.record("pagespeed", reason, detail=detail)

    def score(self, url: str, business: str | None = None) -> PageSpeedResult | None:
        """The page's performance score (0-100), or None (capped, off, or no answer)."""
        if not self._take():
            return None
        params: dict[str, Any] = {"url": url, "strategy": self.settings.strategy,
                                  "category": "performance"}
        if self._key:
            params["key"] = self._key
        request = f"{ENDPOINT}?{urlencode(params)}"
        try:
            self.http.limiter.wait(RATE_KEY)
            response = self.http.session_for("www.googleapis.com").get(request, timeout=TIMEOUT)
        except Exception as exc:
            # The request URL carries the key: report the page, never the request.
            self.failures.record("pagespeed", "pagespeed_failed", url=url, business=business,
                                 detail=type(exc).__name__)
            return None
        status = int(response.status_code)
        if status == 429:
            self._off("pagespeed_quota", "quota exhausted (HTTP 429)"
                      + ("" if self._key else "; set the pagespeed-api-key secret"))
            return None
        if status in (401, 403):
            self._off("pagespeed_key_refused", f"HTTP {status}: the API key was refused")
            return None
        if status != 200:
            self.failures.record("pagespeed", "pagespeed_failed", url=url, business=business,
                                 detail=f"HTTP {status}")
            return None
        try:
            return parse(response.json())
        except Exception as exc:
            self.failures.record("pagespeed", "pagespeed_failed", url=url, business=business,
                                 detail=f"unreadable answer: {exc!r}"[:300])
            return None


def parse(data: dict[str, Any]) -> PageSpeedResult | None:
    lighthouse = data.get("lighthouseResult") or {}
    raw = ((lighthouse.get("categories") or {}).get("performance") or {}).get("score")
    if raw is None:
        return None
    audits = lighthouse.get("audits") or {}

    def metric(name: str) -> float | None:
        value = (audits.get(name) or {}).get("numericValue")
        return round(float(value), 3) if value is not None else None

    return PageSpeedResult(score=int(round(float(raw) * 100)),
                           lcp_ms=metric("largest-contentful-paint"),
                           cls=metric("cumulative-layout-shift"),
                           tbt_ms=metric("total-blocking-time"))
