"""Fill in what a run config leaves out about where it is, by geocoding.

A config only has to name a country and a place. Before a run, the place's
centre (Maps viewport, browser position) and its state/province (name noise,
address matching) come from Nominatim -- politely and cached, see
``core/geo.py``. Venue runs, the only runs that place and filter listings by
pin, also get area centres and, when unset, a radius of 1.5x the place's own
extent. A vendor run's radius is never guessed: it would only drop listings.

Anything the config states is kept as stated; anything the geocoder cannot
find stays empty and the run goes ahead without it.
"""

from __future__ import annotations

import logging

from ..core.geo import GeocodeCache, Geocoder
from ..paths import cache_dir
from .schema import RunConfig

log = logging.getLogger(__name__)


def default_geocoder() -> Geocoder:
    return Geocoder(cache=GeocodeCache(cache_dir() / "geocode.json"))


def needs_geocoding(rc: RunConfig) -> bool:
    geo = rc.geography
    if geo.center is None or not geo.admin_area:
        return True
    local_business = rc.lead_type("local_business")
    venue_run = bool(local_business and local_business.is_venue_run)
    return venue_run and any(area.center is None for area in geo.areas)


def resolve_geography(rc: RunConfig, geocoder: Geocoder | None = None) -> RunConfig:
    """A copy of `rc` with missing centre / admin area / radius / area centres filled."""
    if not needs_geocoding(rc):
        return rc
    geocoder = geocoder or default_geocoder()
    geo = rc.geography
    updates: dict = {}

    place = geocoder.geocode(geo.places[0], geo.country)
    local_business = rc.lead_type("local_business")
    venue_run = bool(local_business is not None and local_business.is_venue_run)
    if place is None:
        log.warning("could not geocode %r in %s; running without a map centre",
                    geo.places[0], geo.country)
    else:
        if geo.center is None:
            updates["center"] = place.center
        if not geo.admin_area and place.admin_area:
            updates["admin_area"] = place.admin_area
        if venue_run and geo.radius_km is None and place.radius_km:
            updates["radius_km"] = round(place.radius_km * 1.5, 1)
        log.info("geocoded %s: centre %.4f,%.4f%s", geo.places[0], *place.center,
                 f", {place.admin_area}" if place.admin_area else "")

    if venue_run and geo.areas:
        areas = []
        for area in geo.areas:
            if area.center is None:
                found = geocoder.geocode(f"{area.name}, {geo.places[0]}", geo.country)
                if found is not None:
                    area = area.model_copy(update={"center": found.center})
            areas.append(area)
        updates["areas"] = areas

    if not updates:
        return rc
    return rc.model_copy(update={"geography": geo.model_copy(update=updates)})
