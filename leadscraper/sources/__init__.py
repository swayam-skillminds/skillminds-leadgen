"""Sources: the Source/Enricher contract plus the core sources that apply
everywhere. Pack sources live under ``leadscraper/packs``; the combined,
ordered catalogue is ``sources.registry``."""

from .base import Enricher, Source, SourceContext

__all__ = ["Enricher", "Source", "SourceContext", "DISCOVERY_SOURCES", "ENRICHMENT_SOURCES"]


def __getattr__(name: str):
    # Lazy: the registry imports every pack, and packs import this package.
    if name in ("DISCOVERY_SOURCES", "ENRICHMENT_SOURCES"):
        from . import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
