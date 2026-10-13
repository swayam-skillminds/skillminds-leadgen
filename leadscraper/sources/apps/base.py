"""One app listing, as either store describes it, and store links found on a page."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

APP_STORE = "app_store"
GOOGLE_PLAY = "google_play"
STORE_TITLES = {APP_STORE: "App Store", GOOGLE_PLAY: "Google Play"}

_APPLE_ID = re.compile(r"/id(\d{5,12})(?:[/?#]|$)")
_PACKAGE = re.compile(r"^[A-Za-z][\w]*(?:\.[A-Za-z0-9_]+)+$")


@dataclass
class Review:
    """A review a store page shows: its stars, date and text (never its author)."""

    rating: int | None
    text: str
    date: date | None = None


@dataclass
class AppListing:
    store: str                      # app_store | google_play
    app_id: str                     # Apple's track id; Play's package name
    name: str
    url: str
    developer: str = ""
    developer_website: str | None = None
    #: Contacts the listing publishes for the developer (Play's "App support").
    developer_email: str | None = None
    developer_phone: str | None = None
    developer_address: str = ""
    bundle_id: str = ""
    rating: float | None = None
    ratings: int | None = None
    #: Play's download band as shown ("10K+") and its lower bound.
    installs: str = ""
    installs_min: int | None = None
    updated: date | None = None
    released: date | None = None
    version: str = ""
    genre: str = ""
    description: str = ""
    reviews: list[Review] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.store}:{self.app_id}"

    @property
    def store_title(self) -> str:
        return STORE_TITLES.get(self.store, self.store)

    def months_since_update(self, today: date) -> float | None:
        if self.updated is None:
            return None
        return max(0.0, (today - self.updated).days / 30.44)


def iso_date(value: str | None) -> date | None:
    """A date from an ISO timestamp ("2026-10-08T03:45:31Z") or date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) \
            .astimezone(timezone.utc).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def apple_id(url: str) -> str | None:
    """The track id in an App Store link (``apps.apple.com/in/app/name/id123456789``)."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not (host == "apps.apple.com" or host == "itunes.apple.com"):
        return None
    match = _APPLE_ID.search(parts.path)
    return match.group(1) if match else None


def play_package(url: str) -> str | None:
    """The package in a Play link (``play.google.com/store/apps/details?id=com.example``)."""
    parts = urlsplit(url or "")
    if (parts.hostname or "").lower() != "play.google.com" or \
            not parts.path.startswith("/store/apps/details"):
        return None
    package = (parse_qs(parts.query).get("id") or [""])[0].strip()
    return package if is_package(package) else None


def is_package(value: str) -> bool:
    """An Android package name (also how many iOS bundle ids look)."""
    return bool(value) and len(value) <= 150 and bool(_PACKAGE.match(value))


def store_links(urls) -> list[tuple[str, str]]:
    """(store, app id) for each App Store or Play link among ``urls``, first seen first."""
    out: list[tuple[str, str]] = []
    for url in urls:
        found = None
        apple = apple_id(url)
        if apple:
            found = (APP_STORE, apple)
        else:
            package = play_package(url)
            if package:
                found = (GOOGLE_PLAY, package)
        if found and found not in out:
            out.append(found)
    return out


def parse_app_ref(value: str) -> tuple[str, str] | None:
    """A configured app: a store link, an Apple id ("id123456789" or digits) or a Play package."""
    value = (value or "").strip()
    if not value:
        return None
    links = store_links([value])
    if links:
        return links[0]
    digits = value[2:] if value.lower().startswith("id") else value
    if digits.isdigit() and 5 <= len(digits) <= 12:
        return APP_STORE, digits
    if is_package(value):
        return GOOGLE_PLAY, value
    return None
