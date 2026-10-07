"""Tests for the HTTP layer: dead-domain short-circuiting, the retry policy
and the per-host session pool.

Fully offline -- both `requests.Session` and `socket.getaddrinfo` are stubbed,
so a regression here can never turn into a real request.
"""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError

from leadscraper.net import http as http_mod
from leadscraper.net.http import DeadDomainError, HttpClient
from leadscraper.net.ratelimit import PermanentError, RateLimiter

LIVE_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
LOOPBACK_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.0", 0))]


@pytest.fixture(autouse=True)
def isolate_resolution_cache():
    """The resolution cache is process-wide by design; tests must not share it."""
    http_mod._resolution_cache.clear()
    yield
    http_mod._resolution_cache.clear()


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "<html>fine</html>"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Records every request instead of making one."""

    def __init__(self, responder, created: list):
        self.headers: dict[str, str] = {}
        self.mounted: dict[str, object] = {}
        self.calls: list[str] = []
        self.closed = False
        self._responder = responder
        created.append(self)

    def mount(self, prefix, adapter):
        self.mounted[prefix] = adapter

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self._responder(url)

    def close(self):
        self.closed = True


def ok(url: str) -> FakeResponse:
    return FakeResponse()


def make_client(monkeypatch, responder, *, addrinfo=LIVE_ADDRINFO, lookups=None, **kwargs):
    """An HttpClient wired to fake sessions and a fake resolver.

    Returns the client and the list of sessions it created, which is how the
    pooling tests tell "one session per host" from "one session per request".
    """
    sessions: list[FakeSession] = []
    monkeypatch.setattr(http_mod.requests, "Session", lambda: FakeSession(responder, sessions))

    def fake_getaddrinfo(host, *args, **kw):
        if lookups is not None:
            lookups.append(host)
        if isinstance(addrinfo, BaseException):
            raise addrinfo
        return addrinfo

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    kwargs.setdefault("backoff_base", 0.001)
    client = HttpClient(RateLimiter((0.0, 0.0)), **kwargs)
    return client, sessions


def total_calls(sessions) -> int:
    return sum(len(session.calls) for session in sessions)


# --- dead domains ----------------------------------------------------------

def test_dns_failure_raises_dead_domain_after_a_single_attempt(monkeypatch):
    def unresolvable(url):
        reason = NameResolutionError(
            "deyzevents.com", None,
            socket.gaierror(8, "nodename nor servname provided, or not known"),
        )
        try:
            raise MaxRetryError(None, url, reason=reason)
        except MaxRetryError as exc:
            # Exactly how requests wraps it: no `from`, chained via context.
            raise requests.ConnectionError(exc)

    client, sessions = make_client(monkeypatch, unresolvable, max_retries=3)
    with pytest.raises(DeadDomainError):
        client.get("https://deyzevents.com/contact")
    assert total_calls(sessions) == 1


def test_transient_resolver_failure_is_retried_not_blacklisted(monkeypatch):
    marked: list[tuple[str, str]] = []

    def resolver_down(url):
        try:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        except socket.gaierror as exc:
            raise requests.ConnectionError(exc)

    client, sessions = make_client(monkeypatch, resolver_down, max_retries=2,
                                   on_dead_domain=lambda host, reason: marked.append((host, reason)))
    with pytest.raises(requests.ConnectionError):
        client.get("https://royalcaterers.in/")
    assert total_calls(sessions) == 2
    assert marked == []


def test_loopback_only_host_is_dead_without_a_request(monkeypatch):
    client, sessions = make_client(monkeypatch, ok, addrinfo=LOOPBACK_ADDRINFO)
    with pytest.raises(DeadDomainError, match="resolves to 127.0.0.0"):
        client.get("https://expertevents.in/")
    assert sessions == []


def test_known_dead_domain_costs_nothing(monkeypatch):
    lookups: list[str] = []
    client, sessions = make_client(
        monkeypatch, ok, lookups=lookups,
        dead_domain_check=lambda host: host == "khushi-events.grexa.site",
    )
    with pytest.raises(DeadDomainError):
        client.get("https://khushi-events.grexa.site/about")
    assert sessions == []
    assert lookups == []


def test_on_dead_domain_fires_once_per_host(monkeypatch):
    marked: list[tuple[str, str]] = []
    lookups: list[str] = []
    client, _ = make_client(
        monkeypatch, ok, addrinfo=LOOPBACK_ADDRINFO, lookups=lookups,
        on_dead_domain=lambda host, reason: marked.append((host, reason)),
    )
    for url in ("https://expertevents.in/", "https://expertevents.in/contact"):
        with pytest.raises(DeadDomainError):
            client.get(url)
    assert [host for host, _ in marked] == ["expertevents.in"]
    assert lookups == ["expertevents.in"]  # second call answered from the cache


# --- retry policy ----------------------------------------------------------

def test_404_is_permanent_and_not_retried(monkeypatch):
    client, sessions = make_client(monkeypatch, lambda url: FakeResponse(404, ""), max_retries=3)
    with pytest.raises(PermanentError) as caught:
        client.get("https://www.wedmegood.com/vendors/mumbai/caterers/")
    assert not isinstance(caught.value, DeadDomainError)
    assert total_calls(sessions) == 1


def test_500_is_retried(monkeypatch):
    client, sessions = make_client(monkeypatch, lambda url: FakeResponse(500, ""), max_retries=3)
    with pytest.raises(requests.HTTPError):
        client.get("https://www.sulekha.com/catering-services/mumbai")
    assert total_calls(sessions) == 3


# --- session pool ----------------------------------------------------------

def test_one_session_per_host(monkeypatch):
    client, sessions = make_client(monkeypatch, ok)
    client.get("https://www.sulekha.com/catering-services/mumbai")
    client.get("https://www.sulekha.com/gyms/bangalore")
    client.get("https://www.wedmegood.com/vendors/mumbai/wedding-catering/")
    assert len(sessions) == 2
    assert len(sessions[0].calls) == 2
    assert client.session_for("sulekha.com") is sessions[0]
    assert set(sessions[0].mounted) == {"http://", "https://"}
    client.close()
    assert all(session.closed for session in sessions)
