"""The pack contract.

A pack groups sources (and vertical logic) that only make sense somewhere:
Justdial inside India, WedMeGood for the events trade, StayVista for stays.
It declares where it applies; a pack that does not apply to a run is
*silently inactive* -- its sources are reported as not built, with the reason,
and never produce a failure.

A source class may narrow its pack's reach with its own
``applicable_countries`` (the hospitality pack is worldwide, but StayVista
only lists Indian villas).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Mapping


def slugify(value: str) -> str:
    """Casefold, strip accents, and collapse anything else into single dashes."""
    decomposed = unicodedata.normalize("NFKD", (value or "").casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", stripped).strip("-")


@dataclass
class Pack:
    key: str
    title: str
    description: str = ""
    #: ISO 3166-1 alpha-2 codes; None means anywhere.
    applicable_countries: frozenset[str] | None = None
    #: Verticals ("events", "hospitality", ...); None means any run.
    applicable_verticals: frozenset[str] | None = None
    discovery: tuple[type, ...] = ()
    enrichment: tuple[type, ...] = ()
    #: Company registries whose officers are decision-makers (Phase 6: decision_makers).
    #: Each class has ``name`` and ``find(http, company, *, key)``; see ``packs/uk``.
    registries: tuple[type, ...] = ()
    #: source name -> {lower-cased city -> URL segment}, for directories that
    #: route a city under a name other than its plain slug.
    city_slugs: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for cls in (*self.discovery, *self.enrichment, *self.registries):
            cls.pack = self.key

    @property
    def sources(self) -> tuple[type, ...]:
        return (*self.discovery, *self.enrichment)

    def inactive_reason(self, country: str, verticals: Iterable[str]) -> str | None:
        """Why this pack does not apply to a run, or None when it does."""
        country = (country or "").upper()
        if self.applicable_countries is not None and country not in self.applicable_countries:
            return (f"pack '{self.key}' covers {', '.join(sorted(self.applicable_countries))};"
                    f" this run is in {country or 'no country'}")
        wanted = {v.lower() for v in verticals}
        if self.applicable_verticals is not None and not (wanted & self.applicable_verticals):
            return (f"pack '{self.key}' is for {', '.join(sorted(self.applicable_verticals))}"
                    f" runs; this run's verticals are {', '.join(sorted(wanted)) or 'none'}")
        return None

    def city_slug(self, source: str, city: str) -> str:
        """The URL segment `source` uses for `city`."""
        override = (self.city_slugs.get(source) or {}).get((city or "").strip().lower())
        return override or slugify(city)
