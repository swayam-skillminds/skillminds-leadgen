"""robots.txt for generic website crawling (A.2.4: honoured, toggle, on by default).

Used by the website enricher before it fetches a business's pages. Each
host's robots.txt is fetched once per run with one paced request on the
host's own rate key (``HttpClient.get_once``: no retries, no penalty), and
read with the standard library's parser for the ``*`` group -- our requests
identify as a browser, so the generic rules are the ones that apply.

Status handling follows RFC 9309:

* 2xx -- the rules apply;
* 4xx (404, 410, ...) -- there are no rules: everything is allowed;
* 5xx -- the site is unavailable: assume everything is disallowed (this run);
* the request fails outright -- allowed; the page fetch will fail on its own
  and be recorded as such.

Toggle: ``runtime.robots_txt`` (from the workspace setting, which defaults to
on; a run config can set it).
"""

from __future__ import annotations

import logging
import threading
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

AGENT = "*"


class RobotsCache:
    def __init__(self, http, *, enabled: bool = True):
        self.http = http
        self.enabled = enabled
        self._parsers: dict[str, RobotFileParser | bool] = {}
        self._lock = threading.Lock()

    def _load(self, origin: str, rate_key: str | None) -> RobotFileParser | bool:
        """A parser, or True (all allowed) / False (all disallowed)."""
        fetch = getattr(self.http, "get_once", None) or self.http.get
        try:
            response = fetch(f"{origin}/robots.txt", rate_key=rate_key)
        except Exception as exc:
            log.debug("robots.txt of %s unavailable (%s): allowed", origin, exc)
            return True
        status = int(getattr(response, "status_code", 200) or 200)
        if 400 <= status < 500:
            return True
        if status >= 500:
            return False
        parser = RobotFileParser()
        parser.parse((getattr(response, "text", "") or "").splitlines())
        return parser

    def allowed(self, url: str, *, rate_key: str | None = None) -> bool:
        if not self.enabled:
            return True
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            cached = self._parsers.get(origin)
        if cached is None:
            cached = self._load(origin, rate_key)
            with self._lock:
                self._parsers[origin] = cached
        if isinstance(cached, bool):
            return cached
        return cached.can_fetch(AGENT, url)
