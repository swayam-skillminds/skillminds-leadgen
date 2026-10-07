"""Headless Playwright smoke test against a local fixture server (Phase 3.6).

No internet: a ``http.server`` on 127.0.0.1 serves two pages. One builds its
listings with JavaScript, so the test proves Chromium really launched and
executed scripts through ``net/browser.BrowserSession``; the other is a
bot-check page, which ``check_blocked`` must recognise (we record blocks, we
never evade them).

Skipped when Chromium is not installed, unless ``LEADSCRAPER_REQUIRE_BROWSER=1``
(CI sets it, after ``playwright install --with-deps chromium``).
"""

from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from leadscraper.core.normalize import extract_phones
from leadscraper.net.browser import BrowserSession
from leadscraper.net.ratelimit import BlockedError
from leadscraper.services.health import browser as browser_check

FIXTURES = Path(__file__).parent / "fixtures" / "smoke"
pytestmark = pytest.mark.browser


def _chromium_or_skip() -> None:
    if browser_check()["status"] == "ok":
        return
    if os.environ.get("LEADSCRAPER_REQUIRE_BROWSER") == "1":
        pytest.fail("Chromium is required here: run `playwright install --with-deps chromium`")
    pytest.skip("Chromium is not installed (playwright install chromium)")


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output clean
        pass


@pytest.fixture
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_headless_chromium_renders_a_scripted_listing_page(fixture_server):
    _chromium_or_skip()
    session = BrowserSession(headless=True, timeout_ms=20_000, locale="en-IN",
                             timezone_id="Asia/Kolkata")
    try:
        with session.page() as page:
            page.goto(f"{fixture_server}/listing.html")
            page.wait_for_selector(".listing")
            names = page.locator(".listing .name").all_inner_texts()
            phones = page.locator(".listing .phone").all_inner_texts()
            BrowserSession.check_blocked(page)                     # a normal page passes
        assert names == ["Smile Dental", "Bright Smiles"]
        assert extract_phones("\n".join(phones), "IN") == ["+919820012345", "+919820054321"]

        with session.page() as page:
            page.goto(f"{fixture_server}/blocked.html")
            with pytest.raises(BlockedError):
                BrowserSession.check_blocked(page)
    finally:
        session.close()
