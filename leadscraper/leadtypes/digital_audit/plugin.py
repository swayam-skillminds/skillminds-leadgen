"""The ``digital_audit`` lead type: organisations with a weak or missing digital presence.

It audits organisations other lead types found (``audit: run`` or ``project``)
and any uploaded list of businesses or domains. Eleven checks, each toggleable
and each its own signal; an audit score (100 = nothing to fix) and
plain-language findings. Weak presence is a reason to buy now, so it scores
**intent**: the share of the score the findings took off, fading with the
audit's age.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from ...core.scoring import ScoreParts
from ..base import ColumnSpec, LeadType, LeadTypeResult, SignalSpec, WorkUnit
from ..toolkit import lead_type_section
from .checks import FINDINGS, audit_score
from .config import DigitalAuditConfig
from .targets import load_targets

log = logging.getLogger(__name__)

#: An audit's findings halve in weight every this many days: sites change slowly.
HALF_LIFE_DAYS = 180.0
SITE_PALETTE = (("OK", "1E7B34"), ("No website", "B42318"), ("Unreachable", "B42318"))


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


class DigitalAuditLeadType(LeadType):
    key = "digital_audit"
    title = "Digital audit"
    description = ("Weak or missing digital presence: no website, no HTTPS, not "
                   "mobile-friendly, slow, outdated CMS, stale, broken links, no contact form "
                   "or online booking, an incomplete Maps profile. Audits other lead types' "
                   "organisations or an uploaded list.")
    config_model = DigitalAuditConfig
    applicable_countries = None
    post_order = 10

    def _section(self, cfg: Any) -> DigitalAuditConfig:
        return lead_type_section(cfg, self.key, DigitalAuditConfig)

    def _has_uploads(self, cfg: Any) -> bool:
        try:
            return bool(load_targets(self._section(cfg)))
        except Exception:
            return bool(self._section(cfg).targets)

    # --- planning -------------------------------------------------------------------

    def sources(self, cfg: Any, geo: Any = None) -> dict[str, list[str]]:
        section = self._section(cfg)
        enrichment = ["site_audit"]
        if "pagespeed" in section.checks and section.pagespeed.max_per_run:
            enrichment.append("pagespeed")
        return {"discovery": ["upload"] if self._has_uploads(cfg) else [],
                "enrichment": enrichment}

    def plan(self, cfg: Any, geo: Any = None, depth: str = "balanced") -> list[WorkUnit]:
        return [WorkUnit(self.key, "upload", None, None, "run")] if self._has_uploads(cfg) else []

    @staticmethod
    def depth_changes(section: DigitalAuditConfig, depth: str) -> tuple[dict[str, Any], list[str]]:
        """(field updates, notes) a depth preset makes; "balanced" changes nothing."""
        if depth == "quick":
            update: dict[str, Any] = {}
            notes = []
            if section.max_links > 3:
                update["max_links"] = 3
                notes.append("digital_audit: at most 3 links checked per site")
            if "pagespeed" in section.checks:
                update["checks"] = [c for c in section.checks if c != "pagespeed"]
                notes.append("digital_audit: PageSpeed off")
            return update, notes
        if depth == "deep" and section.max_links < 20:
            return {"max_links": 20}, ["digital_audit: up to 20 links checked per site"]
        return {}, []

    def effective(self, cfg: Any) -> DigitalAuditConfig:
        """The section with the run's depth preset applied (a copy: the config is untouched)."""
        section = self._section(cfg)
        update, _ = self.depth_changes(section, str(cfg.get("depth") or "balanced")
                                       if hasattr(cfg, "get") else "balanced")
        return section.model_copy(update=update) if update else section

    def apply_depth(self, cfg: Any, depth: str) -> list[str]:
        return self.depth_changes(self._section(cfg), depth)[1]

    def signal_specs(self) -> list[SignalSpec]:
        return [SignalSpec(f.type, f.title, f.strength, HALF_LIFE_DAYS, f.description)
                for f in FINDINGS.values()]

    def catalog_sources(self) -> list[dict[str, Any]]:
        """Its sources for the catalog; terms notes are in ``sources/notes.py``."""
        return [
            {"name": "upload", "kind": "discovery", "scope": "run", "defaults": {}},
            {"name": "site_audit", "kind": "enrichment", "scope": "run",
             "defaults": {"min_delay": 1.0, "max_delay": 3.0, "workers": 6, "max_links": 8}},
            {"name": "pagespeed", "kind": "enrichment", "scope": "run",
             "defaults": {"max_per_run": 25, "strategy": "mobile", "threshold": 50}},
        ]

    # --- output -------------------------------------------------------------------------

    def columns(self, cfg: Any) -> list[ColumnSpec]:
        return [
            ColumnSpec("site", "Site", description="Digital audit: OK, No website, "
                       "Unreachable, or not checked (the site refused us, or robots.txt).",
                       placement="front", width=13, breakdown=True, palette=SITE_PALETTE),
            ColumnSpec("audit_score", "Audit Score", "score", width=9, number_format="0",
                       description="Digital health, 0–100: 100 minus points per finding (no "
                                   "website 60, unreachable 50, no HTTPS 20, outdated tech or "
                                   "not mobile-friendly 15, slow or no online booking 10, …). "
                                   "Low = a reason to buy."),
            ColumnSpec("findings", "Audit Findings", width=60,
                       description="What the audit found, in plain words."),
            ColumnSpec("stack", "Site Stack", width=28,
                       description="CMS, frameworks and server the site shows (fingerprint "
                                   "rules); outdated ones are in the findings."),
            ColumnSpec("pagespeed", "PageSpeed", "number", width=10, number_format="0",
                       description="Google PageSpeed Insights performance score, 0–100 "
                                   "(mobile by default)."),
            ColumnSpec("audited", "Audited", "date", placement="tail", width=15,
                       number_format="dd-mmm-yy"),
        ]

    def column_values(self, org: Any, cfg: Any) -> dict[str, Any]:
        item = (getattr(org, "attributes", None) or {}).get("audit") or {}
        audit = item.get("value") if isinstance(item, dict) else None
        if not isinstance(audit, dict):
            return {}
        audited = _aware(audit.get("audited_at"))
        return {"site": audit.get("status_text"), "audit_score": audit.get("score"),
                "findings": "; ".join(audit.get("findings") or []) or "Nothing to fix",
                "stack": ", ".join(audit.get("stack") or []) or None,
                "pagespeed": audit.get("pagespeed"),
                "audited": audited.replace(tzinfo=None) if audited else None}

    def score(self, org: Any, signals: list, cfg: Any,
              now: datetime | None = None) -> ScoreParts:
        """Intent: the points the findings took off the audit score, fading with age."""
        if not signals:
            return ScoreParts(self.key, intent=0.0)
        types = [s.get("type") for s in signals if s.get("type") in FINDINGS]
        health = audit_score(types)
        dates = [_aware(s.get("observed_at")) for s in signals]
        newest = max((d for d in dates if d is not None), default=None)
        fade = 1.0
        if newest is not None:
            age = max(0.0, ((now or datetime.now(timezone.utc)) - newest).total_seconds()
                      / 86400)
            fade = math.pow(0.5, age / HALF_LIFE_DAYS)
        intent = round((100 - health) / 100 * fade, 4)
        titles = [FINDINGS[t].title.lower() for t in dict.fromkeys(types)]
        return ScoreParts(self.key, intent=intent,
                          reasons=[f"digital audit {health}/100: " + ", ".join(titles)])

    # --- execution ------------------------------------------------------------------------

    def open(self, context: Any):
        from .session import DigitalAuditSession

        return DigitalAuditSession(context, self.effective(context.config))

    def run(self, context: Any) -> LeadTypeResult:
        """Without the engine: the uploads, then the audit, in one go. Never raises."""
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


DIGITAL_AUDIT = DigitalAuditLeadType()
