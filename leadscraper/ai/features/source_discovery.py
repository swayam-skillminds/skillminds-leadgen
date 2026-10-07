"""``source_discovery``: propose country- or industry-specific directories and feeds.

The model (with web search when a provider has it) names business directories,
association member lists and feeds for a country, places and categories. Each
lands as a **candidate source** with status ``proposed``; nothing is scraped
until a user approves it (``POST /ai/candidate-sources/{id}/approve``). Approved
candidates are read by the generic AI-assisted extractor (``ai_directory``).

Proposals are filtered before they are stored:

* http(s) URLs only, deduplicated per workspace;
* never the platforms A.2.4 excludes (Upwork, Indeed, Glassdoor, LinkedIn) nor
  directories the tool already has a source for;
* with web search on, the URL's site must be among the pages the search
  actually returned -- an invented URL is dropped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

#: Never proposed (A.2.4): terms forbid scraping or a public API exists.
EXCLUDED_HOSTS = ("upwork.com", "indeed.", "glassdoor.", "linkedin.com")
#: Directories that already have a source of their own.
KNOWN_HOSTS = ("google.", "justdial.com", "sulekha.com", "indiamart.com", "wedmegood.com",
               "weddingwire.in", "stayvista.com", "saffronstays.com", "elivaas.com",
               "airbnb.", "booking.com", "facebook.com", "instagram.com")

SYSTEM = (
    "You find public business directories, chamber or association member lists and "
    "listing feeds that list businesses of the given categories in the given country and "
    "places. Only propose sites that publicly list many businesses with contact details "
    "and are open to visitors without logging in. Give the URL of a listing page (or "
    "feed), not a home page, when you know it. Never propose job boards, LinkedIn, social "
    "networks, or sites that require login. Say in terms_note anything you know about the "
    "site's terms of use. Return ONLY JSON matching the schema."
)


class Candidate(BaseModel):
    name: str
    url: str = Field(..., description="A listing page or feed URL.")
    kind: str = Field("directory", description="directory | feed")
    place: str | None = None
    category: str | None = None
    rationale: str = Field("", description="Why it lists the businesses wanted.")
    terms_note: str = ""


class Candidates(BaseModel):
    items: list[Candidate] = Field(default_factory=list)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def acceptable(url: str) -> str | None:
    """Why a proposed URL is refused, or None."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "not an http(s) URL"
    host = _host(url)
    if any(bad in host for bad in EXCLUDED_HOSTS):
        return "excluded by policy (A.2.4)"
    if any(known in host for known in KNOWN_HOSTS):
        return "already has a dedicated source"
    return None


def _in_sources(url: str, sources: list[str]) -> bool:
    host = _host(url)
    for source in sources:
        other = _host(source) if "://" in source else source.lower().removeprefix("www.")
        if other and (host == other or host.endswith("." + other) or other.endswith("." + host)):
            return True
    return False


def discover(client, *, country: str, categories: list[str], places: list[str] | None = None,
             industry: str | None = None, limit: int = 10) -> dict[str, Any]:
    """``{candidates: [...], rejected: [...], searched: bool, provider}``; nothing stored."""
    if client is None or not client.uses("source_discovery"):
        return {"candidates": [], "rejected": [], "searched": False, "provider": None,
                "note": "AI unavailable or source_discovery not enabled"}
    search = client.can_search()
    user = (f"Country: {country}\nPlaces: {', '.join(places or []) or 'whole country'}\n"
            f"Categories: {', '.join(categories) or industry or 'any'}\n"
            + (f"Industry: {industry}\n" if industry else "")
            + f"Propose at most {limit}.")
    result = client.call("source_discovery", system=SYSTEM, user=user, output=Candidates,
                         web_search=search, max_searches=4, max_tokens=4000)
    if result is None:
        return {"candidates": [], "rejected": [], "searched": search, "provider": None,
                "note": "no AI answer"}
    kept, rejected, seen = [], [], set()
    for item in result.data.items[:limit * 2]:
        why = acceptable(item.url)
        if why is None and search and not _in_sources(item.url, result.sources):
            why = "not among the pages the web search returned"
        if why is None and item.url in seen:
            why = "duplicate"
        if why is not None:
            rejected.append({"name": item.name, "url": item.url, "reason": why})
            continue
        seen.add(item.url)
        kept.append({**item.model_dump(), "host": _host(item.url),
                     "kind": item.kind if item.kind in ("directory", "feed") else "directory",
                     "verified_by_search": search})
    return {"candidates": kept[:limit], "rejected": rejected, "searched": search,
            "provider": f"{result.provider}/{result.model}"}
