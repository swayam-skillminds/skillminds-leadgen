"""Public LinkedIn profile URLs, found through search only.

We never log into or crawl LinkedIn. This source runs targeted queries of the
form `site:linkedin.com/in "<category>" "<locality>" <city>` and keeps the
public profile URLs the search engine already indexed, plus whatever the
result title tells us about the person and their company.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from ...config import Category, Locality
from ...core.lead import Lead
from ...core.normalize import clean_text, extract_linkedin
from ...sources.base import Source
from ...sources.google_search import SerpClient

log = logging.getLogger(__name__)

# "Rahul Sharma - Event Manager - ABC Events Pvt Ltd | LinkedIn"
_TITLE = re.compile(
    r"^(?P<person>[^\-–|]{2,60}?)\s*[\-–]\s*(?P<role>[^\-–|]{2,80}?)"
    r"(?:\s*[\-–]\s*(?P<company>[^|]{2,80}?))?\s*[\|–\-]?\s*LinkedIn\s*$",
    re.I,
)


class LinkedInSerpSource(Source):
    name = "linkedin_serp"
    priority = 51

    def rate_keys(self) -> list[str]:
        return ["linkedin_serp", "www.google.com", "duckduckgo.com"]

    def discover(self, locality: Locality, category: Category) -> Iterator[Lead]:
        seen: set[str] = set()
        for query_term in category.queries[:2]:
            query = f'site:linkedin.com/in "{query_term}" "{locality.name}" {self.ctx.config.city}'
            try:
                results = SerpClient(self.ctx).search(query, pages=1, per_page=20)
            except Exception as exc:
                self.fail("serp_failed", query=query, locality=locality.name,
                          category=category.name, detail=repr(exc))
                continue

            if not results:
                self.fail("no_results", query=query, locality=locality.name,
                          category=category.name)
                continue

            for result in results:
                profile = extract_linkedin(result.url)
                if not profile or profile in seen:
                    continue
                seen.add(profile)
                lead = self._to_lead(result.title, profile, locality, category)
                if lead:
                    yield lead

    def _to_lead(self, title: str, profile_url: str,
                 locality: Locality, category: Category) -> Lead | None:
        person, role, company = self._parse_title(title)
        # Without a company we'd be creating a lead keyed on a person's name,
        # which dedups badly against business records -- skip and log it.
        business = company or ""
        if not business:
            self.fail("no_company_in_title", url=profile_url, business=person or None,
                      locality=locality.name, category=category.name, detail=title[:200])
            return None

        lead = self.make_lead(
            business_name=business,
            contact_name=person,
            category=category.name,
            locality=locality.name,
            linkedin=profile_url,
            source_urls=[profile_url],
        )
        if role:
            lead.notes.append(f"LinkedIn role: {role}")
        return lead

    @staticmethod
    def _parse_title(title: str) -> tuple[str, str, str]:
        title = clean_text(title)
        match = _TITLE.match(title)
        if not match:
            return "", "", ""
        person = clean_text(match.group("person"))
        role = clean_text(match.group("role") or "")
        company = clean_text(match.group("company") or "")
        # A "role" that contains a company suffix is really the company.
        if not company and re.search(r"\b(pvt|ltd|llp|events?|caterers?|studio|decor)\b", role, re.I):
            company, role = role, ""
        return person, role, company
