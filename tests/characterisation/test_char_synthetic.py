"""Characterisation on hand-built leads: always runs, goldens committed.

Pins, for both profiles and under the frozen configs: dedup/merge decisions,
score breakdowns, venue typing and tiering, the sort order, and the exact CSV
column layout and cell rendering. The goldens record what the code does
today; a diff here is a behaviour change to justify, not a test to "fix".

Regenerate after an intended change:  CHAR_UPDATE=1 pytest tests/characterisation
"""

from __future__ import annotations

import copy
import csv
import re

import pytest

from snapshot_lib import (
    TIMESTAMP_COLUMNS, apply_name_noise, check_golden, comparable_row, load_config, open_store,
)
from leadscraper.export import write_csv
from leadscraper.models import Lead
from leadscraper.scoring import Scorer

pytestmark = pytest.mark.characterisation

T0 = 1_757_000_000.0      # fixed epoch so rows don't depend on when the test ran

PLACE = ("https://www.google.com/maps/place/X/data=!4m7!3m6"
         "!1s0x3be7c83806155505:0xd93628e9d1588bd3!8m2!3d19.1069285!4d72.8639382")


def lead(**fields) -> Lead:
    fields.setdefault("first_seen", T0)
    fields.setdefault("last_updated", T0)
    return Lead(**fields)


def summary(item: Lead) -> dict:
    """The merge-relevant fields; timestamps excluded (merge stamps time.time())."""
    return {
        "business_name": item.business_name, "locality": item.locality,
        "category": item.category, "phones": item.phones, "emails": item.emails,
        "website": item.website, "sources": item.sources, "notes": item.notes,
        "gmaps_rating": item.gmaps_rating, "gmaps_reviews": item.gmaps_reviews,
        "score": item.score,
    }


# --- dedup / merge decisions -------------------------------------------------

VENDOR_PAIRS = {
    "shared_phone_across_localities": (
        lead(business_name="Royal Caterers", locality="Andheri", phones=["+919820012345"],
             sources=["google_maps"], gmaps_rating=4.2, gmaps_reviews=40),
        lead(business_name="Royal Catering Co", locality="Dadar", phones=["+919820012345"],
             sources=["justdial"], gmaps_rating=4.6, gmaps_reviews=310)),
    "same_name_same_locality": (
        lead(business_name="Sharma Caterers Pvt Ltd", locality="Powai", sources=["google_maps"]),
        lead(business_name="Sharma Catering Services", locality="Powai", sources=["sulekha"],
             emails=["Hello@Sharma.in"])),
    "same_name_other_locality": (
        lead(business_name="Royal Banquets", locality="Andheri", sources=["google_maps"]),
        lead(business_name="Royal Banquets", locality="Dadar", sources=["google_maps"])),
    "same_name_contradicting_phones": (
        lead(business_name="Royal Banquets", locality="Andheri", phones=["+919820011111"]),
        lead(business_name="Royal Banquets", locality="Andheri", phones=["+919820022222"])),
    "fuzzy_name_same_locality": (
        lead(business_name="Shree Ganesh Decorators", locality="Dadar", sources=["google_maps"]),
        lead(business_name="Shri Ganesh Decorators", locality="Dadar", sources=["sulekha"])),
    "same_maps_place_other_locality": (
        lead(business_name="Flora Store", locality="Andheri", gmaps_url=PLACE,
             sources=["google_maps"]),
        lead(business_name="Flora Store", locality="Powai", gmaps_url=PLACE,
             sources=["google_maps"])),
    "name_extension_wins": (
        lead(business_name="Galaxy Caterers", locality="Andheri", phones=["+919820033333"]),
        lead(business_name="Galaxy Caterers - St. Arnold's Lawns", locality="Andheri",
             phones=["+919820033333"], address="St. Arnold's Lawns, Andheri West")),
    "seo_title_kept_as_alias": (
        lead(business_name="Royal Caterers", locality="Andheri", phones=["+919820044444"]),
        lead(business_name="Royal Caterers | Best Caterers in Mumbai 2024", locality="Andheri",
             phones=["+919820044444"], sources=["google_search"])),
}

VENUE_PAIRS = {
    "shared_phone_dissimilar_names": (
        lead(business_name="StayVista at Mokshah", locality="Igatpuri", phones=["+918069160000"],
             platforms=["stayvista"]),
        lead(business_name="StayVista at Casa Del Verde", locality="Igatpuri",
             phones=["+918069160000"], platforms=["stayvista"])),
    "shared_phone_similar_names": (
        lead(business_name="Riddhi Siddhi Family Guest House", locality="Nashik City",
             phones=["+917020681214"]),
        lead(business_name="Riddhi Siddhi Guest House", locality="Nashik City",
             phones=["+917020681214"])),
    "pins_within_radius_other_locality": (
        lead(business_name="Mokshah", locality="Igatpuri", latitude=19.6950, longitude=73.5620,
             sources=["stayvista"]),
        lead(business_name="Mokshah Villa Igatpuri", locality="Nashik Region (unplaced)",
             latitude=19.6960, longitude=73.5625, sources=["google_maps"])),
    "pins_far_apart_other_locality": (
        lead(business_name="Mokshah", locality="Igatpuri", latitude=19.6950, longitude=73.5620),
        lead(business_name="Mokshah Retreat", locality="Trimbakeshwar",
             latitude=19.9330, longitude=73.5290)),
    "distinctive_name_other_locality": (
        lead(business_name="Bellissimo Panorama Hilltop", locality="Igatpuri",
             sources=["airbnb"]),
        lead(business_name="Bellissimo Panorama Hilltop", locality="Nashik City",
             sources=["booking"])),
    "generic_name_other_locality": (
        lead(business_name="Green Villa", locality="Igatpuri", sources=["airbnb"]),
        lead(business_name="Green Villa", locality="Nashik City", sources=["booking"])),
}


def run_pairs(profile: str, pairs: dict, tmp_path) -> dict:
    config = load_config(profile)
    apply_name_noise(config)
    outcome = {}
    for name, (first, second) in pairs.items():
        store = open_store(tmp_path / f"{profile}-{name}.sqlite3", config)
        try:
            store.upsert(copy.deepcopy(first))
            _, action, changed = store.upsert(copy.deepcopy(second))
            outcome[name] = {
                "action": action,
                "changed": sorted(changed),
                "stored": store.count(),
                "records": [summary(item) for _, item in store.all_with_ids()],
            }
        finally:
            store.close()
    return outcome


def test_vendor_dedup_decisions(tmp_path):
    check_golden("dedup_vendors", run_pairs("mumbai", VENDOR_PAIRS, tmp_path))


def test_venue_dedup_decisions(tmp_path):
    check_golden("dedup_venues", run_pairs("nashik", VENUE_PAIRS, tmp_path))


def test_error_titles_are_refused(tmp_path):
    config = load_config("mumbai")
    store = open_store(tmp_path / "errors.sqlite3", config)
    try:
        refused = []
        for title in ("Access Denied", "Just a moment...", "403 Forbidden",
                      "Access Control Systems Pvt Ltd"):
            try:
                store.upsert(lead(business_name=title, locality="Andheri"))
            except ValueError:
                refused.append(title)
        assert refused == ["Access Denied", "Just a moment...", "403 Forbidden"]
    finally:
        store.close()


# --- scoring -----------------------------------------------------------------

VENDOR_LEADS = {
    "bare_one_source": lead(business_name="Tiny Tiffin Co", locality="Andheri",
                            category="Caterers", sources=["google_maps"]),
    "strong_maps_two_sources": lead(
        business_name="Royal Caterers", locality="Andheri", category="Caterers",
        phones=["+919820012345"], emails=["hello@royal.in"], website="https://royal.in",
        sources=["google_maps", "website"], gmaps_rating=4.6, gmaps_reviews=310),
    "one_star_high_volume": lead(business_name="Grim Grub", locality="Dadar",
                                 category="Caterers", sources=["google_maps"],
                                 gmaps_rating=1.0, gmaps_reviews=500),
    "reviews_without_rating": lead(business_name="Quiet Kitchen", locality="Dadar",
                                   category="Caterers", sources=["google_maps"],
                                   gmaps_reviews=50),
    "wedmegood_featured": lead(business_name="Bloom Decor", locality="Powai",
                               category="Decorators", sources=["wedmegood", "google_maps"],
                               wmg_rating=4.8, wmg_reviews=45, wmg_featured=True,
                               gmaps_rating=4.4, gmaps_reviews=80),
    "every_contact_channel": lead(
        business_name="Full House Events", locality="Powai", category="Event Planners",
        phones=["+919820055555"], emails=["a@fullhouse.in"], website="https://fullhouse.in",
        address="Hiranandani Gardens, Powai", instagram="fullhouse", whatsapp="+919820055555",
        facebook="https://facebook.com/fullhouse", linkedin="https://linkedin.com/company/fh",
        sources=["google_maps", "website", "instagram", "facebook"]),
}

VENUE_LEADS = {
    "brand_row": lead(business_name="StayVista", category="Brands & Operators",
                      record_type="Brand", sources=["rollup"]),
    "platform_pool_villa_luxury": lead(
        business_name="StayVista at The Glasscape", locality="Nashik City",
        property_type="Villa", platforms=["stayvista"], has_pool=True, bedrooms=5,
        price_min=30000, price_max=42000, price_basis="per night", price_source="stayvista",
        platform_ratings={"stayvista": [4.9, 36]}, latitude=19.9671, longitude=73.6162,
        phones=["+918069160000"], sources=["stayvista", "google_maps"]),
    "rated_farmhouse_premium": lead(
        business_name="Green Acres Farm Stay", locality="Gangapur Dam Belt",
        property_type="Farm stay", gmaps_rating=4.5, gmaps_reviews=50,
        sources=["google_maps"], latitude=20.03, longitude=73.68),
    "budget_price_capped": lead(
        business_name="Budget Nest Homestay", locality="Igatpuri", property_type="Homestay",
        price_min=3000, price_basis="per night", gmaps_rating=4.7, gmaps_reviews=120,
        sources=["airbnb"]),
    "no_evidence_unverified": lead(business_name="Mystery Retreat", locality="Igatpuri",
                                   sources=["google_search"]),
    "ai_not_a_venue": lead(business_name="Nashik Property Dealers", locality="Nashik City",
                           notes=["AI: not a bookable venue"], sources=["google_maps"]),
    "premium_hotel_chain": lead(
        business_name="Courtyard by Marriott Nashik", locality="Nashik City",
        property_type="5-star hotel", gmaps_rating=4.4, gmaps_reviews=2500,
        sources=["google_maps", "booking"]),
    "banquet_lawn_per_plate": lead(
        business_name="Sai Shraddha Lawns", locality="Nashik City",
        property_type="Wedding venue", price_min=1800, price_basis="per plate",
        event_capacity=800, has_lawn=True, gmaps_rating=4.2, gmaps_reviews=600,
        sources=["wedmegood", "google_maps"]),
}


def score_table(profile: str, leads: dict) -> dict:
    config = load_config(profile)
    apply_name_noise(config)
    scorer = Scorer(config)
    table = {}
    for name, item in leads.items():
        item = copy.deepcopy(item)
        scorer.apply(item)
        entry = {"score": item.score, "breakdown": scorer.breakdown(item).as_dict()}
        if config.is_venue_run:
            entry.update(tier=item.venue_tier, tier_points=item.tier_points,
                         tier_reasons=item.tier_reasons, venue_type=item.venue_type,
                         venue_type_source=item.venue_type_source,
                         record_type=item.record_type, managed_by=item.managed_by,
                         distance_km=item.distance_km, hotel_class=item.hotel_class)
        table[name] = entry
    return table


def test_vendor_scores():
    check_golden("scores_vendors", score_table("mumbai", VENDOR_LEADS))


def test_venue_scores_types_and_tiers():
    check_golden("scores_venues", score_table("nashik", VENUE_LEADS))


# --- sort order and CSV layout -----------------------------------------------

def stored_and_sorted(profile: str, leads: dict, tmp_path) -> list[Lead]:
    config = load_config(profile)
    apply_name_noise(config)
    store = open_store(tmp_path / f"{profile}-sort.sqlite3", config)
    try:
        for item in leads.values():
            store.upsert(copy.deepcopy(item))
        return store.sorted_leads()
    finally:
        store.close()


@pytest.mark.parametrize("profile,leads", [("mumbai", VENDOR_LEADS), ("nashik", VENUE_LEADS)])
def test_sort_order(profile, leads, tmp_path):
    ordered = stored_and_sorted(profile, leads, tmp_path)
    check_golden(f"sort_{profile}", [item.business_name for item in ordered])


@pytest.mark.parametrize("profile,leads", [("mumbai", VENDOR_LEADS), ("nashik", VENUE_LEADS)])
def test_csv_layout(profile, leads, tmp_path):
    """Header order and every rendered cell, read back from the written file."""
    config = load_config(profile)
    ordered = stored_and_sorted(profile, leads, tmp_path)
    path = write_csv(ordered, tmp_path / f"{profile}.csv", config.profile)

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV is written with a UTF-8 BOM (Excel-friendly)"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    stamp = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
    for row in rows:
        for column in TIMESTAMP_COLUMNS:
            assert stamp.match(row.pop(column)), f"{column} is not YYYY-MM-DD HH:MM"

    check_golden(f"csv_{profile}", {
        "headers": Lead.row_headers(config.profile),
        "rows": rows,
        "comparable_rows": [comparable_row(item, config.profile) for item in ordered],
    })
