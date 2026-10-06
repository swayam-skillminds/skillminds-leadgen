"""Unit tests for the parts that must be right for everything else to work:
normalisation, dedup/merge, and scoring."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from leadscraper.models import Lead
from leadscraper.normalize import (
    extract_emails, extract_facebook, extract_instagram, extract_linkedin,
    extract_phones, extract_whatsapp, is_vendor_website, looks_like_seo_title,
    maps_place_key, name_key,
    normalize_phone, normalize_url, parse_rating, parse_review_count,
)
from leadscraper.scoring import Scorer
from leadscraper.store import LeadStore


# --- phones ----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("+91 98200 12345", "+919820012345"),
    ("9820012345", "+919820012345"),
    ("098200 12345", "+919820012345"),
    ("+91-98200-12345", "+919820012345"),
    ("022 2345 6789", "+912223456789"),
])
def test_normalize_phone_variants(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "12", "0000000000", "99"])
def test_normalize_phone_rejects_junk(raw):
    assert normalize_phone(raw) is None


def test_extract_phones_from_contact_blob():
    text = "Call us: +91 98200 12345 or 022-23456789. Fax 1234"
    found = extract_phones(text)
    assert "+919820012345" in found
    assert "+912223456789" in found


def test_extract_phones_dedupes_same_number_written_differently():
    text = "Ring 9820012345 or +91 98200 12345"
    assert extract_phones(text).count("+919820012345") == 1


# --- emails ----------------------------------------------------------------

def test_extract_emails_basic_and_obfuscated():
    text = "Mail info@royalcaterers.in or sales [at] royalcaterers [dot] in"
    found = extract_emails(text)
    assert "info@royalcaterers.in" in found
    assert "sales@royalcaterers.in" in found


def test_extract_emails_filters_noise():
    text = "noreply@site.com logo2x@2x.png hello@realvendor.com wix@wixpress.com"
    found = extract_emails(text)
    assert found == ["hello@realvendor.com"]


# --- names -----------------------------------------------------------------

def test_name_key_collapses_legal_and_category_noise():
    assert name_key("Sharma Caterers Pvt. Ltd.") == name_key("Sharma Catering Services")


def test_name_key_keeps_distinct_businesses_distinct():
    assert name_key("Royal Banquets") != name_key("Regal Banquets")


# --- urls / handles --------------------------------------------------------

def test_normalize_url_strips_tracking_and_www():
    assert normalize_url("https://www.Example.com/contact/?utm_source=x&id=3") == \
        "https://example.com/contact?id=3"


def test_is_vendor_website_rejects_aggregators():
    assert is_vendor_website("https://sharmacaterers.in")
    assert not is_vendor_website("https://www.justdial.com/Mumbai/abc")
    assert not is_vendor_website("https://instagram.com/sharma")


def test_social_extractors():
    assert extract_instagram("Follow https://www.instagram.com/mumbai.caterers/") == "mumbai.caterers"
    assert extract_linkedin("see linkedin.com/in/rahul-sharma-123") == \
        "https://www.linkedin.com/in/rahul-sharma-123"
    assert extract_facebook("https://facebook.com/RoyalBanquetsMumbai") == \
        "https://www.facebook.com/RoyalBanquetsMumbai"
    assert extract_whatsapp("chat https://wa.me/919820012345") == "https://wa.me/919820012345"


# --- numbers ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("4.5", 4.5), ("4,5", 4.5), ("4.5/5", 4.5), (None, None), ("9.1", None)])
def test_parse_rating(raw, expected):
    assert parse_rating(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1,234 reviews", 1234), ("(89)", 89), ("2.1K reviews", 2100), ("no data", None),
])
def test_parse_review_count(raw, expected):
    assert parse_review_count(raw) == expected


# --- merge -----------------------------------------------------------------

def test_merge_unions_lists_and_fills_blanks():
    a = Lead(business_name="Royal Caterers", phones=["+919820012345"], sources=["google_maps"])
    b = Lead(business_name="Royal Caterers", emails=["info@royal.in"],
             website="https://royal.in", sources=["wedmegood"])
    changed = a.merge(b)
    assert a.emails == ["info@royal.in"]
    assert a.website == "https://royal.in"
    assert set(a.sources) == {"google_maps", "wedmegood"}
    assert "emails" in changed and "website" in changed


def test_merge_prefers_rating_backed_by_more_reviews():
    a = Lead(business_name="X", gmaps_rating=5.0, gmaps_reviews=3)
    b = Lead(business_name="X", gmaps_rating=4.3, gmaps_reviews=520)
    a.merge(b)
    assert a.gmaps_reviews == 520 and a.gmaps_rating == 4.3


def test_merge_keeps_featured_sticky():
    a = Lead(business_name="X", wmg_featured=True)
    a.merge(Lead(business_name="X", wmg_featured=False))
    assert a.wmg_featured is True


# --- store / dedup ---------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = LeadStore(tmp_path / "t.sqlite3")
    yield s
    s.close()


def test_dedup_by_shared_phone_across_localities(store):
    id1, act1, _ = store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                                     phones=["+919820012345"], sources=["google_maps"]))
    id2, act2, _ = store.upsert(Lead(business_name="Royal Catering Co", locality="Dadar",
                                     phones=["+919820012345"], sources=["justdial"]))
    assert act1 == "new" and act2 == "merged" and id1 == id2
    assert store.count() == 1


def test_dedup_by_name_in_same_locality(store):
    store.upsert(Lead(business_name="Sharma Caterers Pvt Ltd", locality="Powai",
                      sources=["google_maps"]))
    _, action, _ = store.upsert(Lead(business_name="Sharma Catering Services",
                                     locality="Powai", sources=["sulekha"]))
    assert action == "merged"
    assert store.count() == 1


def test_same_name_different_phones_stays_separate(store):
    store.upsert(Lead(business_name="Royal Banquets", locality="Andheri",
                      phones=["+919820011111"], sources=["google_maps"]))
    _, action, _ = store.upsert(Lead(business_name="Royal Banquets", locality="Andheri",
                                     phones=["+919820022222"], sources=["google_maps"]))
    assert action == "new"
    assert store.count() == 2


@pytest.mark.parametrize("name,expected", [
    ("Vaviya Events – Event Management Company in Mumbai | Corporate Event Planner", True),
    ("Galaxy Banquet Hall | Wedding Party Hall", True),
    ("Homefood Kravings - Best Tiffin Service in Andheri", True),
    ("Galaxy Caterers - St. Arnold's Lawns", False),
    ("Zen Haven - Igatpuri", False),
    ("Royal Caterers", False),
    ("", False),
])
def test_looks_like_seo_title(name, expected):
    assert looks_like_seo_title(name) is expected


# Same place, three spellings: a search-result URL, the feature id alone, a
# share link. 0xd93628e9d1588bd3 == 15651742539677469651.
PLACE_URL = ("https://www.google.com/maps/place/Aayoni+Banquet/data=!4m7!3m6"
             "!1s0x3be7c83806155505:0xd93628e9d1588bd3!8m2!3d19.10!4d72.86?hl=en")
OTHER_PLACE_URL = ("https://www.google.com/maps/place/Other/data=!4m7!3m6"
                   "!1s0x3be7c83806155505:0x1111111111111111!8m2!3d19.10!4d72.86")


def test_maps_place_key_reads_feature_id_and_cid():
    assert maps_place_key(PLACE_URL) == "cid:15651742539677469651"
    assert maps_place_key("https://maps.google.com/?cid=15651742539677469651") \
        == "cid:15651742539677469651"
    assert maps_place_key("https://www.google.com/maps/search/caterers") is None
    assert maps_place_key(None) is None


def test_same_maps_place_merges_across_localities(store):
    """Regression (REBUILD Part 0.6): the Mumbai DB held 25 places stored once per
    locality searched -- 'Flora Store' six times -- because nothing compared places."""
    id1, _, _ = store.upsert(Lead(business_name="Flora Store - Flower Bouquets in Andheri",
                                  locality="Andheri", gmaps_url=PLACE_URL,
                                  sources=["google_maps"]))
    id2, action, _ = store.upsert(Lead(business_name="Flora Store - Flower Bouquets in Andheri",
                                       locality="Powai", gmaps_url=PLACE_URL,
                                       sources=["google_maps"]))
    assert action in ("merged", "unchanged") and id1 == id2
    assert store.count() == 1


def test_same_maps_place_with_contradicting_phones_stays_separate(store):
    store.upsert(Lead(business_name="Aayoni Banquet", locality="Andheri",
                      phones=["+919820011111"], gmaps_url=PLACE_URL))
    _, action, _ = store.upsert(Lead(business_name="Aayoni Hall", locality="Powai",
                                     phones=["+919820022222"], gmaps_url=PLACE_URL))
    assert action == "new"
    assert store.count() == 2


def test_a_place_learned_through_a_merge_is_indexed(store):
    """`merge` keeps the first gmaps_url; the second place must still identify the lead."""
    store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                      phones=["+919820012345"], gmaps_url=PLACE_URL))
    store.upsert(Lead(business_name="Royal Caterers", locality="Dadar",
                      phones=["+919820012345"], gmaps_url=OTHER_PLACE_URL))
    _, action, _ = store.upsert(Lead(business_name="Royal Catering Hall", locality="Worli",
                                     gmaps_url=OTHER_PLACE_URL))
    assert action == "merged"
    assert store.count() == 1


def test_place_index_is_backfilled_for_a_store_written_before_it(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = LeadStore(path)
    old.upsert(Lead(business_name="Aayoni Banquet", locality="Andheri", gmaps_url=PLACE_URL))
    old._conn.execute("DELETE FROM place_index")       # as written by the old code
    old.close()

    reopened = LeadStore(path)
    try:
        _, action, _ = reopened.upsert(Lead(business_name="Aayoni Banquet", locality="Powai",
                                            gmaps_url=PLACE_URL))
        assert action in ("merged", "unchanged")
        assert reopened.count() == 1
    finally:
        reopened.close()


def test_upsert_rejects_nameless_lead(store):
    with pytest.raises(ValueError):
        store.upsert(Lead(business_name="   ", phones=["+919820012345"]))


def test_sorted_leads_groups_by_locality_then_score(store):
    store.upsert(Lead(business_name="Low", locality="Andheri", category="Caterers",
                      sources=["google_maps"]))
    store.upsert(Lead(business_name="High", locality="Andheri", category="Caterers",
                      phones=["+919820012345"], emails=["a@b.com"],
                      website="https://x.in", address="Some road, Andheri",
                      gmaps_rating=4.8, gmaps_reviews=900,
                      sources=["google_maps", "wedmegood", "justdial"]))
    names = [lead.business_name for lead in store.sorted_leads()]
    assert names == ["High", "Low"]


# --- scoring ---------------------------------------------------------------

def test_score_rewards_reviews_sources_and_completeness():
    scorer = Scorer()
    weak = Lead(business_name="Weak", sources=["google_maps"])
    strong = Lead(business_name="Strong", phones=["+919820012345"], emails=["a@b.com"],
                  website="https://x.in", address="Road", instagram="x",
                  gmaps_rating=4.7, gmaps_reviews=800,
                  wmg_rating=4.6, wmg_reviews=120, wmg_featured=True,
                  sources=["google_maps", "wedmegood", "justdial", "indiamart"])
    assert scorer.score(strong) > scorer.score(weak)
    assert 0 <= scorer.score(weak) <= 100
    assert 0 <= scorer.score(strong) <= 100


def test_low_signal_leads_are_kept_not_zeroed_out():
    """Low-signal listings must still be storable and rankable, never dropped."""
    scorer = Scorer()
    lead = Lead(business_name="Tiny Vendor", sources=["sulekha"])
    assert scorer.score(lead) >= 0


def test_one_star_listing_scores_below_five_star_at_same_volume():
    scorer = Scorer()
    bad = Lead(business_name="Bad", gmaps_rating=1.2, gmaps_reviews=200, sources=["google_maps"])
    good = Lead(business_name="Good", gmaps_rating=4.9, gmaps_reviews=200, sources=["google_maps"])
    assert scorer.score(good) > scorer.score(bad)


# --- parser cross-contamination guards -------------------------------------

def test_rating_parser_rejects_a_review_count():
    """'1,234 reviews' must not become a 1.0 rating."""
    assert parse_rating("1,234 reviews") is None


def test_review_parser_rejects_a_rating():
    """'4.5 stars' must not become 4 reviews."""
    assert parse_review_count("4.5 stars") is None
    assert parse_review_count("4.5") is None


def test_review_parser_still_reads_real_counts():
    assert parse_review_count("(1,234)") == 1234
    assert parse_review_count("973") == 973
    assert parse_review_count("281 reviews") == 281


# --- name merge guard ------------------------------------------------------

def test_merge_accepts_a_name_that_extends_ours():
    a = Lead(business_name="Galaxy Caterers")
    a.merge(Lead(business_name="Galaxy Caterers - St. Arnolds Lawns & Banquet"))
    assert a.business_name == "Galaxy Caterers - St. Arnolds Lawns & Banquet"


def test_merge_rejects_seo_title_and_keeps_it_as_an_alias():
    a = Lead(business_name="Royal Caterers")
    a.merge(Lead(business_name="Top 10 Wedding Caterers You Must Book In Mumbai 2024"))
    assert a.business_name == "Royal Caterers"
    assert any("also known as" in note for note in a.notes)



def test_merge_rejects_an_seo_title_that_starts_with_our_name():
    """Regression (found by characterisation): the length/containment check let
    'Royal Caterers | Best Caterers in Mumbai 2024' overwrite a clean name."""
    a = Lead(business_name="Royal Caterers")
    a.merge(Lead(business_name="Royal Caterers | Best Caterers in Mumbai 2024"))
    assert a.business_name == "Royal Caterers"
    assert "also known as: Royal Caterers | Best Caterers in Mumbai 2024" in a.notes

# --- rate limiter -----------------------------------------------------------

def test_shared_host_keeps_the_most_cautious_delay():
    """A source with short delays must not undo another's long ones."""
    from leadscraper.ratelimit import RateLimiter
    limiter = RateLimiter()
    limiter.configure("www.google.com", 12.0, 28.0)   # google_search
    limiter.configure("www.google.com", 4.0, 9.0)     # a directory's fallback
    assert limiter.range_for("www.google.com") == (12.0, 28.0)


def test_rate_limiter_penalty_escalates_and_decays():
    from leadscraper.ratelimit import RateLimiter
    limiter = RateLimiter()
    limiter.penalise("x", 30.0)
    limiter.penalise("x", 30.0)
    assert limiter.penalty_for("x") == 60.0
    limiter.relax("x", factor=0.5)
    assert limiter.penalty_for("x") == 30.0


# --- SERP title -> business name --------------------------------------------

def test_title_parser_extracts_a_real_business_name():
    from leadscraper.sources.google_search import business_name_from_title
    terms = ("wedding photographer", "event photographer")
    assert business_name_from_title(
        "Studio Fine Arts | Best Wedding Photographer in Powai Mumbai", "Powai", terms
    ) == "Studio Fine Arts"


def test_title_parser_rejects_pure_keyword_pages():
    """These are category pages, not businesses -- they must be rejected."""
    from leadscraper.sources.google_search import business_name_from_title
    terms = ("wedding photographer", "event photographer", "photography studio")
    for junk in ["Event Photographer",
                 "Event Photographer in Mumbai",
                 "Corporate & Event Photographer Mumbai",
                 "Verified Corporate event Photographer Mumbai",
                 "Best Wedding Photographers in Powai - Contact Number"]:
        assert business_name_from_title(junk, "Powai", terms) == "", junk


def test_title_parser_keeps_a_brand_that_contains_category_words():
    from leadscraper.sources.google_search import business_name_from_title
    assert business_name_from_title(
        "Knotty Affair Photography - Wedding Photographer Powai", "Powai",
        ("wedding photographer",)
    ) == "Knotty Affair Photography"


# --- SERP circuit breaker ---------------------------------------------------

def test_serp_breaker_trips_after_repeated_failures():
    """A permanently blocked search path must stop being called."""
    from leadscraper.sources.google_search import _SerpHealth
    health = _SerpHealth(threshold=3)
    assert health.disabled is False
    for _ in range(3):
        health.record_failure()
    assert health.disabled is True


def test_serp_breaker_resets_on_any_success():
    from leadscraper.sources.google_search import _SerpHealth
    health = _SerpHealth(threshold=3)
    health.record_failure()
    health.record_failure()
    health.record_success()
    health.record_failure()
    assert health.disabled is False, "a success must clear the failure streak"
