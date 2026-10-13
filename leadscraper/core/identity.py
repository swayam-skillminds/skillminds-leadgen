"""Organisation identity: which stored record an incoming one is.

Match rules, in priority order:

  0. Same registry ID (company number, GSTIN, ...)  -> same organisation
  1. Shared phone                                    -> same business
  2. Same Google Maps place, phones not contradicting
  3. Identical canonical name, phones not contradicting
  4. Fuzzy name >= 92 within the same locality
  5. (venue runs) pins within a few hundred metres, names loosely similar
  6. Same registrable website domain, phones not contradicting, names similar

Rules 1-5 are the original store's, unchanged and in the same order, so every
decision they made before still stands. Registry IDs are authoritative and go
first; no record carried one before. The domain rule goes last so it can only
add a merge the original rules would have stored as new -- never move one.

Rules 1, 2 and 6 cross localities (a caterer listed in Andheri and Dadar is
one business); 3 and 4 are deliberately conservative because "Royal Banquets"
in two suburbs may well be two companies.

The rules run against a ``CandidateIndex`` -- the store answers the lookups
-- so they are testable without a database and identical on SQLite and
Postgres. The index must reproduce the original store's row order where the
rules break ties (see the method docstrings).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Protocol

from rapidfuzz import fuzz

from .geo import haversine_km
from .normalize import NON_VENDOR_DOMAINS, domain_of, maps_place_key, normalize_name

FUZZY_NAME_THRESHOLD = 92

# --- keys --------------------------------------------------------------------

#: Hosts whose pages belong to many unrelated organisations, told apart by the
#: path (instagram.com/<handle>) or not at all. A "website" on one of these
#: says nothing about identity.
SHARED_HOSTS = frozenset({
    *NON_VENDOR_DOMAINS,
    # social and messaging
    "facebook.com", "fb.com", "fb.me", "m.me", "messenger.com", "instagram.com",
    "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com", "pinterest.com",
    "threads.net", "snapchat.com", "wa.me", "wa.link", "whatsapp.com", "t.me", "telegram.me",
    # link-in-bio and shorteners
    "linktr.ee", "bio.link", "beacons.ai", "lnk.bio", "linkin.bio", "taplink.cc",
    "campsite.bio", "solo.to", "about.me", "bit.ly", "tinyurl.com", "goo.gl", "g.page",
    "g.co", "abnb.me", "rb.gy", "cutt.ly",
    # marketplaces that list many businesses under their own domain (seen as
    # the "website" of unrelated florists and stays in the original stores)
    "fnp.com", "floweraura.com", "interflora.in", "onlineflowerscakesdelivery.com",
    "bluepillow.com", "freecancellations.com", "hotels.com", "trip.com", "ixigo.com",
    # Google surfaces (sites.google.com/view/<x>, maps, docs)
    "google.com", "google.co.in", "sites.google.com",
    # free email providers
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "rediffmail.com",
    "protonmail.com", "proton.me", "gmx.com", "yandex.com", "mail.com",
})

#: Site builders that give each customer a subdomain the public suffix list
#: doesn't know about: the tenant's full host is its identity.
SUBDOMAIN_TENANT_PARENTS = frozenset({
    "business.site", "wordpress.com", "weebly.com", "godaddysites.com", "square.site",
    "webflow.io", "jimdosite.com", "mystrikingly.com", "strikingly.com", "yolasite.com",
    "site123.me", "carrd.co", "wixstudio.io", "framer.website", "notion.site",
    # Indian small-business site builders seen in the original stores
    "grexa.site", "localo.site",
})


@lru_cache(maxsize=1)
def _extractor():
    import tldextract

    # The bundled suffix-list snapshot: no network fetch, no cache writes.
    # Private suffixes on, so foo.blogspot.com is its own registrable domain.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                                 include_psl_private_domains=True)


def _registrable(host: str) -> str:
    result = _extractor()(host)
    if hasattr(result, "top_domain_under_public_suffix"):      # tldextract >= 5.3
        return result.top_domain_under_public_suffix
    return result.registered_domain


def _is_shared(host: str) -> bool:
    return any(host == shared or host.endswith("." + shared) for shared in SHARED_HOSTS)


def is_shared_host(host: str | None) -> bool:
    """A host that identifies nobody: social networks, link-in-bio, free email…"""
    return bool(host) and _is_shared(host.casefold().strip("."))


@lru_cache(maxsize=4096)
def identity_domain(url: str | None) -> str | None:
    """The registrable domain that identifies an organisation, or None.

    None for no URL, an IP address, or a shared host (social, link-in-bio,
    directories, marketplaces, free email): those identify nobody.
    """
    host = domain_of(url)
    if not host or _is_shared(host):
        return None
    registrable = _registrable(host)
    if not registrable:
        return None
    if registrable in SUBDOMAIN_TENANT_PARENTS:
        return host if host != registrable else None
    if _is_shared(registrable):
        return None
    return registrable


def identity_keys(lead) -> dict[str, list[str]]:
    """Every identity key a record carries, by kind."""
    keys: dict[str, list[str]] = {
        "registry": [f"{scheme.lower()}:{value}".strip()
                     for scheme, value in sorted((getattr(lead, "registry_ids", None) or {}).items())
                     if value],
        "phone": [p for p in lead.phones if p],
        "place": [],
        "domain": [],
    }
    place = maps_place_key(lead.gmaps_url)
    if place:
        keys["place"].append(place)
    domain = identity_domain(lead.website)
    if domain:
        keys["domain"].append(domain)
    return keys


# --- options -----------------------------------------------------------------

@dataclass
class DedupOptions:
    """Per-run dedup knobs. The defaults reproduce the vendor run exactly."""

    #: When set, a phone match also needs this name similarity (0-100).
    phone_match_min_similarity: int | None = None
    #: When set, records within this many metres are merge candidates...
    geo_match_radius_m: float | None = None
    #: ...provided their names reach this similarity.
    geo_match_min_similarity: int = 70
    #: "vendors": locality -> category -> score. "venues": tier -> score.
    sort: str = "vendors"
    #: Let a distinctive identical name merge across localities (venue runs:
    #: Airbnb files a villa under "Nashik", Booking under "Igatpuri").
    cross_locality_names: bool = False
    #: Merge records that share a registrable website domain...
    domain_match: bool = True
    #: ...when their names are at least this similar (0-100). Branches and
    #: chain properties share a domain but not a name, nor usually a phone...
    domain_match_min_similarity: int = 60
    #: ...and fewer than this many organisations already hold the domain: one
    #: many unrelated records point at is a marketplace, not an identity.
    domain_max_holders: int = 3

    @classmethod
    def from_config(cls, config) -> "DedupOptions":
        if config is None:
            return cls()
        get = config.get
        venue_run = str(get("profile", "vendors") or "vendors").lower() == "venues"
        phone_sim = get("dedup.phone_match_min_similarity", 60 if venue_run else None)
        geo_radius = get("dedup.geo_match_radius_m", 250 if venue_run else None)
        # A venue run's rows are properties, and an operator's properties all
        # share its domain: there a domain names the operator (the brand
        # roll-up's job), not the venue. Off unless the config turns it on.
        domain_match = get("dedup.domain_match", not venue_run)
        return cls(
            phone_match_min_similarity=int(phone_sim) if phone_sim is not None else None,
            geo_match_radius_m=float(geo_radius) if geo_radius else None,
            geo_match_min_similarity=int(get("dedup.geo_match_min_similarity", 70)),
            sort="venues" if venue_run else "vendors",
            cross_locality_names=bool(get("dedup.cross_locality_names", venue_run)),
            domain_match=bool(domain_match),
            domain_match_min_similarity=int(get("dedup.domain_match_min_similarity", 60)),
            domain_max_holders=int(get("dedup.domain_max_holders", 3)),
        )


# --- comparisons -------------------------------------------------------------

def name_similarity(a, b) -> float:
    """0-100; containment counts as a full match ("Mokshah" in "Mokshah Villa")."""
    key_a, key_b = a.name_key, b.name_key
    if not key_a or not key_b:
        return 0.0
    if len(min(key_a, key_b, key=len)) >= 4 and (key_a in key_b or key_b in key_a):
        return 100.0
    return float(max(fuzz.token_sort_ratio(key_a, key_b), fuzz.token_set_ratio(key_a, key_b)))


def phones_contradict(a, b) -> bool:
    """True when both have phones and none overlap -- i.e. different firms."""
    if not a.phones or not b.phones:
        return False
    return not (set(a.phones) & set(b.phones))


def registries_contradict(a, b) -> bool:
    """True when both carry an id in the same register and the ids differ."""
    if b is None:
        return False
    theirs = {str(k).lower(): str(v) for k, v in (getattr(b, "registry_ids", None) or {}).items()
              if v}
    return any(str(k).lower() in theirs and theirs[str(k).lower()] != str(v)
               for k, v in (getattr(a, "registry_ids", None) or {}).items() if v)


def locality_compatible(a, b) -> bool:
    """Same-name records merge only when locality agrees or is unknown."""
    if not a.locality or not b.locality:
        return True
    return normalize_name(a.locality) == normalize_name(b.locality)


def distinctive_match(a, b, options: DedupOptions) -> bool:
    """Same distinctive name in different localities, pins (if any) close by."""
    if not options.cross_locality_names:
        return False
    key = a.name_key
    if len(key) < 10 or len(key.split()) < 2:
        return False
    if None not in (a.latitude, a.longitude, b.latitude, b.longitude):
        return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude) <= 15
    return True


# --- the index the rules consult ------------------------------------------------

class CandidateIndex(Protocol):
    def first_claimant(self, kind: str, value: str, exclude: frozenset[int]) -> int | None:
        """The record that claimed `value` first (lowest claim), not excluded."""

    def holders(self, kind: str, values: Iterable[str],
                exclude: frozenset[int]) -> list[tuple[int, object]]:
        """Every record holding any of `values`, in id order."""

    def by_name_key(self, name_key: str, exclude: frozenset[int]) -> list[tuple[int, object]]:
        """Records with exactly this name key, in id order."""

    def in_locality(self, locality: str,
                    exclude: frozenset[int]) -> list[tuple[int, str, object]]:
        """(id, name_key, record) in one locality, ordered (category, score desc, id) --
        the order the original store's index returned them in."""

    def near(self, lat: float, lng: float, radius_m: float,
             exclude: frozenset[int]) -> list[tuple[int, object]]:
        """Records pinned within roughly `radius_m` (a superset is fine), id order."""

    def get(self, record_id: int) -> object | None:
        ...


class IdentityResolver:
    """Applies the match rules for one run's options."""

    def __init__(self, options: DedupOptions | None = None):
        self.options = options or DedupOptions()

    def find_match(self, lead, index: CandidateIndex,
                   exclude: Iterable[int] = ()) -> int | None:
        exclude = frozenset(exclude)
        for rule in (self.by_registry, self.by_phone, self.by_place, self.by_name,
                     self.by_fuzzy_name, self.by_location, self.by_domain):
            found = rule(lead, index, exclude)
            if found is None:
                continue
            # Phase 7: two official numbers in one register are two organisations, however
            # alike they look (two councils behind one procurement helpdesk's phone).
            if getattr(lead, "registry_ids", None) and \
                    registries_contradict(lead, index.get(found)):
                continue
            return found
        return None

    # --- rules ---------------------------------------------------------------

    def by_registry(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        for key in identity_keys(lead)["registry"]:
            found = index.first_claimant("registry", key, exclude)
            if found is not None:
                return found
        return None

    def by_phone(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        phones = [p for p in lead.phones if p]
        if not phones:
            return None
        threshold = self.options.phone_match_min_similarity
        if threshold is not None:
            # A shared reservations line fronts many venues: the first claimant
            # is exactly wrong, so every holder is a candidate, best name wins.
            wanted = set(phones)
            best_id, best_score = None, -1.0
            for record_id, existing in index.holders("phone", phones, exclude):
                if not wanted & set(existing.phones):
                    continue
                similarity = name_similarity(lead, existing)
                if similarity >= threshold and similarity > best_score:
                    best_id, best_score = record_id, similarity
            return best_id
        # The original store asked for the claimant of `phone IN (...)`, which
        # SQLite answers in sorted phone order: the lowest number wins.
        for phone in sorted(set(phones)):
            found = index.first_claimant("phone", phone, exclude)
            if found is not None:
                return found
        return None

    def by_place(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        place = maps_place_key(lead.gmaps_url)
        if not place:
            return None
        found = index.first_claimant("place", place, exclude)
        if found is None:
            return None
        existing = index.get(found)
        if existing is None or phones_contradict(lead, existing):
            return None
        return found

    def by_name(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        key = lead.name_key
        if not key or len(key) < 3:
            return None
        for record_id, existing in index.by_name_key(key, exclude):
            if phones_contradict(lead, existing):
                continue
            if locality_compatible(lead, existing) or distinctive_match(lead, existing, self.options):
                return record_id
        return None

    def by_fuzzy_name(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        key = lead.name_key
        if not key or len(key) < 5 or not lead.locality:
            return None
        best_id, best_score = None, 0.0
        for record_id, other_key, existing in index.in_locality(lead.locality, exclude):
            ratio = fuzz.token_sort_ratio(key, other_key)
            if ratio >= FUZZY_NAME_THRESHOLD and ratio > best_score:
                if phones_contradict(lead, existing):
                    continue
                best_id, best_score = record_id, ratio
        return best_id

    def by_location(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        radius = self.options.geo_match_radius_m
        if not radius or lead.latitude is None or lead.longitude is None:
            return None
        best_id, best_score = None, -1.0
        for record_id, existing in index.near(lead.latitude, lead.longitude, radius, exclude):
            if existing.latitude is None or existing.longitude is None:
                continue
            metres = 1000 * haversine_km(lead.latitude, lead.longitude,
                                         existing.latitude, existing.longitude)
            if metres > radius:
                continue
            similarity = name_similarity(lead, existing)
            if similarity >= self.options.geo_match_min_similarity and similarity > best_score:
                best_id, best_score = record_id, similarity
        return best_id

    def by_domain(self, lead, index: CandidateIndex, exclude: frozenset[int]) -> int | None:
        if not self.options.domain_match:
            return None
        domains = identity_keys(lead)["domain"]
        if not domains:
            return None
        holders = index.holders("domain", domains, exclude)
        if len(holders) >= self.options.domain_max_holders:
            return None
        best_id, best_score = None, -1.0
        for record_id, existing in holders:
            if identity_domain(existing.website) not in domains:
                continue
            if phones_contradict(lead, existing):
                continue
            similarity = name_similarity(lead, existing)
            if similarity >= self.options.domain_match_min_similarity and similarity > best_score:
                best_id, best_score = record_id, similarity
        return best_id
