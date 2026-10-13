"""Country generalisation (Phase 1.3) and pack applicability (Phase 1.4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from leadscraper.config import Config
from leadscraper.core.country import UnknownCountry, country, currency_pattern, format_money
from leadscraper.core.geo import GeocodeCache, Geocoder
from leadscraper.core.normalize import extract_phones, name_key, normalize_phone, phone_display
from leadscraper.packs import all_packs, city_slug, inactive_reason
from leadscraper.packs.hospitality.venues import VenueSettings, compute_tier, nightly_prices
from leadscraper.sources.registry import DISCOVERY_SOURCES, ENRICHMENT_SOURCES
from leadscraper.sources.website import WebsiteEnricher


# --- phones -------------------------------------------------------------------------

@pytest.mark.parametrize("region,text,expected", [
    ("US", "Call (201) 555-0123 today", ["+12015550123"]),
    ("GB", "Ring 0121 234 5678", ["+441212345678"]),
    ("AE", "Call 04 234 5678", ["+97142345678"]),
    ("IN", "Call 98200 12345", ["+919820012345"]),
])
def test_phones_parse_in_the_runs_country(region, text, expected):
    assert extract_phones(text, region) == expected


def test_without_a_region_only_international_numbers_parse():
    assert extract_phones("Call 98200 12345 or +1 201 555 0123") == ["+12015550123"]
    assert normalize_phone("9820012345") is None
    assert normalize_phone("9820012345", "IN") == "+919820012345"


def test_a_us_number_is_not_misread_as_indian():
    # Before Phase 1 every number was parsed as IN.
    assert extract_phones("(201) 555-0123", "US") == ["+12015550123"]
    assert phone_display("+12015550123") == "+1 201-555-0123"


# --- currency, locale, postal codes ---------------------------------------------------------

def test_country_facts():
    us = country("us")
    assert (us.currency, us.locale, us.google_gl, us.ddg_region) == ("USD", "en-US", "us", "us-en")
    assert country("IN").accept_language == "en-IN,en-GB;q=0.9,en;q=0.8"
    br = country("BR")                          # not tabled: neutral defaults, phones still work
    assert br.currency == "" and br.calling_code == 55
    with pytest.raises(UnknownCountry):
        country("XX")


def test_prices_are_read_in_the_runs_currency():
    assert nightly_prices("₹25,000 per night") == [25000.0]
    assert nightly_prices("$350 per night", "USD") == [350.0]
    assert nightly_prices("£220 / night", "GBP") == [220.0]
    assert nightly_prices("$350 per night") == []          # an INR run ignores dollars
    assert format_money(25000, "INR") == "₹25,000"
    assert format_money(350, "USD") == "$350"
    assert format_money(500, "AED") == "AED 500"
    assert currency_pattern("INR") == r"(?:₹|\&\#8377;|\bINR\b|\bRs\.?)\s?"


def test_tier_reasons_use_the_runs_currency():
    from leadscraper.core.lead import Lead

    usd = VenueSettings(currency="USD", night_price_points=((300, 3),))
    lead = Lead(business_name="Hill Country Villa", price_min=450, price_max=450,
                price_basis="per night", has_pool=True)
    assert "$450/night" in compute_tier(lead, usd).reasons


def test_addresses_end_in_the_countrys_postal_code():
    text = ("Bright Smile Dental, 1200 Congress Avenue, Austin, Texas 78701. "
            "Royal Caterers, Shop 4, Andheri West, Mumbai, Maharashtra 400053.")
    soup = BeautifulSoup("<p></p>", "lxml")
    assert WebsiteEnricher._address(soup, text, "Austin", "Texas", country("US").postal_code) \
        .endswith("Texas 78701")
    assert WebsiteEnricher._address(soup, text, "Mumbai", "Maharashtra",
                                    country("IN").postal_code).endswith("400053")


# --- names --------------------------------------------------------------------------------

def test_name_noise_comes_from_the_run_not_the_code():
    assert name_key("Royal Caterers Mumbai") == "royal mumbai"     # no run noise configured
    Config({"country": "IN", "city": "Mumbai"}).apply_name_noise()
    assert name_key("Royal Caterers Mumbai India") == "royal"
    Config({"country": "US", "city": "Austin", "geo": {"admin_area": "Texas"}}).apply_name_noise()
    assert name_key("Smile Dental Austin Texas USA") == "smile dental"


# --- packs ---------------------------------------------------------------------------------

def test_every_registered_source_belongs_to_core_or_one_pack():
    # ai_web_search and ai_directory are core too, built only when their AI feature is on.
    core = {"google_maps", "google_search", "website", "maps_lookup", "ai_web_search",
            "ai_directory"}
    by_pack = {pack.key: {cls.name for cls in pack.sources} for pack in all_packs()}
    assert by_pack == {
        "in": {"justdial", "sulekha", "indiamart"},
        "events": {"wedmegood", "weddingwire"},
        "hospitality": {"stayvista", "saffronstays", "elivaas", "airbnb", "booking"},
        "social": {"facebook", "instagram", "linkedin_serp"},
        # Phase 6: the UK pack has no local_business source, only a company registry
        # (decision_makers' Companies House directors).
        "uk": set(),
        # Phase 7: packs for tender portals only.
        "eu": set(),
        "us": set(),
    }
    assert {pack.key: [cls.name for cls in pack.registries] for pack in all_packs()
            if pack.registries} == {"uk": ["companies_house"]}
    assert {pack.key: [cls.name for cls in pack.tenders] for pack in all_packs()
            if pack.tenders} == {"in": ["cppp", "gem"],
                                 "uk": ["find_a_tender", "contracts_finder"],
                                 "eu": ["ted"], "us": ["sam_gov"]}
    assert {pack.key: [cls.name for cls in pack.incorporations] for pack in all_packs()
            if pack.incorporations} == {"uk": ["companies_house"]}
    every = set(DISCOVERY_SOURCES) | set(ENRICHMENT_SOURCES)
    assert every == core | set().union(*by_pack.values())


def test_registry_keeps_the_original_run_order():
    assert list(DISCOVERY_SOURCES) == [
        "google_maps", "google_search", "wedmegood", "weddingwire", "indiamart", "justdial",
        "sulekha", "facebook", "linkedin_serp", "stayvista", "saffronstays", "elivaas",
        "airbnb", "booking", "ai_web_search", "ai_directory"]   # AI sources last (Phase 5)
    assert list(ENRICHMENT_SOURCES) == ["maps_lookup", "website", "instagram"]


@pytest.mark.parametrize("source,country_code,verticals,active", [
    ("justdial", "IN", ["events"], True),
    ("justdial", "US", ["events"], False),
    ("wedmegood", "IN", ["events"], True),
    ("wedmegood", "IN", ["hospitality"], False),       # vertical not in the run
    ("wedmegood", "US", ["events"], False),            # India-only site
    ("stayvista", "IN", ["hospitality"], True),
    ("stayvista", "US", ["hospitality"], False),       # narrower than its pack
    ("airbnb", "US", ["hospitality"], True),
    ("airbnb", "US", [], False),
    ("facebook", "US", [], True),
    ("google_maps", "AE", [], True),
])
def test_pack_applicability(source, country_code, verticals, active):
    cls = DISCOVERY_SOURCES.get(source) or ENRICHMENT_SOURCES[source]
    reason = inactive_reason(cls, country_code, verticals)
    assert (reason is None) is active, reason


def test_directory_city_slugs_come_from_the_pack():
    assert city_slug("justdial", "Bengaluru") == "Bangalore"
    assert city_slug("sulekha", "New Delhi") == "delhi"
    assert city_slug("wedmegood", "Navi Mumbai") == "navi-mumbai"


# --- geocoding -----------------------------------------------------------------------------

AUSTIN = [{"lat": "30.2711286", "lon": "-97.7436995", "display_name": "Austin, Travis County, Texas",
           "boundingbox": ["30.0985133", "30.5166255", "-97.9367663", "-97.5605288"],
           "address": {"city": "Austin", "state": "Texas", "country_code": "us"}}]


def test_geocoder_is_polite_and_caches(tmp_path):
    calls = []

    def fetch(url, params, headers):
        calls.append((url, params, headers))
        return AUSTIN if "austin" in params["q"].lower() else []

    geocoder = Geocoder(cache=GeocodeCache(tmp_path / "geo.json"), fetch=fetch, min_interval=0)
    place = geocoder.geocode("Austin", "US")
    assert place.center == (30.2711286, -97.7436995)
    assert place.admin_area == "Texas" and place.country_code == "US"
    assert 20 < place.radius_km < 40
    url, params, headers = calls[0]
    assert url.startswith("https://nominatim.openstreetmap.org/")
    assert params["countrycodes"] == "us" and "leadscraper" in headers["User-Agent"]

    assert geocoder.geocode("austin", "US").center == place.center      # cached
    assert geocoder.geocode("Atlantis", "US") is None                   # a miss...
    assert geocoder.geocode("Atlantis", "US") is None                   # ...is cached too
    assert len(calls) == 2
    reloaded = Geocoder(cache=GeocodeCache(tmp_path / "geo.json"), fetch=fetch, min_interval=0)
    assert reloaded.geocode("Austin", "US").admin_area == "Texas"
    assert len(calls) == 2


def test_geocoder_failure_is_not_fatal_and_not_cached(tmp_path):
    def broken(url, params, headers):
        raise ConnectionError("offline")

    geocoder = Geocoder(cache=GeocodeCache(tmp_path / "geo.json"), fetch=broken, min_interval=0)
    assert geocoder.geocode("Austin", "US") is None
    assert not (tmp_path / "geo.json").exists()


def test_resolve_geography_fills_only_what_the_config_left_out(tmp_path):
    from leadscraper.config.compile import compile_run_config
    from leadscraper.config.geography import needs_geocoding, resolve_geography
    from leadscraper.config.schema import RunConfig

    calls = []

    def fetch(url, params, headers):
        calls.append(params["q"])
        return AUSTIN

    geocoder = Geocoder(cache=GeocodeCache(tmp_path / "geo.json"), fetch=fetch, min_interval=0)
    rc = RunConfig.model_validate({
        "name": "Austin dentists", "project": "austin",
        "geography": {"country": "US", "places": ["Austin"]},
        "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}}})
    assert needs_geocoding(rc)
    resolved = resolve_geography(rc, geocoder)
    config = compile_run_config(resolved)
    assert config.geo_center == (30.2711286, -97.7436995)
    assert config.admin_area == "Texas"
    assert config.region_radius_km is None          # a vendor run's radius is never guessed
    assert not needs_geocoding(resolved)
    assert calls == ["Austin"]

    # The legacy presets state everything: no lookups at all.
    for name in ("mumbai-event-vendors", "nashik-venues"):
        preset = Config.load(Path(__file__).resolve().parent.parent / "presets" / f"{name}.yaml")
        assert not needs_geocoding(preset.run_config)
