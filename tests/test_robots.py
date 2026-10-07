"""robots.txt (A.2.4): honoured by the website enricher, toggleable, RFC 9309 statuses."""

from __future__ import annotations

from types import SimpleNamespace

from leadscraper.net.robots import RobotsCache

ROBOTS = "User-agent: *\nDisallow: /private/\n\nUser-agent: OtherBot\nDisallow: /\n"


class StubHttp:
    def __init__(self, status: int = 200, text: str = ROBOTS, error: Exception | None = None):
        self.status, self.text, self.error = status, text, error
        self.calls: list[str] = []

    def get_once(self, url: str, rate_key=None):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status_code=self.status, text=self.text)


def test_rules_apply_and_are_fetched_once_per_host():
    http = StubHttp()
    robots = RobotsCache(http)
    assert robots.allowed("https://smile.example/")
    assert not robots.allowed("https://smile.example/private/team")
    assert robots.allowed("https://smile.example/contact")      # OtherBot's rules aren't ours
    assert http.calls == ["https://smile.example/robots.txt"]


def test_statuses_follow_rfc_9309():
    assert RobotsCache(StubHttp(404, "")).allowed("https://a.example/private/x")
    assert RobotsCache(StubHttp(403, "")).allowed("https://a.example/private/x")
    assert not RobotsCache(StubHttp(503, "")).allowed("https://a.example/")
    assert not RobotsCache(StubHttp(429, "")).allowed("https://a.example/")
    assert RobotsCache(StubHttp(error=ConnectionError("down"))).allowed("https://a.example/")


def test_the_toggle_switches_it_off_without_a_request():
    http = StubHttp(503, "")
    robots = RobotsCache(http, enabled=False)
    assert robots.allowed("https://a.example/private/x") and http.calls == []
