"""The Records columns ``local_business`` contributes (Phase 4).

Only its own: what a category-and-location search learns about a business
(category, ratings, directory standing) and, on a venue run, the venue
itself (tier, type, price, size, platforms). The core columns -- name,
location, published contacts, scores, signals, decision-makers, provenance --
are the export's (``export/columns.py``), shared by every lead type.

Values are read from the legacy row (``Lead.row_for``), so the workbook and
the run's CSV can never disagree about a value.
"""

from __future__ import annotations

from typing import Any

from ..base import ColumnSpec

TIER_COLOURS = (("Operator", "5B2C83"), ("Luxury", "2E7D32"), ("Premium", "1565C0"),
                ("Standard", "B26A00"), ("Unverified", "7F7F7F"), ("Excluded", "B3261E"))

RATING = "0.0"
COUNT = "#,##0"


def _vendor_columns(events: bool = True) -> list[tuple[str, ColumnSpec]]:
    """(legacy row header, column) for a vendor run; WedMeGood only for the events vertical."""
    columns = [
        ("Category", ColumnSpec("category", "Category", placement="front", breakdown=True,
                                width=20)),
        ("Maps Rating", ColumnSpec("maps_rating", "Maps Rating", "number", field="ratings",
                                   number_format=RATING, width=9)),
        ("Maps Reviews", ColumnSpec("maps_reviews", "Maps Reviews", "number", field="ratings",
                                    number_format=COUNT, width=9)),
    ]
    if not events:
        return columns
    return columns + [
        ("WedMeGood Rating", ColumnSpec("wedmegood_rating", "WedMeGood Rating", "number",
                                        field="ratings", number_format=RATING, width=11)),
        ("WedMeGood Reviews", ColumnSpec("wedmegood_reviews", "WedMeGood Reviews", "number",
                                         field="ratings", number_format=COUNT, width=11)),
        ("WedMeGood Featured", ColumnSpec(
            "wedmegood_featured", "WedMeGood Featured", "flag", field="ratings", width=11,
            description="Yes when WedMeGood marks the vendor as Featured (a paid placement).")),
    ]


def _venue_columns(currency: str) -> list[tuple[str, ColumnSpec]]:
    """(legacy row header, column) for a venue run (the hospitality vertical)."""
    def money(header: str, key: str) -> tuple[str, ColumnSpec]:
        return (f"{header} ({currency})",
                ColumnSpec(key, f"{header} ({currency})", "money", field="prices", width=12))

    return [
        ("Tier", ColumnSpec(
            "tier", "Tier", placement="front", breakdown=True, width=11, palette=TIER_COLOURS,
            description="How upscale the venue is (not how good a lead). Operator: a brand "
                        "running several venues. Luxury: a high-end criterion plus a top price. "
                        "Premium: one high-end criterion (premium platform or operator, strong "
                        "rating over many reviews, private pool or 3+ bedrooms, premium hotel). "
                        "Standard: none met, or a budget price. Unverified: too little known. "
                        "Excluded: not a venue.")),
        ("Venue Type", ColumnSpec("venue_type", "Venue Type", placement="front", breakdown=True,
                                  width=22)),
        ("Record Type", ColumnSpec(
            "record_type", "Record Type", placement="front", width=11,
            description="Property: one venue. Brand: an operator such as a villa-rental "
                        "company, with its venues under Managed By.")),
        ("Managed By", ColumnSpec("managed_by", "Managed By", placement="front", width=15)),
        ("Distance from City (km)", ColumnSpec(
            "distance_km", "Distance (km)", "number", width=9, number_format="0",
            description="Straight-line distance from the run's centre.")),
        money("Price From", "price_from"),
        money("Price To", "price_to"),
        ("Price Basis", ColumnSpec("price_basis", "Price Basis", field="prices", width=10)),
        ("Bedrooms", ColumnSpec("bedrooms", "Bedrooms", "number", width=9, number_format="0")),
        ("Max Guests (stay)", ColumnSpec("max_guests", "Max Guests", "number", width=9,
                                         number_format="0",
                                         description="Overnight capacity.")),
        ("Event Capacity", ColumnSpec("event_capacity", "Event Capacity", "number", width=10,
                                      number_format=COUNT,
                                      description="Guests for a function.")),
        ("Private Pool", ColumnSpec("private_pool", "Private Pool", "flag", width=8)),
        ("Event Friendly", ColumnSpec("event_friendly", "Event Friendly", "flag", width=9)),
        ("Pet Friendly", ColumnSpec("pet_friendly", "Pet Friendly", "flag", width=8)),
        ("Best Rating", ColumnSpec(
            "best_rating", "Best Rating", "number", field="ratings", number_format=RATING,
            width=8, description="The rating backed by the most reviews across Maps, the "
                                 "directories and every booking platform, on a 5-point scale.")),
        ("Total Reviews", ColumnSpec("total_reviews", "Total Reviews", "number",
                                     field="ratings", number_format=COUNT, width=9)),
        ("Booking Contact", ColumnSpec(
            "booking_contact", "Booking Contact", "phone", field="phones", placement="contacts",
            width=30, description="For an operator-run venue: the operator's booking line "
                                  "(the owner's number is not published).")),
        ("Listing URLs", ColumnSpec(
            "listing", "Listing", "url", placement="contacts", width=12, link_label="View",
            description="The first booking-platform page; Platforms lists every platform "
                        "the venue is on.")),
        ("Platforms", ColumnSpec("platforms", "Platforms", placement="contacts", width=22)),
        ("Amenities", ColumnSpec("amenities", "Amenities", placement="tail", width=44)),
        ("Maps Rating", ColumnSpec("maps_rating", "Maps Rating", "number", field="ratings",
                                   placement="tail", number_format=RATING, width=9)),
        ("Maps Reviews", ColumnSpec("maps_reviews", "Maps Reviews", "number", field="ratings",
                                    placement="tail", number_format=COUNT, width=10)),
        ("Platform Ratings", ColumnSpec("platform_ratings", "Platform Ratings",
                                        field="ratings", placement="tail", width=30)),
        ("WedMeGood Rating", ColumnSpec("wedmegood_rating", "WedMeGood Rating", "number",
                                        field="ratings", placement="tail",
                                        number_format=RATING, width=11)),
        ("Hotel Stars", ColumnSpec("hotel_stars", "Hotel Stars", "number", placement="tail",
                                   number_format="0", width=8)),
        ("Bathrooms", ColumnSpec("bathrooms", "Bathrooms", "number", placement="tail",
                                 number_format="0.#", width=9)),
        ("Lawn / Garden", ColumnSpec("lawn_garden", "Lawn / Garden", "flag", placement="tail",
                                     width=8)),
        ("Check-in / Check-out", ColumnSpec("checkin_checkout", "Check-in / Check-out",
                                            placement="tail", width=16)),
        ("Tier Reasons", ColumnSpec("tier_reasons", "Tier Reasons", placement="tail", width=46,
                                    description="The facts that set the tier.")),
        ("Type Reason", ColumnSpec("type_reason", "Type Reason", placement="tail", width=30,
                                   description="Why the venue type was chosen: a rule, "
                                               "or 'ai'.")),
        ("Price Source", ColumnSpec("price_source", "Price Source", field="prices",
                                    placement="tail", width=16)),
        ("Source Category", ColumnSpec("source_category", "Source Category", placement="tail",
                                       width=18)),
        ("Discovery Bucket", ColumnSpec(
            "discovery_bucket", "Discovery Bucket", placement="tail", width=18,
            description="The run category whose search found the venue.")),
        ("Description", ColumnSpec("description", "Description", placement="tail", width=50)),
        ("Photo URL", ColumnSpec("photo", "Photo", "url", placement="tail", width=10,
                                 link_label="View")),
        ("Latitude", ColumnSpec("latitude", "Latitude", "number", placement="tail",
                                number_format="0.00000", width=10)),
        ("Longitude", ColumnSpec("longitude", "Longitude", "number", placement="tail",
                                 number_format="0.00000", width=10)),
    ]


def column_table(profile: str, currency: str,
                 verticals: list[str] | None = None) -> list[tuple[str, ColumnSpec]]:
    if profile == "venues":
        return _venue_columns(currency)
    return _vendor_columns(events=verticals is None or "events" in verticals)


def _plain(value: Any, kind: str) -> Any:
    """A legacy row value ("" for unknown) as an export value (None for unknown)."""
    if value == "" or value is None:
        return None
    if kind == "flag":
        return {"Yes": True, "No": False}.get(value, value)
    return value


def values(lead: Any, profile: str, currency: str,
           verticals: list[str] | None = None) -> dict[str, Any]:
    row = lead.row_for(profile, currency)
    out: dict[str, Any] = {}
    for header, spec in column_table(profile, currency, verticals):
        value = row.get(header, "")
        if spec.key == "listing":
            value = next((u.strip() for u in str(value or "").split("|") if u.strip()), "")
        out[spec.key] = _plain(value, spec.kind)
    return out
