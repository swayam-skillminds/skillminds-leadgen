"""Geography: distances, pins in Maps URLs, and place-name geocoding.

Geocoding goes through OpenStreetMap's Nominatim, under its usage policy
(https://operations.osmfoundation.org/policies/nominatim/): at most one
request per second, an identifying User-Agent, and every answer cached so a
place is looked up once, not once per run. A failed lookup returns None and
the run carries on without a centre -- geocoding is a convenience, never a
reason to stop.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km."""
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


_MAPS_COORDS = (
    re.compile(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)"),   # place data -- the pin itself
    re.compile(r"@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)"),        # viewport centre -- close enough
)


def coords_from_maps_url(url: str | None) -> tuple[float, float] | None:
    """(lat, lng) embedded in a Google Maps place URL, pin before viewport."""
    if not url:
        return None
    for pattern in _MAPS_COORDS:
        match = pattern.search(url)
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return lat, lng
    return None


def as_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return int(round(number)) if number is not None else None


def as_point(value: Any) -> tuple[float, float] | None:
    """[lat, lng] / {lat, lng} / "lat,lng" -> (lat, lng), else None."""
    if not value:
        return None
    try:
        if isinstance(value, dict):
            return float(value["lat"]), float(value.get("lng", value.get("lon")))
        if isinstance(value, str):
            lat, lng = value.split(",")
            return float(lat), float(lng)
        lat, lng = value
        return float(lat), float(lng)
    except (KeyError, TypeError, ValueError):
        return None


# --- geocoding ---------------------------------------------------------------

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "leadscraper/1.x (Skillminds lead generation; https://www.skillminds.in)"


@dataclass
class GeoPlace:
    """One geocoded place name."""

    query: str
    lat: float
    lng: float
    display_name: str = ""
    #: State / province / emirate the place sits in, when Nominatim says.
    admin_area: str = ""
    country_code: str = ""
    #: (south, north, west, east), as Nominatim reports the place's extent.
    bbox: tuple[float, float, float, float] | None = None

    @property
    def center(self) -> tuple[float, float]:
        return self.lat, self.lng

    @property
    def radius_km(self) -> float | None:
        """Half the extent's diagonal: a sensible default search radius."""
        if not self.bbox:
            return None
        south, north, west, east = self.bbox
        return round(haversine_km(south, west, north, east) / 2, 1)

    @classmethod
    def from_nominatim(cls, query: str, hit: dict[str, Any]) -> "GeoPlace | None":
        lat, lng = as_float(hit.get("lat")), as_float(hit.get("lon"))
        if lat is None or lng is None:
            return None
        address = hit.get("address") or {}
        bbox = hit.get("boundingbox")
        parsed_bbox = None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            values = [as_float(v) for v in bbox]
            if all(v is not None for v in values):
                parsed_bbox = tuple(values)  # type: ignore[assignment]
        return cls(
            query=query, lat=lat, lng=lng,
            display_name=str(hit.get("display_name") or ""),
            admin_area=str(address.get("state") or address.get("region") or ""),
            country_code=str(address.get("country_code") or "").upper(),
            bbox=parsed_bbox,
        )


class GeocodeCache:
    """A small JSON file of past answers, misses included.

    A miss is cached too: asking Nominatim again about a name it could not
    place costs a request and returns the same nothing.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict | None] = {}
        if path is not None and path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("geocode cache %s unreadable (%s); starting empty", path, exc)

    def get(self, key: str) -> tuple[bool, dict | None]:
        with self._lock:
            if key in self._data:
                return True, self._data[key]
        return False, None

    def put(self, key: str, value: dict | None) -> None:
        with self._lock:
            self._data[key] = value
            if self.path is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8")
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("could not write geocode cache %s: %s", self.path, exc)


Fetcher = Callable[[str, dict[str, str], dict[str, str]], Any]


def _requests_fetch(url: str, params: dict[str, str], headers: dict[str, str]) -> Any:
    import requests

    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


@dataclass
class Geocoder:
    """Place name -> GeoPlace via Nominatim, politely and with a cache."""

    cache: GeocodeCache = field(default_factory=lambda: GeocodeCache(None))
    fetch: Fetcher = _requests_fetch
    min_interval: float = 1.1
    user_agent: str = USER_AGENT
    _last_request: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def cache_key(query: str, country: str | None) -> str:
        return f"{(country or '').upper()}|{' '.join(query.lower().split())}"

    def geocode(self, query: str, country: str | None = None) -> GeoPlace | None:
        query = (query or "").strip()
        if not query:
            return None
        key = self.cache_key(query, country)
        hit, cached = self.cache.get(key)
        if hit:
            if cached is None:
                return None
            data = dict(cached)
            if data.get("bbox"):
                data["bbox"] = tuple(data["bbox"])
            return GeoPlace(**data)

        params = {"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "1"}
        if country:
            params["countrycodes"] = country.lower()
        try:
            with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()
                answer = self.fetch(NOMINATIM_SEARCH, params, {"User-Agent": self.user_agent})
        except Exception as exc:
            # Not cached: a network blip should not become a permanent miss.
            log.warning("geocoding %r failed (%s); continuing without it", query, exc)
            return None

        place = None
        if isinstance(answer, list) and answer:
            place = GeoPlace.from_nominatim(query, answer[0])
        self.cache.put(key, asdict(place) if place else None)
        if place is None:
            log.info("geocoding %r found nothing", query)
        return place
