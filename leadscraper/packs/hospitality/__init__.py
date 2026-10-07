"""Hospitality pack: stay platforms plus venue classification and tiering.

Airbnb and Booking.com work anywhere; StayVista, SaffronStays and Elivaas
list Indian villas only, so those classes narrow the pack's reach themselves.
"""

from ..base import Pack
from .platforms import (
    AirbnbSource, BookingSource, ElivaasSource, SaffronStaysSource, StayVistaSource,
)

PACK = Pack(
    key="hospitality",
    title="Hospitality",
    description="Villa/stay platforms, venue-type classification and venue tiers.",
    applicable_verticals=frozenset({"hospitality"}),
    discovery=(StayVistaSource, SaffronStaysSource, ElivaasSource, AirbnbSource, BookingSource),
)
