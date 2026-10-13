"""Shared requests session: UA rotation, pacing, retries and block detection."""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import random
import socket
import threading
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..core.normalize import domain_of
from .ratelimit import BlockedError, PermanentError, RateLimiter, with_retry

try:
    from urllib3.exceptions import NameResolutionError
except ImportError:  # urllib3 < 2.0 reports resolver failures as plain gaierror
    NameResolutionError = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

# A small pool of current, real desktop UA strings. Rotating these is the
# cheapest way to avoid the trivial "same UA 500 times" fingerprint.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

def _supported_encodings() -> str:
    """Advertise only what this install can actually decode.

    Claiming `br` without a brotli library installed gets you a 200 response
    full of undecodable bytes, which looks exactly like a parser bug. Build
    the header from what's importable instead of hard-coding it.
    """
    encodings = ["gzip", "deflate"]
    try:
        import brotli  # noqa: F401
        encodings.append("br")
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
            encodings.append("br")
        except ImportError:
            log.debug("brotli not installed; not advertising 'br' encoding")
    try:
        import zstandard  # noqa: F401
        encodings.append("zstd")
    except ImportError:
        pass
    return ", ".join(encodings)


ACCEPT_ENCODING = _supported_encodings()

# Phrases that mean "you've been detected", not "page not found".
_BLOCK_MARKERS = (
    "unusual traffic from your computer network",
    "our systems have detected unusual traffic",
    "/sorry/index",
    "recaptcha",
    "are you a robot",
    "access denied",
    "request blocked",
    "please enable javascript and cookies",
    "captcha-delivery.com",
)


class DeadDomainError(PermanentError):
    """The host cannot be reached, now or later: NXDOMAIN, or a bogus address.

    Subclassing PermanentError is what keeps it out of the retry ladder --
    `with_retry(give_up_on=(PermanentError,))` re-raises on the first attempt.
    That matters: DNS is deterministic, so the three-attempt ladder spent ~15s
    per dead vendor site re-asking a question with one possible answer.
    """


# Resolver messages that mean "this name does not exist". Only consulted when
# walking the exception chain turned up no gaierror to inspect -- some
# resolvers surface a bare OSError with nothing structured underneath.
_DNS_FAILURE_MARKERS = (
    "nodename nor servname provided",
    "name or service not known",
    "no address associated with hostname",
    "failed to resolve",
    "nxdomain",
)

# Resolution results are cached for the whole process: DNS for a host does not
# change mid-run, and several sources hit the same host from different threads.
_resolution_cache: dict[str, str | None] = {}
_resolution_lock = threading.Lock()


def _chain(exc: BaseException) -> list[BaseException]:
    """Every exception reachable from `exc`, however it was wrapped.

    requests wraps urllib3 wraps socket, and each layer uses a different link:
    `raise ... from`, implicit context, urllib3's `.reason`, or simply passing
    the inner error as an argument. Following all of them beats matching on
    repr(), which changes between urllib3 releases.
    """
    seen: dict[int, BaseException] = {}
    pending = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen[id(current)] = current
        links = [current.__cause__, current.__context__,
                 getattr(current, "reason", None), getattr(current, "_reason", None)]
        links.extend(current.args)
        pending.extend(link for link in links if isinstance(link, BaseException))
    return list(seen.values())


def looks_like_dns_failure(exc: BaseException) -> bool:
    """Is this connection error a hostname that is never going to resolve?"""
    chain = _chain(exc)
    for link in chain:
        if isinstance(link, socket.gaierror):
            return not _transient_resolver_error(link)
    if NameResolutionError is not None:
        if any(isinstance(link, NameResolutionError) for link in chain):
            return True
    text = str(exc).lower()
    return any(marker in text for marker in _DNS_FAILURE_MARKERS)


def _transient_resolver_error(exc: socket.gaierror) -> bool:
    """EAI_AGAIN is the resolver being unreachable, not the name being gone.

    Worth separating because the verdict is persisted: blacklisting a live
    vendor domain over a DNS hiccup would silently drop it from every later run.
    """
    return exc.errno == socket.EAI_AGAIN


def _dead_address_reason(host: str) -> str | None:
    """Why `host` is unreachable, or None if it looks live. Not cached."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if _transient_resolver_error(exc):
            return None
        return f"does not resolve ({exc.strerror or exc})"
    except (UnicodeError, ValueError) as exc:  # malformed hostname in scraped HTML
        return f"does not resolve ({exc})"
    addresses = []
    for info in infos:
        # IPv6 results can carry a scope suffix (fe80::1%en0) that ip_address rejects.
        raw = str(info[4][0]).split("%", 1)[0]
        with contextlib.suppress(ValueError):
            addresses.append(ipaddress.ip_address(raw))
    if not addresses:
        return "does not resolve (no usable address)"
    # One vendor site in the logs answers with 127.0.0.0, which costs two full
    # 30s connect timeouts before failing. Nothing routable, nothing to fetch.
    if all(addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_reserved
           for addr in addresses):
        return f"resolves to {addresses[0]}"
    return None


def resolution_failure(host: str) -> str | None:
    """Cached `_dead_address_reason`: one lookup per host per process."""
    with _resolution_lock:
        if host in _resolution_cache:
            return _resolution_cache[host]
    reason = _dead_address_reason(host)
    with _resolution_lock:
        _resolution_cache.setdefault(host, reason)
    return reason


def _request_host(url: str) -> str | None:
    """The literal host a request would resolve, credentials and port stripped."""
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        return None
    host = netloc.rsplit("@", 1)[-1].lower()
    if host.startswith("["):  # bracketed IPv6 literal
        return host.split("]", 1)[0].lstrip("[") or None
    return host.split(":", 1)[0] or None


#: Set to "1" to let the public-only clients reach internal addresses (a lab, a test).
PRIVATE_ENV = "LEADSCRAPER_ALLOW_PRIVATE_TARGETS"


class PrivateAddressError(requests.exceptions.InvalidURL):
    """A public-only client refused a host that resolves to an internal address."""


def private_address(host: str | None) -> str | None:
    """Why `host` is internal (loopback, private, link-local, reserved, multicast), or None.

    For clients that fetch addresses users or third parties supplied (audit
    targets, links in job ads): a site on the public internet never needs one.
    Unresolvable names pass (the request fails on its own). Not cached: a host
    that resolves privately later (DNS rebinding) is caught on that request.
    """
    import os

    if not host or os.environ.get(PRIVATE_ENV) == "1":
        return None
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return None
    for info in infos:
        with contextlib.suppress(ValueError):
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved \
                    or ip.is_unspecified or ip.is_multicast:
                return f"resolves to an internal address ({ip})"
    return None


class PublicOnlyAdapter(requests.adapters.HTTPAdapter):
    """Refuses internal addresses on every request it sends -- each redirect hop included."""

    def send(self, request, **kwargs):
        host = _request_host(request.url)
        reason = private_address(host)
        if reason:
            raise PrivateAddressError(f"{host} {reason}: not fetched (set {PRIVATE_ENV}=1 to "
                                      "allow internal targets)", request=request)
        return super().send(request, **kwargs)


class HttpClient:
    """Polite HTTP client. All source modules share one instance."""

    def __init__(
        self,
        limiter: RateLimiter,
        *,
        timeout: int = 30,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        dead_domain_check: Callable[[str], bool] | None = None,
        on_dead_domain: Callable[[str, str], None] | None = None,
        accept_language: str = "en-US,en;q=0.9",
        public_only: bool = False,
    ):
        self.limiter = limiter
        #: Refuse hosts that resolve to internal addresses (``PublicOnlyAdapter``).
        self.public_only = public_only
        #: From the run's country, so sites answer in the local variant.
        self.accept_language = accept_language
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # Bound to the store by the pipeline, so a domain found dead on Monday
        # is still skipped on Tuesday without a single packet.
        self.dead_domain_check = dead_domain_check
        self.on_dead_domain = on_dead_domain
        self.headers = self._base_headers()
        # One session per host. A single shared session pools connections by
        # host anyway, but its pool is sized for one and thrashes once worker
        # threads interleave requests across a dozen hosts.
        self._sessions: dict[str, requests.Session] = {}
        self._sessions_lock = threading.Lock()
        self._dead_hosts: set[str] = set()
        self._dead_lock = threading.Lock()

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self.accept_language,
            "Accept-Encoding": ACCEPT_ENCODING,
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    def session_for(self, host: str) -> requests.Session:
        """The session serving `host`, created on first use."""
        with self._sessions_lock:
            session = self._sessions.get(host)
            if session is None:
                session = requests.Session()
                session.headers.update(self.headers)
                # max_retries=0 because with_retry owns the retry policy --
                # urllib3 retrying underneath it would multiply the attempts
                # and quietly defeat give_up_on.
                adapter_class = PublicOnlyAdapter if self.public_only \
                    else requests.adapters.HTTPAdapter
                adapter = adapter_class(pool_connections=4, pool_maxsize=8, max_retries=0)
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                self._sessions[host] = session
            return session

    def _note_dead(self, host: str, reason: str) -> None:
        """Tell the persistent cache about a newly-dead host, once per run."""
        with self._dead_lock:
            if host in self._dead_hosts:
                return
            self._dead_hosts.add(host)
        log.warning("dead domain %s: %s -- skipping it for the rest of the run", host, reason)
        if self.on_dead_domain is not None:
            self.on_dead_domain(host, reason)

    def _guard_dead(self, host: str, resolve_target: str | None) -> None:
        """Raise DeadDomainError before touching the network, when we can tell.

        A public-only client first refuses internal addresses: those are not
        dead (and must not land in the dead-domain cache), they are off limits.
        """
        if self.public_only:
            reason = private_address(resolve_target)
            if reason:
                raise PrivateAddressError(f"{resolve_target} {reason}: not fetched (set "
                                          f"{PRIVATE_ENV}=1 to allow internal targets)")
        if self.dead_domain_check is not None and self.dead_domain_check(host):
            raise DeadDomainError(f"{host} is a known dead domain")
        if not resolve_target:
            return
        reason = resolution_failure(resolve_target)
        if reason is not None:
            self._note_dead(host, reason)
            raise DeadDomainError(f"{host} {reason}")

    def get(
        self,
        url: str,
        *,
        rate_key: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        allow_redirects: bool = True,
        blocked_statuses: tuple[int, ...] = (),
        attempts: int | None = None,
    ) -> requests.Response:
        """Paced, retried GET. Raises BlockedError when the host pushes back.

        `blocked_statuses` lets a caller declare host-specific throttle codes.
        DuckDuckGo, for instance, answers 202 with an empty results page when
        it wants you to slow down -- without this it reads as a clean "no
        results" and the backoff never kicks in.

        `attempts` overrides the client's retry count for this call (an API
        that asks callers not to retry a throttle answer gets 1).
        """
        return self._send("GET", url, rate_key=rate_key, headers=headers, params=params,
                          allow_redirects=allow_redirects, blocked_statuses=blocked_statuses,
                          attempts=attempts)

    def post(
        self,
        url: str,
        *,
        rate_key: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        blocked_statuses: tuple[int, ...] = (),
        attempts: int | None = None,
    ) -> requests.Response:
        """Paced, retried POST, with ``get``'s semantics: for search APIs that take their
        query as a body (TED, Contracts Finder) and listing pages that load by form post.

        It shares the host's session with ``get``, cookies included, so a page fetched
        first can hand its form token to the post."""
        return self._send("POST", url, rate_key=rate_key, headers=headers, params=params,
                          json=json, data=data, allow_redirects=True,
                          blocked_statuses=blocked_statuses, attempts=attempts)

    def _send(self, method: str, url: str, *, rate_key: str | None, headers: dict | None,
              params: dict | None, allow_redirects: bool, blocked_statuses: tuple[int, ...],
              attempts: int | None, json: Any = None, data: Any = None) -> requests.Response:
        key = rate_key or domain_of(url) or "default"
        # Dead-domain bookkeeping is keyed by the canonical domain, the same
        # string the store records; DNS is asked about the literal host, since
        # `www.foo.com` and `foo.com` are different names to a resolver.
        host = domain_of(url) or key
        self._guard_dead(host, _request_host(url))
        session = self.session_for(host)

        def _attempt() -> requests.Response:
            self.limiter.wait(key)
            request_headers = dict(session.headers)
            request_headers["User-Agent"] = random.choice(USER_AGENTS)
            if headers:
                request_headers.update(headers)
            send = session.get if method == "GET" else session.post
            body = {} if method == "GET" else {"json": json, "data": data}
            try:
                response = send(
                    url,
                    headers=request_headers,
                    params=params,
                    timeout=self.timeout,
                    allow_redirects=allow_redirects,
                    **body,
                )
            except requests.ConnectionError as exc:
                if not looks_like_dns_failure(exc):
                    raise
                self._note_dead(host, "does not resolve")
                raise DeadDomainError(f"{host} does not resolve") from exc
            if response.status_code in (404, 410, 451):
                # Dead link: retrying costs ~14s and can never succeed.
                raise PermanentError(f"HTTP {response.status_code} for {url[:100]}")
            if response.status_code in blocked_statuses:
                self.limiter.penalise(key, seconds=30.0)
                raise BlockedError(
                    f"HTTP {response.status_code} from {key} (host throttle signal)"
                )
            if response.status_code in (429, 403, 503):
                self.limiter.penalise(key, seconds=45.0)
                raise BlockedError(f"HTTP {response.status_code} from {key}")
            response.raise_for_status()
            # A bot wall is a page; an API's JSON that mentions "access denied" (a tender
            # for an access-control system) is data, not a block.
            is_json = "json" in str(response.headers.get("Content-Type") or "").lower()
            if not is_json and self.looks_blocked(response.text):
                self.limiter.penalise(key, seconds=90.0)
                raise BlockedError(f"block/CAPTCHA page returned by {key}")
            self.limiter.relax(key)
            return response

        return with_retry(
            _attempt,
            attempts=attempts if attempts is not None else self.max_retries,
            base=self.backoff_base,
            label=f"{method} {url[:80]}",
            give_up_on=(PermanentError, PrivateAddressError),
        )

    def get_once(self, url: str, *, rate_key: str | None = None) -> requests.Response:
        """One paced GET, its response returned whatever the status.

        For probes whose status *is* the answer (robots.txt): no retries, no
        block penalty on the host. Dead domains are still skipped unasked.
        """
        key = rate_key or domain_of(url) or "default"
        host = domain_of(url) or key
        self._guard_dead(host, _request_host(url))
        session = self.session_for(host)
        self.limiter.wait(key)
        headers = dict(session.headers)
        headers["User-Agent"] = random.choice(USER_AGENTS)
        return session.get(url, headers=headers, timeout=self.timeout, allow_redirects=True)

    def probe(self, url: str, *, rate_key: str | None = None) -> int:
        """The HTTP status of `url`, without downloading its body (a link check).

        HEAD first; a server that refuses HEAD (405/501, some 403s) gets one
        streamed GET whose body is never read. Paced like any request, no
        retries. Raises like ``get_once`` for a dead domain or a dropped
        connection, which a caller counts as "unreachable".
        """
        key = rate_key or domain_of(url) or "default"
        host = domain_of(url) or key
        self._guard_dead(host, _request_host(url))
        session = self.session_for(host)
        self.limiter.wait(key)
        headers = dict(session.headers)
        headers["User-Agent"] = random.choice(USER_AGENTS)
        response = session.head(url, headers=headers, timeout=self.timeout, allow_redirects=True)
        status = int(response.status_code)
        response.close()
        if status in (403, 405, 501):
            self.limiter.wait(key)
            with session.get(url, headers=headers, timeout=self.timeout, allow_redirects=True,
                             stream=True) as response:
                status = int(response.status_code)
        return status

    def get_soup(self, url: str, **kwargs: Any) -> BeautifulSoup:
        """GET and parse. lxml when available, stdlib parser as a fallback."""
        response = self.get(url, **kwargs)
        return self.soup(response.text)

    @staticmethod
    def soup(markup: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(markup, "lxml")
        except Exception:  # lxml missing or malformed input
            return BeautifulSoup(markup, "html.parser")

    @staticmethod
    def looks_blocked(markup: str | None) -> bool:
        """Heuristic: does this response body mean we've been detected?"""
        if not markup:
            return False
        lowered = markup[:8000].lower()
        return any(marker in lowered for marker in _BLOCK_MARKERS)

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                session.close()
