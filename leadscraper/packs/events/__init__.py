"""Events pack: wedding / event-vendor directories (India-only sites)."""

from ..base import Pack
from .directories import WedMeGoodSource, WeddingWireSource

PACK = Pack(
    key="events",
    title="Events trade",
    description="WedMeGood and WeddingWire India: event-vendor and venue directories.",
    # Both sites list Indian vendors only.
    applicable_countries=frozenset({"IN"}),
    applicable_verticals=frozenset({"events"}),
    discovery=(WedMeGoodSource, WeddingWireSource),
    city_slugs={
        # WedMeGood serves Delhi as "delhi-ncr" and 404s on plain "delhi"; the
        # source still probes variants, this is only its first guess.
        "wedmegood": {"delhi": "delhi-ncr", "new delhi": "delhi-ncr"},
    },
)
