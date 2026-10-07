"""Every source the tool knows, core and pack, in run order.

Run order is each class's ``priority`` (lower first): it decides which source
runs first within a unit of work, and so which record a later one merges into.
The values reproduce the order the registry had before packs existed.
"""

from __future__ import annotations

from ..packs import all_packs
from .base import Enricher, Source
from .google_maps import GoogleMapsSource
from .google_search import GoogleSearchSource
from .maps_lookup import MapsLookupEnricher
from .website import WebsiteEnricher

#: Sources that apply everywhere.
CORE_DISCOVERY: tuple[type[Source], ...] = (GoogleMapsSource, GoogleSearchSource)
CORE_ENRICHMENT: tuple[type[Enricher], ...] = (MapsLookupEnricher, WebsiteEnricher)


def _ordered(classes) -> dict[str, type]:
    ordered = sorted(classes, key=lambda cls: (getattr(cls, "priority", 100), cls.name))
    return {cls.name: cls for cls in ordered}


def _discovery() -> dict[str, type[Source]]:
    classes = list(CORE_DISCOVERY)
    for pack in all_packs():
        classes.extend(pack.discovery)
    return _ordered(classes)


def _enrichment() -> dict[str, type[Enricher]]:
    classes = list(CORE_ENRICHMENT)
    for pack in all_packs():
        classes.extend(pack.enrichment)
    return _ordered(classes)


DISCOVERY_SOURCES: dict[str, type[Source]] = _discovery()
ENRICHMENT_SOURCES: dict[str, type[Enricher]] = _enrichment()


def source_class(name: str) -> type | None:
    return DISCOVERY_SOURCES.get(name) or ENRICHMENT_SOURCES.get(name)
