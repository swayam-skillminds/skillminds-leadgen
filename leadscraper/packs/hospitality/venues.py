"""Venue domain logic: attribute extraction, venue-type classification, tiering.

Everything here is about the VENUE, not the lead. A venue's tier says how
upscale the property is (price, platform, amenities, reputation); whether we
can actually reach its owner is the lead score's business, in `scoring.py`.

Kept free of I/O so every rule is unit-testable against plain strings, and
driven by `VenueSettings` so thresholds live in config rather than code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from ...core.geo import as_float, as_int, coords_from_maps_url, haversine_km  # noqa: F401
from ...core.normalize import clean_text, dedupe_preserving_order
from ...core.ratings import all_ratings, best_rating  # noqa: F401

if TYPE_CHECKING:  # pragma: no cover
    from ...core.lead import Lead

# --- money -----------------------------------------------------------------

_INR = r"(?:₹|&#8377;|\bINR\b|\bRs\.?)\s?"
_AMOUNT = r"(\d{1,3}(?:,\d{2,3})+|\d{3,7})(?:\.\d{1,2})?"

# Night-priced offers. Group 1 is always the amount.
_PER_NIGHT = re.compile(
    _INR + _AMOUNT + r"\s*(?:\([^)]{0,30}\)\s*)?(?:/|per|a)\s*(?:room\s*(?:/|per)\s*)?night",
    re.I,
)
_NIGHT_THEN_PRICE = re.compile(r"per\s+night\s*[:\-]?\s*" + _INR + _AMOUNT, re.I)
_FOR_N_NIGHTS = re.compile(_INR + _AMOUNT + r"\s*(?:total\s*)?for\s+(\d{1,2})\s+nights?", re.I)
_PER_PLATE = re.compile(_INR + _AMOUNT + r"\s*(?:/|per|a)\s*(?:plate|pax|person)\b", re.I)

# If one of these sits just before a price, the price is an add-on, a discount
# or a deposit -- not what a night at the venue costs.
_PRICE_NOISE = re.compile(
    r"(discount|\boff\b|cashback|\bupto\b|up to|\bemi\b|save|deposit|extra\s+(?:bed|person|guest)|"
    r"driver|maid|house-?help|\bpets?\b|meal|thali|dinner|lunch|breakfast|decor|bonfire|"
    r"bbq|barbe?que|movie|add-?on|massage|spa\b|photoshoot|cake|minimum|min\.?\s+transaction|"
    r"bookings?\s+worth|coupon|voucher|offer)",
    re.I,
)
_PRICE_NOISE_WINDOW = 70

MIN_NIGHT_PRICE, MAX_NIGHT_PRICE = 1_500.0, 1_500_000.0
MIN_PLATE_PRICE, MAX_PLATE_PRICE = 250.0, 20_000.0


def parse_inr(raw: str | None) -> float | None:
    """'₹1,34,520' / 'INR 25000' / '46,575' -> float rupees."""
    if not raw:
        return None
    match = re.search(_AMOUNT, str(raw).replace(" ", " "))
    return as_float(match.group(1)) if match else None


def _is_noise(text: str, start: int) -> bool:
    window = text[max(0, start - _PRICE_NOISE_WINDOW):start]
    # Only the current line/segment counts: a discount banner two cards up
    # must not disqualify this card's price.
    for separator in ("\n", "|", "•"):
        if separator in window:
            window = window.rsplit(separator, 1)[-1]
    return bool(_PRICE_NOISE.search(window))


def nightly_prices(text: str | None) -> list[float]:
    """Per-night prices in document order, add-ons and discounts excluded."""
    if not text:
        return []
    text = str(text).replace(" ", " ")
    found: list[tuple[int, float]] = []
    for match in _PER_NIGHT.finditer(text):
        if not _is_noise(text, match.start()):
            found.append((match.start(), as_float(match.group(1)) or 0))
    for match in _NIGHT_THEN_PRICE.finditer(text):
        if not _is_noise(text, match.start()):
            found.append((match.start(), as_float(match.group(1)) or 0))
    for match in _FOR_N_NIGHTS.finditer(text):
        if _is_noise(text, match.start()):
            continue
        total, nights = as_float(match.group(1)) or 0, int(match.group(2))
        if nights > 0:
            found.append((match.start(), round(total / nights)))
    found.sort()
    return [price for _, price in found if MIN_NIGHT_PRICE <= price <= MAX_NIGHT_PRICE]


def plate_prices(text: str | None) -> list[float]:
    """Per-plate (per-guest) catering prices -- how banquets and lawns price."""
    if not text:
        return []
    out = []
    for match in _PER_PLATE.finditer(str(text).replace(" ", " ")):
        price = as_float(match.group(1)) or 0
        if MIN_PLATE_PRICE <= price <= MAX_PLATE_PRICE and not _is_noise(text, match.start()):
            out.append(price)
    return out


def own_plate_prices(text: str | None) -> list[float]:
    """The venue's own veg / non-veg pair: the first two plate prices on the page.

    Further plate prices on a directory profile are its "similar venues" cards.
    """
    return plate_prices(text)[:2]


# --- size & capacity -------------------------------------------------------

_BEDROOMS = re.compile(r"(?<![\d.])(\d{1,2})\s*[-\s]?(?:bed\s?rooms?|bhk|bdrs?|br\b|bedroom)", re.I)
_BATHROOMS = re.compile(r"\b(\d{1,2}(?:\.5)?)\s*(?:private\s+|shared\s+)?bath(?:room)?s?\b", re.I)
_GUESTS = re.compile(
    r"(?:up\s*to|upto|max(?:imum)?|accommodates|sleeps|for)?\s*(\d{1,3})\s*(?:-|to|–)?\s*(\d{1,3})?\s*guests?\b",
    re.I,
)
_EVENT_CAPACITY = re.compile(
    r"(?:(?:capacity|seating|floating|accommodat\w*|host\w*)\D{0,25}(\d{2,4})(?:\s*(?:-|to|–)\s*(\d{2,4}))?"
    r"|(\d{2,4})\s*(?:-|to|–)\s*(\d{2,4})\s*(?:pax|guests|people)"
    r"|(\d{2,4})\s*pax)",
    re.I,
)


def parse_bedrooms(text: str | None) -> int | None:
    values = [int(m.group(1)) for m in _BEDROOMS.finditer(text or "") if 0 < int(m.group(1)) <= 40]
    return values[0] if values else None


def parse_bathrooms(text: str | None) -> float | None:
    values = [float(m.group(1)) for m in _BATHROOMS.finditer(text or "") if 0 < float(m.group(1)) <= 40]
    return values[0] if values else None


def parse_guests(text: str | None) -> int | None:
    """Maximum stay capacity, e.g. '10 - 15 Guests' -> 15."""
    for match in _GUESTS.finditer(text or ""):
        high = match.group(2) or match.group(1)
        value = int(high)
        if 1 < value <= 200:
            return value
    return None


def parse_event_capacity(text: str | None) -> int | None:
    """The FIRST stated function capacity. Later ones on a listing page belong
    to the "similar venues" strip, so the maximum would describe a neighbour."""
    for match in _EVENT_CAPACITY.finditer(text or ""):
        numbers = [int(g) for g in match.groups() if g]
        if numbers and 20 <= max(numbers) <= 5000:
            return max(numbers)
    return None


# --- amenities & policies --------------------------------------------------

AMENITY_PATTERNS: dict[str, re.Pattern[str]] = {
    label: re.compile(pattern, re.I) for label, pattern in {
        "Private Pool": r"private\s+(?:infinity\s+|plunge\s+|swimming\s+|heated\s+)?pool|pool\s+villa|with\s+(?:a\s+)?(?:private\s+)?pool|pool\s*/\s*jacuzzi",
        "Infinity Pool": r"infinity\s+pool",
        "Swimming Pool": r"swimming\s+pool|\bpools?\b",
        "Jacuzzi": r"jacuzzi|hot\s+tub",
        "Lawn / Garden": r"\blawns?\b|garden",
        "Lake / Dam View": r"lake\s*(?:view|front|side)|dam\s+view|lakefront|waterfront|by\s+the\s+lake",
        "Valley / Mountain View": r"valley\s+view|mountain\s+view|hill\s+view|sahyadri",
        "Vineyard Setting": r"vineyard",
        "In-house Chef / Meals": r"in-?house\s+chef|private\s+chef|meals?\s+available|chef\s+on\s+call",
        "Bar": r"\bbar\b|bar\s+counter",
        "Bonfire": r"bonfire",
        "Barbecue": r"barbe?que|\bbbq\b",
        "Games Room": r"games?\s*room|indoor\s+games|snooker|pool\s+table|table\s+tennis|foosball",
        "Sports / Turf": r"turf|pickleball|tennis\s+court|cricket|badminton|volleyball",
        "Spa": r"\bspa\b|sauna|steam\s+room",
        "Gym": r"\bgym\b|fitness\s+cent",
        "Banquet Hall": r"banquet",
        "Conference / Offsite": r"conference|board\s*room|corporate\s+offsite|meeting\s+room",
        "Wi-Fi": r"wi-?fi",
        "Parking": r"parking",
        "Power Backup": r"power\s+back-?up|inverter|generator",
        "Caretaker": r"caretaker",
        "Pet Friendly": r"pet[\s-]?friendly|pets?\s+(?:are\s+)?(?:allowed|welcome)",
        "Horse Riding": r"horse\s+riding",
        "Boating": r"boating|kayak",
    }.items()
}

_EVENT_YES = re.compile(
    r"ideal\s+for\s+events|events?\s+(?:are\s+)?(?:allowed|permitted|welcome)|parties\s+(?:are\s+)?allowed|"
    r"(?:host|hosting)\s+(?:an?\s+)?(?:events?|parties|celebrations|weddings?|functions?)|"
    r"(?:intimate\s+)?(?:wedding|functions|celebrations?|sangeet|haldi|mehendi|pre-?wedding|corporate\s+offsites?)"
    r"(?:\s+and\s+\w+)?\s+(?:venue|friendly|ideal|possible|welcome)|perfect\s+for\s+(?:group\s+)?celebrations|"
    r"available\s+for\s+(?:shoots|events)|event\s+venue|banquet|wedding\s+venue",
    re.I,
)
_EVENT_NO = re.compile(
    r"(?:events?|parties|loud\s+music)\s+(?:are\s+)?(?:not|strictly\s+not)\s+(?:allowed|permitted)|"
    r"no\s+(?:events|parties)\b|does\s+not\s+allow\s+(?:events|parties)",
    re.I,
)
_PET_NO = re.compile(r"pets?\s+(?:are\s+)?(?:usually\s+)?not\s+allowed|no\s+pets", re.I)


def extract_amenities(text: str | None) -> list[str]:
    if not text:
        return []
    found = [label for label, pattern in AMENITY_PATTERNS.items() if pattern.search(text)]
    # "Swimming Pool" is the generic catch-all; it adds nothing next to a
    # private or infinity pool.
    if "Swimming Pool" in found and ({"Private Pool", "Infinity Pool"} & set(found)):
        found.remove("Swimming Pool")
    return found


def detect_event_friendly(text: str | None) -> bool | None:
    if not text:
        return None
    if _EVENT_NO.search(text):
        return False
    return True if _EVENT_YES.search(text) else None


def detect_pet_friendly(text: str | None) -> bool | None:
    if not text:
        return None
    if _PET_NO.search(text):
        return False
    return True if AMENITY_PATTERNS["Pet Friendly"].search(text) else None


def enrich_from_text(lead: "Lead", text: str | None, *, price_source: str = "") -> "Lead":
    """Fill EMPTY venue attributes on `lead` from free text. Never overwrites.

    Shared by every source that reads a venue's own description -- platform
    pages, directory profiles, the venue's website -- so they all read the
    same things the same way.
    """
    if not text:
        return lead
    text = clean_text(text)
    if lead.bedrooms is None:
        lead.bedrooms = parse_bedrooms(text)
    if lead.bathrooms is None:
        lead.bathrooms = parse_bathrooms(text)
    if lead.max_guests is None:
        lead.max_guests = parse_guests(text)
    if lead.event_capacity is None:
        lead.event_capacity = parse_event_capacity(text)
    lead.amenities = dedupe_preserving_order([*lead.amenities, *extract_amenities(text)])
    if lead.has_pool is None and ({"Private Pool", "Infinity Pool"} & set(lead.amenities)):
        lead.has_pool = True
    if lead.has_lawn is None and "Lawn / Garden" in lead.amenities:
        lead.has_lawn = True
    if lead.event_friendly is None:
        lead.event_friendly = detect_event_friendly(text)
    if lead.pet_friendly is None:
        lead.pet_friendly = detect_pet_friendly(text)
    if lead.price_min is None:
        nights = nightly_prices(text)
        plates = own_plate_prices(text) if not nights else []
        if nights:
            set_price(lead, nights[0], nights[0], "per night", price_source)
        elif plates:
            set_price(lead, min(plates), max(plates), "per plate", price_source)
    return lead


def set_price(lead: "Lead", low: float | None, high: float | None,
              basis: str, source: str) -> None:
    if low is None and high is None:
        return
    low = low if low is not None else high
    high = high if high is not None else low
    lead.price_min, lead.price_max = float(min(low, high)), float(max(low, high))
    lead.price_basis = basis
    lead.price_source = source


# --- settings --------------------------------------------------------------

VENUE_TYPES = (
    "Private Pool Villa",
    "Villa / Bungalow",
    "Farmhouse / Farm Stay",
    "Vineyard / Winery Resort",
    "Resort",
    "Glamping / Camp",
    "Heritage Property",
    "Premium Hotel",
    "Hotel",
    "Banquet / Event Lawn",
    "Boutique Stay / Homestay",
    "Brand / Operator",
    "Unclassified",
    "Not a Venue",
)

#: The discovery category platform sources and the brand roll-up file
#: genuine operators under. A Brand row from anywhere else is re-derived.
BRAND_CATEGORY = "Brands & Operators"

# Operators first: one of them controls a whole portfolio of the venues below.
# Operators first: one of them controls a whole portfolio of the venues below.
# Excluded last: not a venue at all (a real-estate agent, a tour operator).
TIERS = ("Operator", "Luxury", "Premium", "Standard", "Unverified", "Excluded")
TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}

DEFAULT_PREMIUM_PLATFORMS = ("stayvista", "saffronstays", "elivaas", "lohono", "isprava",
                             "luxunlock", "airbnb_luxe")

DEFAULT_HOTEL_CHAINS = (
    "taj", "vivanta", "gateway", "ginger", "courtyard", "marriott", "fairfield", "radisson",
    "hyatt", "novotel", "ibis", "lemon tree", "express inn", "fortune", "welcomhotel", "itc",
    "holiday inn", "sayaji", "club mahindra", "sterling", "the source at sula", "beyond by sula",
    "st. laurn", "st laurn", "kamat", "mahindra", "renaissance", "four points", "hilton",
    "doubletree", "the fern", "fern residency", "sai leela", "regenta", "royal orchid",
)
PREMIUM_HOTEL_CHAINS = (
    "taj", "vivanta", "courtyard", "marriott", "radisson", "hyatt", "novotel", "express inn",
    "welcomhotel", "itc", "the source at sula", "beyond by sula", "club mahindra", "renaissance",
    "four points", "hilton", "doubletree", "st. laurn", "st laurn",
)


@dataclass
class Brand:
    name: str
    aliases: list[str] = field(default_factory=list)
    #: premium / luxury operators lift a property's tier; budget ones cap it.
    tier: str = "premium"          # premium | budget | neutral
    website: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    instagram: str | None = None

    def matches(self, text: str) -> bool:
        haystack = f" {text.lower()} "
        return any(
            re.search(r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])", haystack)
            for alias in [self.name, *self.aliases] if alias
        )


@dataclass
class VenueSettings:
    center: tuple[float, float] | None = None
    premium_platforms: tuple[str, ...] = DEFAULT_PREMIUM_PLATFORMS
    hotel_chains: tuple[str, ...] = DEFAULT_HOTEL_CHAINS
    premium_hotel_chains: tuple[str, ...] = PREMIUM_HOTEL_CHAINS
    brands: list[Brand] = field(default_factory=list)
    # Qualification ("high-end venue") thresholds -- any ONE criterion is enough.
    rating_min: float = 4.3
    rating_min_reviews: int = 20
    bedrooms_min: int = 3
    # Price bands drive the premium signal and the Luxury tier, never inclusion.
    luxury_night_price: float = 25_000
    luxury_plate_price: float = 2_000
    luxury_points: int = 8
    #: A KNOWN price below this caps the venue at Standard. null disables.
    budget_night_price: float | None = 4_000
    night_price_points: tuple[tuple[float, int], ...] = (
        (50_000, 5), (25_000, 4), (15_000, 3), (8_000, 2), (4_000, 1))
    plate_price_points: tuple[tuple[float, int], ...] = (
        (2_500, 4), (1_500, 3), (1_000, 2), (700, 1))
    points_saturation: float = 12.0

    @classmethod
    def from_config(cls, config) -> "VenueSettings":
        get = config.get if config is not None else (lambda _k, d=None: d)
        tiers = get("venue_tiers", {}) or {}
        center = None
        if config is not None and get("geo.center"):
            center = config.geo_center
        brands = [
            Brand(
                name=str(item["name"]),
                aliases=[str(a) for a in (item.get("aliases") or [])],
                tier=str(item.get("tier", "premium")).lower(),
                website=item.get("website"),
                phones=[str(p) for p in (item.get("phones") or [])],
                emails=[str(e) for e in (item.get("emails") or [])],
                instagram=item.get("instagram"),
            )
            for item in (get("brands", []) or []) if item and item.get("name")
        ]

        def bands(key, default):
            raw = tiers.get(key)
            if not raw:
                return default
            return tuple(sorted(((float(k), int(v)) for k, v in dict(raw).items()), reverse=True))

        budget = tiers.get("budget_night_price", cls.budget_night_price)
        return cls(
            center=center,
            premium_platforms=tuple(str(p).lower() for p in
                                    (get("premium_platforms", None) or DEFAULT_PREMIUM_PLATFORMS)),
            hotel_chains=tuple(str(c).lower() for c in (get("hotel_chains", None) or DEFAULT_HOTEL_CHAINS)),
            premium_hotel_chains=tuple(str(c).lower() for c in
                                       (get("premium_hotel_chains", None) or PREMIUM_HOTEL_CHAINS)),
            brands=brands,
            rating_min=float(tiers.get("rating_min", cls.rating_min)),
            rating_min_reviews=int(tiers.get("rating_min_reviews", cls.rating_min_reviews)),
            bedrooms_min=int(tiers.get("bedrooms_min", cls.bedrooms_min)),
            luxury_night_price=float(tiers.get("luxury_night_price", cls.luxury_night_price)),
            luxury_plate_price=float(tiers.get("luxury_plate_price", cls.luxury_plate_price)),
            luxury_points=int(tiers.get("luxury_points", cls.luxury_points)),
            budget_night_price=float(budget) if budget else None,
            night_price_points=bands("night_price_points", cls.night_price_points),
            plate_price_points=bands("plate_price_points", cls.plate_price_points),
            points_saturation=float(tiers.get("points_saturation", cls.points_saturation)),
        )

    def brand_by_name(self, name: str) -> Brand | None:
        for brand in self.brands:
            if brand.name.lower() == (name or "").lower():
                return brand
        return None


# --- classification --------------------------------------------------------

_NEAR_PHRASE = re.compile(r"\b(?:near|close\s+to|next\s+to|by|opposite|behind|off)\s+[\w\s'&.-]{2,40}", re.I)
_STAR_CLASS = re.compile(r"\b([1-5])[\s-]?star\b", re.I)
_OPERATOR_WORDS = re.compile(
    r"travel\s+agency|tour\s+operator|property\s+management|real\s+estate|holiday\s+homes?\s+(?:company|rental)|"
    r"villa\s+rental\s+(?:agency|company|services)|rental\s+agency|hospitality\s+(?:company|services)",
    re.I,
)


_REAL_ESTATE_CATEGORY = re.compile(
    r"real\s+estate|builders?|construction|property\s+(?:dealer|consultant|agent)", re.I)
_NON_VENUE_CATEGORY = re.compile(
    r"tour\s+operator|travel\s+agency|tour\s+agency|car\s+rental|bike|two\s+wheeler|taxi|"
    r"tempo|cab\s+service|bus\s+(?:company|service)|transport",
    re.I,
)
_SHORT_STAY_CATEGORY = re.compile(r"(?:short\s+term\s+apartment|condominium|apartment)\s+rental", re.I)


def _has(pattern: str, *texts: str) -> bool:
    return any(re.search(pattern, t or "", re.I) for t in texts)


def is_hotel_chain(name: str, settings: VenueSettings, premium_only: bool = False) -> bool:
    chains = settings.premium_hotel_chains if premium_only else settings.hotel_chains
    lowered = f" {name.lower()} "
    return any(re.search(r"(?<![a-z])" + re.escape(chain) + r"(?![a-z])", lowered) for chain in chains)


def classify_venue_type(lead: "Lead", settings: VenueSettings | None = None) -> tuple[str, str]:
    """(venue_type, reason). Ordered rules: the first confident match wins."""
    settings = settings or VenueSettings()
    if lead.record_type == "Brand" and lead.category == BRAND_CATEGORY:
        return "Brand / Operator", "record is a brand / operator"
    if any(note.startswith("AI: not a bookable venue") for note in lead.notes):
        return "Not a Venue", "AI: not a bookable venue"

    name = lead.business_name or ""
    ptype = lead.property_type or ""
    desc = (lead.description or "")[:800]
    # "Villa near Sula Vineyards" is a villa, not a winery: drop location phrases
    # before looking for winery / resort words in the name.
    bare_name = _NEAR_PHRASE.sub(" ", name)
    stars = lead.hotel_class or 0

    # Maps answers "villa rental agency" with estate agents, tour operators and
    # bike-rental shops. Only one whose NAME says it runs stays is an operator.
    stay_words = r"villas?|\bstays?\b|holiday\s+homes?|hospitality|resorts?|homestays?"
    if _REAL_ESTATE_CATEGORY.search(ptype) or _has(r"for\s+sale|\bplots?\b", name):
        # Builders sell villas and farmhouses; they do not host anyone in them.
        return "Not a Venue", f"{ptype.lower() or 'property for sale'}"
    if _NON_VENUE_CATEGORY.search(ptype):
        if _has(stay_words, name):
            return "Brand / Operator", "rental/travel category, but the name runs stays"
        return "Not a Venue", f"{ptype.lower()}"
    if _SHORT_STAY_CATEGORY.search(ptype):
        return "Boutique Stay / Homestay", f"{ptype.lower()}"

    if _OPERATOR_WORDS.search(ptype) or (
        _OPERATOR_WORDS.search(name) and not _has(r"villa|resort|farm|lawn|hotel|camp", bare_name)
    ):
        return "Brand / Operator", f"operator wording in {'category' if _OPERATOR_WORDS.search(ptype) else 'name'}"

    # "2BHK" is how a flat is sold too, so only 3+ BHK reads as a villa.
    villa_words = (r"\bvillas?\b|bungalow|bunglow|\b(?:[3-9]|1\d)\s?bhk\b|chalet|mansion|\bestate\b|"
                   r"entire\s+villa")
    small_unit = _has(r"^(?:flat|apartment|condo|serviced\s+apartment|room|guest\s+suite|"
                      r"private\s+room|loft|studio)$", ptype.strip())
    if small_unit and not _has(villa_words, bare_name):
        return "Boutique Stay / Homestay", f"{ptype.lower()} listing"
    hotel_like = _has(r"\bhotel\b|\binn\b|\bsuites?\b", bare_name, ptype) and not _has(villa_words, bare_name)
    if hotel_like or is_hotel_chain(name, settings):
        if stars >= 4 or is_hotel_chain(name, settings, premium_only=True):
            return "Premium Hotel", f"{stars}-star hotel" if stars >= 4 else "premium hotel chain"
        if not _has(r"resort", bare_name, ptype):
            return "Hotel", "hotel in name/category"

    if _has(r"winery|vineyard|wine\s+(?:resort|village|estate|park)|\bwines\b", ptype, bare_name) \
            and not _has(villa_words, bare_name):
        return "Vineyard / Winery Resort", "winery / vineyard"

    if _has(r"glamp|\btents?\b|\bcamp(?:s|site|ing)?\b|\bdomes?\b|treehouse|tree\s+house", bare_name, ptype):
        return "Glamping / Camp", "camp / tent / glamping"

    if _has(r"banquet|wedding\s+venue|event\s+venue|marriage\s+hall|mangal\s+karyalay|karyalaya?|"
            r"convention|\blawns?\b|party\s+hall|function\s+hall", ptype, bare_name) \
            and not _has(villa_words + r"|resort|farm", bare_name):
        return "Banquet / Event Lawn", "banquet / lawn / event venue"

    if _has(r"resort", bare_name, ptype):
        return "Resort", "resort in name/category"

    if _has(r"heritage|haveli|\bwada\b|palace|\bfort\b|rajwada", bare_name, ptype):
        return "Heritage Property", "heritage / wada / palace"

    if _has(r"farm\s*house|farmhouse|farm\s+stay|\bfarms?\b|orchard|agro|agri", bare_name, ptype):
        return "Farmhouse / Farm Stay", "farm / farmhouse"

    home_like = _has(r"\bhome\b|\bhouse\b|holiday\s+home|entire\s+home", ptype, bare_name)
    if _has(villa_words, bare_name, ptype) or (home_like and ((lead.bedrooms or 0) >= 3 or lead.has_pool)):
        if lead.has_pool:
            return "Private Pool Villa", "villa with a private pool"
        return "Villa / Bungalow", "villa / bungalow / 3+ bedroom home"

    if _has(r"home\s*stay|homestay|cottage|guest\s*house|b\s*&\s*b|bed\s+and\s+breakfast|\bstays?\b|"
            r"\bhome\b|\bflat\b|apartment|\bhouse\b|\broom\b", bare_name, ptype):
        return "Boutique Stay / Homestay", "homestay / cottage / home"

    if _has(r"resort|villa|farm", desc):
        # Name told us nothing; the description is weaker evidence, say so.
        word = re.search(r"resort|villa|farm", desc, re.I).group(0).lower()
        return {"resort": "Resort", "villa": "Villa / Bungalow" if not lead.has_pool else "Private Pool Villa",
                "farm": "Farmhouse / Farm Stay"}[word], f"'{word}' in description"
    return "Unclassified", "no venue-type evidence"


def detect_brand(text: str, settings: VenueSettings) -> Brand | None:
    """The configured operator named in `text`, longest alias first."""
    matches = [brand for brand in settings.brands if brand.matches(text or "")]
    if not matches:
        return None
    return max(matches, key=lambda b: max(len(a) for a in [b.name, *b.aliases]))


# --- tiering ---------------------------------------------------------------


@dataclass
class TierResult:
    tier: str
    points: int
    qualifies: bool
    reasons: list[str]


def _band_points(value: float, bands: Iterable[tuple[float, int]]) -> int:
    for threshold, points in bands:
        if value >= threshold:
            return points
    return 0


def compute_tier(lead: "Lead", settings: VenueSettings) -> TierResult:
    """Tier the VENUE. Any one high-end criterion qualifies it; price ranks it.

    Criteria (any one): a premium platform / operator, a strong rating, a
    private pool or enough bedrooms, or being a premium hotel / winery resort.
    Price is deliberately not a criterion -- it only moves a qualified venue
    up to Luxury and weighs most in the premium signal -- except that a KNOWN
    budget price caps a venue at Standard.
    """
    if lead.record_type == "Brand":
        return TierResult(tier="Operator", points=0, qualifies=False,
                          reasons=["brand / operator, not a single venue"])
    if lead.venue_type == "Not a Venue":
        return TierResult(tier="Excluded", points=0, qualifies=False,
                          reasons=["not a venue: " + (lead.venue_type_source or "")])

    reasons: list[str] = []
    points = 0
    qualifies = False

    brand = settings.brand_by_name(lead.managed_by) if lead.managed_by else None
    premium_platforms = [p for p in lead.platforms if p.lower() in settings.premium_platforms]
    if premium_platforms or (brand and brand.tier == "premium"):
        qualifies = True
        points += 2
        via = ", ".join(premium_platforms) or (brand.name if brand else "")
        reasons.append(f"premium platform/operator: {via}")

    rating, reviews = best_rating(lead)
    if rating is not None and rating >= settings.rating_min and reviews >= settings.rating_min_reviews:
        qualifies = True
        strong = rating >= 4.6 and reviews >= 30
        points += 2 if strong else 1
        reasons.append(f"rated {rating:.1f} over {reviews} reviews")

    if lead.has_pool:
        qualifies = True
        points += 1
        reasons.append("private pool")
    if lead.bedrooms and lead.bedrooms >= settings.bedrooms_min:
        qualifies = True
        points += 2 if lead.bedrooms >= 5 else 1
        reasons.append(f"{lead.bedrooms} bedrooms")

    if lead.venue_type in ("Premium Hotel", "Vineyard / Winery Resort"):
        qualifies = True
        points += 2
        reasons.append(lead.venue_type.lower())
        if (lead.hotel_class or 0) >= 5:
            points += 1
    if lead.event_capacity and lead.event_capacity >= 300:
        points += 1
        reasons.append(f"event capacity {lead.event_capacity}")

    night_price = lead.price_min if lead.price_basis == "per night" else None
    plate_price = lead.price_min if lead.price_basis == "per plate" else None
    if night_price:
        earned = _band_points(night_price, settings.night_price_points)
        points += earned
        reasons.append(f"₹{night_price:,.0f}/night")
    if plate_price:
        points += _band_points(plate_price, settings.plate_price_points)
        reasons.append(f"₹{plate_price:,.0f}/plate")

    has_evidence = bool(rating is not None or lead.price_min or lead.platforms
                        or lead.bedrooms or lead.has_pool or lead.amenities)
    budget_brand = brand is not None and brand.tier == "budget"
    budget_price = (settings.budget_night_price is not None and night_price is not None
                    and night_price < settings.budget_night_price)

    if budget_brand or budget_price:
        tier = "Standard" if has_evidence else "Unverified"
        reasons.append("budget operator" if budget_brand else
                       f"price under ₹{settings.budget_night_price:,.0f}/night")
    elif qualifies:
        luxury = (
            (night_price and night_price >= settings.luxury_night_price)
            or (plate_price and plate_price >= settings.luxury_plate_price)
            or points >= settings.luxury_points
        )
        tier = "Luxury" if luxury else "Premium"
    else:
        tier = "Standard" if has_evidence else "Unverified"
    return TierResult(tier=tier, points=points, qualifies=qualifies, reasons=reasons)


def annotate(lead: "Lead", settings: VenueSettings) -> "Lead":
    """Recompute every derived venue field. Idempotent; run on every rescore."""
    if lead.latitude is None and lead.gmaps_url:
        point = coords_from_maps_url(lead.gmaps_url)
        if point:
            lead.latitude, lead.longitude = point
    if settings.center and lead.latitude is not None and lead.longitude is not None:
        lead.distance_km = round(haversine_km(lead.latitude, lead.longitude, *settings.center), 1)
    if lead.hotel_class is None:
        match = _STAR_CLASS.search(f"{lead.property_type} {lead.business_name}")
        if match:
            lead.hotel_class = int(match.group(1))
    if not lead.managed_by:
        brand = detect_brand(lead.business_name, settings)
        if brand:
            lead.managed_by = brand.name
    if not lead.record_type:
        lead.record_type = "Property"
    # The AI's call stands once made; rules re-run so a new fact (a pool, a
    # Maps category) can still upgrade a rules-based type.
    if any(note.startswith("AI: not a bookable venue") for note in lead.notes):
        lead.venue_type, lead.venue_type_source = "Not a Venue", "ai"
    elif lead.venue_type_source != "ai" or not lead.venue_type:
        venue_type, why = classify_venue_type(lead, settings)
        lead.venue_type = venue_type
        lead.venue_type_source = f"rules: {why}"
    # Record type follows the type, except for genuine operator rows.
    if lead.venue_type == "Brand / Operator":
        lead.record_type = "Brand"
    elif lead.venue_type == "Not a Venue":
        lead.record_type = "Not a venue"
    elif lead.category != BRAND_CATEGORY:
        lead.record_type = "Property"
    result = compute_tier(lead, settings)
    lead.venue_tier = result.tier
    lead.tier_points = result.points
    lead.tier_reasons = result.reasons
    return lead
