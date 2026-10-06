"""The Lead record and its merge semantics.

A Lead is deliberately source-agnostic: every scraper produces partial Leads
and the store merges them. Merge rules live here (not in the store) so they
stay testable in isolation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .normalize import (
    clean_text,
    dedupe_preserving_order,
    looks_like_seo_title,
    name_key,
    normalize_name,
    normalize_url,
    parse_rating,
    parse_review_count,
    phone_display,
)


@dataclass
class Lead:
    """One business. Fields left empty simply weren't found yet."""

    business_name: str = ""
    contact_name: str = ""
    category: str = ""
    locality: str = ""

    phones: list[str] = field(default_factory=list)      # E.164
    emails: list[str] = field(default_factory=list)
    website: str | None = None
    instagram: str | None = None                          # bare handle
    linkedin: str | None = None
    facebook: str | None = None
    whatsapp: str | None = None
    gmaps_url: str | None = None
    address: str = ""

    gmaps_rating: float | None = None
    gmaps_reviews: int | None = None

    wmg_rating: float | None = None
    wmg_reviews: int | None = None
    wmg_featured: bool = False
    wmg_url: str | None = None

    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # --- venue attributes (venue runs only; empty on a vendor run) ----------
    #: "Property" or "Brand" (an operator such as StayVista, not one venue).
    record_type: str = ""
    #: Raw type as the source put it: a Maps category, "Farm stay", "Villa".
    property_type: str = ""
    #: Classified type, one of venues.VENUE_TYPES; derived on every rescore.
    venue_type: str = ""
    #: "ai" once the model has classified it, else "rules: <why>".
    venue_type_source: str = ""
    venue_tier: str = ""
    tier_points: int = 0
    tier_reasons: list[str] = field(default_factory=list)
    managed_by: str = ""
    #: Who takes bookings for a managed venue: the operator's line, e.g.
    #: "SaffronStays: +91 80 6916 0000". Set by the brand roll-up.
    booking_contact: str = ""

    price_min: float | None = None       # INR
    price_max: float | None = None
    price_basis: str = ""                # "per night" | "per plate" | "per event"
    price_source: str = ""

    bedrooms: int | None = None
    bathrooms: float | None = None
    max_guests: int | None = None        # overnight stay capacity
    event_capacity: int | None = None    # guests for a function
    hotel_class: int | None = None       # stars
    amenities: list[str] = field(default_factory=list)
    has_pool: bool | None = None
    has_lawn: bool | None = None
    event_friendly: bool | None = None
    pet_friendly: bool | None = None

    #: Booking platforms the venue is listed on ("stayvista", "airbnb", ...).
    platforms: list[str] = field(default_factory=list)
    #: The listing pages themselves, one per platform.
    listing_urls: list[str] = field(default_factory=list)
    #: platform -> [rating on a 5-point scale, review count]
    platform_ratings: dict[str, list] = field(default_factory=dict)

    latitude: float | None = None
    longitude: float | None = None
    distance_km: float | None = None     # from the run's geo centre; derived
    description: str = ""
    image_url: str | None = None
    checkin_checkout: str = ""

    score: float = 0.0
    first_seen: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    # --- keys -------------------------------------------------------------

    @property
    def name_key(self) -> str:
        """Fuzzy-ish canonical name used as the secondary dedup key."""
        return name_key(self.business_name)

    @property
    def phone_keys(self) -> set[str]:
        """Every phone that can identify this lead (primary dedup key)."""
        return set(self.phones)

    def identity_key(self) -> str:
        """Stable-ish single string identity, for logging and CSV row ids."""
        primary_phone = self.phones[0] if self.phones else ""
        return f"{self.name_key}|{primary_phone}"

    # --- construction -----------------------------------------------------

    def normalized(self) -> "Lead":
        """Return a copy with every field run through the normalisers."""
        self.business_name = clean_text(self.business_name)
        self.contact_name = clean_text(self.contact_name)
        self.address = clean_text(self.address)
        self.website = normalize_url(self.website)
        self.gmaps_url = self.gmaps_url or None
        self.gmaps_rating = parse_rating(self.gmaps_rating)
        self.gmaps_reviews = parse_review_count(self.gmaps_reviews)
        self.wmg_rating = parse_rating(self.wmg_rating)
        self.wmg_reviews = parse_review_count(self.wmg_reviews)
        self.phones = dedupe_preserving_order(self.phones)
        self.emails = dedupe_preserving_order(e.lower() for e in self.emails)
        self.sources = dedupe_preserving_order(self.sources)
        self.source_urls = dedupe_preserving_order(self.source_urls)
        self.notes = dedupe_preserving_order(self.notes)
        self.amenities = dedupe_preserving_order(self.amenities)
        self.platforms = dedupe_preserving_order(self.platforms)
        self.listing_urls = dedupe_preserving_order(self.listing_urls)
        self.description = clean_text(self.description)
        if self.instagram:
            self.instagram = self.instagram.lstrip("@").lower()
        return self

    # --- merge ------------------------------------------------------------

    def merge(self, other: "Lead") -> list[str]:
        """Fold `other` into self. Returns the names of fields that changed.

        Merge policy:
          * list fields  -> union, preserving the order we first saw values in
          * empty scalar -> take the incoming value
          * filled scalar-> keep ours, unless the incoming one is strictly
                            better (longer address, more reviews, a real
                            vendor site replacing nothing)
        """
        changed: list[str] = []

        # List fields: always union.
        for list_field in ("phones", "emails", "sources", "source_urls", "notes",
                           "amenities", "platforms", "listing_urls"):
            before = list(getattr(self, list_field))
            merged = dedupe_preserving_order(before + list(getattr(other, list_field)))
            if merged != before:
                setattr(self, list_field, merged)
                changed.append(list_field)

        # Scalars we take only when currently empty.
        for scalar in ("website", "instagram", "linkedin", "facebook", "whatsapp",
                       "gmaps_url", "wmg_url", "contact_name", "record_type",
                       "property_type", "managed_by", "image_url", "checkin_checkout",
                       "booking_contact"):
            if not getattr(self, scalar) and getattr(other, scalar):
                setattr(self, scalar, getattr(other, scalar))
                changed.append(scalar)

        changed.extend(self._merge_venue(other))

        # Longer address usually means the more complete one.
        if len(other.address) > len(self.address):
            self.address = other.address
            changed.append("address")

        # A longer name wins only when it *extends* the one we have -- e.g.
        # "Galaxy Caterers" -> "Galaxy Caterers - St. Arnold's Lawns". Without
        # this guard an SEO page title ("Royal Caterers | Best Caterers in
        # Mumbai 2024") would overwrite a clean Maps name.
        if other.business_name and len(other.business_name) > len(self.business_name):
            if self._is_extension_of(other.business_name, self.business_name):
                self.business_name = other.business_name
                changed.append("business_name")
            else:
                alias = f"also known as: {other.business_name}"
                if alias not in self.notes:
                    self.notes.append(alias)
                    changed.append("notes")

        # Ratings: trust the record backed by more reviews.
        if other.gmaps_reviews is not None and (
            self.gmaps_reviews is None or other.gmaps_reviews > self.gmaps_reviews
        ):
            self.gmaps_reviews = other.gmaps_reviews
            if other.gmaps_rating is not None:
                self.gmaps_rating = other.gmaps_rating
            changed.append("gmaps_reviews")
        elif self.gmaps_rating is None and other.gmaps_rating is not None:
            self.gmaps_rating = other.gmaps_rating
            changed.append("gmaps_rating")

        if other.wmg_reviews is not None and (
            self.wmg_reviews is None or other.wmg_reviews > self.wmg_reviews
        ):
            self.wmg_reviews = other.wmg_reviews
            if other.wmg_rating is not None:
                self.wmg_rating = other.wmg_rating
            changed.append("wmg_reviews")
        elif self.wmg_rating is None and other.wmg_rating is not None:
            self.wmg_rating = other.wmg_rating
            changed.append("wmg_rating")

        # Featured is sticky: once a directory flags them, keep it.
        if other.wmg_featured and not self.wmg_featured:
            self.wmg_featured = True
            changed.append("wmg_featured")

        # Category/locality: keep the first non-empty, but record a second one
        # as a note rather than losing the information.
        for label, value in (("category", other.category), ("locality", other.locality)):
            current = getattr(self, label)
            if not current and value:
                setattr(self, label, value)
                changed.append(label)
            elif current and value and normalize_name(current) != normalize_name(value):
                note = f"also listed under {label}: {value}"
                if note not in self.notes:
                    self.notes.append(note)
                    changed.append("notes")

        if changed:
            self.last_updated = time.time()
            self.first_seen = min(self.first_seen, other.first_seen)
        return dedupe_preserving_order(changed)

    def _merge_venue(self, other: "Lead") -> list[str]:
        """Venue attributes: sizes take the larger, flags stick once True."""
        changed: list[str] = []

        if other.latitude is not None and self.latitude is None:
            self.latitude, self.longitude = other.latitude, other.longitude
            changed.append("latitude")

        if len(other.description) > len(self.description):
            self.description = other.description
            changed.append("description")

        # A different listing of the same venue may quote a subset of it ("book
        # 2 of 6 rooms"), so the larger figure is the venue's.
        for size in ("bedrooms", "bathrooms", "max_guests", "event_capacity", "hotel_class"):
            theirs = getattr(other, size)
            if theirs is not None and (getattr(self, size) is None or theirs > getattr(self, size)):
                setattr(self, size, theirs)
                changed.append(size)

        for flag in ("has_pool", "has_lawn", "event_friendly", "pet_friendly"):
            ours, theirs = getattr(self, flag), getattr(other, flag)
            if theirs is None or ours is True:
                continue
            if ours is None or (ours is False and theirs is True):
                setattr(self, flag, theirs)
                changed.append(flag)

        for platform, value in (other.platform_ratings or {}).items():
            current = self.platform_ratings.get(platform)
            if not current or (len(value) > 1 and len(current) > 1
                               and (value[1] or 0) > (current[1] or 0)):
                self.platform_ratings[platform] = list(value)
                changed.append("platform_ratings")

        # Price: a per-night quote beats a per-plate one; within one basis the
        # range widens to cover both quotes.
        if other.price_min is not None:
            if self.price_min is None or (self.price_basis != "per night"
                                          and other.price_basis == "per night"):
                self.price_min, self.price_max = other.price_min, other.price_max
                self.price_basis, self.price_source = other.price_basis, other.price_source
                changed.append("price_min")
            elif self.price_basis == other.price_basis:
                low = min(self.price_min, other.price_min)
                high = max(self.price_max or self.price_min, other.price_max or other.price_min)
                if (low, high) != (self.price_min, self.price_max):
                    self.price_min, self.price_max = low, high
                    if other.price_source and other.price_source not in self.price_source:
                        self.price_source = f"{self.price_source}, {other.price_source}".strip(", ")
                    changed.append("price_min")

        # An AI classification is a decision; a rules one is re-derived anyway.
        if other.venue_type_source == "ai" and self.venue_type_source != "ai":
            self.venue_type, self.venue_type_source = other.venue_type, "ai"
            changed.append("venue_type")
        return changed

    @staticmethod
    def _is_extension_of(longer: str, shorter: str, max_len: int = 90) -> bool:
        """True when `longer` is `shorter` plus extra detail, not a different name."""
        if len(longer) > max_len:
            return False
        # "Royal Caterers | Best Caterers in Mumbai" contains the name too, but
        # what it adds is keywords, not detail.
        if looks_like_seo_title(longer) and not looks_like_seo_title(shorter):
            return False
        return normalize_name(shorter) in normalize_name(longer)

    # --- serialisation ----------------------------------------------------

    def to_row(self) -> dict[str, Any]:
        """Flat dict for CSV / Google Sheets."""
        return {
            "Score": round(self.score, 2),
            "Business Name": self.business_name,
            "Category": self.category,
            "Locality": self.locality,
            "Contact Name": self.contact_name,
            "Phones": " | ".join(phone_display(p) for p in self.phones),
            "Emails": " | ".join(self.emails),
            "Website": self.website or "",
            "Instagram": f"https://instagram.com/{self.instagram}" if self.instagram else "",
            "LinkedIn": self.linkedin or "",
            "Facebook": self.facebook or "",
            "WhatsApp": self.whatsapp or "",
            "Google Maps URL": self.gmaps_url or "",
            "Address": self.address,
            "Maps Rating": self.gmaps_rating if self.gmaps_rating is not None else "",
            "Maps Reviews": self.gmaps_reviews if self.gmaps_reviews is not None else "",
            "WedMeGood Rating": self.wmg_rating if self.wmg_rating is not None else "",
            "WedMeGood Reviews": self.wmg_reviews if self.wmg_reviews is not None else "",
            "WedMeGood Featured": "Yes" if self.wmg_featured else "",
            "Sources": ", ".join(self.sources),
            "Source Count": len(self.sources),
            "Notes": "; ".join(self.notes),
            "First Seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.first_seen)),
            "Last Updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_updated)),
        }

    def to_venue_row(self) -> dict[str, Any]:
        """Flat dict for the venue run's CSV / Sheet: the venue first, contacts after."""
        from .venues import best_rating

        def flag(value: bool | None) -> str:
            return "" if value is None else ("Yes" if value else "No")

        def num(value) -> Any:
            if value is None:
                return ""
            return int(value) if float(value).is_integer() else value

        rating, reviews = best_rating(self)
        platform_ratings = "; ".join(
            f"{platform} {value[0]}" + (f" ({value[1]})" if len(value) > 1 and value[1] else "")
            for platform, value in sorted(self.platform_ratings.items()) if value
        )
        return {
            "Tier": self.venue_tier,
            "Score": round(self.score, 2),
            "Venue Name": self.business_name,
            "Record Type": self.record_type,
            "Venue Type": self.venue_type,
            "Managed By": self.managed_by,
            "Locality": self.locality,
            "Distance from City (km)": num(self.distance_km),
            "Price From (INR)": num(self.price_min),
            "Price To (INR)": num(self.price_max),
            "Price Basis": self.price_basis,
            "Price Source": self.price_source,
            "Bedrooms": num(self.bedrooms),
            "Bathrooms": num(self.bathrooms),
            "Max Guests (stay)": num(self.max_guests),
            "Event Capacity": num(self.event_capacity),
            "Hotel Stars": num(self.hotel_class),
            "Private Pool": flag(self.has_pool),
            "Lawn / Garden": flag(self.has_lawn),
            "Event Friendly": flag(self.event_friendly),
            "Pet Friendly": flag(self.pet_friendly),
            "Amenities": ", ".join(self.amenities),
            "Best Rating": rating if rating is not None else "",
            "Total Reviews": reviews or "",
            "Maps Rating": self.gmaps_rating if self.gmaps_rating is not None else "",
            "Maps Reviews": self.gmaps_reviews if self.gmaps_reviews is not None else "",
            "Platform Ratings": platform_ratings,
            "WedMeGood Rating": self.wmg_rating if self.wmg_rating is not None else "",
            "Platforms": ", ".join(self.platforms),
            "Listing URLs": " | ".join(self.listing_urls),
            "Contact Name": self.contact_name,
            "Phones": " | ".join(phone_display(p) for p in self.phones),
            "Booking Contact": self.booking_contact,
            "Emails": " | ".join(self.emails),
            "WhatsApp": self.whatsapp or "",
            "Website": self.website or "",
            "Instagram": f"https://instagram.com/{self.instagram}" if self.instagram else "",
            "Facebook": self.facebook or "",
            "LinkedIn": self.linkedin or "",
            "Google Maps URL": self.gmaps_url or "",
            "Address": self.address,
            "Latitude": self.latitude if self.latitude is not None else "",
            "Longitude": self.longitude if self.longitude is not None else "",
            "Check-in / Check-out": self.checkin_checkout,
            "Source Category": self.property_type,
            "Description": self.description[:500],
            "Photo URL": self.image_url or "",
            "Tier Reasons": "; ".join(self.tier_reasons),
            "Type Reason": self.venue_type_source,
            "Discovery Bucket": self.category,
            "Sources": ", ".join(self.sources),
            "Source Count": len(self.sources),
            "Notes": "; ".join(self.notes),
            "First Seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.first_seen)),
            "Last Updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_updated)),
        }

    def row_for(self, profile: str = "vendors") -> dict[str, Any]:
        return self.to_venue_row() if profile == "venues" else self.to_row()

    @staticmethod
    def row_headers(profile: str = "vendors") -> list[str]:
        return list(Lead().row_for(profile).keys())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, blob: str) -> "Lead":
        data = json.loads(blob)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
