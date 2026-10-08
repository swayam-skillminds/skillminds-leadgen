"""Country and vertical packs.

* ``in``          -- India: Justdial, Sulekha, IndiaMART.
* ``events``      -- the events trade: WedMeGood, WeddingWire India.
* ``hospitality`` -- stays and venues: StayVista, SaffronStays, Elivaas, Airbnb,
  Booking.com, plus venue classification and tiers (``venues.py``).
* ``social``      -- Facebook, Instagram, search-indexed LinkedIn URLs.
* ``uk``          -- United Kingdom: Companies House officers (decision_makers).

Core sources (Google Maps, Google Search, website, Maps lookup) are not in a
pack: they apply everywhere. See ``sources/registry.py``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from .base import Pack, slugify

__all__ = ["Pack", "all_packs", "pack", "inactive_reason", "registries_for", "slugify",
           "city_slug"]


@lru_cache(maxsize=1)
def _packs() -> tuple[Pack, ...]:
    # Imported here, not at module level: pack modules import the sources
    # package, which imports this one.
    from .events import PACK as EVENTS
    from .hospitality import PACK as HOSPITALITY
    from .india import PACK as INDIA
    from .social import PACK as SOCIAL
    from .uk import PACK as UK

    return (INDIA, EVENTS, HOSPITALITY, SOCIAL, UK)


def all_packs() -> list[Pack]:
    return list(_packs())


def registries_for(country: str) -> list[type]:
    """Company registries (decision_makers) of every pack that applies to ``country``."""
    return [cls for p in _packs() if p.applicable_countries is None
            or (country or "").upper() in p.applicable_countries for cls in p.registries]


def pack(key: str) -> Pack | None:
    return next((p for p in _packs() if p.key == key), None)


def inactive_reason(source_cls: type, country: str, verticals: Iterable[str]) -> str | None:
    """Why a source class does not apply to a run, or None when it does.

    Classes outside any pack (core sources, test stubs) always apply.
    """
    verticals = list(verticals)
    owner = pack(getattr(source_cls, "pack", None) or "")
    if owner is not None:
        reason = owner.inactive_reason(country, verticals)
        if reason:
            return reason
    countries = getattr(source_cls, "applicable_countries", None)
    if countries is not None and (country or "").upper() not in countries:
        return (f"{getattr(source_cls, 'name', source_cls.__name__)} only covers "
                f"{', '.join(sorted(countries))}; this run is in {(country or '').upper()}")
    return None


def city_slug(source_cls_or_name, city: str) -> str:
    """URL segment a source uses for a city, via its pack's overrides."""
    name = source_cls_or_name if isinstance(source_cls_or_name, str) else source_cls_or_name.name
    key = None if isinstance(source_cls_or_name, str) else getattr(source_cls_or_name, "pack", None)
    owner = pack(key or "")
    if owner is None:
        owner = next((p for p in _packs() if name in {c.name for c in p.sources}), None)
    return owner.city_slug(name, city) if owner else slugify(city)
