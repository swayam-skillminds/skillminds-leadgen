"""What unhappy reviewers complain about: a fixed set of themes, found by rules.

Only low-star reviews (two stars or fewer by default) are read. A theme counts
when its words appear; each theme keeps a short quote from the review it came
from, as evidence. Reviewers' names are never read. With the
``signal_interpretation`` AI feature, reviews the rules can't place go to the
model (``ai/features/review_themes.py``), which must quote the review verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .base import Review

#: theme -> (label, pattern). Labels are what the workbook shows.
THEMES: dict[str, tuple[str, re.Pattern]] = {
    "crashes": ("crashes / won't open", re.compile(
        r"\b(?:crash(?:es|ed|ing)?|freez(?:e|es|ing)|hangs?|force[sd]? (?:close|stop)|"
        r"(?:won'?t|doesn'?t|does not|not) (?:open|load|launch|start)(?:ing)?|"
        r"stuck (?:on|at)|blank screen|white screen)\b", re.I)),
    "login": ("login / OTP", re.compile(
        r"\b(?:log ?in|sign ?in|otp|one[- ]time password|verification code|password|"
        r"logged out|account (?:locked|blocked))\b", re.I)),
    "slow": ("slow", re.compile(
        r"\b(?:slow(?:ly|ness)?|lag(?:s|gy|ging)?|takes? (?:forever|too long|ages)|"
        r"loading (?:time|forever)|keeps? loading|battery drain)\b", re.I)),
    "payments": ("payments / refunds", re.compile(
        r"\b(?:payments?|refunds?|charged|deducted|money (?:not|never|was)|"
        r"transaction (?:failed|declined)|billing|double charged)\b", re.I)),
    "bugs": ("bugs / errors", re.compile(
        r"\b(?:bugs?|buggy|glitch(?:es|y)?|errors?|not working|stopped working|"
        r"(?:after|since) (?:the )?(?:latest |new |recent )?update)\b", re.I)),
    "usability": ("hard to use", re.compile(
        r"\b(?:confusing|complicated|difficult to (?:use|navigate)|not user[- ]?friendly|"
        r"(?:bad|poor|terrible|worst|outdated) (?:ui|ux|design|interface)|hard to find)\b",
        re.I)),
    "support": ("no support", re.compile(
        r"\b(?:customer (?:care|support|service)|no (?:response|reply)|support team|"
        r"never (?:respond|replied|answer)|no one (?:responds|answers|helps))\b", re.I)),
    "ads": ("too many ads", re.compile(
        r"\b(?:too many ads|full of ads|ads? (?:every|pop)|advertisements?)\b", re.I)),
    "outdated": ("not updated", re.compile(
        r"\b(?:outdated|not (?:been )?updated|no updates?|old version|not compatible|"
        r"incompatible|doesn'?t support (?:android|ios))\b", re.I)),
}
LOW_STARS = 2


@dataclass
class Theme:
    key: str
    label: str
    #: Low-star reviews that name it.
    count: int
    #: A short quote from one of them (evidence).
    quote: str


def _quote(text: str, match: re.Match, width: int = 90) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    snippet = " ".join(text[start:end].split())
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def complaints(reviews: list[Review], *, low_stars: int = LOW_STARS) -> list[Review]:
    """The low-star reviews."""
    return [r for r in reviews if r.rating is not None and r.rating <= low_stars and r.text]


def themes(reviews: list[Review], *, low_stars: int = LOW_STARS) -> list[Theme]:
    """Complaint themes across the low-star reviews, most-named first."""
    found: dict[str, Theme] = {}
    for review in complaints(reviews, low_stars=low_stars):
        for key, (label, pattern) in THEMES.items():
            match = pattern.search(review.text)
            if match is None:
                continue
            theme = found.get(key)
            if theme is None:
                found[key] = Theme(key, label, 1, _quote(review.text, match))
            else:
                theme.count += 1
    order = list(THEMES)
    return sorted(found.values(), key=lambda t: (-t.count, order.index(t.key)))


def unplaced(reviews: list[Review], *, low_stars: int = LOW_STARS) -> list[Review]:
    """Low-star reviews no theme rule matched (for the AI, when it is on)."""
    return [r for r in complaints(reviews, low_stars=low_stars)
            if not any(p.search(r.text) for _, p in THEMES.values())]
