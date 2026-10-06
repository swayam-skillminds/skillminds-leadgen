"""Error-page title detection.

Scrapers that hit a bot wall get a valid HTML page whose <title> is something
like "Access Denied" or "Just a moment...". Parsed as a listing, that title
becomes a business name and then survives dedup (six such rows in the live DB),
so it has to be rejected at the point of writing.

The whole difficulty is precision: "Access Control Systems Pvt Ltd" and
"Gateway Banquets" are real businesses. So matching is done on the *whole*
normalised title -- an exact set plus anchored patterns -- never on a substring
of a generic word.
"""

from __future__ import annotations

import html
import re

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Error titles as they normalise. Whole-string matches only.
ERROR_TITLES = frozenset({
    "access denied",
    "403 forbidden",
    "404 not found",
    "just a moment",
    "attention required",
    "are you a robot",
    "please verify you are human",
    "security check",
    "error",
    "page not found",
    "site not found",
    "bad gateway",
    "service unavailable",
    "too many requests",
    "request blocked",
    "unusual traffic",
    "sign in",
    "log in",
    "untitled",
    "one moment please",
    "under maintenance",
    "blocked",
})

# The parameterised ones. Anchored at both ends wherever the tail can be a real
# business name ("Denied Entry Nightclub", "Moment Photography").
ERROR_TITLE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"^\d{3} (forbidden|not found|bad gateway|service unavailable|too many requests)$",
    r"^just a moment",
    r"^attention required",
    r"^checking your browser",
    r"^access (to .+ has been )?denied$",
    r"^(please )?verify (that )?you are (a )?human$",
    r"^ddos protection",
))


def normalize_title(name: str) -> str:
    """Canonical form used for both match paths: case-, punctuation- and
    whitespace-insensitive, with any " | Site Name" suffix dropped."""
    if not name:
        return ""
    text = html.unescape(str(name))
    # Bot walls brand themselves in the title's tail ("... | Cloudflare"); the
    # claim we're testing is always in the leading segment.
    text = text.split("|", 1)[0]
    text = _PUNCT.sub(" ", text.casefold())
    return _WS.sub(" ", text).strip()


def is_error_title(name: str) -> bool:
    """True when `name` is a scraper/bot-wall error page title, not a business."""
    normalized = normalize_title(name)
    if not normalized:
        return False
    if normalized in ERROR_TITLES:
        return True
    return any(pattern.search(normalized) for pattern in ERROR_TITLE_PATTERNS)
