"""Offline tests for the Google Maps source's search loop (no Playwright).

Found by the first live check (docs/live-check-1.md): once a combination had
its `max_results_per_query` places, Maps still ran every remaining query x
locality phrasing -- 5 of 6 searches at quick depth -- and threw the results
away. Each one is a request to a host that blocks quickly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadscraper.config import Category, Config, Locality
from leadscraper.sources.base import SourceContext
from leadscraper.sources.google_maps import GoogleMapsSource

LOCALITY = Locality(name="Andheri", aliases=["Andheri East Mumbai", "Andheri Mumbai"])
CATEGORY = Category(name="Caterers",
                    queries=["catering services", "wedding caterers", "event catering"])


class _Limiter:
    def configure(self, *args, **kwargs):
        pass


class _Failures:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def record(self, source, reason, **kwargs):
        self.records.append((source, reason))


def maps_source(results_per_search: int, **cfg) -> tuple[GoogleMapsSource, list[str]]:
    config = Config({"sources": {"google_maps": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0, **cfg}}})
    ctx = SourceContext(config=config, http=None, limiter=_Limiter(), failures=_Failures(),
                        browser=None, store=None)
    source = GoogleMapsSource(ctx)
    searches: list[str] = []

    def search(query, scroll_rounds, max_results):
        searches.append(query)
        n = len(searches)
        return [f"https://www.google.com/maps/place/p{n}x{i}/data=!1s0x{n}:{i}"
                for i in range(results_per_search)]

    source._search = search
    source._fetch_place = lambda url, locality, category: source.make_lead(
        business_name=url, category=category.name, locality=locality.name)
    return source, searches


def test_maps_stops_searching_once_the_combination_has_its_places():
    source, searches = maps_source(results_per_search=10, max_results_per_query=10)
    leads = list(source.discover(LOCALITY, CATEGORY))
    assert len(leads) == 10
    assert searches == ["catering services in Andheri East Mumbai"]


def test_maps_keeps_searching_while_the_combination_is_short():
    source, searches = maps_source(results_per_search=4, max_results_per_query=10)
    leads = list(source.discover(LOCALITY, CATEGORY))
    assert len(leads) == 10
    assert searches == ["catering services in Andheri East Mumbai",
                        "catering services in Andheri Mumbai",
                        "wedding caterers in Andheri East Mumbai"]
