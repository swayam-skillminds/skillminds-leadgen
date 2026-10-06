"""Venue run: extraction, classification, tiering, merge, dedup, locality, prompts.

Everything here is offline and uses synthetic listings shaped like what the
scrapers see, so a site redesign shows up as a live-run failure in the logs,
not as a test that quietly stopped testing anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from leadscraper.core import normalize
from leadscraper.ai import AIAssistant
from leadscraper.config import Config
from leadscraper.core.lead import Lead
from leadscraper.leadtypes.local_business.scoring import Scorer
from leadscraper.export.sheets import column_letter
from leadscraper.db.store import DedupOptions, LeadStore
from leadscraper.packs.hospitality.venues import (
    Brand, VenueSettings, annotate, classify_venue_type, compute_tier, coords_from_maps_url,
    detect_event_friendly, detect_pet_friendly, extract_amenities, nightly_prices,
    own_plate_prices, parse_bedrooms, parse_event_capacity, parse_guests,
)


@pytest.fixture(autouse=True)
def _reset_name_noise():
    """name_key's extra noise is process-global; never leak it between tests."""
    normalize.set_extra_name_noise([])
    yield
    normalize.set_extra_name_noise([])


def venue_config(tmp_path, **overrides) -> Config:
    data = {
        "city": "Nashik",
        "profile": "venues",
        "geo": {"center": [19.9975, 73.7898], "radius_km": 125,
                "exclude_keywords": ["shahapur", "bhiwandi"]},
        "localities": [
            {"name": "Nashik City", "center": [19.9975, 73.7898],
             "keywords": ["nashik", "gangapur road", "trimbak road"]},
            {"name": "Gangapur Dam Belt", "center": [20.03, 73.67], "keywords": ["gangapur", "sula"]},
            {"name": "Igatpuri", "center": [19.695, 73.5626], "keywords": ["igatpuri", "igtpuri", "ghoti"]},
            {"name": "Trimbakeshwar", "center": [19.9322, 73.5306], "keywords": ["trimbak", "anjaneri"]},
        ],
        "categories": [
            {"name": "Villas", "queries": ["luxury villa"], "sources": ["stub"]},
            {"name": "Lawns", "queries": ["wedding lawn"], "only_localities": ["Nashik City"]},
        ],
        "brands": [
            {"name": "StayVista", "aliases": ["vista rooms"], "tier": "premium",
             "phones": ["+918104954254"], "website": "https://www.stayvista.com"},
            {"name": "EKOSTAY", "aliases": ["ekostay luxe"], "tier": "premium"},
            {"name": "OYO", "aliases": ["oyo rooms"], "tier": "budget"},
        ],
        "scoring": {"weights": {"premium_signal": 0.4, "reputation": 0.25,
                                "contact_completeness": 0.2, "source_corroboration": 0.15}},
        "sources": {"stub": {"enabled": True, "min_delay": 0, "max_delay": 0}},
        "runtime": {
            "db_path": str(tmp_path / "venues.sqlite3"),
            "failure_log": str(tmp_path / "failures.jsonl"),
            "run_log": str(tmp_path / "run.log"),
            "csv_export": str(tmp_path / "venues.csv"),
            "sheets_flush_every": 1000, "max_retries": 1, "backoff_base": 0.01,
        },
        "sheets": {"enabled": False},
        "ai": {"enabled": False},
    }
    data.update(overrides)
    return Config(data)


def settings() -> VenueSettings:
    return VenueSettings(
        center=(19.9975, 73.7898),
        brands=[Brand("StayVista", ["vista rooms"], "premium"), Brand("EKOSTAY", [], "premium"),
                Brand("OYO", ["oyo rooms"], "budget")],
    )


# --- prices -----------------------------------------------------------------


def test_nightly_prices_read_the_common_platform_phrasings():
    assert nightly_prices("₹68,123(for 6 rooms) Per Night + Taxes") == [68123]
    assert nightly_prices("10 - 15 Guests | 5 Bedrooms | ₹34,375 / night + GST") == [34375]
    assert nightly_prices("₹1,18,300 for 5 nights, originally ₹1,35,914") == [23660]
    assert nightly_prices("₹51,837\nfor 1 night\nCheck-In") == [51837]


def test_nightly_prices_skip_add_ons_offers_and_deposits():
    text = ("Candlelight Dinner (for two) | Starting ₹8,000 / per night\n"
            "Bonfire | Starting ₹1,000 / per night\n"
            "NO-COST EMI and Extra 10% instant discount upto ₹50,000 per night\n"
            "Security deposit ₹5,000 per night\n"
            "Entire Home | ₹31,250 / night + GST")
    assert nightly_prices(text) == [31250]


def test_nightly_prices_keep_document_order_so_the_first_is_the_venues_own():
    page = "₹51,837\nfor 1 night\n... Similar stays ... ₹41,859\nfor 1 night"
    assert nightly_prices(page)[0] == 51837


def test_own_plate_prices_ignore_similar_venue_cards():
    text = ("₹ 1,999 per plate (taxes extra) Veg price ₹ 2,299 per plate Non Veg price ... "
            "Similar venues: Veg ₹ 1,950 per plate Non veg ₹ 2,150 per plate")
    assert own_plate_prices(text) == [1999, 2299]


# --- size, capacity, amenities ------------------------------------------------


def test_bedrooms_guests_and_capacity():
    assert parse_bedrooms("Amrapali-5BR-Luxury-Family-Villa") == 5
    assert parse_bedrooms("White House - 4-BHK Villa With Pool") == 4
    assert parse_bedrooms("2.5BR Private Pool Villa") is None      # not "5BR"
    assert parse_guests("10 - 15 Guests") == 15
    assert parse_guests("up to 18 guests") == 18
    assert parse_event_capacity(
        "accommodating up to 200 guests ... similar: 100-1500 pax") == 200


def test_amenities_and_policies():
    amenities = extract_amenities("Private infinity pool, lush lawns, bonfire and a games room")
    assert {"Private Pool", "Infinity Pool", "Lawn / Garden", "Bonfire", "Games Room"} <= set(amenities)
    assert "Swimming Pool" not in amenities
    assert detect_event_friendly("Is it possible to host events here? Yes, ideal for events.") is True
    assert detect_event_friendly("Parties are not allowed at the property") is False
    assert detect_event_friendly("A quiet place") is None
    assert detect_pet_friendly("Pets are usually not allowed here.") is False
    assert detect_pet_friendly("A pet-friendly villa") is True


def test_coords_prefer_the_pin_over_the_viewport():
    url = "https://www.google.com/maps/place/X/@19.70,73.50,15z/data=!3d19.7123!4d73.5678"
    assert coords_from_maps_url(url) == (19.7123, 73.5678)
    assert coords_from_maps_url("https://www.google.com/maps/place/X/@19.70,73.50,15z") == (19.70, 73.50)


# --- classification ------------------------------------------------------------


@pytest.mark.parametrize("name, ptype, pool, expected", [
    ("Mango Lakeview Villa near Sula Vineyards", "", True, "Private Pool Villa"),
    ("Soma Vine Village", "Winery", None, "Vineyard / Winery Resort"),
    ("Courtyard by Marriott Nashik", "Hotel", None, "Premium Hotel"),
    ("Hotel Sai Plaza", "Hotel", None, "Hotel"),
    ("Sai Shraddha Lawns", "Wedding venue", None, "Banquet / Event Lawn"),
    ("Tropical Retreat", "Resort", None, "Resort"),
    ("Wild Camp - The Nature Resort", "Luxury tent", None, "Glamping / Camp"),
    ("Shrimant Wada Royal Stay", "", None, "Heritage Property"),
    ("Root Farms", "Farm stay", None, "Farmhouse / Farm Stay"),
    ("1 BHK for couple close to waterfall", "Flat", None, "Boutique Stay / Homestay"),
    ("The Rainfield", "Villa", None, "Villa / Bungalow"),
    ("Konkan Villa Rental Agency", "Travel agency", None, "Brand / Operator"),
])
def test_classify_venue_type(name, ptype, pool, expected):
    lead = Lead(business_name=name, property_type=ptype, has_pool=pool)
    assert classify_venue_type(lead, settings())[0] == expected


@pytest.mark.parametrize("name, ptype, expected", [
    ("Prasanna Properties Nashik", "Real estate agency", "Not a Venue"),
    ("Green Village Farm House Nashik", "Real Estate Builders & Construction Company", "Not a Venue"),
    ("Touchwood | NA Plots, Villa & Land For Sale In Igatpuri", "", "Not a Venue"),
    ("Jawhar Tours and Travel", "Tour operator", "Not a Venue"),
    ("Nashik Bike On Rent", "Two Wheeler Rental Agency", "Not a Venue"),
    ("Villas N Fun Hospitality", "Travel agency", "Brand / Operator"),
    ("Sai Anand Service Apartment", "Short term apartment rental agency", "Boutique Stay / Homestay"),
])
def test_rental_and_travel_businesses_are_not_venues(name, ptype, expected):
    lead = Lead(business_name=name, property_type=ptype)
    assert classify_venue_type(lead, settings())[0] == expected


def test_a_derived_brand_row_is_reclassified_on_rescore():
    """Rows typed "Brand" by an older rule must not keep that type forever."""
    stale = Lead(business_name="Touchwood Lifespaces", record_type="Brand",
                 property_type="Real Estate Builders & Construction Company")
    annotate(stale, settings())
    assert (stale.venue_type, stale.record_type, stale.venue_tier) == (
        "Not a Venue", "Not a venue", "Excluded")
    genuine = Lead(business_name="StayVista", record_type="Brand", category="Brands & Operators")
    annotate(genuine, settings())
    assert genuine.venue_tier == "Operator"


def test_ai_not_a_venue_note_excludes_the_row():
    lead = Lead(business_name="Shree Rajbhog Thali", notes=["AI: not a bookable venue"])
    annotate(lead, settings())
    assert lead.venue_tier == "Excluded"


def test_a_flat_with_villa_words_in_its_title_is_still_a_villa():
    lead = Lead(business_name="4BHK Villa with Private Pool", property_type="Flat", has_pool=True)
    assert classify_venue_type(lead, settings())[0] == "Private Pool Villa"


# --- tiering ---------------------------------------------------------------------


def test_any_one_criterion_qualifies_and_price_lifts_to_luxury():
    s = settings()
    assert compute_tier(Lead(business_name="A", has_pool=True), s).tier == "Premium"
    pricey = Lead(business_name="B", has_pool=True, price_min=30000, price_basis="per night")
    assert compute_tier(pricey, s).tier == "Luxury"
    rated = Lead(business_name="C", gmaps_rating=4.5, gmaps_reviews=120)
    assert compute_tier(rated, s).tier == "Premium"
    thin = Lead(business_name="D", gmaps_rating=4.8, gmaps_reviews=5)   # too few reviews
    assert compute_tier(thin, s).tier == "Standard"


def test_price_alone_never_qualifies_but_a_budget_price_caps():
    s = settings()
    price_only = Lead(business_name="E", price_min=45000, price_basis="per night")
    assert compute_tier(price_only, s).tier == "Standard"
    cheap_pool = Lead(business_name="F", has_pool=True, price_min=2500, price_basis="per night")
    result = compute_tier(cheap_pool, s)
    assert result.tier == "Standard"
    assert any("price under" in r for r in result.reasons)


def test_platform_brand_and_unverified_tiers():
    s = settings()
    assert compute_tier(Lead(business_name="G", platforms=["stayvista"]), s).tier == "Premium"
    budget = Lead(business_name="H", managed_by="OYO", has_pool=True)
    assert compute_tier(budget, s).tier == "Standard"
    assert compute_tier(Lead(business_name="I"), s).tier == "Unverified"
    assert compute_tier(Lead(business_name="StayVista", record_type="Brand"), s).tier == "Operator"


def test_annotate_derives_operator_distance_and_type():
    lead = Lead(business_name="EKOSTAY Luxe - Hercules Villa", has_pool=True,
                latitude=19.695, longitude=73.5626)
    annotate(lead, settings())
    assert lead.managed_by == "EKOSTAY"
    assert lead.venue_type == "Private Pool Villa"
    assert lead.distance_km and 35 < lead.distance_km < 50
    assert lead.venue_tier in ("Premium", "Luxury")


def test_an_ai_classification_survives_rescoring():
    lead = Lead(business_name="Lakeshore", venue_type="Resort", venue_type_source="ai")
    annotate(lead, settings())
    assert lead.venue_type == "Resort"


def test_price_weighs_most_within_a_tier(tmp_path):
    scorer = Scorer(venue_config(tmp_path))
    cheap = scorer.apply(Lead(business_name="J", has_pool=True, price_min=9000, price_basis="per night"))
    dear = scorer.apply(Lead(business_name="K", has_pool=True, price_min=24000, price_basis="per night"))
    assert cheap.venue_tier == dear.venue_tier == "Premium"
    assert dear.score > cheap.score


# --- merge ---------------------------------------------------------------------------


def test_merge_venue_fields():
    ours = Lead(business_name="Mokshah", bedrooms=4, has_pool=None, pet_friendly=False,
                price_min=1999, price_max=2299, price_basis="per plate", price_source="wedmegood",
                platform_ratings={"stayvista": [4.9, 62]})
    theirs = Lead(business_name="Mokshah", bedrooms=6, has_pool=True, pet_friendly=True,
                  price_min=68000, price_max=68000, price_basis="per night", price_source="stayvista",
                  platform_ratings={"stayvista": [4.8, 80], "airbnb": [4.9, 12]},
                  latitude=19.77, longitude=73.75, amenities=["Lawn / Garden"])
    changed = ours.merge(theirs)
    assert ours.bedrooms == 6 and ours.has_pool is True and ours.pet_friendly is True
    assert (ours.price_min, ours.price_basis) == (68000, "per night")
    assert ours.platform_ratings == {"stayvista": [4.8, 80], "airbnb": [4.9, 12]}
    assert ours.latitude == 19.77 and "Lawn / Garden" in ours.amenities
    assert "price_min" in changed


def test_merge_widens_a_price_range_within_one_basis():
    ours = Lead(business_name="X", price_min=20000, price_max=20000, price_basis="per night",
                price_source="airbnb")
    ours.merge(Lead(business_name="X", price_min=26000, price_max=26000, price_basis="per night",
                    price_source="booking"))
    assert (ours.price_min, ours.price_max) == (20000, 26000)
    assert ours.price_source == "airbnb, booking"


def test_venue_row_layout():
    headers = Lead.row_headers("venues")
    assert headers[:5] == ["Tier", "Score", "Venue Name", "Record Type", "Venue Type"]
    assert "Price From (INR)" in headers and "Listing URLs" in headers
    assert Lead.row_headers() == Lead.row_headers("vendors")        # vendor layout untouched
    assert "Tier" not in Lead.row_headers()
    row = Lead(business_name="V", has_pool=True, event_friendly=False).to_venue_row()
    assert (row["Private Pool"], row["Event Friendly"], row["Pet Friendly"]) == ("Yes", "No", "")


def test_old_rows_without_venue_fields_still_load():
    legacy = '{"business_name": "Royal Caterers", "phones": ["+919820012345"], "score": 12.0}'
    lead = Lead.from_json(legacy)
    assert lead.business_name == "Royal Caterers" and lead.amenities == [] and lead.price_min is None


def test_column_letter():
    assert [column_letter(n) for n in (1, 26, 27, 52, 54)] == ["A", "Z", "AA", "AZ", "BB"]


# --- store --------------------------------------------------------------------------


def venue_store(tmp_path, config) -> LeadStore:
    return LeadStore(tmp_path / "s.sqlite3", Scorer(config), DedupOptions.from_config(config))


def test_a_shared_reservations_number_does_not_merge_different_villas(tmp_path):
    store = venue_store(tmp_path, venue_config(tmp_path))
    shared = "+918069160000"
    store.upsert(Lead(business_name="Amrapali", phones=[shared], locality="Igatpuri"))
    _, action, _ = store.upsert(Lead(business_name="Sol Mere", phones=[shared], locality="Igatpuri"))
    assert action == "new"
    _, action, _ = store.upsert(Lead(business_name="Amrapali Villa", phones=[shared], locality="Igatpuri"))
    assert action == "merged"
    assert store.count() == 2
    store.close()


def test_the_vendor_run_still_merges_on_phone_alone(tmp_path):
    store = LeadStore(tmp_path / "v.sqlite3", Scorer())
    store.upsert(Lead(business_name="Royal Caterers", phones=["+919820012345"], locality="Andheri"))
    _, action, _ = store.upsert(Lead(business_name="Totally Different", phones=["+919820012345"],
                                     locality="Dadar"))
    assert action == "merged"
    store.close()


def test_nearby_pins_with_similar_names_merge_across_localities(tmp_path):
    normalize.set_extra_name_noise(["villa", "igatpuri"])
    store = venue_store(tmp_path, venue_config(tmp_path))
    store.upsert(Lead(business_name="Mokshah", locality="Igatpuri", latitude=19.771756, longitude=73.748812))
    _, action, _ = store.upsert(Lead(business_name="Mokshah Luxe Villa", locality="Trimbakeshwar",
                                     latitude=19.7725, longitude=73.7490))
    assert action == "merged"
    store.close()


def test_distinctive_names_merge_across_localities_but_generic_ones_do_not(tmp_path):
    store = venue_store(tmp_path, venue_config(tmp_path))
    store.upsert(Lead(business_name="Vayavia The Frame", locality="Nashik City"))
    assert store.upsert(Lead(business_name="Vayavia-The Frame", locality="Igatpuri"))[1] == "merged"
    store.upsert(Lead(business_name="Lake View", locality="Nashik City"))
    assert store.upsert(Lead(business_name="Lake View", locality="Igatpuri"))[1] == "new"
    store.close()


def test_venue_sort_puts_operators_then_luxury_first(tmp_path):
    store = venue_store(tmp_path, venue_config(tmp_path))
    store.upsert(Lead(business_name="Plain Lodge", gmaps_rating=3.9, gmaps_reviews=10))
    store.upsert(Lead(business_name="Grand Pool Estate", has_pool=True, price_min=40000,
                      price_basis="per night"))
    store.upsert(Lead(business_name="StayVista", record_type="Brand", locality="Region-wide",
                      category="Brands & Operators"))
    store.upsert(Lead(business_name="Prasanna Properties", property_type="Real estate agency"))
    assert [lead.venue_tier for lead in store.sorted_leads()] == [
        "Operator", "Luxury", "Standard", "Excluded"]
    store.close()


# --- config: localities ----------------------------------------------------------------


def test_locality_for_prefers_specific_places_over_the_city_name(tmp_path):
    config = venue_config(tmp_path)
    assert config.locality_for("villa in nashik near sula vineyards").name == "Gangapur Dam Belt"
    assert config.locality_for("hotel on trimbak road nashik").name == "Nashik City"
    assert config.locality_for("Amrapali - Igtpuri, Nashik").name == "Igatpuri"
    assert config.locality_for("a villa somewhere") is None


def test_a_contradicting_pin_beats_the_keyword(tmp_path):
    config = venue_config(tmp_path)
    # "Gangapur" in the address, but pinned 2 km from the city centre.
    assert config.locality_for("Racca's Lawns, Gangapur Rd", 20.005, 73.775).name == "Nashik City"
    # An agreeing pin leaves the keyword's answer alone.
    assert config.locality_for("Ghoti, Igatpuri", 19.72, 73.60).name == "Igatpuri"


def test_category_source_and_locality_filters(tmp_path):
    config = venue_config(tmp_path)
    villas, lawns = config.categories
    assert villas.runs_source("stub") and not villas.runs_source("google_maps")
    assert lawns.runs_source("anything")
    igatpuri = config.select_localities(["Igatpuri"])[0]
    assert villas.runs_in(igatpuri) and not lawns.runs_in(igatpuri)


def test_search_terms_use_the_configured_city(tmp_path):
    loc = venue_config(tmp_path, localities=[{"name": "Sinnar", "aliases": ["Sinnar Taluka"]}]).localities[0]
    assert loc.search_terms == ["Sinnar Taluka", "Sinnar Nashik"]


# --- AI prompts ---------------------------------------------------------------------------


def test_mumbai_prompts_are_byte_identical_and_venue_prompts_are_not(tmp_path):
    mumbai = AIAssistant(Config({"ai": {"enabled": False}}))
    assert mumbai._prompt(AIAssistant.NAME_SYSTEM) == AIAssistant.NAME_SYSTEM
    nashik = AIAssistant(venue_config(tmp_path))
    prompt = nashik._prompt(AIAssistant.NAME_SYSTEM)
    assert "Mumbai" not in prompt and "Nashik" in prompt and "high-end venue list" in prompt
