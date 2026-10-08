"""The ``decision_makers`` lead type: who to talk to at organisations any lead type found.

An enrichment lead type (A.7): it finds no organisations itself. For the run's
(or the project's) organisations it reads the company's own team / about /
leadership pages, the country's company registry where a pack has one (UK:
Companies House directors), and -- opt-in -- search-indexed LinkedIn profiles
and public GitHub org members. Each decision-maker is a Person with a name,
title, profile URL and source. The optional, off-by-default email pattern
adds labelled ``Inferred Email (unverified)`` addresses. It scores **reach**.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from ...core.scoring import ScoreParts, contact_reach
from ...sources.people.roles import family_of
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section
from .config import DecisionMakersConfig

log = logging.getLogger(__name__)

KEY = "decision_makers"
SOURCE_TITLES = {"team_pages": "Team page", "registry": "Company registry",
                 "linkedin_search": "LinkedIn (search)", "github": "GitHub"}
#: Reach added for a named decision-maker with a title, and for a way to reach them.
NAMED_BONUS = 0.15
DIRECT_BONUS = 0.05


def _mine(record: Any) -> list[dict[str, Any]]:
    """Its own people on the record, one per name (case-folded), first seen first."""
    out: dict[str, dict[str, Any]] = {}
    for person in getattr(record, "people", None) or []:
        if person.get("lead_type") == KEY and person.get("name"):
            out.setdefault(" ".join(str(person["name"]).split()).casefold(), person)
    return list(out.values())


def _role(person: dict[str, Any]) -> str:
    """"CTO", "Founder"…: the title's first part, short."""
    return str(person.get("title") or "").split(",")[0].strip()[:40]


class DecisionMakersLeadType(LeadType):
    key = KEY
    title = "Decision-makers"
    description = ("Founders, owners, CEOs, CTOs and IT heads of organisations any lead type "
                   "found: from their own team and about pages, company registries (UK "
                   "Companies House), and opt-in search-indexed LinkedIn profiles and GitHub "
                   "org members. An optional, labelled email pattern.")
    config_model = DecisionMakersConfig
    applicable_countries = None
    #: After the finders and hiring's careers pages; before the audit.
    post_order = 7

    def _section(self, cfg: Any) -> DecisionMakersConfig:
        return lead_type_section(cfg, self.key, DecisionMakersConfig)

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        sources = self._section(cfg).sources
        names = [name for name in ("team_pages", "registry", "linkedin_search", "github")
                 if getattr(sources, name).enabled]
        return {"discovery": [], "enrichment": names}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return []

    @staticmethod
    def depth_changes(section: DecisionMakersConfig, depth: str
                      ) -> tuple[dict[str, Any], list[str]]:
        """(field updates, notes) a depth preset makes; "balanced" changes nothing."""
        team = section.sources.team_pages
        if depth == "quick" and (team.max_pages > 1 or section.max_orgs > 50):
            sources = section.sources.model_copy(
                update={"team_pages": team.model_copy(update={"max_pages": 1})})
            return ({"sources": sources, "max_orgs": min(section.max_orgs, 50)},
                    ["decision_makers: one team page each, at most 50 organisations"])
        if depth == "deep" and (team.max_pages < 6 or section.max_orgs < 500):
            sources = section.sources.model_copy(
                update={"team_pages": team.model_copy(update={"max_pages": 6})})
            return ({"sources": sources, "max_orgs": max(section.max_orgs, 500)},
                    ["decision_makers: up to 6 team pages each, up to 500 organisations"])
        return {}, []

    def effective(self, cfg: Any) -> DecisionMakersConfig:
        section = self._section(cfg)
        depth = str(cfg.get("depth") or "balanced") if hasattr(cfg, "get") else "balanced"
        update, _ = self.depth_changes(section, depth)
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return []            # people are facts about an organisation, not reasons to buy

    def catalog_sources(self) -> list[dict[str, Any]]:
        config = DecisionMakersConfig()
        return [{"name": "team_pages", "kind": "enrichment", "scope": "run",
                 "defaults": config.sources.team_pages.model_dump()},
                {"name": "companies_house", "kind": "enrichment", "scope": "run",
                 "pack": "uk", "applicable_countries": ["GB"],
                 "defaults": config.sources.registry.model_dump()},
                {"name": "linkedin_search", "kind": "enrichment", "scope": "run",
                 "defaults": config.sources.linkedin_search.model_dump()},
                {"name": "github", "kind": "enrichment", "scope": "run",
                 "defaults": config.sources.github.model_dump()},
                {"name": "email_pattern", "kind": "enrichment", "scope": "run",
                 "defaults": config.email_pattern.model_dump()}]

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("roles", "DM Roles", width=22, field="decision_makers",
                       description="The decision-makers' roles: founder, CEO, CTO, IT head…"),
            ColumnSpec("count", "People Found", "number", width=8, number_format="0",
                       field="decision_makers",
                       description="Named decision-makers found for it."),
            ColumnSpec("sources", "People Sources", placement="tail", width=24,
                       field="decision_makers",
                       description="Where they were read: its own team pages, a company "
                                   "registry, LinkedIn search results, GitHub."),
            ColumnSpec("email_pattern", "Email Pattern", placement="tail", width=30,
                       field="emails",
                       description="The address pattern its published addresses reveal (the "
                                   "basis of Inferred Email); MX-checked only, never "
                                   "verified."),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        people = _mine(org)
        out: dict[str, Any] = {}
        if people:
            roles = list(dict.fromkeys(_role(p) for p in people if _role(p)))
            sources = Counter(SOURCE_TITLES.get(p.get("source") or "", p.get("source") or "")
                              for p in people)
            out.update(roles=", ".join(roles[:6]) or None, count=len(people),
                       sources=", ".join(k for k, _ in sources.most_common() if k) or None)
        item = (getattr(org, "attributes", None) or {}).get("email_pattern") or {}
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, dict) and value.get("pattern") and value.get("mx"):
            examples = len(value.get("examples") or [])
            plural = "es" if examples != 1 else ""
            out["email_pattern"] = (f"{value['pattern']}@{value.get('domain')} "
                                    f"(from {examples} published address{plural})")
        return out

    def score(self, org: Any, signals: list, cfg: Any, record: Any = None) -> ScoreParts:
        """Reach: the published contact channels, plus a named decision-maker with a title,
        plus a way to reach them (a profile or an address published for them)."""
        people = [p for p in _mine(record) if family_of(p.get("title") or "")] \
            if record is not None else []
        if not people:
            return ScoreParts(self.key)            # nothing to add: core reach applies
        lead = getattr(record, "lead", org)
        reach = contact_reach(lead) + NAMED_BONUS
        if any(p.get("profile_url") or p.get("email") for p in people):
            reach += DIRECT_BONUS
        top = people[0]
        title = f"{top['name']} ({top.get('title')})" if top.get("title") else top["name"]
        more = f" and {len(people) - 1} more" if len(people) > 1 else ""
        return ScoreParts(self.key, reach=round(min(1.0, reach), 4),
                          reasons=[f"decision-maker named: {title}{more}"])

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import DecisionMakersSession

        return DecisionMakersSession(context, self.effective(context.config))

    def run(self, context: Any) -> LeadTypeResult:
        """Without the engine: the enrichment in one go. Never raises."""
        try:
            session = self.open(context)
        except Exception as exc:
            return LeadTypeResult(lead_type=self.key, failed=True, error=repr(exc))
        try:
            session.post_discovery(getattr(context, "control", None))
            return session.result()
        except Exception as exc:
            log.exception("lead type %s failed", self.key)
            return LeadTypeResult(lead_type=self.key, failed=True, error=repr(exc))
        finally:
            session.close()


DECISION_MAKERS = DecisionMakersLeadType()
