"""Audit one organisation: fetch what the checks need, politely, and run them.

Per site: one TLS handshake, the homepage (HTTPS first, then HTTP), at most
one contact page, and a HEAD (or streamed GET) per checked homepage link --
all paced per host, robots.txt honoured for every page and link. PageSpeed is
one API call to Google, capped per run. Never raises: anything that goes wrong
is a failure record, and a check that could not run says nothing rather than
guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from ...core.lead import Lead
from ...core.normalize import domain_of, is_vendor_website, normalize_url
from ...net.http import DeadDomainError, PrivateAddressError
from ...net import tls as _tls
from ...net.tls import Certificate
from . import checks as C
from .config import DigitalAuditConfig
from .fingerprint import Detected

log = logging.getLogger(__name__)

SOURCE = "digital_audit"
#: Site-builder subdomains: not a domain of its own, but a real site to audit.
_BUILDER_HOSTS = ("wixsite.com", "blogspot.com")

STATUS_TEXT = {"audited": "OK", "no_website": "No website", "unreachable": "Unreachable",
               "blocked": "Not checked (site refused us)",
               "robots": "Not checked (robots.txt)",
               "refused": "Not checked (internal address)",
               "failed": "Not checked (audit failed)", "stopped": "Not checked (run stopped)"}
#: Outcomes that say nothing about the site: the previous audit stands.
NOT_CHECKED = frozenset({"blocked", "robots", "refused", "failed", "stopped"})


@dataclass
class AuditResult:
    #: audited | no_website | unreachable | blocked | robots | refused | failed | stopped
    status: str
    url: str | None = None
    findings: list[C.Finding] = field(default_factory=list)
    stack: list[Detected] = field(default_factory=list)
    pagespeed: int | None = None
    copyright_year: int | None = None
    checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return C.audit_score(self.findings)

    @property
    def checked(self) -> bool:
        """It says something about the site (blocked, refused, failed or stopped: nothing)."""
        return self.status not in NOT_CHECKED

    def summary(self, now: datetime) -> dict[str, Any]:
        """The ``audit`` attribute: what the workbook and the API show."""
        return {"score": self.score, "status": self.status,
                "status_text": STATUS_TEXT.get(self.status, self.status), "url": self.url,
                "findings": [f.text for f in self.findings],
                "types": [f.type for f in self.findings],
                "stack": [d.label for d in self.stack],
                "outdated": [d.label for d in self.stack if d.outdated],
                "pagespeed": self.pagespeed, "copyright_year": self.copyright_year,
                "checks": list(self.checks), "notes": list(self.notes),
                "audited_at": now.isoformat(timespec="seconds")}


def own_website(lead: Lead) -> str | None:
    """The organisation's own site: a social profile or a directory listing doesn't count."""
    url = normalize_url(lead.website) if lead.website else None
    if not url:
        return None
    host = domain_of(url) or ""
    if is_vendor_website(url) or any(host.endswith(h) for h in _BUILDER_HOSTS):
        return url
    return None


class Auditor:
    def __init__(self, cfg: DigitalAuditConfig, http, robots, failures, *,
                 pagespeed=None, tls: Callable[[str], Certificate] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.cfg = cfg
        self.http = http
        self.robots = robots
        self.failures = failures
        self.pagespeed = pagespeed
        #: Looked up when called, so a test (or a caller) can swap the handshake.
        self.tls = tls or (lambda host: _tls.certificate(host))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.checks = set(cfg.checks)
        #: The run's stop request, checked between a site's requests too.
        self.should_stop = should_stop or (lambda: False)

    def _fail(self, reason: str, lead: Lead, url: str | None, detail: str = "") -> None:
        self.failures.record(SOURCE, reason, url=url, business=lead.business_name,
                             detail=detail)

    def audit(self, lead: Lead) -> AuditResult:
        try:
            return self._audit(lead)
        except Exception as exc:  # one site's surprise never ends the audit
            log.exception("audit of %s failed", lead.business_name)
            self._fail("audit_failed", lead, lead.website, repr(exc))
            return AuditResult(status="failed", url=lead.website,
                               notes=[f"audit failed: {exc!r}"[:200]])

    # --- one site ------------------------------------------------------------------------

    def _audit(self, lead: Lead) -> AuditResult:
        result = AuditResult(status="audited", checks=sorted(self.checks))
        if "maps_profile" in self.checks:
            self._add(result, C.check_maps_profile(lead, self.cfg.min_reviews))
        site = own_website(lead)
        if site is None:
            result.status = "no_website"
            result.url = lead.website
            if "no_website" in self.checks:
                where = (f"only {domain_of(lead.website)}" if lead.website
                         else "none in any source that listed it")
                self._add(result, C.Finding("no_website", f"No website of its own ({where}).",
                                            {"listed": lead.website}))
            return result

        parts = urlsplit(site)
        host = parts.hostname or ""
        path = parts.path or "/"
        rate_key = domain_of(site) or host
        self.http.limiter.configure(rate_key, self.cfg.min_delay, self.cfg.max_delay)
        result.url = f"https://{parts.netloc}{path}"
        if not self.robots.allowed(result.url, rate_key=rate_key):
            result.status = "robots"
            result.notes.append("robots.txt disallows the homepage: page checks skipped")
            self._fail("robots_disallowed", lead, result.url, "robots.txt disallows the homepage")
            if "https" in self.checks:
                self._https(result, self.tls(host), None)
            return result

        response, https_error = self._homepage(lead, parts.netloc, path, rate_key, result)
        if response is None:
            return result
        result.url = response.url
        served_https = response.url.startswith("https://")
        if "https" in self.checks:
            self._https(result, self.tls(host) if served_https else None,
                        None if served_https else (https_error or "it only loads over http://"))

        html = response.text or ""
        facts = C.page_facts(html, response.url)
        result.copyright_year = facts.copyright_year
        if "mobile" in self.checks:
            self._add(result, C.check_mobile(facts))
        stack, finding = C.check_tech(facts, html, dict(response.headers), self.cfg.outdated)
        result.stack = stack
        if "cms_age" in self.checks:
            self._add(result, finding)
        if "copyright" in self.checks:
            self._add(result, C.check_copyright(facts, self.clock(),
                                                self.cfg.stale_copyright_years))
        pages = [facts]
        if self.should_stop():
            return self._stopped(result)
        if {"contact_form", "booking"} & self.checks and facts.contact_links \
                and not facts.has_contact_form:
            contact = self._page(lead, facts.contact_links[0], rate_key)
            if contact is not None:
                pages.append(contact)
        if "contact_form" in self.checks:
            self._add(result, C.check_contact_form(pages))
        if "booking" in self.checks:
            self._add(result, C.check_booking(pages, lead.category or "",
                                              self.cfg.booking_categories))
        if "broken_links" in self.checks and self.cfg.max_links:
            links = self._links(lead, facts.internal_links[:self.cfg.max_links], rate_key)
            if links is None:
                return self._stopped(result)
            finding = C.check_broken_links(links)
            if finding is not None:
                finding.url = response.url
            self._add(result, finding)
        if "pagespeed" in self.checks and self.pagespeed is not None:
            if self.should_stop():
                return self._stopped(result)
            self._pagespeed(lead, result, response.url)
        return result

    @staticmethod
    def _stopped(result: AuditResult) -> AuditResult:
        """A stop mid-site: half an audit is no audit (the previous one stands)."""
        result.status = "stopped"
        result.notes.append("the run stopped during this site's audit")
        return result

    @staticmethod
    def _add(result: AuditResult, finding: C.Finding | None) -> None:
        if finding is not None and finding.type not in {f.type for f in result.findings}:
            result.findings.append(finding)

    def _pagespeed(self, lead: Lead, result: AuditResult, url: str) -> None:
        speed = self.pagespeed.score(url, lead.business_name)
        if speed is None:
            return
        result.pagespeed = speed.score
        settings = self.pagespeed.settings
        if speed.score < settings.threshold:
            lcp = f", largest paint {speed.lcp_ms / 1000:.1f} s" if speed.lcp_ms else ""
            self._add(result, C.Finding(
                "slow_site", f"Slow site: PageSpeed {speed.score}/100 on {settings.strategy}"
                f"{lcp} (below {settings.threshold}).",
                {"score": speed.score, "lcp_ms": speed.lcp_ms, "cls": speed.cls,
                 "tbt_ms": speed.tbt_ms, "strategy": settings.strategy}, url=url))

    def _homepage(self, lead: Lead, netloc: str, path: str, rate_key: str,
                  result: AuditResult) -> tuple[Any, str | None]:
        """(response, why HTTPS failed). None as the response: unreachable or blocked."""
        https_error = None
        errors = []
        for scheme in ("https", "http"):
            url = f"{scheme}://{netloc}{path}"
            try:
                response = self._fetch_home(url, rate_key)
            except PrivateAddressError as exc:
                result.status = "refused"
                result.notes.append(str(exc)[:200])
                self._fail("private_address", lead, url, str(exc))
                return None, None
            except DeadDomainError as exc:
                self._unreachable(lead, result, url, f"the domain doesn't resolve ({exc})")
                return None, None
            except requests.exceptions.SSLError as exc:
                https_error = f"its HTTPS certificate is invalid ({_short(exc)})"
                errors.append(f"{scheme}: certificate error")
                continue
            except requests.RequestException as exc:
                if scheme == "https":
                    https_error = f"HTTPS doesn't answer ({type(exc).__name__})"
                errors.append(f"{scheme}: {type(exc).__name__}")
                continue
            status = int(response.status_code)
            if status in (401, 403, 429) or self.http.looks_blocked(response.text):
                result.status = "blocked"
                result.notes.append(f"the site answered HTTP {status} or a bot wall: "
                                    "page checks skipped")
                self._fail("blocked", lead, url, f"HTTP {status}")
                return None, https_error
            if status in (404, 410) or status >= 500:
                self._unreachable(lead, result, url, f"the homepage answers HTTP {status}")
                return None, https_error
            if status >= 400:
                errors.append(f"{scheme}: HTTP {status}")
                continue
            return response, https_error
        self._unreachable(lead, result, f"https://{netloc}{path}",
                          "no answer over HTTPS or HTTP (" + "; ".join(errors) + ")")
        return None, https_error

    def _fetch_home(self, url: str, rate_key: str):
        """The homepage, with one more try after a dropped connection or a timeout:
        "unreachable" is a claim, and one blip doesn't make it."""
        try:
            return self.http.get_once(url, rate_key=rate_key)
        except (DeadDomainError, PrivateAddressError, requests.exceptions.SSLError):
            raise
        except requests.RequestException:
            return self.http.get_once(url, rate_key=rate_key)

    def _unreachable(self, lead: Lead, result: AuditResult, url: str, why: str) -> None:
        result.status = "unreachable"
        self._fail("site_unreachable", lead, url, why)
        if "unreachable" in self.checks:
            self._add(result, C.Finding("site_unreachable", f"Website unreachable: {why}.",
                                        {"url": url}, url=url))

    def _https(self, result: AuditResult, cert: Certificate | None, http_only: str | None) -> None:
        if http_only is not None:
            self._add(result, C.Finding("no_https", f"No HTTPS: {http_only}.", url=result.url))
            return
        if cert is None:
            return
        if not cert.valid:
            if cert.reachable:
                self._add(result, C.Finding("no_https", f"No valid HTTPS: {cert.error}.",
                                            {"error": cert.error}, url=result.url))
            return
        days = cert.days_left(self.clock())
        if days is not None and days < self.cfg.cert_expiry_days:
            when = cert.expires_at.date().isoformat() if cert.expires_at else "soon"
            self._add(result, C.Finding(
                "cert_expiring", f"Certificate expiring: the HTTPS certificate runs out on "
                f"{when} ({max(0, int(days))} days).",
                {"expires_at": when, "days_left": round(days, 1), "issuer": cert.issuer},
                url=result.url))

    def _page(self, lead: Lead, url: str, rate_key: str) -> C.PageFacts | None:
        if not self.robots.allowed(url, rate_key=rate_key):
            return None
        try:
            response = self.http.get_once(url, rate_key=rate_key)
        except Exception as exc:
            self._fail("fetch_failed", lead, url, type(exc).__name__)
            return None
        if int(response.status_code) != 200:
            return None
        return C.page_facts(response.text or "", response.url)

    def _links(self, lead: Lead, links: list[str], rate_key: str) -> dict[str, str] | None:
        """Link -> "ok" or why it is broken; None when the run stopped meanwhile."""
        results: dict[str, str] = {}
        for link in links:
            if self.should_stop():
                return None
            if not self.robots.allowed(link, rate_key=rate_key):
                continue
            try:
                status = self.http.probe(link, rate_key=rate_key)
            except PrivateAddressError:
                continue                         # not ours to check, not broken
            except DeadDomainError:
                results[link] = "unreachable"
                continue
            except requests.RequestException as exc:
                results[link] = f"unreachable ({type(exc).__name__})"
                continue
            results[link] = f"HTTP {status}" if status in (404, 410) or status >= 500 else "ok"
        return results


def _short(exc: Exception) -> str:
    text = str(exc).lower()
    for marker in ("certificate has expired", "self-signed certificate", "hostname mismatch",
                   "unable to get local issuer certificate", "certificate verify failed"):
        if marker in text:
            return marker
    return type(exc).__name__
