"""digital_audit as an engine session.

Discovery: one unit, only when targets were uploaded -- it writes them into
the project as organisations. Post-discovery (after every other lead type's,
``post_order``): the audit itself, over the uploads plus the run's (or the
project's) organisations, several sites at once, one request at a time per
site. Each audit is a snapshot: its findings replace the last audit's signals
on that organisation, and its summary is the ``audit`` attribute.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...core.lead import Lead
from ...core.normalize import domain_of, normalize_phone, normalize_url
from ...engine.costs import UnitCost, prior_cost
from ..base import LeadTypeResult, LeadTypeSession, UnitOutcome, WorkUnit
from ..toolkit import Kit
from .auditor import Auditor, AuditResult
from .checks import FINDINGS
from .config import AuditTarget, DigitalAuditConfig
from .pagespeed import PageSpeed
from .targets import load_targets

log = logging.getLogger(__name__)

KEY = "digital_audit"
UPLOAD = "upload"
#: Seconds one site takes on Google's side for a PageSpeed run.
PAGESPEED_SECONDS = 20.0


def target_lead(target: AuditTarget, region: str | None) -> Lead:
    site = normalize_url(target.website) if target.website else None
    phone = normalize_phone(target.phone, region) if target.phone else None
    return Lead(business_name=(target.name or domain_of(site) or "").strip(), website=site,
                phones=[phone] if phone else [], category=target.category,
                locality=target.place, sources=[UPLOAD])


class DigitalAuditSession(LeadTypeSession):
    lead_type = KEY
    post_scope = "run"
    #: Uploads are written after the finders run: one they already hold is audited
    #: as it is, never rewritten.
    discovery_stage = 1

    def __init__(self, context: Any, section: DigitalAuditConfig, *, kit: Kit | None = None,
                 tls=None, clock=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stats: dict[str, int] = {"uploaded": 0, "new": 0, "merged": 0, "audited": 0,
                                      "findings": 0, "recent_skipped": 0, "no_website": 0,
                                      "unreachable": 0, "not_checked": 0}
        self._lock = threading.Lock()
        self.units_done = 0
        self.targets = self._load_targets()
        self._units = [WorkUnit(KEY, UPLOAD, None, None, "run")] if self.targets else []
        pagespeed = None
        if "pagespeed" in section.checks and section.pagespeed.max_per_run > 0:
            pagespeed = PageSpeed(self.kit.http, section.pagespeed, self.kit.failures,
                                  key=self._pagespeed_key())
        self.pagespeed = pagespeed
        options: dict[str, Any] = {"pagespeed": pagespeed, "clock": self.clock}
        if tls is not None:
            options["tls"] = tls
        control = getattr(context, "control", None)
        if control is not None:
            options["should_stop"] = lambda: bool(control.stop_requested)
        self.auditor = Auditor(section, self.kit.http, self.kit.robots, self.kit.failures,
                               **options)

    def _load_targets(self) -> list[AuditTarget]:
        path = getattr(self.config, "path", None)
        try:
            return load_targets(self.section, Path(path).parent if path else None)
        except Exception as exc:  # an unreadable upload costs the uploads, not the run
            self.kit.failures.record(KEY, "targets_unreadable",
                                     url=self.section.targets_file, detail=repr(exc))
            return list(self.section.targets)

    def _pagespeed_key(self) -> str | None:
        name = self.section.pagespeed.key_secret_name
        if not name:
            return None
        try:
            from ...core.secrets import get_secret

            return get_secret(name)
        except Exception:
            return None

    # --- planning ---------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        return frozenset({f"{KEY}:{UPLOAD}"})

    def prior(self, unit: WorkUnit) -> UnitCost:
        return prior_cost(1.0 + 0.05 * len(self.targets), float(len(self.targets)))

    def site_seconds(self) -> float:
        """Expected seconds to audit one site: requests x the per-site delay, plus PageSpeed."""
        delay = (self.section.min_delay + self.section.max_delay) / 2
        requests = 3 + 0.6 * self.section.max_links      # robots, home, contact, links
        seconds = requests * (delay + 0.5)
        if self.pagespeed is not None and self.pagespeed._key:
            seconds += PAGESPEED_SECONDS
        return seconds

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        count = expected_new if self.section.audit != "targets" else 0.0
        count += len(self.targets)
        if self.section.max_orgs:
            count = min(count, float(self.section.max_orgs))
        mean = count * self.site_seconds() / max(1, self.section.workers)
        return mean, mean * 0.5

    # --- discovery: the uploads -------------------------------------------------------

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        outcome = UnitOutcome(status="done")
        if self.store is None or getattr(self.context, "dry_run", False):
            return outcome
        for target in self.targets:
            lead = target_lead(target, self.config.country)
            if not lead.business_name:
                continue
            try:
                # An organisation the project already holds is left as it is (it is
                # audited with the upload's details, in memory); a new one is stored.
                match = self.store.find_match(lead.normalized())
                action = "found"
                if match is None:
                    _, action, _ = self.store.upsert(lead)
            except Exception as exc:
                self.kit.failures.record(KEY, "target_rejected", url=target.website,
                                         business=target.name, detail=repr(exc))
                continue
            outcome.found += 1
            outcome.new += action == "new"
            outcome.merged += action == "merged"
        with self._lock:
            self.stats["uploaded"] += outcome.found
            self.stats["new"] += outcome.new
            self.stats["merged"] += outcome.merged
            self.units_done += 1
        return outcome

    # --- post-discovery: the audit ----------------------------------------------------

    def _uploads(self) -> dict[int, Lead]:
        """Each uploaded target's organisation, with the upload's details merged in memory
        (an upload's website counts even when the stored record has none)."""
        out: dict[int, Lead] = {}
        for target in self.targets:
            lead = target_lead(target, self.config.country).normalized()
            if not lead.business_name:
                continue
            match = self.store.find_match(lead)
            stored = self.store.get(match) if match is not None else None
            if stored is None:
                continue
            seen = out.setdefault(match, stored)
            seen.merge(lead)
        return out

    def candidates(self) -> list[tuple[int, Lead]]:
        """The organisations this run audits, uploads first, minus those audited recently."""
        run_id = getattr(self.context, "run_id", None)
        wanted = self._uploads()
        ids = list(wanted)
        if self.section.audit == "run" and run_id is not None:
            rows = self.store.tagged(run_id=run_id)
        elif self.section.audit == "project":
            rows = self.store.tagged()
        else:
            rows = []
        ordered = [(i, wanted[i]) for i in ids if i in wanted]
        ordered += [(i, lead) for i, lead, _ in rows if i not in wanted]
        force = bool(getattr(self.context, "force_enrich", False))
        days = self.section.reaudit_after_days
        if days and not force:
            cutoff = self.clock() - timedelta(days=days)
            audited = self.store.attribute_times("audit")
            kept = [(i, lead) for i, lead in ordered
                    if not (audited.get(i) and audited[i] > cutoff)]
            self.stats["recent_skipped"] += len(ordered) - len(kept)
            ordered = kept
        if self.section.max_orgs:
            ordered = ordered[:self.section.max_orgs]
        return ordered

    def post_discovery(self, control: Any) -> None:
        if self.store is None or getattr(self.context, "dry_run", False):
            return
        try:
            todo = self.candidates()
        except Exception as exc:
            log.exception("digital_audit could not list its organisations")
            self.kit.failures.record(KEY, "post_discovery_failed", detail=repr(exc))
            return
        if not todo:
            return
        events = getattr(self.context, "events", None)
        total, done = len(todo), [0]

        def one(item: tuple[int, Lead]) -> None:
            if control is not None and control.stop_requested:
                return
            self.store.tag_thread(KEY)
            org_id, lead = item
            result = self.auditor.audit(lead)
            try:
                self.write(org_id, result)
            except Exception as exc:
                log.exception("could not store the audit of %s", lead.business_name)
                self.kit.failures.record(KEY, "audit_not_stored", business=lead.business_name,
                                         detail=repr(exc))
            with self._lock:
                done[0] += 1
                count = done[0]
            if events is not None and (count % 10 == 0 or count == total):
                from ...engine import events as ev

                events.publish(ev.Activity(phase="post", lead_type=KEY,
                                           text=f"digital_audit: audited {count}/{total}"))

        with ThreadPoolExecutor(max_workers=self.section.workers,
                                thread_name_prefix="audit") as pool:
            list(pool.map(one, todo))

    def write(self, org_id: int, result: AuditResult) -> None:
        """Store one audit: its findings as signals (replacing the last audit's), its summary.

        An audit that said nothing about the site (refused, robots.txt, failed,
        stopped) changes nothing: the previous audit and its findings stand, and
        the site is audited again next run.
        """
        if not result.checked:
            with self._lock:
                self.stats["not_checked"] += 1
            return
        now = self.clock()
        store = self.store
        store.stamp(org_id, source=KEY, url=result.url, fields=["audit"])
        for finding in result.findings:
            spec = FINDINGS[finding.type]
            store.add_signal(org_id, finding.type, key=finding.type, title=finding.text,
                             payload={"check": spec.check, "penalty": spec.penalty,
                                      "audit_score": result.score, **finding.data},
                             strength=spec.strength, source_url=finding.url or result.url,
                             source=KEY)
        store.prune_signals(org_id, {(f.type, f.type) for f in result.findings})
        store.set_attribute(org_id, "audit", result.summary(now), origin="rule", producer=KEY)
        with self._lock:
            self.stats["audited"] += 1
            self.stats["findings"] += len(result.findings)
            if result.status == "no_website":
                self.stats["no_website"] += 1
            elif result.status == "unreachable":
                self.stats["unreachable"] += 1

    # --- the end ----------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"], stats=dict(self.stats))
