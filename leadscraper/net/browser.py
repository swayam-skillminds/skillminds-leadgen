"""Playwright wrapper.

One browser per worker thread, many pages. Sources borrow a page via
`BrowserSession.page()` so we pay the browser-launch cost once per thread per
run instead of once per query.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import threading
from dataclasses import dataclass
from typing import Iterator

from .http import USER_AGENTS
from .ratelimit import BlockedError

log = logging.getLogger(__name__)

# Applied before any page script runs; removes the most obvious automation tells.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => __LANGUAGES__});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : origQuery(params);
}
"""

_BLOCK_TEXT = (
    "unusual traffic",
    "recaptcha",
    "our systems have detected",
    "verify you are human",
    "enable javascript and cookies to continue",
    "sorry, you have been blocked",          # Cloudflare WAF block page
    "you are unable to access",
)


@dataclass
class _ThreadBrowser:
    """One thread's private playwright stack."""

    playwright: object | None = None
    browser: object | None = None
    context: object | None = None


def _teardown(session: _ThreadBrowser) -> None:
    """Close whatever parts of a stack exist, best-effort."""
    for part in (session.context, session.browser):
        if part is not None:
            with contextlib.suppress(Exception):
                part.close()
    # `sync_playwright()` needs .stop(), not .close().
    if session.playwright is not None:
        with contextlib.suppress(Exception):
            session.playwright.stop()


class BrowserSession:
    """Lazily-started Chromium. Safe to construct even if never used.

    The pool is thread-affine -- each worker thread gets its own playwright,
    browser and context -- rather than a shared pool of contexts handed to
    whichever thread asks next. Playwright's *sync* API is bound to the thread
    that created it: a context or page driven from another thread raises
    "greenlet in another thread" or simply deadlocks. `max_contexts` caps how
    many browsers are alive at once, so a thread pool wider than the cap waits
    for a slot instead of launching chromium once per worker.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        locale: str = "en-US",
        max_contexts: int = 3,
        slot_timeout: float = 120.0,
        timezone_id: str = "UTC",
        geolocation: tuple[float, float] | None = None,
        accept_language: str = "en-US,en;q=0.9",
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.locale = locale
        self.timezone_id = timezone_id
        self.geolocation = geolocation
        self.accept_language = accept_language
        self.max_contexts = max_contexts
        self.slot_timeout = slot_timeout
        self._slots = threading.Semaphore(max_contexts)
        self._local = threading.local()
        self._sessions: list[_ThreadBrowser] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        """Ensure the calling thread has a browser of its own."""
        self._session()

    def _session(self) -> _ThreadBrowser:
        existing = getattr(self._local, "session", None)
        if existing is not None:
            return existing
        from playwright.sync_api import sync_playwright  # imported late: heavy

        # Blocks while `max_contexts` browsers are already live. A slot is held
        # until close(), so more browser-using threads than slots would wait
        # for the whole run: bound the wait and raise instead, because the
        # pipeline records and steps over a failure but cannot step over a
        # deadlock. Size the browser worker pool <= max_contexts.
        if not self._slots.acquire(timeout=self.slot_timeout):
            raise RuntimeError(
                f"no browser slot after {self.slot_timeout:.0f}s "
                f"(max_contexts={self.max_contexts}); too many threads want a browser"
            )
        session = _ThreadBrowser()
        try:
            log.info("launching chromium (headless=%s) for %s",
                     self.headless, threading.current_thread().name)
            session.playwright = sync_playwright().start()
            session.browser = session.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                ],
            )
            # Locale, timezone and position follow the run's country and
            # centre; with no centre the browser simply reports none.
            located = ({"geolocation": {"latitude": self.geolocation[0],
                                        "longitude": self.geolocation[1]},
                        "permissions": ["geolocation"]} if self.geolocation else {})
            session.context = session.browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale=self.locale,
                timezone_id=self.timezone_id,
                viewport={"width": random.randint(1440, 1680), "height": random.randint(820, 1000)},
                extra_http_headers={"Accept-Language": self.accept_language},
                **located,
            )
            languages = [self.locale, *(["en-GB", "en"] if self.locale.startswith("en") else ["en"])]
            session.context.add_init_script(
                _STEALTH_INIT.replace("__LANGUAGES__", json.dumps(languages)))
            session.context.set_default_timeout(self.timeout_ms)
        except BaseException:
            # Nothing is registered yet, so unwind by hand and hand the slot back.
            _teardown(session)
            self._slots.release()
            raise
        self._local.session = session
        with self._lock:
            self._sessions.append(session)
        return session

    @contextlib.contextmanager
    def page(self) -> Iterator["object"]:
        """Yield a fresh page from the calling thread's own context, and always
        close it, even on error.
        """
        context = self._session().context
        page = context.new_page()
        # Images/fonts/media are pure cost for us -- block them.
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                page.close()

    @staticmethod
    def check_blocked(page) -> None:
        """Raise BlockedError if the current page is an interstitial."""
        try:
            content = (page.content() or "")[:6000].lower()
        except Exception:
            return
        if any(marker in content for marker in _BLOCK_TEXT):
            raise BlockedError(f"browser hit a challenge page at {page.url[:100]}")

    @staticmethod
    def dismiss_google_consent(page) -> None:
        """Click through Google's consent interstitial if one is shown."""
        try:
            on_consent = "consent." in page.url or "/consent" in page.url
        except Exception:
            return
        if not on_consent:
            return
        for selector in ('button[aria-label*="Accept all"]',
                         'button:has-text("Accept all")',
                         'button:has-text("I agree")',
                         'form[action*="consent"] button'):
            try:
                button = page.query_selector(selector)
                if button:
                    button.click()
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    return
            except Exception:
                continue

    @staticmethod
    def human_pause(low: float = 0.4, high: float = 1.6) -> None:
        """Small randomised pause between in-page interactions."""
        import time

        time.sleep(random.uniform(low, high))

    def close(self) -> None:
        """Tear down every thread's browser, not just the caller's.

        Best-effort across threads: the driver may refuse a cross-thread call,
        but the chromium process still dies with its pipe, and a suppressed
        complaint at shutdown beats leaking a browser per worker. Dropping the
        thread-local means a thread that keeps working simply gets a new one.
        """
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            _teardown(session)
            self._slots.release()
        self._local = threading.local()
