"""Offline tests for the wedding-directory sources.

Everything here is stubbed: no HTTP, no Playwright. The point is the URL
resolution that used to be hardcoded -- a config slug that the live site no
longer serves must lose to the discovered one, an unknown category must skip
rather than 404, and the SERP fallback must stay off unless it is asked for.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from leadscraper.config import Category, Config, Locality
from leadscraper.http import HttpClient
from leadscraper.ratelimit import PermanentError
from leadscraper.sources import DISCOVERY_SOURCES
from leadscraper.sources import directories as directories_mod
from leadscraper.sources.base import SourceContext
from leadscraper.sources.directories import WEDMEGOOD_STATIC_SLUGS, WedMeGoodSource

# Enough of the real listing page to exercise the href harvester.
NAV_SLUGS = (
    "wedding-photographers", "wedding-catering", "wedding-decorators",
    "wedding-venues", "wedding-planners", "djs", "bridal-makeup",
    "mehendi-artists", "wedding-cakes", "wedding-favors",
)


def listing_html(city: str = "mumbai", profiles: tuple[str, ...] = ()) -> str:
    nav = "\n".join(
        f'    <li><a href="/vendors/{city}/{slug}/">{slug.replace("-", " ").title()}</a></li>'
        for slug in NAV_SLUGS
    )
    cards = "\n".join(
        f'    <div class="vendor-card"><a href="/profile/{handle}">{handle}</a></div>'
        for handle in profiles
    )
    # Padded past _MIN_LISTING_BYTES so city probing reads it as a real page.
    filler = "<p>" + ("Find the best wedding vendors in your city. " * 60) + "</p>"
    return (
        "<!doctype html>\n<html><head><title>Vendors</title></head><body>\n"
        f'  <ul class="category-nav">\n{nav}\n  </ul>\n'
        f'  <div class="results">\n{cards}\n  </div>\n'
        f"  {filler}\n</body></html>"
    )


class StubResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


class StubHttp:
    """Serves canned pages by URL prefix and remembers what was requested."""

    soup = staticmethod(HttpClient.soup)

    def __init__(self, pages: dict[str, StubResponse] | None = None, error: Exception | None = None):
        self.pages = pages or {}
        self.error = error
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        for prefix, response in self.pages.items():
            if url.startswith(prefix):
                return response
        raise PermanentError(f"HTTP 404 for {url}")


class StubLimiter:
    def configure(self, *args, **kwargs):
        pass

    def wait(self, *args, **kwargs):
        pass


class StubFailures:
    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def record(self, source, reason, **kwargs):
        self.records.append((source, reason, kwargs))


class StubStore:
    def __init__(self):
        self.seen: set[str] = set()

    def already_seen(self, url):
        return url in self.seen

    def mark_seen(self, url, source):
        self.seen.add(url)


def make_source(pages=None, error=None, city="Mumbai", **source_cfg):
    config = Config({
        "city": city,
        "sources": {"wedmegood": {"enabled": True, "min_delay": 0, "max_delay": 0,
                                  **source_cfg}},
    })
    http = StubHttp(pages=pages, error=error)
    ctx = SourceContext(config=config, http=http, limiter=StubLimiter(),
                        failures=StubFailures(), browser=None, store=StubStore())
    return WedMeGoodSource(ctx)


def mumbai_pages(profiles: tuple[str, ...] = ()):
    return {"https://www.wedmegood.com/vendors/mumbai/":
            StubResponse(listing_html("mumbai", profiles))}


@pytest.fixture(autouse=True)
def clear_caches():
    """Slug/city memos live for the process, so they must not leak between tests."""
    directories_mod._SLUG_CACHE.clear()
    directories_mod._CITY_CACHE.clear()
    yield
    directories_mod._SLUG_CACHE.clear()
    directories_mod._CITY_CACHE.clear()


LOCALITY = Locality(name="Andheri", aliases=["Andheri East Mumbai"])


# --- slug discovery and resolution ---------------------------------------

def test_discovered_slugs_parses_listing_hrefs():
    source = make_source(pages=mumbai_pages())
    assert source.discovered_slugs("mumbai") == frozenset(NAV_SLUGS)


def test_discovered_slugs_is_memoised_per_city():
    source = make_source(pages=mumbai_pages())
    source.discovered_slugs("mumbai")
    source.discovered_slugs("mumbai")
    assert len(source.ctx.http.urls) == 1


def test_discovery_failure_falls_back_to_static_slugs(caplog, monkeypatch):
    source = make_source(error=PermanentError("HTTP 404"))
    monkeypatch.setattr(WedMeGoodSource, "_render",
                        lambda self, url: (_ for _ in ()).throw(RuntimeError("no browser")))

    with caplog.at_level(logging.WARNING, logger="leadscraper.sources.directories"):
        slugs = source.discovered_slugs("mumbai")

    assert slugs == WEDMEGOOD_STATIC_SLUGS
    assert any("discovery FAILED" in rec.getMessage()
               for rec in caplog.records if rec.levelno >= logging.WARNING)


def test_resolve_slug_prefers_live_slug_over_stale_config():
    # config.yaml says "caterers"; WedMeGood has served "wedding-catering"
    # since some taxonomy change, and every "caterers" URL 404s.
    source = make_source(pages=mumbai_pages())
    category = Category(name="Caterers", directory_slugs={"wedmegood": "caterers"})
    assert source.resolve_slug(category, "mumbai") == "wedding-catering"


def test_resolve_slug_keeps_a_configured_slug_that_is_live():
    source = make_source(pages=mumbai_pages())
    category = Category(name="Venues", directory_slugs={"wedmegood": "wedding-venues"})
    assert source.resolve_slug(category, "mumbai") == "wedding-venues"


def test_resolve_slug_fuzzy_matches_photographers():
    source = make_source(pages=mumbai_pages())
    category = Category(name="Photographers")
    assert source.resolve_slug(category, "mumbai") == "wedding-photographers"


def test_resolve_slug_returns_none_for_a_category_the_site_lacks(caplog):
    source = make_source(pages=mumbai_pages())
    category = Category(name="Florists", directory_slugs={"wedmegood": "florist"})

    with caplog.at_level(logging.INFO, logger="leadscraper.sources.directories"):
        assert source.resolve_slug(category, "mumbai") is None

    assert any("no slug for category Florists" in rec.getMessage()
               for rec in caplog.records)


def test_discover_yields_nothing_for_an_unmapped_category():
    source = make_source(pages=mumbai_pages())
    category = Category(name="Florists", directory_slugs={"wedmegood": "florist"})

    assert list(source.discover(LOCALITY, category)) == []
    # A category the directory does not carry is a skip, not a failure.
    assert source.ctx.failures.records == []


# --- city slug probing ----------------------------------------------------

def test_city_slug_slugifies_multiword_cities():
    source = make_source(pages=mumbai_pages())
    assert source.city_slug("Navi Mumbai") == "navi-mumbai"


def test_resolve_city_slug_tries_variants_until_one_answers():
    # The real shape of the bug: "delhi" 404s, "new-delhi" serves a listing.
    pages = {
        "https://www.wedmegood.com/vendors/new-delhi/": StubResponse(listing_html("new-delhi")),
        # A 200 with a bot-wall-sized body must not count as a live city.
        "https://www.wedmegood.com/vendors/delhi/": StubResponse("<html></html>"),
    }
    source = make_source(pages=pages, city="Delhi")

    assert source.resolve_city_slug("Delhi") == "new-delhi"
    assert source.ctx.http.urls[0].startswith("https://www.wedmegood.com/vendors/delhi/")


def test_resolve_city_slug_returns_none_when_no_variant_works(caplog):
    source = make_source(pages={}, city="Atlantis")

    with caplog.at_level(logging.INFO, logger="leadscraper.sources.directories"):
        assert source.resolve_city_slug("Atlantis") is None

    assert any("no city slug worked" in rec.getMessage() for rec in caplog.records)


def test_discover_skips_when_the_city_does_not_resolve():
    source = make_source(pages={}, city="Atlantis")
    category = Category(name="Caterers", directory_slugs={"wedmegood": "caterers"})
    assert list(source.discover(LOCALITY, category)) == []


# --- the SERP fallback is opt-in -----------------------------------------

def _no_profiles(monkeypatch, source):
    monkeypatch.setattr(WedMeGoodSource, "_render", lambda self, url: "<html></html>")
    called: list[str] = []
    monkeypatch.setattr(WedMeGoodSource, "_serp_fallback",
                        lambda self, slug, locality, category: called.append(slug) or [])
    return called


def test_serp_fallback_is_off_by_default(monkeypatch, caplog):
    source = make_source(pages=mumbai_pages())
    called = _no_profiles(monkeypatch, source)
    category = Category(name="Caterers", directory_slugs={"wedmegood": "caterers"})

    with caplog.at_level(logging.INFO, logger="leadscraper.sources.directories"):
        urls = source._collect_profile_urls("wedding-catering", "mumbai", LOCALITY, category)

    assert urls == []
    assert called == []
    assert any("serp_fallback is off" in rec.getMessage() for rec in caplog.records)


def test_serp_fallback_runs_when_enabled(monkeypatch):
    source = make_source(pages=mumbai_pages(), serp_fallback=True)
    called = _no_profiles(monkeypatch, source)
    category = Category(name="Caterers", directory_slugs={"wedmegood": "caterers"})

    source._collect_profile_urls("wedding-catering", "mumbai", LOCALITY, category)
    assert called == ["wedding-catering"]


def test_empty_listing_still_records_no_listings(monkeypatch):
    source = make_source(pages=mumbai_pages())
    _no_profiles(monkeypatch, source)
    category = Category(name="Caterers", directory_slugs={"wedmegood": "caterers"})

    assert list(source.discover(LOCALITY, category)) == []
    assert [reason for _, reason, _ in source.ctx.failures.records] == ["no_listings"]


# --- listing crawl uses the resolved city and slug ------------------------

def test_listing_urls_use_the_configured_city(monkeypatch):
    source = make_source(pages=mumbai_pages(), city="Navi Mumbai")
    requested: list[str] = []
    monkeypatch.setattr(WedMeGoodSource, "_render",
                        lambda self, url: requested.append(url) or "<html></html>")
    category = Category(name="Caterers", directory_slugs={"wedmegood": "caterers"})

    source._collect_profile_urls("wedding-catering", "navi-mumbai", LOCALITY, category)
    assert requested[0] == ("https://www.wedmegood.com/vendors/navi-mumbai/"
                            "wedding-catering/?page=1")


# --- ShaadiSaga is gone ---------------------------------------------------

def test_shaadisaga_source_is_deleted():
    import leadscraper.sources as sources_pkg

    assert not hasattr(sources_pkg, "ShaadiSagaSource")
    assert not hasattr(directories_mod, "ShaadiSagaSource")
    assert "shaadisaga" not in DISCOVERY_SOURCES


# --- Justdial / Sulekha: SERP lookups are opt-in ---------------------------
#
# REBUILD Part 0.4 turned the SERP fallback off in directories.py, but the
# local directories still ran one `site:` query per locality x category on
# top of their listing template -- the same CAPTCHA trap by another door.

class RecordingSerp:
    queries: list[str] = []

    def __init__(self, ctx):
        pass

    def search(self, query, pages=1, per_page=20):
        RecordingSerp.queries.append(query)
        return []


def local_source(cls, **source_cfg):
    config = Config({"city": "Mumbai",
                     "sources": {cls.name: {"enabled": True, "min_delay": 0, "max_delay": 0,
                                            **source_cfg}}})
    ctx = SourceContext(config=config, http=StubHttp(), limiter=StubLimiter(),
                        failures=StubFailures(), browser=None, store=StubStore())
    return cls(ctx)


@pytest.mark.parametrize("name", ["justdial", "sulekha"])
def test_local_directory_makes_no_serp_query_by_default(monkeypatch, name):
    from leadscraper.sources import local_directories

    monkeypatch.setattr(local_directories, "SerpClient", RecordingSerp)
    RecordingSerp.queries = []
    source = local_source(DISCOVERY_SOURCES[name])
    category = Category(name="Caterers", queries=["catering services"])

    urls = source._listing_urls(LOCALITY, category, max_pages=1)

    assert RecordingSerp.queries == []
    assert urls and all(source.host in url for url in urls), "listing template still used"


@pytest.mark.parametrize("name", ["justdial", "sulekha"])
def test_local_directory_serp_runs_when_opted_in(monkeypatch, name):
    from leadscraper.sources import local_directories

    monkeypatch.setattr(local_directories, "SerpClient", RecordingSerp)
    RecordingSerp.queries = []
    source = local_source(DISCOVERY_SOURCES[name], serp_fallback=True)
    category = Category(name="Caterers", queries=["catering services"])

    source._listing_urls(LOCALITY, category, max_pages=1)
    assert RecordingSerp.queries == [f"site:{source.host} catering services Andheri East Mumbai"]
