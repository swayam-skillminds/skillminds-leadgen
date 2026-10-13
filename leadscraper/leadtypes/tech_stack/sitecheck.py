"""Fingerprint one site: its homepage, fetched politely, read by the shared rules.

Per site: robots.txt, then the homepage over HTTPS (HTTP if HTTPS doesn't
answer), paced per site through the run's limiter. What the page and its
response headers show is matched against ``sources/web/fingerprints.yaml``.
Never raises. A site that refused us, robots.txt-blocked the homepage, resolved
to an internal address or failed says nothing ("not checked"): the previous
check stands.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import urlsplit

import requests

from ...core.normalize import domain_of, is_vendor_website, normalize_url
from ...net.http import DeadDomainError, PrivateAddressError
from ...sources.web.fingerprint import Detected, detect, page_evidence

log = logging.getLogger(__name__)

SOURCE = "site_fingerprint"
#: Site-builder subdomains: not a domain of their own, but a real site running something.
BUILDER_HOSTS = ("wixsite.com", "blogspot.com", "myshopify.com", "webflow.io", "weebly.com")
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:·]\s+")


@dataclass
class SiteCheck:
    #: checked | no_website | unreachable | blocked | robots | refused | failed | stopped
    status: str
    url: str | None = None
    stack: list[Detected] = field(default_factory=list)
    #: The site's own name (og:site_name, else the first part of its title).
    title: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        """It says what the site runs; anything else leaves the previous check standing."""
        return self.status == "checked"


def own_site(website: str | None) -> str | None:
    """The organisation's own site: a social profile or a directory listing isn't one."""
    url = normalize_url(website) if website else None
    if not url:
        return None
    host = domain_of(url) or ""
    if is_vendor_website(url) or any(host == h or host.endswith("." + h) for h in BUILDER_HOSTS):
        return url
    return None


def site_name(html: str) -> str:
    """The name a page gives its site: ``og:site_name``, else its title's first part."""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:
        soup = BeautifulSoup(html or "", "html.parser")
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and (meta.get("content") or "").strip():
        return " ".join(meta["content"].split())[:200]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return " ".join(_TITLE_SPLIT.split(title, maxsplit=1)[0].split())[:200]


class SiteChecker:
    def __init__(self, http, robots, failures, *, thresholds: Mapping[str, str] | None = None,
                 min_delay: float = 1.0, max_delay: float = 3.0,
                 should_stop: Callable[[], bool] | None = None):
        self.http = http
        self.robots = robots
        self.failures = failures
        self.thresholds = dict(thresholds or {})
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.should_stop = should_stop or (lambda: False)

    def _fail(self, reason: str, name: str, url: str | None, detail: str = "") -> None:
        self.failures.record(SOURCE, reason, url=url, business=name, detail=detail)

    def check(self, name: str, website: str | None) -> SiteCheck:
        try:
            return self._check(name, website)
        except Exception as exc:  # one site's surprise never ends the run
            log.exception("fingerprinting %s failed", name)
            self._fail("check_failed", name, website, repr(exc))
            return SiteCheck("failed", website, notes=[f"check failed: {exc!r}"[:200]])

    def _check(self, name: str, website: str | None) -> SiteCheck:
        site = own_site(website)
        if site is None:
            return SiteCheck("no_website", website)
        if self.should_stop():
            return SiteCheck("stopped", site)
        parts = urlsplit(site)
        path = parts.path or "/"
        rate_key = domain_of(site) or parts.hostname or ""
        self.http.limiter.configure(rate_key, self.min_delay, self.max_delay)
        home = f"https://{parts.netloc}{path}"
        if not self.robots.allowed(home, rate_key=rate_key):
            self._fail("robots_disallowed", name, home, "robots.txt disallows the homepage")
            return SiteCheck("robots", home, notes=["robots.txt disallows the homepage"])
        errors: list[str] = []
        for scheme in ("https", "http"):
            url = f"{scheme}://{parts.netloc}{path}"
            try:
                response = self._fetch(url, rate_key)
            except PrivateAddressError as exc:
                self._fail("private_address", name, url, str(exc))
                return SiteCheck("refused", url, notes=[str(exc)[:200]])
            except DeadDomainError as exc:
                self._fail("site_unreachable", name, url, f"the domain doesn't resolve ({exc})")
                return SiteCheck("unreachable", url)
            except requests.RequestException as exc:
                errors.append(f"{scheme}: {type(exc).__name__}")
                continue
            status = int(response.status_code)
            if status in (401, 403, 429) or self.http.looks_blocked(response.text):
                self._fail("blocked", name, url, f"HTTP {status}")
                return SiteCheck("blocked", url, notes=[f"HTTP {status} or a bot wall"])
            if status >= 400:
                errors.append(f"{scheme}: HTTP {status}")
                if status in (404, 410) or status >= 500:
                    break
                continue
            html = response.text or ""
            generators, assets = page_evidence(html, response.url)
            stack = detect(html, dict(response.headers), generators=generators,
                           script_urls=assets, thresholds=self.thresholds)
            return SiteCheck("checked", response.url, stack, site_name(html))
        self._fail("site_unreachable", name, home, "; ".join(errors))
        return SiteCheck("unreachable", home, notes=errors)

    def _fetch(self, url: str, rate_key: str):
        """The homepage, with one more try after a dropped connection or a timeout."""
        try:
            return self.http.get_once(url, rate_key=rate_key)
        except (DeadDomainError, PrivateAddressError, requests.exceptions.SSLError):
            raise
        except requests.RequestException:
            return self.http.get_once(url, rate_key=rate_key)
