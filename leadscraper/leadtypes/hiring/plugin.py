"""The ``hiring`` lead type: companies hiring tech, IT and digital roles.

Sources: public ATS job-board APIs (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters), the careers pages of organisations other lead types found,
Hacker News "Who is hiring?", and an opt-in web search for ATS boards. No job
board whose terms forbid it: Upwork, Indeed, Glassdoor and LinkedIn Jobs are
never used (A.2.4), and no country job board qualified yet (docs/sources.md).

Each matching open role is a ``job_posting`` signal (role, posted date, URL);
the more open roles an organisation has, the stronger. It scores **intent**.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ...core.scoring import ScoreParts
from ...sources.jobs.ats import HOSTS
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section
from .config import HiringConfig
from .roles import FAMILY_TITLES
from .session import SIGNAL, SPEC, plan_units

log = logging.getLogger(__name__)

#: A role counts as open when it was seen within this many days.
OPEN_DAYS = 45


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _when(signal: dict[str, Any]) -> datetime | None:
    return _aware(signal.get("published_at")) or _aware(signal.get("observed_at"))


def open_postings(signals: list[dict[str, Any]], now: datetime | None = None) -> list[dict]:
    """Job postings seen recently enough to still be open, newest first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=OPEN_DAYS)
    rows = [s for s in signals if s.get("type") == SIGNAL
            and (_aware(s.get("observed_at")) or now) >= cutoff]
    return sorted(rows, key=lambda s: _when(s) or now, reverse=True)


def _role_count(rows: list[dict]) -> int:
    """Open roles: one per posting, or the roles an ad lists (an HN ad names several)."""
    return sum(max(1, len((s.get("payload") or {}).get("roles") or [])) for s in rows)


def _families(rows: list[dict]) -> str | None:
    counts = Counter((s.get("payload") or {}).get("family_title")
                     or FAMILY_TITLES.get((s.get("payload") or {}).get("family"), "Other")
                     for s in rows)
    return ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in counts.most_common()) or None


class HiringLeadType(LeadType):
    key = "hiring"
    title = "Hiring"
    description = ("Companies hiring tech, IT and digital roles, from public ATS job boards "
                   "(Greenhouse, Lever, Ashby, Workable, SmartRecruiters), careers pages and "
                   "Hacker News 'Who is hiring?'. Several open roles = a stronger signal.")
    config_model = HiringConfig
    applicable_countries = None
    #: Its careers-page scan reads other lead types' organisations after they are enriched.
    post_order = 5

    def _section(self, cfg: Any) -> HiringConfig:
        return lead_type_section(cfg, self.key, HiringConfig)

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        section = self._section(cfg)
        found = {u.source for u in plan_units(cfg, section)} - {"ats"}
        if section.sources.ats.enabled:
            found |= {b.split(":", 1)[0] for b in section.boards
                      if b.split(":", 1)[0] in section.sources.ats.kinds}
        return {"discovery": sorted(found),
                "enrichment": ["careers_pages"] if section.sources.careers_pages.enabled else []}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return plan_units(cfg, self._section(cfg))

    @staticmethod
    def depth_changes(section: HiringConfig, depth: str) -> tuple[dict[str, Any], list[str]]:
        """(field updates, notes) a depth preset makes; "balanced" changes nothing."""
        sources = section.sources
        if depth == "quick" and sources.careers_pages.max_orgs > 50:
            careers = sources.careers_pages.model_copy(update={"max_orgs": 50})
            return ({"sources": sources.model_copy(update={"careers_pages": careers})},
                    ["hiring: careers pages of at most 50 organisations"])
        # (Not more HN threads: with posted_within_days at 30, an older thread's ads
        # are filtered out anyway.)
        if depth == "deep" and sources.careers_pages.max_orgs < 500:
            careers = sources.careers_pages.model_copy(update={"max_orgs": 500})
            return ({"sources": sources.model_copy(update={"careers_pages": careers})},
                    ["hiring: careers pages of up to 500 organisations"])
        return {}, []

    def effective(self, cfg: Any) -> HiringConfig:
        """The section with the run's depth preset applied (a copy: the config is untouched)."""
        section = self._section(cfg)
        depth = str(cfg.get("depth") or "balanced") if hasattr(cfg, "get") else "balanced"
        update, _ = self.depth_changes(section, depth)
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return [SPEC]

    def catalog_sources(self) -> list[dict[str, Any]]:
        """Its sources for the catalog; terms notes are in ``sources/notes.py``."""
        defaults = HiringConfig().sources
        out = [{"name": kind, "kind": "discovery", "scope": "run",
                "defaults": defaults.ats.model_dump()} for kind in HOSTS]
        out.append({"name": "hn_whos_hiring", "kind": "discovery", "scope": "run",
                    "defaults": defaults.hn_whos_hiring.model_dump()})
        out.append({"name": "careers_pages", "kind": "enrichment", "scope": "run",
                    "defaults": defaults.careers_pages.model_dump()})
        out.append({"name": "board_search", "kind": "discovery", "scope": "locality",
                    "defaults": defaults.board_search.model_dump()})
        return out

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("open_roles", "Open Tech Roles", "number", width=9, number_format="0",
                       description=f"Matching tech, IT or digital roles seen open in the last "
                                   f"{OPEN_DAYS} days (an HN ad counts each role it names)."),
            ColumnSpec("families", "Role Families", width=26,
                       description="Which kinds of roles: Developer, QA, DevOps, Data, IT "
                                   "support, Digital marketing…"),
            ColumnSpec("latest_posting", "Latest Posting", "date", width=12,
                       number_format="dd-mmm-yy",
                       description="When the newest matching role was posted (or first seen, "
                                   "for careers pages without dates)."),
            ColumnSpec("jobs_page", "Jobs Page", "url", width=14, link_label="Jobs",
                       description="Its job board or careers page."),
            ColumnSpec("roles", "Open Roles", placement="tail", width=50,
                       description="The matching job titles, newest first."),
            ColumnSpec("hiring_sources", "Hiring Sources", placement="tail", width=18),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        rows = open_postings([s for s in getattr(org, "signals", []) or []
                              if s.get("lead_type") == self.key], getattr(org, "as_of", None))
        if not rows:
            return {}
        latest = max((_when(s) for s in rows if _when(s)), default=None)
        titles = list(dict.fromkeys(s.get("title") or "" for s in rows if s.get("title")))
        page = next(((s.get("payload") or {}).get("board_page") for s in rows
                     if (s.get("payload") or {}).get("board_page")), None)
        sources = sorted({(s.get("payload") or {}).get("source") or "" for s in rows} - {""})
        more = f"; +{len(titles) - 12} more" if len(titles) > 12 else ""
        return {"open_roles": _role_count(rows), "families": _families(rows),
                "latest_posting": latest.replace(tzinfo=None) if latest else None,
                "jobs_page": page or rows[0].get("source_url"),
                "roles": "; ".join(titles[:12]) + more if titles else None,
                "hiring_sources": ", ".join(sources) or None}

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Intent: more open roles, more intent; fading as the newest posting ages."""
        now = now or datetime.now(timezone.utc)
        rows = open_postings(signals, now)
        if not rows:
            return ScoreParts(self.key, intent=0.0)
        count = _role_count(rows)
        newest = max((_when(s) for s in rows if _when(s)), default=None)
        fade = 1.0
        if newest is not None:
            age = max(0.0, (now - newest).total_seconds() / 86400)
            fade = math.pow(0.5, age / float(SPEC.half_life_days or 30))
        intent = round(min(1.0, 0.4 + 0.15 * (count - 1)) * fade, 4)
        when = f", latest posted {newest:%Y-%m-%d}" if newest else ""
        plural = "s" if count != 1 else ""
        return ScoreParts(self.key, intent=intent,
                          reasons=[f"hiring {count} tech role{plural} ({_families(rows)}){when}"])

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import HiringSession

        return HiringSession(context, self.effective(context.config))

    def run(self, context: Any) -> LeadTypeResult:
        """Without the engine: every unit, then the careers pages, in one go. Never raises."""
        try:
            session = self.open(context)
        except Exception as exc:
            return LeadTypeResult(lead_type=self.key, failed=True, error=repr(exc))
        try:
            for unit in session.units():
                session.run_unit(unit)
            session.post_discovery(getattr(context, "control", None))
            return session.result()
        except Exception as exc:
            log.exception("lead type %s failed", self.key)
            return LeadTypeResult(lead_type=self.key, failed=True, error=repr(exc))
        finally:
            session.close()


HIRING = HiringLeadType()
