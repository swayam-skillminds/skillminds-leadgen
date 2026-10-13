"""tech_stack as an engine session.

Candidates, each confirmed by fingerprinting the site itself (``sitecheck.py``):

* **uploads** (one discovery unit when the config lists any): written into the
  project like digital_audit's, then checked in post-discovery;
* **search footprints** (opt-in, one unit per place x technology): a web search for
  the phrases the technology's users show ("powered by Shopify" "Pune"); each site
  found is fingerprinted at once and kept only if it runs the technology;
* **other lead types' organisations** (post-discovery, after theirs: ``post_order``):
  the run's (``check: run``) or the project's sites.

A wanted technology on a site is a signal: ``technology`` (it runs it) or
``outdated_technology`` (an outdated version). Each check is a snapshot: a
technology no longer seen goes, and the ``tech_stack`` attribute holds the whole
stack. Stage 1, like every lead type that attaches to others' organisations: an
organisation the project holds keeps its record (rule 6).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...core.lead import Lead
from ...core.normalize import domain_of
from ...engine.costs import UnitCost, prior_cost
from ...sources.web.fingerprint import Detected, canonical, rule
from ..base import LeadTypeResult, LeadTypeSession, SignalSpec, UnitOutcome, WorkUnit
from ..toolkit import Kit
from .config import TechStackConfig, TechTarget
from .sitecheck import SOURCE, SiteCheck, SiteChecker, own_site
from .targets import UPLOAD, load_targets, target_lead

log = logging.getLogger(__name__)

KEY = "tech_stack"
FOOTPRINTS = "footprints"
SERP_HOSTS = ("www.google.com", "duckduckgo.com")
SPECS = [
    SignalSpec("technology", "Runs", 0.5, 365.0,
               "The site runs a technology the run looks for (a platform the seller "
               "builds on, migrates from or supports)."),
    SignalSpec("outdated_technology", "Outdated technology", 0.7, 180.0,
               "The site runs an outdated version: a reason to upgrade, migrate or rebuild."),
]
SPEC_OF = {s.type: s for s in SPECS}


def has_uploads(section: TechStackConfig, base_dir: Path | None = None) -> bool:
    try:
        return bool(load_targets(section, base_dir))
    except Exception:
        return bool(section.targets or section.domains)


def phrases(section: TechStackConfig, technology: str) -> list[str]:
    """The footprint phrases for a technology: the table's, then the config's extras."""
    found = rule(technology)
    table = list(found.footprints) if found else []
    extra = section.sources.footprints.phrases.get(technology, [])
    return list(dict.fromkeys([*table, *extra]))


def _places(config: Any) -> list[str]:
    return [p for p in dict.fromkeys(str(p).strip() for p in (config.get("places") or [])) if p]


def plan_units(config: Any, section: TechStackConfig, *, uploads: bool) -> list[WorkUnit]:
    units = [WorkUnit(KEY, UPLOAD, None, None, "run")] if uploads else []
    if section.sources.footprints.enabled:
        for place in _places(config):
            units.extend(WorkUnit(KEY, FOOTPRINTS, place, tech, "combo")
                         for tech in section.technologies if phrases(section, tech))
    return units


def matches(stack: list[Detected], section: TechStackConfig) -> list[tuple[Detected, bool]]:
    """(technology, named by the run) for each wanted technology the site shows.

    Named technologies count (only their outdated versions with ``outdated_only``);
    with none named, any outdated technology does.
    """
    if section.technologies:
        wanted = {canonical(t) for t in section.technologies}
        return [(d, True) for d in stack if canonical(d.name) in wanted
                and (d.outdated or not section.outdated_only)]
    return [(d, False) for d in stack if d.outdated]


class TechStackSession(LeadTypeSession):
    lead_type = KEY
    post_scope = "run"
    #: After the finders: their organisations are checked as they are, never rewritten.
    discovery_stage = 1

    def __init__(self, context: Any, section: TechStackConfig, *, kit: Kit | None = None,
                 clock=None):
        self.context = context
        self.config = context.config
        self.section = section
        self.store = getattr(context, "store", None)
        self.kit = kit or Kit.build(context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dry_run = bool(getattr(context, "dry_run", False))
        self._lock = threading.Lock()
        self.units_done = 0
        self.stats: dict[str, int] = {"uploaded": 0, "new": 0, "merged": 0, "searches": 0,
                                      "search_sites": 0, "checked": 0, "matched": 0,
                                      "signals": 0, "outdated": 0, "not_checked": 0,
                                      "no_website": 0, "recent_skipped": 0,
                                      "skipped_unknown": 0}
        #: Organisations checked this run (footprint finds): not checked again.
        self.checked_ids: set[int] = set()
        self.targets = self._load_targets()
        control = getattr(context, "control", None)
        self.checker = SiteChecker(
            self.kit.http, self.kit.robots, self.kit.failures, thresholds=section.thresholds,
            min_delay=section.min_delay, max_delay=section.max_delay,
            should_stop=(lambda: bool(control.stop_requested)) if control is not None else None)
        footprints = section.sources.footprints
        if footprints.enabled:              # the run's limiter: only when it searches
            for host in SERP_HOSTS:
                self.kit.limiter.configure(host, footprints.min_delay, footprints.max_delay)
        self._units = plan_units(self.config, section, uploads=bool(self.targets))

    def _load_targets(self) -> list[TechTarget]:
        path = getattr(self.config, "path", None)
        try:
            return load_targets(self.section, Path(path).parent if path else None)
        except Exception as exc:  # an unreadable upload costs the uploads, not the run
            self.kit.failures.record(KEY, "targets_unreadable",
                                     url=self.section.targets_file, detail=repr(exc))
            return list(self.section.targets)

    # --- planning ---------------------------------------------------------------------

    def units(self) -> list[WorkUnit]:
        return list(self._units)

    def hosts(self, unit: WorkUnit) -> frozenset[str]:
        if unit.source == FOOTPRINTS:
            return frozenset(SERP_HOSTS)
        return frozenset({f"{KEY}:{UPLOAD}"})

    def site_seconds(self) -> float:
        """Expected seconds to fingerprint one site: robots.txt and the homepage."""
        return 2 * ((self.section.min_delay + self.section.max_delay) / 2 + 0.5)

    def prior(self, unit: WorkUnit) -> UnitCost:
        if unit.source == FOOTPRINTS:
            footprints = self.section.sources.footprints
            search = (footprints.min_delay + footprints.max_delay) / 2 + 3.0
            return prior_cost(search + footprints.max_sites * self.site_seconds(), 3.0)
        return prior_cost(1.0 + 0.05 * len(self.targets), float(len(self.targets)))

    def post_estimate(self, expected_new: float) -> tuple[float, float]:
        count = expected_new if self.section.check != "targets" else 0.0
        count += len(self.targets)
        if self.section.max_orgs:
            count = min(count, float(self.section.max_orgs))
        mean = count * self.site_seconds() / max(1, self.section.workers)
        return mean, mean * 0.5

    # --- discovery ----------------------------------------------------------------------

    def run_unit(self, unit: WorkUnit) -> UnitOutcome:
        try:
            if unit.source == UPLOAD:
                outcome = self.upload()
            elif unit.source == FOOTPRINTS:
                outcome = self.search(unit.area or "", unit.category or "")
            else:
                outcome = UnitOutcome(status="failed", error=f"unknown source {unit.source}")
        except Exception as exc:  # contained: a bug costs its unit
            log.exception("tech_stack unit %s failed", unit.key)
            self.kit.failures.record(unit.source, "unit_failed", locality=unit.area,
                                     detail=repr(exc)[:300])
            outcome = UnitOutcome(status="failed", error=repr(exc)[:300])
        with self._lock:
            self.units_done += 1
        return outcome

    def upload(self) -> UnitOutcome:
        """The uploads into the project: one it already holds is left as it is."""
        outcome = UnitOutcome(status="done")
        if self.store is None or self.dry_run:
            return outcome
        for target in self.targets:
            lead = target_lead(target, self.config.country)
            if not lead.business_name:
                continue
            try:
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
        return outcome

    def search(self, place: str, technology: str) -> UnitOutcome:
        """A web search for the technology's footprints in a place; each site found is
        fingerprinted, and kept only when it really runs the technology."""
        from ...sources.google_search import SerpClient

        outcome = UnitOutcome(status="done")
        words = phrases(self.section, technology)
        if not words or self.store is None or self.dry_run:
            return outcome
        footprints = self.section.sources.footprints
        query = (f"({' OR '.join(words)})" if len(words) > 1 else words[0]) + f' "{place}"'
        ctx = SimpleNamespace(config=self.config, http=self.kit.http, limiter=self.kit.limiter,
                              failures=self.kit.failures, browser=None)
        results = SerpClient(ctx).search(query, per_page=footprints.max_results)
        with self._lock:
            self.stats["searches"] += 1
        if not results:
            self.kit.failures.record(FOOTPRINTS, "no_results", query=query, locality=place)
            return outcome
        found = rule(technology)
        vendor = set(found.vendor) if found else set()
        sites: dict[str, tuple[str, str]] = {}
        for result in results:
            site = own_site(result.url)
            host = domain_of(site) if site else None
            if not host or host in sites or any(host == v or host.endswith("." + v)
                                                for v in vendor):
                continue
            sites[host] = (result.title, f"{site.split('//', 1)[0]}//{host}/")
            if len(sites) >= footprints.max_sites:
                break
        with self._lock:
            self.stats["search_sites"] += len(sites)
        for host, (title, home) in sites.items():
            check = self.checker.check(title or host, home)
            wanted = [(d, named) for d, named in matches(check.stack, self.section)
                      if canonical(d.name) == canonical(technology)]
            if not check.checked or not wanted:
                continue
            name = check.title or _title_name(title) or host
            lead = Lead(business_name=name, website=home, locality=place,
                        sources=[FOOTPRINTS], source_urls=[check.url or home])
            org_id = self._resolve(lead, outcome)
            if org_id is None:
                continue
            outcome.found += 1
            self.write(org_id, check)
        return outcome

    def _resolve(self, lead: Lead, outcome: UnitOutcome) -> int | None:
        """A found site's organisation: one the project holds keeps its record (rule 6);
        one nobody holds is created when ``create_new``."""
        with self.store._lock:
            match = self.store.find_match(lead.normalized())
            if match is not None:
                return match
            if not self.section.create_new:
                with self._lock:
                    self.stats["skipped_unknown"] += 1
                return None
            org_id, action, _ = self.store.upsert(lead)
        outcome.new += action == "new"
        outcome.merged += action == "merged"
        with self._lock:
            self.stats["new"] += action == "new"
            self.stats["merged"] += action == "merged"
        return org_id

    # --- post-discovery: other lead types' organisations and the uploads ---------------

    def _uploads(self) -> dict[int, Lead]:
        """Each upload's organisation, with the upload's site merged in memory (an upload's
        website counts even when the stored record has none)."""
        out: dict[int, Lead] = {}
        for target in self.targets:
            lead = target_lead(target, self.config.country).normalized()
            if not lead.business_name:
                continue
            match = self.store.find_match(lead)
            stored = self.store.get(match) if match is not None else None
            if stored is None:
                continue
            out.setdefault(match, stored).merge(lead)
        return out

    def candidates(self) -> list[tuple[int, Lead]]:
        """The organisations to check, uploads first, minus those checked recently."""
        run_id = getattr(self.context, "run_id", None)
        wanted = self._uploads()
        if self.section.check == "run" and run_id is not None:
            rows = self.store.tagged(run_id=run_id)
        elif self.section.check == "project":
            rows = self.store.tagged()
        else:
            rows = []
        ordered = list(wanted.items())
        ordered += [(i, lead) for i, lead, _ in rows if i not in wanted]
        ordered = [(i, lead) for i, lead in ordered if i not in self.checked_ids]
        force = bool(getattr(self.context, "force_enrich", False))
        days = self.section.recheck_after_days
        if days and not force:
            cutoff = self.clock() - timedelta(days=days)
            checked = self.store.attribute_times(KEY)
            kept = [(i, lead) for i, lead in ordered
                    if not (checked.get(i) and checked[i] > cutoff)]
            self.stats["recent_skipped"] += len(ordered) - len(kept)
            ordered = kept
        if self.section.max_orgs:
            ordered = ordered[:self.section.max_orgs]
        return ordered

    def post_discovery(self, control: Any) -> None:
        if self.store is None or self.dry_run:
            return
        try:
            todo = self.candidates()
        except Exception as exc:
            log.exception("tech_stack could not list its organisations")
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
            check = self.checker.check(lead.business_name, lead.website)
            try:
                self.write(org_id, check)
            except Exception as exc:
                log.exception("could not store the stack of %s", lead.business_name)
                self.kit.failures.record(KEY, "check_not_stored", business=lead.business_name,
                                         detail=repr(exc))
            with self._lock:
                done[0] += 1
                count = done[0]
            if events is not None and (count % 10 == 0 or count == total):
                from ...engine import events as ev

                events.publish(ev.Activity(phase="post", lead_type=KEY,
                                           text=f"tech_stack: checked {count}/{total} sites"))

        with ThreadPoolExecutor(max_workers=self.section.workers,
                                thread_name_prefix="techstack") as pool:
            list(pool.map(one, todo))

    # --- writing ------------------------------------------------------------------------

    def write(self, org_id: int, check: SiteCheck) -> None:
        """Store one check: each wanted technology a signal (replacing the last check's),
        the whole stack the ``tech_stack`` attribute. A check that said nothing about the
        site (no site, unreachable, refused, robots.txt, stopped) changes nothing."""
        if not check.checked:
            with self._lock:
                self.stats["not_checked" if check.status != "no_website" else "no_website"] += 1
            return
        found = matches(check.stack, self.section)
        keep: set[tuple[str, str]] = set()
        added = 0
        for detected, named in found:
            type_ = "outdated_technology" if detected.outdated else "technology"
            key = f"tech:{canonical(detected.name)}"
            keep.add((type_, key))
            added += bool(self.store.add_signal(
                org_id, type_, key=key, title=detected.label,
                payload={"technology": detected.name, "version": detected.version,
                         "category": detected.category, "outdated": detected.outdated,
                         "why": detected.why, "evidence": detected.evidence,
                         "targeted": named, "url": check.url},
                strength=SPEC_OF[type_].default_strength, source_url=check.url,
                source=SOURCE))
        self.store.prune_signals(org_id, keep)
        self.store.set_attribute(org_id, KEY, self.summary(check, found), origin="rule",
                                 producer=KEY)
        with self._lock:
            self.checked_ids.add(org_id)
            self.stats["checked"] += 1
            self.stats["matched"] += bool(found)
            self.stats["signals"] += added
            self.stats["outdated"] += sum(1 for d, _ in found if d.outdated)

    def summary(self, check: SiteCheck, found: list[tuple[Detected, bool]]) -> dict[str, Any]:
        """The ``tech_stack`` attribute: what the workbook and the API show."""
        return {"url": check.url, "status": check.status,
                "stack": [d.label for d in check.stack],
                "technologies": [{"name": d.name, "version": d.version, "category": d.category,
                                  "outdated": d.outdated, "why": d.why, "evidence": d.evidence}
                                 for d in check.stack],
                "matched": [d.label for d, _ in found],
                "outdated": [d.label for d, _ in found if d.outdated],
                "checked_at": self.clock().isoformat(timespec="seconds")}

    # --- the end ------------------------------------------------------------------------

    def close(self) -> None:
        self.kit.close()

    def result(self) -> LeadTypeResult:
        return LeadTypeResult(lead_type=KEY, units_planned=len(self._units),
                              units_done=self.units_done, new=self.stats["new"],
                              merged=self.stats["merged"], stats=dict(self.stats))


def _title_name(title: str) -> str:
    from .sitecheck import _TITLE_SPLIT

    return " ".join(_TITLE_SPLIT.split(title or "", maxsplit=1)[0].split())[:200]
