"""Villa/stay platform parsers and the venue pipeline's scheduling, offline.

The fixtures are trimmed copies of the structures the live pages served in
September 2026 (StayVista / SaffronStays `__NEXT_DATA__`, Airbnb and Booking
search cards), cut down to the fields the parsers read.
"""

import csv
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from bs4 import BeautifulSoup

from leadscraper.core import normalize
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.engine.failures import FailureLog
from leadscraper.leadtypes.local_business.pipeline import Pipeline
from leadscraper.net.ratelimit import RateLimiter
from leadscraper.leadtypes.local_business.scoring import Scorer
from leadscraper.sources.base import Source
from leadscraper.packs.hospitality.platforms import (
    AirbnbSource, BookingSource, SaffronStaysSource, StayVistaSource, split_tagline,
)
from leadscraper.db.store import DedupOptions, LeadStore

from test_venues import venue_config


@pytest.fixture(autouse=True)
def _reset_name_noise():
    normalize.set_extra_name_noise([])
    yield
    normalize.set_extra_name_noise([])


class StubHttp:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, rate_key=None, **_):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"unexpected fetch {url}")
        text = self.pages[url]
        return SimpleNamespace(text=text, content=text.encode(), status_code=200, url=url)

    @staticmethod
    def soup(html):
        return BeautifulSoup(html, "lxml")


def make_ctx(tmp_path, config, pages=None):
    store = LeadStore(tmp_path / "ctx.sqlite3", Scorer(config), DedupOptions.from_config(config))
    return SimpleNamespace(config=config, http=StubHttp(pages or {}), limiter=RateLimiter(),
                           failures=FailureLog(tmp_path / "f.jsonl"), browser=None, store=store)


def page(next_data: dict, *, title="", jsonld=()) -> str:
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in jsonld)
    return (f"<html><head><title>{title}</title>{blocks}</head><body>"
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
            "</body></html>")


STAYVISTA_URL = "https://www.stayvista.com/villa/mokshah-6-bhk-villa-in-nashik-with-private-pool"
STAYVISTA_PAGE = page(
    {"props": {"pageProps": {"propertyDetailsObj": {"data": {
        "property_detail": {
            "vista_name": "Mokshah", "property_category": "villa", "city": "igatpuri",
            "state": "maharashtra", "number_of_rooms": 6, "max_occupancy": 18,
            "no_of_private_bathrooms": 6, "no_of_shared_bathrooms": 2,
            "latitude": "19.771756000", "longitude": "73.748812600", "ratings": 4.9,
            "feedbacks_count": 62, "checkin_time": "02:00 PM", "checkout_time": "11:00 AM",
            "luxury_collection": True, "tags": [{"label": "Best Rated"}],
            "city_tags": "nashik,igatpuri",
        },
        "amenities": {"sorted": [{"name": "Lawn"}, {"name": "Private Pool"}]},
        "usps": [{"name": "Bar"}],
        "faq": [{"question": "Is it possible to host events here?",
                 "answer": "Yes, the place is ideal for events."},
                {"question": "Can I bring a pet along?", "answer": "Pets are usually not allowed here."}],
        "property_contact_number": "9167928471",
    }}}}},
    title="Book Your Luxurious Stay @ Mokshah In Igatpuri",
    jsonld=[{"@type": "Organization", "name": "StayVista", "url": "https://www.stayvista.com/",
             "contactPoint": {"@type": "ContactPoint", "telephone": "08069236750"},
             "sameAs": ["https://www.instagram.com/stayvista_official/"]}],
)

SAFFRON_URL = "https://www.saffronstays.com/view/amrapali-5br-luxury-family-villa-serene-orchard-nashik-3ysp"
SAFFRON_PAGE = page({"props": {"pageProps": {"data": {"listing": {"data": {
    "ListingList": {"file_reference": "Amrapali - Igtpuri, Nashik, Maharashtra", "category": "Villa",
                    "per_night_charge": 31250, "min_rooms": 5, "no_of_bathrooms": 6,
                    "max_capacity": 15, "location": "Nashik, Maharashtra, India",
                    "in_time": "1", "in_time_format": "PM", "out_time": "10", "out_time_format": "AM",
                    "GMB_LINK": "https://g.page/r/CWurTROXY3WnEBM/review", "signature": "Gold",
                    "about_villa": "<p>A 5-bedroom villa in an orchard with a private pool.</p>"},
    "AmenitiesList": {"11": "WiFi", "85": "Lawns"},
    "NotAmnitiesList": {"19": "Pet Friendly"},
    "SuitablesList": {"28": "Pool Villa", "32": "Large Groups"},
    "RatingObj": {"rating_val": 4.7, "review": 40},
}}}}}})


def test_stayvista_page_becomes_a_rich_property_lead(tmp_path):
    config = venue_config(tmp_path)
    source = StayVistaSource(make_ctx(tmp_path, config, {STAYVISTA_URL: STAYVISTA_PAGE}))
    source.cfg = {"render_for_price": False}
    lead = source._read_property(STAYVISTA_URL, "Platform Listings")
    assert lead.business_name == "Mokshah"
    assert (lead.bedrooms, lead.bathrooms, lead.max_guests) == (6, 8, 18)
    assert lead.has_pool is True and lead.has_lawn is True
    assert lead.event_friendly is True and lead.pet_friendly is False
    assert lead.platform_ratings == {"stayvista": [4.9, 62]}
    assert lead.managed_by == "StayVista" and lead.platforms == ["stayvista"]
    assert lead.locality == "Igatpuri"
    assert lead.phones == ["+919167928471"]
    assert lead.checkin_checkout == "02:00 PM / 11:00 AM"
    brand = source.brand_lead()
    assert brand.record_type == "Brand" and brand.phones == ["+918069236750"]
    assert brand.instagram == "stayvista_official"
    assert source.brand_lead() is None          # only once per run


def test_saffronstays_reads_price_size_and_village(tmp_path):
    config = venue_config(tmp_path)
    source = SaffronStaysSource(make_ctx(tmp_path, config, {SAFFRON_URL: SAFFRON_PAGE}))
    lead = source._read_property(SAFFRON_URL, "Platform Listings")
    assert lead.business_name == "Amrapali"
    assert (lead.price_min, lead.price_basis, lead.price_source) == (31250, "per night", "saffronstays")
    assert (lead.bedrooms, lead.bathrooms, lead.max_guests) == (5, 6, 15)
    assert lead.has_pool is True and lead.pet_friendly is False
    assert lead.locality == "Igatpuri"                 # from "Igtpuri", not the district
    assert lead.gmaps_url.startswith("https://g.page/")
    assert lead.checkin_checkout == "1 PM / 10 AM"


def test_sitemap_keeps_only_regional_property_pages(tmp_path):
    config = venue_config(tmp_path)
    sitemap = "https://www.stayvista.com/villas-sitemap.xml"
    xml = ("<urlset>"
           "<url><loc>https://www.stayvista.com/villa/mokshah-6-bhk-villa-in-nashik</loc></url>"
           "<url><loc>https://www.stayvista.com/villa/bel-air-4-bhk-villa-in-lonavala</loc></url>"
           "<url><loc>https://www.stayvista.com/maharashtra/villas-in-igatpuri</loc></url>"
           "<url><loc>https://www.stayvista.com/villa/zen-haven-igatpuri-5-bhk</loc></url>"
           "</urlset>")
    source = StayVistaSource(make_ctx(tmp_path, config, {sitemap: xml}))
    assert source.property_urls() == [
        "https://www.stayvista.com/villa/mokshah-6-bhk-villa-in-nashik",
        "https://www.stayvista.com/villa/zen-haven-igatpuri-5-bhk",
    ]


def test_airbnb_card():
    raw = {"name": "The Backyard Story | 3BHK Villa with Pool & Garden",
           "url": "/rooms/1751221891817322618?check_in=2026-10-01",
           "text": "Guest favourite\nHome in Igatpuri\nThe Backyard Story | 3BHK Villa with Pool\n"
                   "3 bedrooms\n5 beds\n4 bathrooms\n4.92 (37)\n₹1,34,520 for 5 nights"}
    card = AirbnbSource.parse_card(SimpleNamespace(base_url="https://www.airbnb.co.in"), raw)
    assert card["name"] == "The Backyard Story"
    assert card["url"] == "https://www.airbnb.co.in/rooms/1751221891817322618"
    assert (card["property_type"], card["address"]) == ("Home", "Igatpuri")
    assert (card["bedrooms"], card["bathrooms"]) == (3, 4)
    assert (card["rating"], card["reviews"]) == (4.92, 37)
    assert card["price"] == 26904 and card["has_pool"] is True
    assert "Airbnb Guest favourite" in card["notes"]


def test_booking_card_and_saturday_search():
    raw = {"title": "EKOSTAY Luxe - Hercules Villa",
           "url": "https://www.booking.com/hotel/in/ekostay-hercules-villa.en-gb.html?aid=1",
           "score": "Scored 9.2\n9.2\nSuperb\n10 reviews", "price": "₹ 46,575",
           "stars_label": "", "stars_count": 8, "distance": "1.5 km from centre", "text": "Pool"}
    stub = SimpleNamespace(base_url="https://www.booking.com")
    card = BookingSource.parse_card(stub, raw, "Villa")
    assert card["name"] == "EKOSTAY Luxe - Hercules Villa"
    assert card["url"] == "https://www.booking.com/hotel/in/ekostay-hercules-villa.html"
    assert (card["rating"], card["reviews"], card["price"]) == (4.6, 10, 46575)
    assert card["stars"] is None                  # element counts are not star ratings

    booking = SimpleNamespace(cfg={"days_ahead": 21}, base_url="https://www.booking.com")
    url = BookingSource.search_url(booking, "Igatpuri, Maharashtra", "213", today=dt.date(2026, 9, 30))
    assert "checkin=2026-10-24&checkout=2026-10-25" in url      # a Saturday night
    assert "nflt=ht_id%3D213" in url


@pytest.mark.parametrize("title, expected", [
    ("The Backyard Story | 3BHK Villa with Pool", ("The Backyard Story", "3BHK Villa with Pool")),
    ("EKOSTAY Luxe - Hercules Villa", ("EKOSTAY Luxe - Hercules Villa", "")),
    ("Vayavia-The Frame - Premium Stay, Perfect", ("Vayavia-The Frame", "Premium Stay, Perfect")),
    ("Mokshah", ("Mokshah", "")),
])
def test_split_tagline(title, expected):
    assert split_tagline(title) == expected


# --- pipeline scheduling ----------------------------------------------------------


class Recorder(Source):
    calls: list = []

    def discover(self, locality, category):
        type(self).calls.append((self.name, locality and locality.name, category and category.name))
        yield from ()


def scoped(name, scope):
    return type(f"{name}_cls", (Recorder,), {"name": name, "scope": scope, "calls": []})


def test_run_locality_combo_and_city_level_scopes(tmp_path, monkeypatch):
    run_once, per_locality = scoped("sitemap", "run"), scoped("search", "locality")
    per_combo, city_level = scoped("stub", "combo"), scoped("citydir", "combo")
    registry = {"sitemap": run_once, "search": per_locality, "stub": per_combo, "citydir": city_level}
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", registry)
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    sources = {name: {"enabled": True, "min_delay": 0, "max_delay": 0} for name in registry}
    sources["citydir"]["city_level"] = True
    config = venue_config(
        tmp_path,
        categories=[{"name": "Villas", "queries": ["villa"], "sources": ["stub", "citydir"]},
                    {"name": "Lawns", "queries": ["lawn"], "sources": ["stub"],
                     "only_localities": ["Nashik City"]}],
        sources=sources,
    )
    pipe = Pipeline(config, localities=config.localities, categories=config.categories)
    pipe.run()

    assert run_once.calls == [("sitemap", None, None)]
    assert [c[1] for c in per_locality.calls] == [loc.name for loc in config.localities]
    assert sorted((c[1], c[2]) for c in per_combo.calls) == sorted(
        [(loc.name, "Villas") for loc in config.localities] + [("Nashik City", "Lawns")])
    assert city_level.calls == [("citydir", "Nashik City", "Villas")]
    assert pipe.stats.combos_done == len(config.localities) + 1


class BrandedVillas(Source):
    name = "stub"
    scope = "run"

    def discover(self, locality, category):
        for name in ("Mokshah", "Zen Haven"):
            lead = self.make_lead(business_name=name, category="Platform Listings",
                                  locality="Igatpuri")
            lead.managed_by, lead.platforms, lead.has_pool = "StayVista", ["stayvista"], True
            lead.price_min = lead.price_max = 30000.0
            lead.price_basis = "per night"
            yield lead
        brand = self.make_lead(business_name="StayVista", category="Brands & Operators",
                               locality="Region-wide", phones=["+918069236750"])
        brand.record_type = "Brand"
        yield brand


def test_brand_rollup_merges_into_the_platform_brand_row(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": BrandedVillas})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    config = venue_config(tmp_path)
    pipe = Pipeline(config, localities=config.localities[:1], categories=config.categories[:1])
    pipe.run()

    brands = [lead for lead in pipe.leads if lead.record_type == "Brand"]
    assert len(brands) == 1, "config phone differs from the site's, yet it is one brand row"
    brand = brands[0]
    assert set(brand.phones) == {"+918069236750", "+918104954254"}
    assert any(n.startswith("Manages 2 listed properties") for n in brand.notes)
    assert brand.venue_tier == "Operator"

    with open(tmp_path / "venues.csv", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0])[:3] == ["Tier", "Score", "Venue Name"]
    assert [r["Tier"] for r in rows] == ["Operator", "Luxury", "Luxury"]
    assert {r["Booking Contact"] for r in rows if r["Tier"] == "Luxury"} == {
        "StayVista: +91 80 6923 6750 / +91 81049 54254"}


# --- targeted Maps lookup -----------------------------------------------------------

from leadscraper.core.lead import Lead
from leadscraper.sources.maps_lookup import MapsLookupEnricher, distinctive_words


def lookup(tmp_path):
    config = venue_config(tmp_path, sources={"google_maps": {"enabled": True},
                                             "maps_lookup": {"enabled": True}})
    return MapsLookupEnricher(make_ctx(tmp_path, config))


IGATPURI_PIN = "https://www.google.com/maps/place/X/data=!3d19.70!4d73.56"


def test_distinctive_words_drop_villa_boilerplate():
    assert distinctive_words("Luxury 4 Bedrooms Villa W/ Private Pool") == []
    assert distinctive_words("The Backyard Story | 3BHK Villa") == ["backyard", "story"]


def test_lookup_accepts_a_matching_name_in_the_same_place(tmp_path):
    lead = Lead(business_name="The Backyard Story", locality="Igatpuri")
    data = {"name": "The Backyard Story Villa Igatpuri", "phone": "+91 98220 12345",
            "website": "https://backyardstory.in", "url": IGATPURI_PIN, "rating": "4.8",
            "reviews": "(56)"}
    partial = lookup(tmp_path).match(lead, data, IGATPURI_PIN)
    assert isinstance(partial, Lead)
    assert partial.phones == ["+919822012345"] and partial.website == "https://backyardstory.in/"
    assert partial.latitude == 19.70 and partial.gmaps_reviews == 56


def test_lookup_rejects_a_different_venue_or_a_distant_namesake(tmp_path):
    enricher = lookup(tmp_path)
    lead = Lead(business_name="Saukhya Farm", locality="Igatpuri")
    other = {"name": "Saukhya Residency", "phone": "+91 98220 12345", "url": IGATPURI_PIN}
    assert isinstance(enricher.match(lead, other, IGATPURI_PIN), str)
    far = "https://www.google.com/maps/place/X/data=!3d18.52!4d73.85"     # Pune
    namesake = {"name": "Saukhya Farm", "phone": "+91 98220 12345", "url": far}
    assert "elsewhere" in enricher.match(lead, namesake, far)


def test_lookup_ignores_a_platform_page_as_the_website(tmp_path):
    lead = Lead(business_name="Grevayu", locality="Igatpuri")
    data = {"name": "SaffronStays Grevayu - Villa In Nashik", "url": IGATPURI_PIN,
            "website": "https://www.saffronstays.com/view/grevayu-6-bedroom"}
    assert "no phone or own website" in lookup(tmp_path).match(lead, data, IGATPURI_PIN)
