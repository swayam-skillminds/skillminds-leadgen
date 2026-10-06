"""Targeted Google Maps lookup for venues that have no phone yet.

Airbnb and Booking.com never show an owner's contact, so a villa found only
there arrives with a name and a price and nothing to call. Many such villas
also have a Google Maps listing -- often under a slightly different name -- so
this enricher searches Maps for "<venue name> <locality>" and takes the phone,
website, pin and rating from the result.

A wrong match would attach someone else's phone number to a venue, which is
worse than no number. A result is accepted only when ALL hold:
  * the names agree (similarity >= `min_name_similarity`),
  * every distinctive word of the venue's name appears in the Maps name, and
  * the place is where the venue is (same locality, or its pin within
    `max_distance_km` of the venue's pin / locality centre).
Every rejection is logged with its reason, so a borderline miss can be checked
by hand.
"""

from __future__ import annotations

import logging
import re

from rapidfuzz import fuzz

from ..core.lead import Lead
from ..core.normalize import (
    clean_text, extract_instagram, extract_phones, is_vendor_website, name_key, normalize_url,
    parse_rating,
    parse_review_count,
)
from ..net.ratelimit import BlockedError
from ..core.geo import coords_from_maps_url, haversine_km
from .base import Enricher
from .google_maps import GoogleMapsSource

log = logging.getLogger(__name__)

# Words that say nothing about WHICH venue it is; a name made only of these
# ("Luxury Villa With Private Pool") would match any villa on Maps.
_GENERIC = {
    "villa", "villas", "luxury", "private", "pool", "with", "and", "the", "in", "near", "by",
    "bhk", "bedroom", "bedrooms", "home", "homestay", "stay", "stays", "farmhouse", "farm",
    "house", "cottage", "resort", "view", "views", "lake", "mountain", "valley", "hill",
    "hills", "retreat", "garden", "family", "premium", "entire", "room", "rooms", "nashik",
    "igatpuri", "trimbakeshwar", "trimbak", "bungalow", "independent", "beautiful", "cozy",
    "w", "a", "of", "for", "your", "our", "new", "best", "jacuzzi", "lawn", "infinity",
    "hotel", "suite", "suites", "dam", "side", "maharashtra", "india", "at",
}


def distinctive_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if w not in _GENERIC and not w.isdigit() and not re.fullmatch(r"\d+(?:bhk|br|bdr)", w)
            and len(w) > 1]


class MapsLookupEnricher(Enricher):
    name = "maps_lookup"
    priority = 10

    def __init__(self, ctx):
        super().__init__(ctx)
        self.maps = GoogleMapsSource(ctx)
        self.min_similarity = float(self.cfg.get("min_name_similarity", 85))
        self.max_km = float(self.cfg.get("max_distance_km", 20))
        self.candidates = int(self.cfg.get("candidates", 3))
        self._blocked = False
        self._store = None

    def rate_keys(self) -> list[str]:
        return [self.name]

    def targets(self, store) -> list[tuple[int, Lead]]:
        """Venues with no phone and no Maps listing, not yet looked up."""
        self._store = store
        out = []
        for lead_id, lead in store.all_with_ids():
            if lead.record_type != "Property" or lead.venue_tier == "Excluded":
                continue
            if lead.phones or lead.gmaps_url:
                continue
            if store.is_enriched(lead_id, self.name):
                continue
            if not distinctive_words(lead.business_name):
                # Nothing to search on that would single this venue out.
                continue
            out.append((lead_id, lead))
        self._ids = {id(lead): lead_id for lead_id, lead in out}
        return out

    def enrich(self, lead: Lead) -> Lead | None:
        if self._blocked:
            return None
        try:
            return self._lookup(lead)
        finally:
            # Memo every attempt, hit or miss: re-runs only look up new venues.
            lead_id = getattr(self, "_ids", {}).get(id(lead))
            if lead_id is not None and self._store is not None and not self._blocked:
                self._store.mark_enriched(lead_id, self.name)

    def _lookup(self, lead: Lead) -> Lead | None:
        locality = next((loc for loc in self.ctx.config.localities if loc.name == lead.locality), None)
        place_hint = locality.primary_term if locality else self.ctx.config.city
        query = f"{lead.business_name} {place_hint}"
        try:
            urls = self.maps._search(query, scroll_rounds=1, max_results=self.candidates)
        except BlockedError as exc:
            self._blocked = True
            self.fail("blocked", business=lead.business_name, detail=str(exc))
            return None
        except Exception as exc:
            self.fail("search_failed", business=lead.business_name, detail=repr(exc))
            return None
        if not urls:
            self.fail("no_maps_result", business=lead.business_name, detail=query)
            return None

        rejected: list[str] = []
        for url in urls[: self.candidates]:
            try:
                data = self.maps._read_place(url)
            except BlockedError as exc:
                self._blocked = True
                self.fail("blocked", business=lead.business_name, detail=str(exc))
                return None
            except Exception as exc:
                rejected.append(f"unreadable ({exc!r})"[:80])
                continue
            verdict = self.match(lead, data, url)
            if isinstance(verdict, str):
                rejected.append(verdict)
                continue
            log.info("maps_lookup: %s -> %s", lead.business_name, data.get("name"))
            return verdict
        self.fail("no_confident_match", business=lead.business_name,
                  detail=f"{query}: " + "; ".join(rejected)[:300])
        return None

    # --- matching -----------------------------------------------------------

    def match(self, lead: Lead, data: dict, url: str) -> Lead | str:
        """A partial Lead when the place is this venue, else the rejection reason."""
        maps_name = clean_text(data.get("name"))
        if not maps_name:
            return "no name"
        ours, theirs = name_key(lead.business_name), name_key(maps_name)
        similarity = max(fuzz.token_set_ratio(ours, theirs), fuzz.token_sort_ratio(ours, theirs))
        # The words that make the name THIS venue must be in the Maps name too:
        # "Saukhya Farm" must not match "Saukhya Residency" on similarity alone.
        needed = distinctive_words(lead.business_name)
        found = set(distinctive_words(maps_name))
        if similarity < self.min_similarity or not all(w in found for w in needed):
            return f"name '{maps_name}' ({similarity:.0f})"

        point = coords_from_maps_url(data.get("url")) or coords_from_maps_url(url)
        if not self._same_place(lead, data, point):
            return f"'{maps_name}' is elsewhere"

        phones = extract_phones(data.get("phone") or "")
        link = normalize_url(data.get("website")) if data.get("website") else None
        # A managed villa's Maps "website" is its StayVista / SaffronStays page:
        # a listing, not a way to reach the venue.
        website = link if link and is_vendor_website(link) else None
        if not (phones or website):
            return f"'{maps_name}' has no phone or own website on Maps"

        partial = Lead(business_name=lead.business_name)
        partial.phones = phones
        partial.website = website
        if link and not website:
            partial.listing_urls = [link]
        partial.instagram = extract_instagram(data.get("bodyText") or "")
        partial.address = clean_text(data.get("address"))
        partial.gmaps_url = data.get("url") or url
        partial.gmaps_rating = parse_rating(data.get("rating"))
        partial.gmaps_reviews = parse_review_count(data.get("reviews"))
        if point:
            partial.latitude, partial.longitude = point
        partial.sources = [self.name]
        partial.source_urls = [partial.gmaps_url]
        partial.notes = [f"contact from Google Maps listing '{maps_name}' (name match {similarity:.0f})"]
        return partial

    def _same_place(self, lead: Lead, data: dict, point) -> bool:
        """Distance decides whenever Maps gives a pin; place names only without one.

        Nearest-locality is no test here: a namesake pinned in Pune is still
        "nearest" to some Nashik locality.
        """
        locality = next((loc for loc in self.ctx.config.localities if loc.name == lead.locality), None)
        if point:
            if lead.latitude is not None and lead.longitude is not None:
                return haversine_km(*point, lead.latitude, lead.longitude) <= self.max_km
            if locality and locality.center:
                return haversine_km(*point, *locality.center) <= self.max_km
        resolved = self.ctx.config.locality_for(f"{data.get('name', '')} {data.get('address', '')}")
        return resolved is not None and resolved.name == lead.locality
