"""The Source contract shared by every scraper."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

from ..net.browser import BrowserSession
from ..config import Category, Config, Locality
from ..engine.failures import FailureLog
from ..net.http import HttpClient
from ..core.lead import Lead
from ..net.ratelimit import RateLimiter

log = logging.getLogger(__name__)


@dataclass
class SourceContext:
    """Everything a source needs, injected rather than imported."""

    config: Config
    http: HttpClient
    limiter: RateLimiter
    failures: FailureLog
    browser: BrowserSession
    store: "object"  # LeadStore; typed loosely to avoid a circular import


class Source(ABC):
    """Base class for discovery sources.

    Contract: `discover` yields partial Leads and NEVER raises. Anything that
    goes wrong is recorded on `ctx.failures` and the source keeps going or
    returns -- one dead directory must not end the run.
    """

    name: str = "source"
    #: Set True for sources driven by Playwright rather than requests.
    uses_browser: bool = False
    #: How often the pipeline calls `discover`:
    #:   "combo"    -- once per locality x category (the default)
    #:   "locality" -- once per locality, category=None (a platform search page
    #:                 covers every venue type at once)
    #:   "run"      -- once per run, locality=None and category=None (a sitemap
    #:                 lists the whole region in one go)
    scope: str = "combo"
    #: Run order within a unit of work; lower runs first (see sources/registry.py).
    priority: int = 100
    #: Key of the pack this class belongs to, set by the pack; None for core.
    pack: str | None = None
    #: Narrows where this class applies, on top of its pack's own reach.
    applicable_countries: frozenset[str] | None = None

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.cfg = ctx.config.source_cfg(self.name)
        min_delay, max_delay = ctx.config.delays(self.name)
        for key in self.rate_keys():
            ctx.limiter.configure(key, min_delay, max_delay)

    def rate_keys(self) -> list[str]:
        """Hostnames this source hits, for rate-limiter configuration."""
        return [self.name]

    # --- the run's country, as every parser needs it ---------------------

    @property
    def region(self) -> str:
        """Phone-parsing region: the run's country."""
        return self.ctx.config.country

    @property
    def currency(self) -> str:
        return self.ctx.config.currency

    @property
    def place_words(self) -> list[str]:
        """Country / region words that never distinguish one business from another."""
        return self.ctx.config.place_words

    @abstractmethod
    def discover(self, locality: Locality | None, category: Category | None) -> Iterator[Lead]:
        """Yield partial Leads for one unit of work (see `scope`)."""
        raise NotImplementedError

    def assign_locality(self, lead: Lead, fallback: Locality | None = None,
                        *extra_text: str) -> Lead:
        """Venue runs: put the lead in the locality its own name/address/pin says.

        A Maps query for "villa in Igatpuri" happily returns villas in Ghoti and
        Trimbak; filing them under the query's locality would split one venue
        across two localities and defeat the locality-scoped dedup rules.
        """
        if not self.ctx.config.is_venue_run:
            return lead
        text = " ".join([lead.business_name, lead.address, *extra_text])
        resolved = self.ctx.config.locality_for(text, lead.latitude, lead.longitude)
        if resolved is not None:
            if fallback is not None and resolved.name != fallback.name:
                lead.notes.append(f"found via {fallback.name} search")
            lead.locality = resolved.name
        elif fallback is not None and not lead.locality:
            lead.locality = fallback.name
        return lead

    def out_of_region(self, lead: Lead, *extra_text: str) -> bool:
        """True when the lead's pin is beyond `geo.radius_km`, or -- with no pin --
        its name/address names a place in `geo.exclude_keywords`.

        Platform searches spill over: Booking's "Igatpuri" results include
        Shahapur and Bhiwandi farm stays that carry no coordinates at all.
        """
        radius = self.ctx.config.region_radius_km
        center = self.ctx.config.geo_center
        if radius and center and lead.latitude is not None and lead.longitude is not None:
            from ..core.geo import haversine_km

            return haversine_km(lead.latitude, lead.longitude, *center) > radius
        excluded = [str(k).lower() for k in (self.ctx.config.get("geo.exclude_keywords") or [])]
        if not excluded:
            return False
        import re

        text = " ".join([lead.business_name, lead.address, *extra_text]).lower()
        return any(re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", text) for k in excluded)

    # --- helpers available to every subclass ------------------------------

    def fail(self, reason: str, **kwargs) -> None:
        self.ctx.failures.record(self.name, reason, **kwargs)

    def make_lead(self, **kwargs) -> Lead:
        """Build a Lead already tagged with this source."""
        sources = kwargs.pop("sources", [])
        lead = Lead(sources=[self.name, *sources], **kwargs)
        return lead


class Enricher(ABC):
    """Base class for per-lead enrichment (website contact pages, Instagram)."""

    name: str = "enricher"
    #: Run order within a unit of work; lower runs first (see sources/registry.py).
    priority: int = 100
    #: Key of the pack this class belongs to, set by the pack; None for core.
    pack: str | None = None
    #: Narrows where this class applies, on top of its pack's own reach.
    applicable_countries: frozenset[str] | None = None

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.cfg = ctx.config.source_cfg(self.name)
        min_delay, max_delay = ctx.config.delays(self.name)
        for key in self.rate_keys():
            ctx.limiter.configure(key, min_delay, max_delay)

    def rate_keys(self) -> list[str]:
        return [self.name]

    @property
    def region(self) -> str:
        """Phone-parsing region: the run's country."""
        return self.ctx.config.country

    @property
    def currency(self) -> str:
        return self.ctx.config.currency

    def targets(self, store) -> list[tuple[int, Lead]]:
        """Which stored leads this enricher wants, as (lead_id, Lead) pairs.

        Overridden by each enricher so the pipeline can stay generic -- it
        iterates whatever enrichers are registered rather than knowing their
        names.
        """
        return store.all_with_ids()

    @abstractmethod
    def enrich(self, lead: Lead) -> Lead | None:
        """Return a partial Lead with any NEW fields found, or None."""
        raise NotImplementedError

    def fail(self, reason: str, **kwargs) -> None:
        self.ctx.failures.record(self.name, reason, **kwargs)
