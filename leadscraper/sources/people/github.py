"""Public members of a company's GitHub organisation who say they lead it (opt-in).

The company's own site links its GitHub organisation (``github.com/acme``).
Through the official REST API (``api.github.com``; 60 requests an hour
without a token, 5,000 with one):

* ``GET /orgs/{org}``: it must be an organisation whose ``blog`` is the
  company's own domain (so a link to someone else's org proves nothing);
* ``GET /orgs/{org}/public_members``: members who chose to show the membership;
* ``GET /users/{login}``: a member counts only when their own public profile
  names a decision-maker's title in its bio ("CTO at Acme").

GitHub's Acceptable Use Policies restrict personal information to the
purpose its users published it for and forbid using it for unsolicited
email, so the source is off by default, and the email pattern is never
applied to people found here (verified 2026-10-08).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

from ...core.normalize import domain_of, normalize_url
from ...net.ratelimit import PermanentError
from .roles import family_of, person_name
from .team_pages import Found

HOST = "api.github.com"
API = "https://api.github.com"
_NOT_ORGS = frozenset({"about", "features", "pricing", "enterprise", "login", "join",
                       "marketplace", "sponsors", "topics", "explore", "orgs", "settings",
                       "site", "contact", "security", "customer-stories", "readme", "team",
                       "collections", "trending", "apps", "github", "notifications"})
_BIO_TITLE = re.compile(r"(?P<title>(?:co-?\s?founder|founder|ceo|cto|cio|coo|managing "
                        r"director|chief [a-z ]+ officer|vp,? [a-z ]+|head of [a-z ]+|"
                        r"director of [a-z ]+)(?:\s*(?:&|and|/)\s*"
                        r"(?:ceo|cto|coo|co-?founder))?)", re.I)


def org_logins(urls: list[str]) -> list[str]:
    """GitHub organisation names a page links (``github.com/acme``)."""
    out = []
    for url in urls:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
            continue
        segment = parts.path.strip("/").split("/", 1)[0]
        if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", segment or "") and \
                segment.lower() not in _NOT_ORGS:
            out.append(segment)
    return list(dict.fromkeys(out))


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def find(http, login: str, *, domain: str | None, token: str | None = None,
         max_members: int = 30) -> tuple[list[Found], str | None]:
    """(decision-makers among the org's public members, why not) for one GitHub org.

    The reason is ``github_not_an_org`` (a user or nothing), ``github_not_theirs``
    (the org's site is another domain) or None. Other HTTP errors are raised.
    """
    headers = _headers(token)
    try:
        org: Any = http.get(f"{API}/orgs/{quote(login)}", rate_key=HOST, headers=headers).json()
    except PermanentError:
        return [], "github_not_an_org"
    blog = normalize_url(str(org.get("blog") or ""))
    if not domain or not blog or domain_of(blog) != domain:
        return [], "github_not_theirs"
    members = http.get(f"{API}/orgs/{quote(login)}/public_members?per_page="
                       f"{max(1, min(100, max_members))}", rate_key=HOST,
                       headers=headers).json() or []
    out: list[Found] = []
    for member in list(members)[:max_members]:
        user = member.get("login") if isinstance(member, dict) else None
        if not user:
            continue
        profile: Any = http.get(f"{API}/users/{quote(str(user))}", rate_key=HOST,
                                headers=headers).json()
        name = person_name(str(profile.get("name") or ""))
        match = _BIO_TITLE.search(str(profile.get("bio") or ""))
        if not name or match is None or family_of(match.group("title")) is None:
            continue
        out.append(Found(name, match.group("title").strip(), f"https://github.com/{user}",
                         "github", profile_url=str(profile.get("html_url") or "") or None))
    return out, None
