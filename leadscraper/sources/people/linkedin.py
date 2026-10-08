"""Search-indexed LinkedIn profiles of a company's decision-makers (opt-in).

LinkedIn is never logged into or crawled (A.2.4): a web search for
``site:linkedin.com/in "<company>" (founder OR CTO OR …)`` returns profile URLs
the search engine already indexed, and each result's title says who the person
is ("Priya Shah - CTO - Brightline Foods | LinkedIn"). A result counts only
when its title names the company and a decision-maker's title. Google's terms
restrict automated queries, so this is off unless a config enables it.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from ...core.normalize import clean_text, extract_linkedin
from .roles import family_of, person_name, same_company
from .team_pages import Found

#: "Priya Shah - CTO - Brightline Foods | LinkedIn" (also "–" and "|" separators).
_TITLE = re.compile(r"^(?P<person>[^\-–|]{2,60}?)\s*[\-–|]\s*(?P<role>[^\-–|]{2,100}?)"
                    r"(?:\s*[\-–|]\s*(?P<company>[^|]{2,100}?))?\s*(?:[|\-–]\s*LinkedIn)?\s*$",
                    re.I)
SEARCH_TITLES = ("founder", "owner", "CEO", "managing director", "CTO", "CIO",
                 "head of IT", "director")


def query_for(company: str, place: str | None = None) -> str:
    titles = " OR ".join(f'"{t}"' if " " in t else t for t in SEARCH_TITLES)
    where = f' "{place}"' if place else ""
    return f'site:linkedin.com/in "{company}" ({titles}){where}'


def parse_result(title: str) -> tuple[str, str, str] | None:
    """(person, role, company) from a LinkedIn result title, or None."""
    match = _TITLE.match(clean_text(title or ""))
    if match is None:
        return None
    person = clean_text(match.group("person"))
    role = clean_text(match.group("role") or "")
    company = clean_text(match.group("company") or "")
    if company.casefold() == "linkedin":
        company = ""                       # "… | LinkedIn" is the site, not the employer
    # "Priya Shah - Brightline Foods | LinkedIn": no role, the second part is the company.
    if not company and family_of(role) is None:
        company, role = role, ""
    return person, role, company


def find(ctx: Any, company: str, *, place: str | None = None,
         max_results: int = 10) -> list[Found]:
    """Decision-makers of ``company`` whose public profiles a search engine indexed.

    ``ctx`` carries ``config``, ``http``, ``limiter``, ``failures`` (as a source's).
    """
    from ..google_search import SerpClient

    serp = SerpClient(SimpleNamespace(config=ctx.config, http=ctx.http, limiter=ctx.limiter,
                                      failures=ctx.failures, browser=None))
    out: list[Found] = []
    for result in serp.search(query_for(company, place), per_page=max_results):
        profile = extract_linkedin(result.url)
        if not profile or "/in/" not in profile:
            continue
        parsed = parse_result(result.title)
        if parsed is None:
            continue
        person, role, named = parsed
        # The title must name this company (or the role must say "at <company>").
        at = re.search(r"\bat\s+(?P<c>.+)$", role, re.I)
        named = named or (at.group("c") if at else "")
        name = person_name(person)
        if not named or same_company(named, company) < 85 or not name or \
                family_of(role) is None:
            continue
        out.append(Found(name, re.sub(r"\s+at\s+.+$", "", role, flags=re.I), result.url,
                         "linkedin_search", profile_url=profile))
    return out
