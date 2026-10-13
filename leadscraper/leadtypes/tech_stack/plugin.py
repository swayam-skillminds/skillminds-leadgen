"""The ``tech_stack`` lead type: companies running a specific or an outdated technology.

Candidates come from other lead types (the run's or the project's organisations),
uploaded lists, or opt-in search footprints ("powered by Shopify" "Pune"). Each is
confirmed by fingerprinting the site itself with the shared rules
(``sources/web/fingerprints.yaml``). Running a named technology is **fit** (a
platform the seller builds on, migrates from or supports); an outdated version is
**intent** (a reason to upgrade now), fading as the check ages.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.scoring import ScoreParts
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section
from .config import TechStackConfig
from .session import FOOTPRINTS, SPEC_OF, SPECS, has_uploads, plan_units

log = logging.getLogger(__name__)

#: Fit for a site running one named technology, and per further one (to MAX_FIT).
BASE_FIT, MORE_FIT, MAX_FIT = 0.6, 0.1, 0.9
STACK_PALETTE = (("Outdated", "B42318"),)


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
    """The signal's strength, halving every half-life since the site was last checked."""
    spec = SPEC_OF.get(signal.get("type") or "")
    if spec is None:
        return 0.0
    when = _aware(signal.get("observed_at"))
    age = max(0.0, (now - when).total_seconds() / 86400) if when else 0.0
    return float(signal.get("strength") or spec.default_strength) * \
        math.pow(0.5, age / float(spec.half_life_days or 180))


def _payload(signal: dict[str, Any]) -> dict[str, Any]:
    return signal.get("payload") or {}


class TechStackLeadType(LeadType):
    key = "tech_stack"
    title = "Tech stack"
    description = ("Companies running a specific or outdated technology (WordPress, Magento 1, "
                   "Shopify, AngularJS, old PHP …), confirmed by fingerprinting their own "
                   "site. Checks other lead types' organisations, an uploaded list, or (opt-in) "
                   "sites a web search for \"powered by …\" found.")
    config_model = TechStackConfig
    applicable_countries = None
    #: After hiring's and decision_makers' passes, before the audit.
    post_order = 8

    def _section(self, cfg: Any) -> TechStackConfig:
        return lead_type_section(cfg, self.key, TechStackConfig)

    def _uploads(self, cfg: Any) -> bool:
        path = getattr(cfg, "path", None)
        return has_uploads(self._section(cfg), Path(path).parent if path else None)

    # --- planning -------------------------------------------------------------------

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        units = plan_units(cfg, self._section(cfg), uploads=self._uploads(cfg))
        return {"discovery": sorted({u.source for u in units}),
                "enrichment": ["site_fingerprint"]}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return plan_units(cfg, self.effective(cfg), uploads=self._uploads(cfg))

    @staticmethod
    def depth_changes(section: TechStackConfig, depth: str) -> tuple[dict[str, Any], list[str]]:
        """quick: fewer sites per search, no project-wide checks; deep: more of both."""
        footprints = section.sources.footprints
        if depth == "quick":
            update: dict[str, Any] = {}
            notes = []
            if footprints.enabled and footprints.max_sites > 8:
                update["sources"] = section.sources.model_copy(
                    update={"footprints": footprints.model_copy(update={"max_sites": 8})})
                notes.append("tech_stack: at most 8 sites per search")
            if section.max_orgs is None or section.max_orgs > 100:
                update["max_orgs"] = 100
                notes.append("tech_stack: at most 100 sites checked")
            return update, notes
        if depth == "deep" and footprints.enabled and footprints.max_sites < 30:
            update = {"sources": section.sources.model_copy(
                update={"footprints": footprints.model_copy(update={"max_sites": 30})})}
            return update, ["tech_stack: up to 30 sites per search"]
        return {}, []

    def effective(self, cfg: Any) -> TechStackConfig:
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
        defaults = TechStackConfig()
        return [
            {"name": "upload", "kind": "discovery", "scope": "run", "defaults": {}},
            {"name": FOOTPRINTS, "kind": "discovery", "scope": "combo",
             "defaults": defaults.sources.footprints.model_dump()},
            {"name": "site_fingerprint", "kind": "enrichment", "scope": "run",
             "defaults": {"min_delay": defaults.min_delay, "max_delay": defaults.max_delay,
                          "workers": defaults.workers}},
        ]

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("technology", "Technology", placement="front", width=18, breakdown=True,
                       description="The technology the run looks for that the site runs "
                                   "(the first, when several)."),
            ColumnSpec("tech_status", "Tech Status", placement="front", width=11,
                       breakdown=True, palette=STACK_PALETTE,
                       description="Outdated: a version past its end of life or the run's "
                                   "threshold; Current otherwise."),
            ColumnSpec("tech_found", "Tech Found", width=30,
                       description="Each wanted technology on the site, with its version."),
            ColumnSpec("outdated_tech", "Outdated Tech", width=40,
                       description="Outdated versions and why (end-of-life dates, from the "
                                   "fingerprint table)."),
            ColumnSpec("technologies", "Technologies", width=36,
                       description="Everything the site's homepage shows: CMS, shop, "
                                   "frameworks, libraries, server (fingerprint rules)."),
            ColumnSpec("tech_evidence", "Tech Evidence", placement="tail", width=30,
                       description="What gave each wanted technology away (meta generator, a "
                                   "script URL, a header, the HTML)."),
            ColumnSpec("tech_checked", "Tech Checked", "date", placement="tail", width=13,
                       number_format="dd-mmm-yy"),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        rows = [s for s in getattr(org, "signals", []) or []
                if s.get("lead_type") == self.key and s.get("type") in SPEC_OF]
        item = (getattr(org, "attributes", None) or {}).get(self.key) or {}
        summary = item.get("value") if isinstance(item, dict) else None
        if not rows and not isinstance(summary, dict):
            return {}
        rows.sort(key=lambda s: (s.get("type") != "outdated_technology",
                                 str(_payload(s).get("technology") or "")))
        outdated = [s for s in rows if s.get("type") == "outdated_technology"]
        checked = _aware((summary or {}).get("checked_at")) if summary else None
        values: dict[str, Any] = {
            "technology": _payload(rows[0]).get("technology") if rows else None,
            "tech_status": ("Outdated" if outdated else "Current") if rows else None,
            "tech_found": ", ".join(s.get("title") or "" for s in rows) or None,
            "outdated_tech": "; ".join(
                f"{s.get('title')} ({_payload(s).get('why')})" if _payload(s).get("why")
                else str(s.get("title")) for s in outdated) or None,
            "technologies": ", ".join((summary or {}).get("stack") or []) or None,
            "tech_evidence": "; ".join(f"{_payload(s).get('technology')}: "
                                       f"{_payload(s).get('evidence')}"
                                       for s in rows if _payload(s).get("evidence")) or None,
            "tech_checked": checked.replace(tzinfo=None) if checked else None,
        }
        return values

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Fit: it runs named technologies; intent: outdated versions, fading with the check."""
        now = now or datetime.now(timezone.utc)
        rows = [s for s in signals if s.get("type") in SPEC_OF]
        named = list(dict.fromkeys(str(_payload(s).get("technology")) for s in rows
                                   if _payload(s).get("targeted")))
        section = self._section(cfg) if cfg is not None else None
        targets = bool(section.technologies) if section is not None else bool(named)
        fit = None
        if targets:
            fit = 0.0
            seen = [_aware(s.get("observed_at")) for s in rows if _payload(s).get("targeted")]
            if named:
                newest = max((d for d in seen if d is not None), default=now)
                age = max(0.0, (now - newest).total_seconds() / 86400)
                fade = math.pow(0.5, age / float(SPEC_OF["technology"].half_life_days))
                fit = round(min(MAX_FIT, BASE_FIT + MORE_FIT * (len(named) - 1)) * fade, 4)
        old = sorted((s for s in rows if s.get("type") == "outdated_technology"),
                     key=lambda s: weight(s, now), reverse=True)
        intent = 0.0
        if old:
            intent = round(min(1.0, weight(old[0], now) + 0.05 * (len(old) - 1)), 4)
        reasons = []
        if named:
            reasons.append("runs " + ", ".join(named))
        if old:
            reasons.append("outdated: " + ", ".join(str(s.get("title")) for s in old))
        return ScoreParts(self.key, fit=fit, intent=intent, reasons=reasons)

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import TechStackSession

        return TechStackSession(context, self.effective(context.config))

    def run(self, context: Any) -> LeadTypeResult:
        """Without the engine: the units, then the checks, in one go. Never raises."""
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


TECH_STACK = TechStackLeadType()
