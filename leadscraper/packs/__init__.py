"""Country and vertical packs.

* ``in``          -- India: Justdial, Sulekha, IndiaMART.
* ``events``      -- the events trade: WedMeGood, WeddingWire India.
* ``hospitality`` -- stays and venues: StayVista, SaffronStays, Elivaas, Airbnb,
  Booking.com, plus venue classification and tiers (``venues.py``).
* ``social``      -- Facebook, Instagram, search-indexed LinkedIn URLs.
* ``uk``          -- United Kingdom: Companies House officers (decision_makers) and new
  incorporations (registrations); Find a Tender, Contracts Finder (tenders).
* ``eu``          -- EU and EEA: TED (tenders).
* ``us``          -- United States: SAM.gov (tenders).

The ``in`` pack also carries India's CPPP and GeM (tenders, opt-in).

Core sources (Google Maps, Google Search, website, Maps lookup) are not in a
pack: they apply everywhere. See ``sources/registry.py``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from .base import Pack, slugify

__all__ = ["Pack", "all_packs", "pack", "inactive_reason", "registries_for", "slugify",
           "city_slug", "tender_sources_for", "incorporation_sources_for",
           "all_tender_sources", "all_incorporation_sources"]


@lru_cache(maxsize=1)
def _packs() -> tuple[Pack, ...]:
    # Imported here, not at module level: pack modules import the sources
    # package, which imports this one.
    from .eu import PACK as EU
    from .events import PACK as EVENTS
    from .hospitality import PACK as HOSPITALITY
    from .india import PACK as INDIA
    from .social import PACK as SOCIAL
    from .uk import PACK as UK
    from .us import PACK as US

    return (INDIA, EVENTS, HOSPITALITY, SOCIAL, UK, EU, US)


def all_packs() -> list[Pack]:
    return list(_packs())


def _applies(p: Pack, country: str) -> bool:
    return p.applicable_countries is None or (country or "").upper() in p.applicable_countries


def registries_for(country: str) -> list[type]:
    """Company registries (decision_makers) of every pack that applies to ``country``."""
    return [cls for p in _packs() if _applies(p, country) for cls in p.registries]


def all_tender_sources() -> list[type]:
    """Every procurement portal (tenders), in pack order."""
    return [cls for p in _packs() for cls in p.tenders]


def tender_sources_for(country: str) -> list[type]:
    """The procurement portals (tenders) of every pack that applies to ``country``."""
    return [cls for p in _packs() if _applies(p, country) for cls in p.tenders
            if getattr(cls, "applicable_countries", None) is None
            or (country or "").upper() in cls.applicable_countries]


def _generic_incorporations() -> list[type]:
    from ..sources.registers.opencorporates import OpenCorporatesIncorporations

    return [OpenCorporatesIncorporations]


def all_incorporation_sources() -> list[type]:
    """Every company register's new incorporations (registrations): packs', then generic."""
    return [cls for p in _packs() for cls in p.incorporations] + _generic_incorporations()


def incorporation_sources_for(country: str) -> list[type]:
    """The registers (registrations) that cover ``country``: its packs', then the generic
    ones (OpenCorporates covers most jurisdictions)."""
    return [cls for p in _packs() if _applies(p, country) for cls in p.incorporations] \
        + _generic_incorporations()


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
