"""The ``app_store`` lead type: apps with poor ratings, long-stale updates or reviewers
complaining of fixable problems, resolved to their developers.

Sources: Apple's iTunes Search API (official, keyless; search by keyword in the
run's storefront, lookup by id) and Google Play's public listing pages (read one by
one: robots.txt disallows Play's search). The developer's website resolves the
app to an organisation; an app linked from an organisation's own site is that
organisation's. A neglected app is a reason to rebuild or fix it now, so it scores
**intent**, fading as the assessment ages.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from ...core.scoring import ScoreParts
from ...sources.apps.base import STORE_TITLES
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section
from .config import AppStoreConfig
from .session import ITUNES, LISTED, SITE_APPS, SPEC_OF, SPECS, plan_units

log = logging.getLogger(__name__)

ISSUE_TEXT = {"low_rating": "Low rating", "stale_app": "Not updated",
              "review_complaints": "Complaints"}
ISSUE_PALETTE = (("Low rating", "B42318"), ("Not updated", "B54708"))


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def weight(signal: dict[str, Any], now: datetime) -> float:
    """The signal's strength, halving every half-life since the app was last assessed."""
    spec = SPEC_OF.get(signal.get("type") or "")
    if spec is None:
        return 0.0
    when = _aware(signal.get("observed_at"))
    age = max(0.0, (now - when).total_seconds() / 86400) if when else 0.0
    return float(signal.get("strength") or spec.default_strength) * \
        math.pow(0.5, age / float(spec.half_life_days or 120))


def app_signals(signals: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """This lead type's signals still worth something, strongest first."""
    rows = [s for s in signals if s.get("type") in SPEC_OF and weight(s, now) > 0]
    return sorted(rows, key=lambda s: weight(s, now), reverse=True)


def _payload(signal: dict[str, Any]) -> dict[str, Any]:
    return signal.get("payload") or {}


def _app(signal: dict[str, Any]) -> tuple[str, str]:
    payload = _payload(signal)
    return str(payload.get("store") or ""), str(payload.get("app_id") or "")


class AppStoreLeadType(LeadType):
    key = "app_store"
    title = "App store"
    description = ("Apps with poor ratings, long-stale updates or reviewers complaining of "
                   "crashes, login, speed or payments: Apple's App Store (iTunes Search API) "
                   "and Google Play's listing pages, resolved to the developer.")
    config_model = AppStoreConfig
    applicable_countries = None
    #: Its post-discovery reads organisations' homepages for store links.
    post_order = 6

    def _section(self, cfg: Any) -> AppStoreConfig:
        return lead_type_section(cfg, self.key, AppStoreConfig)

    # --- planning -------------------------------------------------------------------

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        section = self._section(cfg)
        discovery = sorted({u.source for u in plan_units(section)})
        enrichment = [SITE_APPS] if section.check_sites != "none" else []
        if section.sources.google_play.enabled:
            enrichment.append("google_play")
        return {"discovery": discovery, "enrichment": enrichment}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return plan_units(self.effective(cfg))

    @staticmethod
    def depth_changes(section: AppStoreConfig, depth: str) -> tuple[dict[str, Any], list[str]]:
        """quick: fewer apps per keyword, Play listings and sites; deep: more."""
        sources = section.sources
        if depth == "quick":
            return ({"max_sites": min(section.max_sites, 50),
                     "sources": sources.model_copy(update={
                         "itunes": sources.itunes.model_copy(update={
                             "max_results": min(sources.itunes.max_results, 25)}),
                         "google_play": sources.google_play.model_copy(update={
                             "max_listings": min(sources.google_play.max_listings, 20)})})},
                    ["app_store: 25 apps per keyword, 20 Play listings, 50 sites"])
        if depth == "deep":
            return ({"max_sites": max(section.max_sites, 500),
                     "sources": sources.model_copy(update={
                         "itunes": sources.itunes.model_copy(update={
                             "max_results": max(sources.itunes.max_results, 200)}),
                         "google_play": sources.google_play.model_copy(update={
                             "max_listings": max(sources.google_play.max_listings, 200)})})},
                    ["app_store: 200 apps per keyword, 200 Play listings, 500 sites"])
        return {}, []

    def effective(self, cfg: Any) -> AppStoreConfig:
        section = self._section(cfg)
        depth = str(cfg.get("depth") or "balanced") if hasattr(cfg, "get") else "balanced"
        update, _ = self.depth_changes(section, depth)
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return list(SPECS)

    def catalog_sources(self) -> list[dict[str, Any]]:
        """Its sources for the catalog; terms notes are in ``sources/notes.py``."""
        defaults = AppStoreConfig()
        return [
            {"name": ITUNES, "kind": "discovery", "scope": "run",
             "defaults": defaults.sources.itunes.model_dump()},
            {"name": "google_play", "kind": "enrichment", "scope": "run",
             "defaults": defaults.sources.google_play.model_dump()},
            {"name": LISTED, "kind": "discovery", "scope": "run", "defaults": {}},
            {"name": SITE_APPS, "kind": "enrichment", "scope": "run",
             "defaults": {"check_sites": defaults.check_sites,
                          "max_sites": defaults.max_sites, "workers": defaults.workers}},
        ]

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("app_issue", "App Issue", placement="front", width=13, breakdown=True,
                       palette=ISSUE_PALETTE,
                       description="The app's main problem: Low rating, Not updated (for the "
                                   "run's months) or Complaints (low-star reviews naming a "
                                   "theme)."),
            ColumnSpec("app", "App", width=30,
                       description="The flagged app (the worst, when several)."),
            ColumnSpec("app_store", "App Store", width=12),
            ColumnSpec("app_rating", "App Rating", "number", width=9, number_format="0.0",
                       description="Its store rating, out of 5."),
            ColumnSpec("app_ratings", "App Ratings", "number", width=10,
                       number_format="#,##0", description="How many ratings it has."),
            ColumnSpec("installs", "Installs", width=10,
                       description="Google Play's download band (the App Store shows none)."),
            ColumnSpec("app_updated", "App Updated", "date", width=13,
                       number_format="dd-mmm-yy", description="Its last release."),
            ColumnSpec("app_link", "App Link", "url", width=11, link_label="View app"),
            ColumnSpec("app_complaints", "App Complaints", placement="tail", width=40,
                       description="What low-star reviews on its Play listing complain about "
                                   "(rules; AI when signal_interpretation is on), with a "
                                   "quote."),
            ColumnSpec("flagged_apps", "Flagged Apps", placement="tail", width=36,
                       description="Every flagged app of this organisation, with its store."),
            ColumnSpec("app_developer", "App Developer", placement="tail", width=26,
                       description="The developer as the store lists it."),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        now = getattr(org, "as_of", None) or datetime.now(timezone.utc)
        rows = [s for s in getattr(org, "signals", []) or []
                if s.get("lead_type") == self.key and s.get("type") in SPEC_OF]
        if not rows:
            return {}
        live = app_signals(rows, now) or rows
        top = live[0]
        app = _app(top)
        same = [s for s in rows if _app(s) == app]
        payload = _payload(top)
        apps = list(dict.fromkeys(f"{_payload(s).get('app')} "
                                  f"({STORE_TITLES.get(_app(s)[0], _app(s)[0])})" for s in live))
        stores = list(dict.fromkeys(STORE_TITLES.get(_app(s)[0], _app(s)[0]) for s in live))
        complaints = next((_payload(s).get("themes") for s in live
                           if s.get("type") == "review_complaints"), None) or []
        updated = payload.get("updated")
        try:
            updated = date.fromisoformat(updated) if updated else None
        except ValueError:
            updated = None
        more = len(apps) - 1
        return {
            "app_issue": ISSUE_TEXT.get(str(top.get("type"))),
            "app": f"{payload.get('app')}" + (f" (+{more} more)" if more > 0 else ""),
            "app_store": ", ".join(stores) or None,
            "app_rating": payload.get("rating"),
            "app_ratings": payload.get("ratings"),
            "installs": payload.get("installs"),
            # A calendar date: noon UTC, so the run's zone (the export converts) keeps the day.
            "app_updated": datetime(updated.year, updated.month, updated.day, 12,
                                    tzinfo=timezone.utc) if updated else None,
            "app_link": payload.get("url") or top.get("source_url"),
            "app_complaints": "; ".join(f"{c.get('label')} ({c.get('reviews')}): "
                                        f"\"{c.get('quote')}\"" for c in complaints) or None,
            "flagged_apps": "; ".join(apps) if len(apps) > 1 or len(same) < len(rows)
            else None,
            "app_developer": payload.get("developer") or None,
        }

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Intent: the app's worst finding, fading as the assessment ages, + 0.05 per
        further finding."""
        now = now or datetime.now(timezone.utc)
        rows = app_signals(signals, now)
        if not rows:
            return ScoreParts(self.key, intent=0.0)
        intent = min(1.0, weight(rows[0], now) + 0.05 * (len(rows) - 1))
        reasons = [str(s.get("title")) for s in rows[:3]]
        return ScoreParts(self.key, intent=round(intent, 4), reasons=reasons)

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import AppStoreSession

        return AppStoreSession(context, self.effective(context.config))

    def run(self, context: Any) -> LeadTypeResult:
        """Without the engine: the units, then the sites, in one go. Never raises."""
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


APP_STORE = AppStoreLeadType()
