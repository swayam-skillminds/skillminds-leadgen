"""Legitimacy / quality scoring.

Four independent sub-scores, each normalised to 0..1, combined with
configurable weights and reported as 0..100:

  1. maps_popularity      Google rating weighted by review volume
  2. source_corroboration how many independent sources listed the business
  3. directory_signal     WedMeGood rating / reviews / Featured status
  4. contact_completeness how reachable the lead actually is

A venue run (`profile: venues`) adds two more, and its config weights them:

  5. premium_signal       how upscale the VENUE is -- price first, then
                          platform, pool, size (see venues.compute_tier)
  6. reputation           best rating x total reviews across Maps, the
                          directories and every booking platform

A component with no weight in config contributes nothing, so the vendor run's
scores are unchanged by the venue components existing.

Nothing is ever discarded for scoring low -- a one-source listing with no
reviews still lands in the sheet, just near the bottom of its group.

Scoring v2 (``core/scoring.py``): this weighted total, /100, is the
``local_business`` **fit** contribution, contact completeness is its **reach**,
and it emits no intent. The run's fit/intent/reach weights then make the final
score; legacy configs weight fit only, so their rankings are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ...core.lead import Lead
from ...core.scoring import LEGACY_WEIGHTS, OverallScore, ScoreParts, combine
from ...packs.hospitality.venues import VenueSettings, annotate, best_rating

DEFAULT_WEIGHTS = {
    "maps_popularity": 0.40,
    "source_corroboration": 0.25,
    "directory_signal": 0.20,
    "contact_completeness": 0.15,
}

DEFAULT_COMPLETENESS_FIELDS = {
    "phones": 0.30,
    "emails": 0.25,
    "website": 0.15,
    "address": 0.10,
    "instagram": 0.08,
    "whatsapp": 0.05,
    "facebook": 0.04,
    "linkedin": 0.03,
}


@dataclass
class ScoreBreakdown:
    """Per-component detail, useful for explaining a ranking."""

    maps_popularity: float = 0.0
    source_corroboration: float = 0.0
    directory_signal: float = 0.0
    contact_completeness: float = 0.0
    premium_signal: float = 0.0
    reputation: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "maps_popularity": round(self.maps_popularity, 4),
            "source_corroboration": round(self.source_corroboration, 4),
            "directory_signal": round(self.directory_signal, 4),
            "contact_completeness": round(self.contact_completeness, 4),
            "premium_signal": round(self.premium_signal, 4),
            "reputation": round(self.reputation, 4),
            "total": round(self.total, 2),
        }


class Scorer:
    def __init__(self, config=None):
        get = config.get if config else (lambda _key, default=None: default)
        weights = get("scoring.weights", None) or DEFAULT_WEIGHTS
        total_weight = sum(float(v) for v in weights.values()) or 1.0
        # Normalise so weights can be expressed in any scale.
        self.weights = {k: float(v) / total_weight for k, v in weights.items()}
        self.reviews_saturation = float(get("scoring.reviews_saturation", 400) or 400)
        self.sources_saturation = float(get("scoring.sources_saturation", 4) or 4)
        self.featured_bonus = float(get("scoring.wedmegood_featured_bonus", 0.25) or 0.25)
        self.completeness_fields = (
            get("scoring.completeness_fields", None) or DEFAULT_COMPLETENESS_FIELDS
        )
        self.venue_run = str(get("profile", "vendors") or "vendors").lower() == "venues"
        self.venue_settings = VenueSettings.from_config(config) if self.venue_run else None
        #: Scoring v2 fit/intent/reach weights; a config without them is a
        #: legacy one, whose score is the legitimacy/venue score alone.
        self.score_weights = get("score_weights", None) or LEGACY_WEIGHTS

    # --- components -------------------------------------------------------

    def _volume(self, reviews: int | None, saturation: float) -> float:
        """Log-scaled review volume in 0..1.

        Log scaling matters: the gap between 5 and 50 reviews is far more
        meaningful than between 500 and 550.
        """
        if not reviews or reviews <= 0:
            return 0.0
        return min(1.0, math.log10(1 + reviews) / math.log10(1 + saturation))

    def maps_popularity(self, lead: Lead) -> float:
        """Rating x volume. An unrated listing scores 0, not 0.5."""
        if lead.gmaps_rating is None and lead.gmaps_reviews is None:
            return 0.0
        volume = self._volume(lead.gmaps_reviews, self.reviews_saturation)
        if lead.gmaps_rating is None:
            # Reviews but no rating captured: credit the volume at half weight.
            return 0.5 * volume
        # Map 1..5 stars onto 0..1 so a 1-star listing isn't rewarded.
        quality = max(0.0, (lead.gmaps_rating - 1.0) / 4.0)
        # Quality only counts once there's enough volume to trust it.
        return quality * (0.3 + 0.7 * volume)

    def source_corroboration(self, lead: Lead) -> float:
        """More independent sources = more confidence the business is real."""
        distinct = len(set(lead.sources))
        if distinct <= 0:
            return 0.0
        return min(1.0, math.log10(1 + distinct) / math.log10(1 + self.sources_saturation))

    def directory_signal(self, lead: Lead) -> float:
        """WedMeGood rating/volume plus a Featured bonus. 0 when not listed."""
        if lead.wmg_rating is None and lead.wmg_reviews is None and not lead.wmg_featured:
            return 0.0
        volume = self._volume(lead.wmg_reviews, self.reviews_saturation / 4)
        if lead.wmg_rating is not None:
            quality = max(0.0, (lead.wmg_rating - 1.0) / 4.0)
            base = quality * (0.4 + 0.6 * volume)
        else:
            base = 0.5 * volume
        if lead.wmg_featured:
            base += self.featured_bonus
        return min(1.0, base)

    def contact_completeness(self, lead: Lead) -> float:
        """Weighted fraction of contact channels we actually captured."""
        score = 0.0
        possible = 0.0
        for field_name, weight in self.completeness_fields.items():
            weight = float(weight)
            possible += weight
            value = getattr(lead, field_name, None)
            if isinstance(value, (list, tuple)):
                if value:
                    score += weight
            elif value:
                score += weight
        return score / possible if possible else 0.0

    def premium_signal(self, lead: Lead) -> float:
        """How upscale the venue is, 0..1. Tier points saturate at config's cap.

        Reads `tier_points`, so it is only meaningful after `venues.annotate`
        -- which `apply` guarantees on a venue run.
        """
        saturation = self.venue_settings.points_saturation if self.venue_settings else 12.0
        return min(1.0, max(0, lead.tier_points) / saturation)

    def reputation(self, lead: Lead) -> float:
        """Best rating backed by review volume, pooled across every source."""
        rating, reviews = best_rating(lead)
        if rating is None:
            return 0.0
        quality = max(0.0, (rating - 1.0) / 4.0)
        volume = self._volume(reviews, self.reviews_saturation)
        return quality * (0.3 + 0.7 * volume)

    # --- combination ------------------------------------------------------

    def breakdown(self, lead: Lead) -> ScoreBreakdown:
        parts = ScoreBreakdown(
            maps_popularity=self.maps_popularity(lead),
            source_corroboration=self.source_corroboration(lead),
            directory_signal=self.directory_signal(lead),
            contact_completeness=self.contact_completeness(lead),
            premium_signal=self.premium_signal(lead),
            reputation=self.reputation(lead),
        )
        parts.total = 100.0 * sum(
            self.weights.get(component, 0) * getattr(parts, component)
            for component in ("maps_popularity", "source_corroboration", "directory_signal",
                              "contact_completeness", "premium_signal", "reputation")
        )
        return parts

    def score(self, lead: Lead) -> float:
        return self.explain(lead).total

    def apply(self, lead: Lead) -> Lead:
        if self.venue_settings is not None:
            annotate(lead, self.venue_settings)
        lead.score = self.score(lead)
        return lead

    # --- scoring v2 ---------------------------------------------------------

    def parts(self, lead: Lead, breakdown: ScoreBreakdown | None = None) -> ScoreParts:
        """This lead type's fit / reach contribution, with reasons."""
        breakdown = breakdown or self.breakdown(lead)
        return ScoreParts(
            lead_type="local_business",
            fit=round(breakdown.total, 2) / 100.0,
            reach=breakdown.contact_completeness,
            reasons=self.reasons(lead, breakdown),
        )

    def explain(self, lead: Lead) -> OverallScore:
        """The combined score for a lead found by this lead type alone."""
        return combine([self.parts(lead)], self.score_weights)

    def reasons(self, lead: Lead, parts: ScoreBreakdown) -> list[str]:
        """Plain-language reasons, strongest contribution first."""
        weighted: list[tuple[float, str]] = []

        def add(component: str, text: str) -> None:
            if text:
                weighted.append((self.weights.get(component, 0) * getattr(parts, component), text))

        if lead.gmaps_rating is not None:
            reviews = f" from {lead.gmaps_reviews:,} reviews" if lead.gmaps_reviews else ""
            add("maps_popularity", f"Google rating {lead.gmaps_rating:.1f}{reviews}")
        sources = sorted(set(lead.sources))
        if len(sources) > 1:
            add("source_corroboration", f"listed by {len(sources)} sources ({', '.join(sources)})")
        elif sources:
            add("source_corroboration", f"found on {sources[0]} only")
        if lead.wmg_rating is not None or lead.wmg_featured:
            bits = []
            if lead.wmg_rating is not None:
                bits.append(f"{lead.wmg_rating:.1f}"
                            + (f" from {lead.wmg_reviews:,} reviews" if lead.wmg_reviews else ""))
            if lead.wmg_featured:
                bits.append("featured")
            add("directory_signal", "WedMeGood " + ", ".join(bits))
        if self.venue_run:
            if lead.venue_tier:
                detail = "; ".join(lead.tier_reasons[:3])
                add("premium_signal", f"{lead.venue_tier} venue" + (f" ({detail})" if detail else ""))
            rating, reviews = best_rating(lead)
            if rating is not None:
                add("reputation", f"best rating {rating:.1f} over {reviews:,} reviews")
        channels = [label for label, present in (
            ("phone", lead.phones), ("email", lead.emails), ("website", lead.website),
            ("Instagram", lead.instagram), ("WhatsApp", lead.whatsapp)) if present]
        add("contact_completeness",
            f"reachable by {', '.join(channels)}" if channels else "no phone, email or website yet")
        weighted.sort(key=lambda pair: -pair[0])
        return [text for _, text in weighted]
