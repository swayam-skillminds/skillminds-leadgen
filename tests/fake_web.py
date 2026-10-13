"""An offline web for the Phase 6 lead types: routes URL -> page, no network ever.

``FakeWeb`` stands in for ``net.http.HttpClient`` (``get``, ``get_once``,
``probe``, ``session_for``, ``soup``, ``looks_blocked``) with the same error
semantics: ``get`` raises ``PermanentError`` on 404/410 and ``BlockedError`` on
403/429/503. An unknown ``/robots.txt`` is a 404 (everything allowed); any
other unknown URL is a connection error.

Fixtures (``tests/fixtures/sites``, ``tests/fixtures/jobs``) are fictional:
reserved ``.example`` domains, invented companies; the JSON keeps the shape of
each API as verified live on 2026-10-07. Dates are written relative to now --
``"@iso:3"`` (3 days ago, ISO), ``"@ms:3"`` (epoch ms), ``"@date:3"`` (a date) --
so a fixture never goes stale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from leadscraper.engine.failures import FailureLog
from leadscraper.leadtypes.toolkit import Kit
from leadscraper.net.http import DeadDomainError, HttpClient
from leadscraper.net.ratelimit import BlockedError, PermanentError, RateLimiter
from leadscraper.net.robots import RobotsCache

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REL = re.compile(r'"@(iso|ms|date):(-?\d+(?:\.\d+)?)"')


def relative(text: str, now: datetime | None = None) -> str:
    """Replace "@iso:N" / "@ms:N" / "@date:N" placeholders with N days before now."""
    now = now or datetime.now(timezone.utc)

    def sub(match: re.Match) -> str:
        when = now - timedelta(days=float(match.group(2)))
        kind = match.group(1)
        if kind == "ms":
            return str(int(when.timestamp() * 1000))
        if kind == "date":
            return json.dumps(when.date().isoformat())
        return json.dumps(when.isoformat(timespec="seconds").replace("+00:00", "Z"))
    return _REL.sub(sub, text)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def json_fixture(name: str) -> Any:
    return json.loads(relative(fixture(name)))


@dataclass
class Page:
    text: str = ""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    #: Where it ended up after redirects; default: the requested URL.
    url: str | None = None
    data: Any = None

    @classmethod
    def of_json(cls, data: Any, status: int = 200) -> "Page":
        return cls(text=json.dumps(data), status=status, data=data,
                   headers={"Content-Type": "application/json"})


class Response:
    def __init__(self, page: Page, url: str):
        self.status_code = page.status
        self.text = page.text
        self.content = page.text.encode()
        self.url = page.url or url
        self.headers = requests.structures.CaseInsensitiveDict(page.headers)
        self._data = page.data

    def json(self) -> Any:
        return self._data if self._data is not None else json.loads(self.text)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


Route = Page | Exception | Callable[[str], Page]


class NoWait(RateLimiter):
    """Records what would have been paced, never sleeps."""

    def __init__(self):
        super().__init__((0.0, 0.0))
        self.waited: list[str] = []

    def wait(self, key: str) -> None:
        self.waited.append(key)


class FakeWeb:
    def __init__(self, routes: dict[str, Route] | None = None, limiter: RateLimiter | None = None):
        self.routes: dict[str, Route] = dict(routes or {})
        self.limiter = limiter or NoWait()
        self.calls: list[str] = []
        self.posts: list[tuple[str, Any]] = []

    def add(self, url: str, route: Route) -> None:
        self.routes[url] = route

    def _page(self, url: str) -> Page:
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            route = self.routes.get(url.split("?", 1)[0])
        if route is None and urlsplit(url).path == "/robots.txt":
            return Page(status=404)
        if route is None:
            raise requests.ConnectionError(f"offline test: no fixture for {url}")
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(url)
        return route

    # --- HttpClient's surface -------------------------------------------------------

    def get(self, url, *, rate_key=None, headers=None, params=None, **_kwargs) -> Response:
        page = self._page(url)
        if page.status in (404, 410, 451):
            raise PermanentError(f"HTTP {page.status} for {url[:100]}")
        if page.status in (403, 429, 503):
            raise BlockedError(f"HTTP {page.status} from {rate_key}")
        if page.status >= 400:
            raise requests.HTTPError(f"HTTP {page.status}")
        return Response(page, url)

    def post(self, url, *, rate_key=None, headers=None, params=None, json=None, data=None,
             **_kwargs) -> Response:
        """``get``'s semantics; every body is kept in ``posts`` (Phase 7: TED, Contracts
        Finder and GeM take their query as a body)."""
        self.posts.append((url, json if json is not None else data))
        return self.get(url, rate_key=rate_key, headers=headers, params=params)

    def get_once(self, url, *, rate_key=None) -> Response:
        return Response(self._page(url), url)

    def probe(self, url, *, rate_key=None) -> int:
        return self._page(url).status

    def session_for(self, host: str):
        web = self

        class _Session:
            def get(self, url, timeout=None, **_kwargs):
                return Response(web._page(url), url)
        return _Session()

    soup = staticmethod(HttpClient.soup)
    looks_blocked = staticmethod(HttpClient.looks_blocked)

    def close(self) -> None:
        pass


def dead(host: str) -> DeadDomainError:
    return DeadDomainError(f"{host} does not resolve")


def kit_for(web: FakeWeb, tmp_path: Path, sink=None) -> Kit:
    failures = FailureLog(tmp_path / "failures.jsonl", sink=sink)
    return Kit(failures=failures, limiter=web.limiter, http=web,
               robots=RobotsCache(web, enabled=True))


def patch_kit(monkeypatch, web: FakeWeb) -> None:
    """Every Phase 6 session in this test builds its Kit on ``web`` (failures still recorded)."""
    def build(cls, context):
        config = context.config
        failures = FailureLog(config.path_for("failure_log", "logs/failures.jsonl"),
                              sink=getattr(context, "failure_sink", None))
        return Kit(failures=failures, limiter=web.limiter, http=web,
                   robots=RobotsCache(web, enabled=config.get("runtime.robots_txt")
                                      is not False))
    monkeypatch.setattr(Kit, "build", classmethod(build))


def project_rows(config, model) -> list[dict]:
    """Rows of ``model`` that belong to the config's project (a shared Postgres holds others)."""
    from sqlalchemy import select

    from leadscraper.db.models import Organization
    from leadscraper.db.store import LeadStore, store_target

    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        table = model.__table__
        query = select(table)
        if "project_id" in table.c:
            query = query.where(table.c.project_id == store.project_id)
        elif "organization_id" in table.c:
            orgs = Organization.__table__
            query = query.where(table.c.organization_id.in_(
                select(orgs.c.id).where(orgs.c.project_id == store.project_id)))
        return [dict(r) for r in store._conn.execute(query).mappings()]
    finally:
        store.close()


def run_database(tmp_path, name: str) -> str:
    """The opt-in engine database (LEADSCRAPER_TEST_ENGINE_DB_URL), else a SQLite file."""
    import os

    return os.environ.get("LEADSCRAPER_TEST_ENGINE_DB_URL") or \
        f"sqlite:///{tmp_path / name / 'app.sqlite3'}"


class _Probe:
    """Records the organisations local_business's enrichment is handed (and changes nothing)."""

    seen: list[str] = []


def probe_enricher():
    """An Enricher class that records its targets: what local_business would enrich."""
    from leadscraper.sources.base import Enricher

    class ProbeEnricher(Enricher):
        name = "probe"

        def targets(self, store):
            pairs = store.all_with_ids()
            _Probe.seen.extend(lead.business_name for _, lead in pairs)
            return pairs

        def enrich(self, lead):
            return None

    _Probe.seen = []
    return ProbeEnricher


def local_business_view(outcome) -> tuple[list[str], list[str]]:
    """What local_business itself put out: its final leads (CSV / Sheet / report) and the
    organisations its enrichment was handed."""
    pipeline = outcome.contexts["local_business"].pipeline
    seen, _Probe.seen = sorted(_Probe.seen), []          # one run's worth
    return sorted(lead.business_name for lead in pipeline.leads), seen
