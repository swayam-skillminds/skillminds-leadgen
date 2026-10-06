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
    # No city overrides: these sites route cities inconsistently ("delhi-ncr",
    # "new-delhi"), so the sources probe spelling variants live instead.
)
