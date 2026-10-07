"""Job postings, and the ATS boards a page links to.

A ``Posting`` is one open role as a job source publishes it. A ``BoardRef``
names one company's board on a public ATS (``greenhouse:acme``); ``find_boards``
reads them out of a careers page's links and embeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


@dataclass
class Posting:
    title: str
    url: str
    company: str = ""
    #: The company's own site, when the source says it.
    company_url: str | None = None
    location: str = ""
    remote: bool | None = None
    posted_at: datetime | None = None
    department: str = ""
    #: "greenhouse:acme", "hn:49922569", "careers:acme.example".
    board: str = ""
    #: The source name: greenhouse, lever, ashby, workable, smartrecruiters, hn_whos_hiring,
    #: careers_pages.
    source: str = ""
    #: Free text around it (an HN comment), for AI interpretation.
    text: str = ""
    #: Roles named in a posting that lists several (an HN comment).
    roles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BoardRef:
    kind: str
    token: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.token}"

    @classmethod
    def parse(cls, value: str) -> "BoardRef":
        kind, _, token = value.partition(":")
        return cls(kind.strip().lower(), token.strip())


#: (kind, pattern capturing the board token) for links and embeds on a careers page.
BOARD_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple((k, re.compile(p, re.I)) for k, p in (
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?"
                   r"([a-z0-9][a-z0-9_-]*)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9][a-z0-9_-]*)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([a-z0-9][a-z0-9._-]*)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9._%-]*)"),
    ("workable", r"apply\.workable\.com/(?!api/|j/)([a-z0-9][a-z0-9_-]*)"),
    ("workable", r"(?<![\w.-])([a-z0-9][a-z0-9-]*)\.workable\.com"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([a-z0-9][a-z0-9_-]*)"),
))
#: Path segments that look like a token but are pages of the ATS itself.
_NOT_TOKENS = {"embed", "api", "j", "www", "apply", "jobs", "careers", "static", "assets",
               "v1", "v0", "help", "privacy", "terms", "login", "signup", "search"}


def find_boards(urls: Iterable[str]) -> list[BoardRef]:
    """The ATS boards these links and embed URLs point at, first seen first."""
    found: list[BoardRef] = []
    for url in urls:
        for kind, pattern in BOARD_PATTERNS:
            for match in pattern.finditer(url or ""):
                token = match.group(1).strip("._-")
                if token and token.lower() not in _NOT_TOKENS:
                    ref = BoardRef(kind, token)
                    if ref not in found:
                        found.append(ref)
    return found


def parse_time(value) -> datetime | None:
    """ISO strings, dates, or epoch milliseconds/seconds, as an aware UTC datetime."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
