"""Identity keys and the rules added in Phase 1: registry IDs, registrable
domains behind a shared-host blocklist, and consolidation after enrichment."""

from __future__ import annotations

import pytest

from leadscraper.core.identity import DedupOptions, identity_domain, identity_keys
from leadscraper.core.lead import Lead
from leadscraper.db.store import LeadStore


@pytest.fixture
def store(tmp_path):
    s = LeadStore(tmp_path / "id.sqlite3")
    yield s
    s.close()


# --- domains -------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.RoyalCaterers.co.in/contact?utm_source=x", "royalcaterers.co.in"),
    ("https://shop.royalcaterers.co.in", "royalcaterers.co.in"),
    # Builders whose customers each get a subdomain: the tenant is the identity.
    ("https://harshal-creation.grexa.site/", "harshal-creation.grexa.site"),
    ("https://royal.business.site", "royal.business.site"),
    ("https://foo.github.io", "foo.github.io"),                # public-suffix private domain
    # Hosts that identify nobody.
    ("https://sites.google.com/view/royal", None),
    ("https://linktr.ee/royal", None),
    ("https://www.instagram.com/royal", None),
    ("https://messenger.com/", None),
    ("https://www.fnp.com/florist/royal", None),               # a marketplace
    ("https://www.justdial.com/Mumbai/royal", None),
    ("https://grexa.site", None),
    (None, None),
])
def test_identity_domain(url, expected):
    assert identity_domain(url) == expected


def test_identity_keys_collect_every_kind():
    lead = Lead(business_name="X", phones=["+919820012345"], website="https://x.in",
                gmaps_url="https://maps.google.com/?cid=42", registry_ids={"GSTIN": "27ABC"})
    assert identity_keys(lead) == {"registry": ["gstin:27ABC"], "phone": ["+919820012345"],
                                   "place": ["cid:42"], "domain": ["x.in"]}


def test_same_domain_and_similar_name_merge_across_localities(store):
    store.upsert(Lead(business_name="Swaraj Events", locality="Andheri",
                      website="https://swarajevents.com", phones=["+919820011111"]))
    _, action, _ = store.upsert(Lead(business_name="Swaraj Events & Weddings", locality="Dadar",
                                     website="https://www.swarajevents.com/contact"))
    assert action == "merged"
    assert store.count() == 1


def test_same_domain_with_contradicting_phones_stays_separate(store):
    """Two branches with their own lines are two leads."""
    store.upsert(Lead(business_name="All Seasons Banquets Andheri", locality="Andheri",
                      website="https://allseasonsbanquets.com", phones=["+919820011111"]))
    _, action, _ = store.upsert(Lead(business_name="All Seasons Banquets Dadar",
                                     locality="Dadar", website="https://allseasonsbanquets.com",
                                     phones=["+919820022222"]))
    assert action == "new"


def test_same_domain_with_unrelated_names_stays_separate(store):
    store.upsert(Lead(business_name="Rose Florist", locality="Andheri",
                      website="https://petals.example.in"))
    _, action, _ = store.upsert(Lead(business_name="Lily Garden Decor", locality="Dadar",
                                     website="https://petals.example.in"))
    assert action == "new"


def test_a_domain_many_records_share_is_not_an_identity(store):
    """Three unrelated holders already: a marketplace nobody blocklisted yet."""
    for name, locality in (("Royal Flowers", "Andheri"), ("Royal Flowers Hub", "Dadar"),
                           ("Royal Flowers Point", "Powai")):
        store.options.domain_match = False      # seed three holders without merging
        store.upsert(Lead(business_name=name, locality=locality,
                          website="https://flowermarket.example.in"))
    store.options.domain_match = True
    _, action, _ = store.upsert(Lead(business_name="Royal Flowers Studio", locality="Thane",
                                     website="https://flowermarket.example.in"))
    assert action == "new"


def test_venue_runs_do_not_match_on_domain():
    """An operator's villas share its domain; there a domain names the operator."""
    class VenueConfig:
        def get(self, key, default=None):
            return {"profile": "venues"}.get(key, default)

    assert DedupOptions.from_config(VenueConfig()).domain_match is False
    assert DedupOptions().domain_match is True


# --- registry IDs ------------------------------------------------------------------------

def test_registry_id_is_authoritative(store):
    store.upsert(Lead(business_name="Sharma Enterprises", locality="Pune",
                      registry_ids={"gstin": "27AAACS1234A1Z5"}, phones=["+919820011111"]))
    _, action, _ = store.upsert(Lead(business_name="S.E. Catering Division", locality="Mumbai",
                                     registry_ids={"gstin": "27AAACS1234A1Z5"},
                                     phones=["+919820022222"]))
    assert action == "merged"
    assert store.get(1).registry_ids == {"gstin": "27AAACS1234A1Z5"}


# --- consolidation after enrichment (Phase 0 open issue) ---------------------------------

def test_enrichment_that_reveals_a_shared_phone_merges_the_records(store):
    first, _, _ = store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                                    sources=["google_maps"]))
    second, _, _ = store.upsert(Lead(business_name="Royal Catering Services", locality="Dadar",
                                     phones=["+919820012345"], sources=["justdial"]))
    assert store.count() == 2
    store.mark_enriched(second, "instagram")

    # The website enricher finds the same number on the first record's site.
    store.merge_into(first, Lead(business_name="", phones=["+919820012345"],
                                 emails=["hello@royal.in"], sources=["website"]))

    assert store.count() == 1
    survivor = store.get(first)                      # the lower id survives
    assert survivor.phones == ["+919820012345"]
    assert set(survivor.sources) == {"google_maps", "justdial", "website"}
    assert store.get(second) is None
    assert store.is_enriched(first, "instagram")     # marks follow the record
    assert store.first_claimant("phone", "+919820012345", frozenset()) == first
    assert store.stats["consolidated"] == 1


def test_enrichment_without_new_identity_leaves_others_alone(store):
    store.upsert(Lead(business_name="Royal Banquets", locality="Andheri",
                      phones=["+919820011111"]))
    other, _, _ = store.upsert(Lead(business_name="Royal Banquets", locality="Dadar",
                                    phones=["+919820022222"]))
    store.merge_into(other, Lead(business_name="", emails=["dadar@royal.in"]))
    assert store.count() == 2


def test_an_ai_rename_that_reveals_a_duplicate_merges_it(store):
    store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                      phones=["+919820012345"]))
    seo, _, _ = store.upsert(Lead(business_name="Best Caterers in Andheri | Book Now",
                                  locality="Andheri"))
    assert store.count() == 2
    store.update_fields(seo, business_name="Royal Caterers", _source="ai")
    assert store.count() == 1
