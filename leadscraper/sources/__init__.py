"""Discovery sources. Each module registers one Source subclass."""

from .base import Source, SourceContext
from .google_maps import GoogleMapsSource
from .google_search import GoogleSearchSource
from .directories import WedMeGoodSource, WeddingWireSource
from .indiamart import IndiaMartSource
from .local_directories import JustdialSource, SulekhaSource
from .facebook import FacebookSource
from .linkedin_serp import LinkedInSerpSource
from .instagram import InstagramEnricher
from .website import WebsiteEnricher
from .maps_lookup import MapsLookupEnricher
from .platforms import (
    AirbnbSource, BookingSource, ElivaasSource, SaffronStaysSource, StayVistaSource,
)

# Discovery sources run per locality x category.
DISCOVERY_SOURCES: dict[str, type[Source]] = {
    "google_maps": GoogleMapsSource,
    "google_search": GoogleSearchSource,
    "wedmegood": WedMeGoodSource,
    "weddingwire": WeddingWireSource,
    "indiamart": IndiaMartSource,
    "justdial": JustdialSource,
    "sulekha": SulekhaSource,
    "facebook": FacebookSource,
    "linkedin_serp": LinkedInSerpSource,
    # Villa / stay platforms. Disabled unless a config enables them, so the
    # vendor run is untouched by their existence.
    "stayvista": StayVistaSource,
    "saffronstays": SaffronStaysSource,
    "elivaas": ElivaasSource,
    "airbnb": AirbnbSource,
    "booking": BookingSource,
}

# Enrichment sources run per already-discovered lead.
ENRICHMENT_SOURCES = {
    # First, so the websites and Instagram handles it finds on Maps are then
    # read by the enrichers below. Off unless a config enables it.
    "maps_lookup": MapsLookupEnricher,
    "website": WebsiteEnricher,
    "instagram": InstagramEnricher,
}

__all__ = ["Source", "SourceContext", "DISCOVERY_SOURCES", "ENRICHMENT_SOURCES"]
