"""Config loading: YAML file + .env, with dotted-path access and CLI overrides."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ..core import normalize
from ..core.country import CountryInfo, country
from ..core.geo import as_point
from ..paths import PROJECT_ROOT
from .importer import LEGACY_COUNTRY, apply_legacy_defaults, is_legacy

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


_as_point = as_point


@dataclass
class Locality:
    name: str
    aliases: list[str] = field(default_factory=list)
    #: The run's city, appended to the bare name as a last search phrasing.
    city: str = ""
    #: Place names that put a listing in this locality when they appear in its
    #: name, URL or address (e.g. "ghoti", "vaitarna" for Igatpuri).
    keywords: list[str] = field(default_factory=list)
    #: (lat, lng) of the locality's centre, for nearest-locality assignment.
    center: tuple[float, float] | None = None

    @property
    def search_terms(self) -> list[str]:
        """Every string worth querying for this locality, most specific first."""
        terms = list(self.aliases) or [self.name]
        base = f"{self.name} {self.city}"
        if base not in terms and self.aliases and self.city.lower() not in self.name.lower():
            terms.append(base)
        return terms

    @property
    def primary_term(self) -> str:
        return self.search_terms[0]


@dataclass
class Category:
    name: str
    queries: list[str] = field(default_factory=list)
    directory_slugs: dict[str, str] = field(default_factory=dict)
    indiamart: bool = False
    indiamart_queries: list[str] = field(default_factory=list)
    #: When set, only these sources run this category (empty = every source).
    #: SERP-backed sources are slow, so a niche category can stay Maps-only.
    sources: list[str] = field(default_factory=list)
    #: When set, the category only runs in these localities (empty = all).
    only_localities: list[str] = field(default_factory=list)

    def runs_source(self, source: str) -> bool:
        return not self.sources or source in self.sources

    def runs_in(self, locality: "Locality") -> bool:
        if not self.only_localities:
            return True
        return locality.name.lower() in {n.lower() for n in self.only_localities}


class Config:
    """Thin wrapper over the parsed YAML with dotted-path lookups."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self.path = path
        # `areas` is the newer vocabulary for the same list; accept either so a
        # config written in one style loads under the other.
        city = str(data.get("city") or "")
        self.localities = [
            Locality(
                name=item["name"],
                aliases=item.get("aliases", []) or [],
                city=city,
                keywords=[str(k).lower() for k in (item.get("keywords") or [])],
                center=_as_point(item.get("center")),
            )
            for item in (data.get("localities") or data.get("areas") or [])
        ]
        self.categories = [
            Category(
                name=item["name"],
                queries=item.get("queries", []),
                directory_slugs=item.get("directory_slugs", {}) or {},
                indiamart=bool(item.get("indiamart", False)),
                indiamart_queries=item.get("indiamart_queries", []) or [],
                sources=list(item.get("sources") or []),
                only_localities=list(item.get("only_localities") or []),
            )
            for item in data.get("categories", [])
        ]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")
        with config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if is_legacy(data):
            return cls(apply_legacy_defaults(data), config_path)
        from .compile import compile_run_config
        from .schema import RunConfig

        return compile_run_config(RunConfig.model_validate(data), path=config_path)

    def get(self, dotted: str, default: Any = None) -> Any:
        """`cfg.get("sources.google_maps.min_delay", 2.0)`"""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        """Apply a CLI override in place."""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @property
    def country(self) -> str:
        """ISO 3166-1 alpha-2: phone region, currency, locale, and which packs apply.

        A bare dict without one (tests, pre-schema code) gets the legacy
        default; run configs and imported presets always state it.
        """
        return str(self.get("country") or LEGACY_COUNTRY).upper()

    @property
    def country_info(self) -> CountryInfo:
        return country(self.country)

    @property
    def currency(self) -> str:
        return str(self.get("currency") or self.country_info.currency)

    @property
    def admin_area(self) -> str:
        """State / province the run is in ("Maharashtra"), when known."""
        return str(self.get("geo.admin_area") or "")

    @property
    def place_words(self) -> list[str]:
        """Words that only name the run's country or region, never a business."""
        words = [*self.country_info.name_noise, self.admin_area,
                 *(self.get("geo.place_noise") or [])]
        return [str(w) for w in words if w]

    def name_noise(self) -> tuple[list[str], list[str]]:
        """(region words, extra words) that `name_key` ignores for this run."""
        region = [*self.country_info.name_noise, self.admin_area]
        extra = [self.city, *(self.get("geo.place_noise") or []),
                 *(self.get("dedup.extra_name_noise") or [])]
        return [w for w in region if w], [str(w) for w in extra if w]

    def apply_name_noise(self) -> None:
        """Install this run's name noise (process-global, as name_key is)."""
        normalize.configure_name_noise(*self.name_noise())

    @property
    def areas(self) -> list[Locality]:
        # `areas` is the city/areas vocabulary's name for the same list.
        return self.localities

    @property
    def city(self) -> str:
        return str(self.get("city") or "")

    @property
    def profile(self) -> str:
        """Output/scoring profile: "vendors" (the Mumbai run) or "venues"."""
        return str(self.get("profile", "vendors") or "vendors").lower()

    @property
    def is_venue_run(self) -> bool:
        return self.profile == "venues"

    @property
    def verticals(self) -> list[str]:
        """Industry verticals the run targets; they decide which packs apply.

        Configs written before verticals existed get what their profile
        implied: a vendor run is the events trade, and a venue run also used
        the events directories for banquets and lawns.
        """
        explicit = self.get("verticals")
        if explicit is not None:
            return [str(v).lower() for v in explicit]
        return ["hospitality", "events"] if self.is_venue_run else ["events"]

    @property
    def geo_center(self) -> tuple[float, float] | None:
        """(lat, lng) the run is centred on: Maps viewport and browser geolocation."""
        return _as_point(self.get("geo.center"))

    @property
    def region_radius_km(self) -> float | None:
        """Listings farther than this from `geo_center` are out of region."""
        value = self.get("geo.radius_km")
        return float(value) if value else None

    def locality_for(self, text: str = "", lat: float | None = None,
                     lng: float | None = None) -> Locality | None:
        """The locality a listing belongs to, from place names first, then coordinates.

        Keywords win because an address that says "Ghoti, Igatpuri" is exact,
        while nearest-centre is only an approximation for a sprawling district.
        The longest keyword match wins so "trimbak road" (a Nashik street) beats
        "trimbak". The city's own name is weak evidence -- platforms put "in
        Nashik" on every regional slug -- so it only decides when nothing more
        specific matched.
        """
        haystack = f" {(text or '').lower()} "
        weak_words = {self.city.lower(), *(str(k).lower() for k in (self.get("geo.keywords") or []))}
        best: tuple[int, Locality] | None = None
        weak: Locality | None = None
        for locality in self.localities:
            for keyword in locality.keywords:
                if not keyword or not re.search(
                        r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])", haystack):
                    continue
                if keyword in weak_words:
                    weak = weak or locality
                elif best is None or len(keyword) > best[0]:
                    best = (len(keyword), locality)
        if lat is None or lng is None:
            return best[1] if best else weak
        from ..core.geo import haversine_km

        candidates = [(haversine_km(lat, lng, *loc.center), loc)
                      for loc in self.localities if loc.center]
        if not candidates:
            return best[1] if best else weak
        nearest_km, nearest = min(candidates, key=lambda pair: pair[0])
        if best:
            # A pin that plainly contradicts the keyword wins: "Gangapur" in an
            # address 2 km from the city centre is Gangapur ROAD, not the dam.
            keyword_loc = best[1]
            if keyword_loc.center and keyword_loc is not nearest:
                keyword_km = haversine_km(lat, lng, *keyword_loc.center)
                if keyword_km > max(8.0, 2 * nearest_km):
                    return nearest
            return keyword_loc
        return nearest

    def source_enabled(self, source: str) -> bool:
        return bool(self.get(f"sources.{source}.enabled", False))

    def source_cfg(self, source: str) -> dict[str, Any]:
        return self.get(f"sources.{source}", {}) or {}

    def delays(self, source: str) -> tuple[float, float]:
        cfg = self.source_cfg(source)
        return float(cfg.get("min_delay", 2.0)), float(cfg.get("max_delay", 5.0))

    def path_for(self, key: str, default: str) -> Path:
        """Resolve a runtime path from config, relative to the project root."""
        raw = self.get(f"runtime.{key}", default) or default
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # --- selection helpers used by the CLI --------------------------------

    def select_localities(self, names: list[str] | None) -> list[Locality]:
        if not names or "all" in [n.lower() for n in names]:
            return self.localities
        wanted = {n.strip().lower() for n in names}
        chosen = [loc for loc in self.localities if loc.name.lower() in wanted]
        if not chosen:
            known = ", ".join(loc.name for loc in self.localities)
            raise ValueError(f"No matching localities for {names!r}. Known: {known}")
        return chosen

    def select_categories(self, names: list[str] | None) -> list[Category]:
        if not names or "all" in [n.lower() for n in names]:
            return self.categories
        wanted = {n.strip().lower() for n in names}
        chosen = [cat for cat in self.categories if cat.name.lower() in wanted]
        if not chosen:
            known = ", ".join(cat.name for cat in self.categories)
            raise ValueError(f"No matching categories for {names!r}. Known: {known}")
        return chosen

    @property
    def google_credentials_path(self) -> str | None:
        """Service-account JSON location: .env, then the user config dir, then the repo."""
        env_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if env_path:
            # dotenv doesn't expand "~", and google.oauth2 won't either.
            return str(Path(env_path).expanduser())
        # Outside the repo first: a private key living in the project root is
        # one `git add .` away from being published. The repo path stays last
        # only so an existing checkout keeps working.
        candidates = [
            Path.home() / ".config" / "lead-scraper" / "credentials.json",
            PROJECT_ROOT / "credentials.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None
