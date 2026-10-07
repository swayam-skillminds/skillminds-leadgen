"""Tests for data hygiene and the store's run bookkeeping.

The error-title tests are deliberately two-sided: rejecting bot-wall titles is
easy, doing it without also rejecting "Access Control Systems Pvt Ltd" is the
part that keeps breaking.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from leadscraper.core.errors import ERROR_TITLES, ERROR_TITLE_PATTERNS, is_error_title
from leadscraper.core.lead import Lead
from leadscraper.db.store import LeadStore


@pytest.fixture
def store(tmp_path):
    s = LeadStore(tmp_path / "hygiene.sqlite3")
    yield s
    s.close()


# --- error titles ----------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Access Denied",
    "403 Forbidden",
    "404 Not Found",
    "Just a moment...",
    "Attention Required!",
    "Attention Required! | Cloudflare",
    "Are you a robot?",
    "Please verify you are human",
    "Checking your browser before accessing",
    "Security check",
    "Just a moment",
    "Error",
    "Page Not Found",
    "Site not found",
    "Bad Gateway",
    "Service Unavailable",
    "Too Many Requests",
    "Request blocked",
    "Unusual traffic",
    "Access to this page has been denied",
    "Sign in",
    "Log in",
    "Untitled",
    "One moment, please",
    "DDoS protection by Cloudflare",
    "Under Maintenance",
    "Blocked",
])
def test_rejects_error_page_titles(title):
    assert is_error_title(title) is True


@pytest.mark.parametrize("name", [
    "Access Control Systems Pvt Ltd",
    "Moment Photography",
    "Robot Events LLP",
    "Human Touch Caterers",
    "Blocked Print Studio",
    "The Error Bar",
    "Maintenance Masters",
    "Gateway Banquets",
    "Security Services India",
    "Untitled Design Studio",
    "Page Three Events",
    "Denied Entry Nightclub",
])
def test_keeps_real_business_names(name):
    assert is_error_title(name) is False


@pytest.mark.parametrize("title", ["ACCESS  DENIED", "access-denied", "Access Denied.", "  Access Denied  "])
def test_matching_ignores_case_punctuation_and_whitespace(title):
    assert is_error_title(title) is True


def test_empty_name_is_not_an_error_title():
    # upsert's own empty-name check owns that case; this must not shadow it.
    assert is_error_title("") is False


def test_pattern_data_is_introspectable():
    assert "access denied" in ERROR_TITLES
    assert all(pattern.pattern.startswith("^") for pattern in ERROR_TITLE_PATTERNS)


def test_upsert_rejects_an_error_title_lead(store):
    with pytest.raises(ValueError, match="error-page title"):
        store.upsert(Lead(business_name="Access Denied", locality="Andheri",
                          sources=["justdial"]))
    assert store.count() == 0


def test_upsert_still_accepts_a_name_containing_an_error_word(store):
    _, action, _ = store.upsert(Lead(business_name="Access Control Systems Pvt Ltd",
                                     locality="Andheri", sources=["justdial"]))
    assert action == "new"
    assert store.count() == 1


# --- dead domains ----------------------------------------------------------

def test_dead_domain_round_trip(store):
    assert store.is_dead_domain("shaadisaga.com") is False
    store.mark_dead_domain("shaadisaga.com", "dns_failure")
    assert store.is_dead_domain("shaadisaga.com") is True
    assert store.dead_domains() == {"shaadisaga.com": "dns_failure"}


def test_mark_dead_domain_is_idempotent(store):
    store.mark_dead_domain("weddingwire.in", "http_403")
    store.mark_dead_domain("weddingwire.in", "http_403")
    assert store.dead_domains() == {"weddingwire.in": "http_403"}


# --- unit stats ------------------------------------------------------------

def test_unit_stat_is_none_before_any_run(store):
    assert store.unit_stat("wedmegood", "Andheri", "Caterers") is None
    assert store.unit_stats_for_source("wedmegood") is None


def test_unit_stat_averages_over_runs(store):
    store.record_unit("wedmegood", "Andheri", "Caterers", 10.0, 4)
    store.record_unit("wedmegood", "Andheri", "Caterers", 20.0, 6)
    assert store.unit_stat("wedmegood", "Andheri", "Caterers") == (15.0, 5.0)


def test_unit_stats_for_source_aggregates_areas_and_categories(store):
    store.record_unit("sulekha", "Andheri", "Caterers", 10.0, 2)
    store.record_unit("sulekha", "Dadar", "Photographers", 30.0, 6)
    store.record_unit("google_maps", "Andheri", "Caterers", 100.0, 99)
    assert store.unit_stats_for_source("sulekha") == (20.0, 4.0)


# --- enrichment memo -------------------------------------------------------

def test_enriched_round_trip(store):
    lead_id, _, _ = store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                                      sources=["google_maps"]))
    assert store.is_enriched(lead_id, "website") is False
    store.mark_enriched(lead_id, "website")
    assert store.is_enriched(lead_id, "website") is True
    # Per-enricher, so one pass finishing doesn't mask another that hasn't run.
    assert store.is_enriched(lead_id, "instagram") is False


def test_mark_enriched_is_idempotent(store):
    # Marks now reference a real organisation (a foreign key), so make one.
    lead_id, _, _ = store.upsert(Lead(business_name="Royal Caterers", locality="Andheri"))
    store.mark_enriched(lead_id, "website")
    store.mark_enriched(lead_id, "website")
    assert store.is_enriched(lead_id, "website") is True


# --- schema upgrade --------------------------------------------------------

def test_reopening_preserves_rows(tmp_path):
    """IF NOT EXISTS everywhere: the live 1,333-row DB must upgrade in place."""
    path = tmp_path / "upgrade.sqlite3"
    first = LeadStore(path)
    lead_id, _, _ = first.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                                      phones=["+919820012345"], sources=["google_maps"]))
    first.mark_dead_domain("shaadisaga.com", "dns_failure")
    first.mark_enriched(lead_id, "website")
    first.record_unit("wedmegood", "Andheri", "Caterers", 12.0, 3)
    first.mark_seen("https://example.in/vendor/1", "wedmegood")
    first.close()

    second = LeadStore(path)
    try:
        assert second.count() == 1
        assert second.get(lead_id).phones == ["+919820012345"]
        assert second.dead_domains() == {"shaadisaga.com": "dns_failure"}
        assert second.is_enriched(lead_id, "website") is True
        assert second.unit_stat("wedmegood", "Andheri", "Caterers") == (12.0, 3.0)
        assert second.already_seen("https://example.in/vendor/1") is True
    finally:
        second.close()
