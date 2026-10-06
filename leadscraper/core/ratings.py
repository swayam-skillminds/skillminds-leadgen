"""Ratings pooled across every source that rates a business."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .geo import as_float, as_int

if TYPE_CHECKING:  # pragma: no cover
    from .lead import Lead


def all_ratings(lead: "Lead") -> list[tuple[str, float, int]]:
    """Every (source, rating on a 5-point scale, review count) we hold."""
    out: list[tuple[str, float, int]] = []
    if lead.gmaps_rating is not None:
        out.append(("google_maps", float(lead.gmaps_rating), int(lead.gmaps_reviews or 0)))
    if lead.wmg_rating is not None:
        out.append(("wedmegood", float(lead.wmg_rating), int(lead.wmg_reviews or 0)))
    for platform, value in (lead.platform_ratings or {}).items():
        if not value:
            continue
        rating = as_float(value[0]) if isinstance(value, (list, tuple)) else as_float(value)
        reviews = as_int(value[1]) if isinstance(value, (list, tuple)) and len(value) > 1 else 0
        if rating is not None and 0 < rating <= 5:
            out.append((platform, rating, reviews or 0))
    return out


def best_rating(lead: "Lead") -> tuple[float | None, int]:
    """The rating backed by the most reviews, and the total review count."""
    ratings = all_ratings(lead)
    if not ratings:
        return None, 0
    total = sum(count for _, _, count in ratings)
    _, rating, _ = max(ratings, key=lambda r: (r[2], r[1]))
    return rating, total
