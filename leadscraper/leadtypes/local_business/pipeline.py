"""Run orchestration: discovery -> enrichment -> ranking -> output.

Design rule for the whole pipeline: a failure anywhere is recorded and stepped
over. A dead directory, a CAPTCHA, a malformed profile page -- none of them
end the run. Everything gathered so far is flushed to disk (and to Sheets)
periodically and again on exit, including on Ctrl-C.

Two ways to drive it:

* ``Pipeline.run()`` -- the whole thing, sequentially, in plan order (direct
  use, tests).
* The engine (``engine/runner.py`` via ``session.LocalBusinessSession``) --
  it takes ``discovery_units()``, schedules them against the time budget,
  runs each with ``run_planned_unit()`` (possibly several at once, on
  different hosts), then calls ``post_discovery()`` and ``finalise()``.
  ``control`` is the engine's ``RunControl``: pause, cancel and the deadline
  stop new work exactly like Ctrl-C does.
"""

from __future__ import annotations

import inspect
import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ...ai import AIAssistant
from ...net.browser import BrowserSession
from ...config import Category, Config, Locality
from ...export.csv_writer import write_csv, write_failures_csv
from ...engine.failures import FailureLog
from ...engine.parallel import HostQueue, run_across_hosts
from ...net.http import HttpClient
from ...core.country import format_money
from ...core.lead import Lead
from ...net.ratelimit import RateLimiter
from .scoring import Scorer
from ...export.sheets import SheetsWriter
from .planning import DiscoveryPlan, SelectionPlan, plan_discovery, select_sources
from ...sources import DISCOVERY_SOURCES, ENRICHMENT_SOURCES, SourceContext
from ...core.normalize import looks_like_seo_title, phone_display
from ...db.store import DedupOptions, LeadStore, store_target
from ...packs.hospitality.venues import TIERS, VENUE_TYPES

log = logging.getLogger(__name__)

#: Failure reasons that mean "try this lead again next run": the enricher was
#: turned away, not told there is nothing there.
TRANSIENT_ENRICH_REASONS = frozenset({"blocked", "fetch_failed", "run_cap_reached",
                                      "search_failed", "login_wall", "enricher_crashed"})


@dataclass
class UnitResult:
    """What one discovery unit did (the engine wraps it in a ``UnitOutcome``)."""

    found: int = 0
    new: int = 0
    merged: int = 0
    status: str = "done"
    error: str = ""


@dataclass
class RunStats:
    started_at: float = field(default_factory=time.time)
    combos_done: int = 0
    combos_total: int = 0
    units_done: int = 0
    leads_seen: int = 0
    new_leads: int = 0
    merged_leads: int = 0
    enriched: int = 0
    ai_updates: int = 0
    per_source: dict[str, int] = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def note_source(self, name: str) -> None:
        self.per_source[name] = self.per_source.get(name, 0) + 1


class Pipeline:
    def __init__(
        self,
        config: Config,
        *,
        localities: list[Locality],
        categories: list[Category],
        source_names: list[str] | None = None,
        enricher_names: list[str] | None = None,
        headless: bool = True,
        dry_run: bool = False,
        store=None,
        install_signals: bool = True,
        failure_sink: Callable[[dict], None] | None = None,
        unit_sink: Callable[[dict], None] | None = None,
        control=None,
        events=None,
        engine_driven: bool = False,
        force_enrich: bool | None = None,
    ):
        self.config = config
        self.localities = localities
        self.categories = categories
        self.dry_run = dry_run
        self.stats = RunStats(combos_total=len(localities) * len(categories))
        #: The engine's RunControl (pause / cancel / deadline), when it drives us.
        self.control = control
        #: The engine's EventBus, for activity events during enrichment.
        self.events = events
        #: The engine records units and unit stats itself.
        self._engine_driven = engine_driven
        self._stop_flag = False
        self.force_enrich = (bool(config.get("runtime.force_enrich", False))
                             if force_enrich is None else force_enrich)
        self.enrich_workers = max(1, int(config.get("runtime.enrich_workers", 8) or 1))
        self._stats_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._plan: DiscoveryPlan | None = None
        self._units: dict[str, tuple] = {}
        self._combo_remaining: dict[int, int] = {}
        self._finalised = False
        #: Final ranked leads, populated by `run()`. Kept after the store is
        #: closed so callers embedding this pipeline can read the results.
        self.leads: list[Lead] = []

        self.failures = FailureLog(config.path_for("failure_log", "logs/failures.jsonl"),
                                   sink=failure_sink)
        #: Told about every finished discovery unit (the run's work-unit history).
        self._unit_sink = unit_sink
        self.limiter = RateLimiter()
        # The city (and, for venue runs, words like "villa") must not stop
        # "Mokshah" and "Mokshah Villa Igatpuri" sharing a dedup key.
        config.apply_name_noise()
        self.scorer = Scorer(config)
        self.profile = config.profile
        # The store is built BEFORE the http client: it owns the dead-domain
        # cache, and the client needs those bound methods to skip hosts that
        # were already proved dead on an earlier run.
        #: A store handed in (by the engine, shared across lead types) belongs
        #: to the caller, who closes it; one opened here is closed here.
        self._owns_store = store is None
        if store is None:
            target, store_kwargs = store_target(config)
            store = LeadStore(target, self.scorer, **store_kwargs)
        self.store = store
        if hasattr(self.store, "scorer"):
            self.store.scorer = self.scorer
        # Set after construction so a store without dedup options (a stub, an
        # older store) still builds a pipeline.
        if hasattr(self.store, "options"):
            self.store.options = DedupOptions.from_config(config)
        self._install_signals = install_signals
        self.http = HttpClient(
            self.limiter,
            timeout=int(config.get("runtime.request_timeout", 30)),
            max_retries=int(config.get("runtime.max_retries", 3)),
            backoff_base=float(config.get("runtime.backoff_base", 2.0)),
            accept_language=config.country_info.accept_language,
            **self._dead_domain_hooks(),
        )
        # Browser contexts come from config so the cap and any future worker
        # pool can be sized from one number -- a pool wider than max_contexts
        # would leave threads waiting on a slot that is never released.
        self.browser = BrowserSession(
            headless=headless,
            timeout_ms=int(config.get("runtime.request_timeout", 30)) * 1000,
            max_contexts=int(config.get("runtime.browser_contexts", 3)),
            geolocation=config.geo_center,
            locale=config.country_info.locale,
            timezone_id=str(config.get("geo.timezone") or config.country_info.timezone),
            accept_language=config.country_info.accept_language,
        )
        self.sheets = SheetsWriter(config, self.failures)

        self.ctx = SourceContext(
            config=config, http=self.http, limiter=self.limiter,
            failures=self.failures, browser=self.browser, store=self.store,
        )

        self.ai = AIAssistant(config, self.failures)
        log.info("AI layer: %s", self._ai_status())
        #: (lead_id, page_text) queued by enrichers for AI fallback extraction.
        self._ai_extract_queue: list[tuple[int, str]] = []

        self.sources, self.source_plan = self._build("source", DISCOVERY_SOURCES, source_names)
        if enricher_names is None and not config.get("runtime.enrichment", True):
            # A depth preset (quick) switched enrichment off; naming an
            # enricher on the command line still runs it.
            log.info("enrichment off for this run (depth %s)", config.get("depth", "?"))
            enricher_names = []
        self.enrichers, self.enricher_plan = self._build(
            "enricher", ENRICHMENT_SOURCES, enricher_names
        )
        self._flush_every = int(config.get("runtime.sheets_flush_every", 25))
        self._since_flush = 0

    def _build(self, kind: str, registry: dict,
               requested: list[str] | None) -> tuple[list, SelectionPlan]:
        """Instantiate the entries that are requested, enabled and applicable.

        Every registered entry is accounted for at INFO, built or not. A source
        that silently never runs is the most expensive kind of bug here: it
        looks exactly like a source that runs and finds nothing.
        """
        classes, plan = select_sources(kind, registry, requested, self.config)
        built = [cls(self.ctx) for cls in classes]
        for name in plan.built:
            log.info("%s %s: built", kind, name)
        for name, reason in plan.skipped.items():
            log.info("%s %s: NOT built -- %s", kind, name, reason)
        log.info("%s selection: %d of %d registered built | %s",
                 kind, len(plan.built), len(registry), plan.describe())
        return built, plan

    def _dead_domain_hooks(self) -> dict[str, Callable]:
        """Hand the store's dead-domain cache to the http client, if both halves exist.

        Store and client change independently, so probe rather than assume: a
        half-landed change must cost us dead-domain skipping, not the ability to
        construct a pipeline at all.
        """
        check = getattr(self.store, "is_dead_domain", None)
        mark = getattr(self.store, "mark_dead_domain", None)
        if not (callable(check) and callable(mark)):
            log.info("dead-domain skipping off -- the store has no dead-domain cache")
            return {}
        accepted = inspect.signature(HttpClient.__init__).parameters
        if "dead_domain_check" not in accepted or "on_dead_domain" not in accepted:
            log.info("dead-domain skipping off -- this HttpClient takes no dead-domain hooks")
            return {}
        log.info("dead-domain cache: %s host(s) carried over from earlier runs",
                 self._dead_domain_count())
        return {"dead_domain_check": check, "on_dead_domain": mark}

    def _dead_domain_count(self) -> str:
        """How many hosts earlier runs wrote off; '?' when the store won't say."""
        for attr in ("dead_domain_count", "dead_domains"):
            reader = getattr(self.store, attr, None)
            if not callable(reader):
                continue
            try:
                value = reader()
            except Exception as exc:
                log.debug("dead-domain count unavailable (ignored): %s", exc)
                return "?"
            return str(value if isinstance(value, int) else len(value))
        return "?"

    def _ai_status(self) -> str:
        """Whether the AI layer is on, and if not, exactly why -- at INFO, every run.

        Enabling AI in config must never be the difference between a run that
        works and one that doesn't, so the reason it is off has to be readable
        without a debugger.
        """
        if self.ai.active:
            cap = ("no cap" if self.ai.max_cost == float("inf")
                   else f"${self.ai.max_cost:.2f} cap")
            features = ", ".join(sorted(self.ai.features)) or "all"
            return (f"active via {self.ai.backend.name}"
                    f" (model {self.ai.model}, features: {features}, {cap})")
        if not self.config.get("ai.enabled", False):
            return "off -- ai.enabled is false in config"
        if self.ai.backend is None:
            return ("off -- ai.enabled is true but no backend is usable"
                    " (no `claude` CLI on PATH, no ANTHROPIC_API_KEY);"
                    " continuing exactly as a non-AI run")
        if self.ai.over_budget:
            return f"off -- ai.max_cost_usd of ${self.ai.max_cost:.2f} is already spent"
        return f"off -- {self.ai.stats.shutdown_reason or 'backend unavailable'}"

    # --- lifecycle --------------------------------------------------------

    @property
    def _stop_requested(self) -> bool:
        """Start nothing new: Ctrl-C here, or the engine's pause/cancel/deadline."""
        return self._stop_flag or (self.control is not None and self.control.stop_requested)

    @_stop_requested.setter
    def _stop_requested(self, value: bool) -> None:
        self._stop_flag = bool(value)

    def _cut_unit(self) -> bool:
        """Abandon the unit in flight? Only on a direct Ctrl-C, or once a unit
        has overrun into the budget's reserved tail (the run must finish
        within budget + tail). A pause or cancel lets it finish."""
        if self._stop_flag:
            return True
        control = self.control
        return bool(control is not None and control.budgeted
                    and control.remaining() < -control.tail_seconds / 2)

    def install_signal_handler(self) -> None:
        """Ctrl-C finishes the current item, flushes, and exits cleanly."""
        def handler(signum, frame):
            if self._stop_requested:
                log.warning("second interrupt -- exiting immediately")
                raise KeyboardInterrupt
            self._stop_requested = True
            log.warning("interrupt received: finishing current item, then flushing output")

        signal.signal(signal.SIGINT, handler)

    def run(self) -> RunStats:
        if self._install_signals:
            self.install_signal_handler()
        log.info(
            "run start: %d localities x %d categories = %d combinations; %s; %s",
            len(self.localities), len(self.categories), self.stats.combos_total,
            self.source_plan.describe(), self.enricher_plan.describe(),
        )
        if self.dry_run:
            self._print_plan()
            return self.stats

        try:
            self._discover()
            self.post_discovery()
        except KeyboardInterrupt:
            log.warning("interrupted -- flushing what we have")
        finally:
            self._finalise()
        return self.stats

    def post_discovery(self) -> None:
        """Everything after discovery: venue passes, name polish, enrichment, AI extraction."""
        if not self._stop_requested and self.config.is_venue_run:
            self._activity("ai", "venue classification and brand roll-up")
            self._ai_phase("venue classification", self._ai_classify_venues)
            self._venue_rollup()
        if not self._stop_requested:
            self._ai_phase("name polish", self._ai_polish_names)
        if not self._stop_requested:
            self._enrich()
        if not self._stop_requested:
            self._ai_phase("fallback extraction", self._ai_fallback_extraction)

    def _activity(self, phase: str, text: str) -> None:
        if self.events is None:
            return
        from ...engine.events import Activity

        self.events.publish(Activity(phase=phase, text=text, lead_type="local_business"))

    # --- phases -----------------------------------------------------------

    def _scoped(self, scope: str) -> list:
        return [s for s in self.sources if getattr(s, "scope", "combo") == scope]

    # --- discovery units ----------------------------------------------------

    def _discovery_plan(self) -> DiscoveryPlan:
        """The units, in plan order, indexed by key (built once)."""
        if self._plan is not None:
            return self._plan
        plan = plan_discovery(self.config, self.sources, self.localities, self.categories)
        for source, unit in plan.run_units:
            self._units[unit.key] = (source, None, None, None)
        for loc_plan in plan.localities:
            for source, unit in loc_plan.units:
                self._units[unit.key] = (source, loc_plan.locality, None, None)
            for combo in loc_plan.combos:
                combo_id = id(combo)
                self._combo_remaining[combo_id] = len(combo.units)
                for source, unit in combo.units:
                    self._units[unit.key] = (source, loc_plan.locality, combo.category, combo_id)
        self.stats.combos_total = plan.combos_total if self.sources else 0
        # A combination none of the sources runs (category filters) is
        # processed by definition.
        self.stats.combos_done += sum(1 for n in self._combo_remaining.values() if n == 0)
        self._plan = plan
        return plan

    def discovery_units(self) -> list:
        """Every discovery WorkUnit, in plan order (what the engine schedules)."""
        if not self.sources:
            return []
        return self._discovery_plan().units()

    def source_named(self, name: str):
        return next((s for s in self.sources if s.name == name), None)

    def run_planned_unit(self, unit) -> UnitResult:
        """Run one planned unit by its key. Thread-safe; never raises."""
        self._discovery_plan()
        entry = self._units.get(unit.key)
        if entry is None:
            return UnitResult(status="failed", error=f"unknown unit {unit.key}")
        source, locality, category, combo_id = entry
        result = self._run_unit(source, locality, category)
        if combo_id is not None:
            with self._stats_lock:
                self._combo_remaining[combo_id] -= 1
                finished = self._combo_remaining[combo_id] == 0
                if finished:
                    self.stats.combos_done += 1
            if finished:
                log.info("progress: %d/%d combinations, %d leads stored",
                         self.stats.combos_done, self.stats.combos_total, self.store.count())
        return result

    def _discover(self) -> None:
        """Every unit in plan order (direct use; the engine schedules them itself)."""
        if not self.sources:
            # --enrich-only: reporting 72/72 combinations here would be a lie.
            self.stats.combos_total = 0
            log.info("no discovery sources active -- skipping straight to enrichment")
            return
        plan = self._discovery_plan()
        for unit in plan.units():
            if self._stop_requested:
                return
            self.run_planned_unit(unit)

    def _run_unit(self, source, locality: Locality | None,
                  category: Category | None) -> UnitResult:
        where = f"{locality.name if locality else 'all localities'} / " \
                f"{category.name if category else 'all categories'}"
        log.info("[%s] %s", source.name, where)
        result = UnitResult()
        started = time.time()
        try:
            for lead in source.discover(locality, category):
                if self._cut_unit():
                    result.status, result.error = "partial", "stopped mid-unit"
                    break
                result.found += 1
                action = self._store_lead(lead, source.name)
                if action == "new":
                    result.new += 1
                elif action == "merged":
                    result.merged += 1
        except Exception as exc:
            # A source that raises despite its own guards must not kill the run.
            result.status, result.error = "failed", repr(exc)
            self.failures.record(
                source.name, "source_crashed",
                locality=locality.name if locality else None,
                category=category.name if category else None, detail=repr(exc),
            )
            log.exception("source %s crashed on %s", source.name, where)
        with self._stats_lock:
            self.stats.units_done += 1
        if not self._engine_driven:
            self._note_unit(source, locality, category, started, result.found, result.new,
                            result.status, result.error)
        log.info("[%s] %s -> %d raw leads", source.name, where, result.found)
        if not self._engine_driven:
            # Driven by the engine, this runs on a unit thread; the engine
            # flushes from its dispatcher instead (session.checkpoint()).
            self._maybe_flush()
        return result

    def _note_unit(self, source, locality, category, started: float, found: int, new: int,
                   status: str, error: str) -> None:
        """Feed the unit's cost and yield to the scheduling stats and the run history."""
        seconds = time.time() - started
        area = locality.name if locality else "*"
        cat = category.name if category else "*"
        recorder = getattr(self.store, "record_unit", None)
        try:
            if callable(recorder):
                recorder(source.name, area, cat, seconds, new)
            if self._unit_sink is not None:
                from datetime import datetime, timezone

                self._unit_sink({
                    "lead_type": "local_business", "source": source.name,
                    "area": locality.name if locality else None,
                    "category": category.name if category else None,
                    "scope": getattr(source, "scope", "combo"), "status": status,
                    "found": found, "error": error,
                    "started_at": datetime.fromtimestamp(started, tz=timezone.utc),
                })
        except Exception as exc:  # bookkeeping never stops a run
            log.warning("could not record unit %s %s/%s: %s", source.name, area, cat, exc)

    def _store_lead(self, lead: Lead, source_name: str) -> str | None:
        with self._stats_lock:
            self.stats.leads_seen += 1
            self.stats.note_source(source_name)
        try:
            _, action, _ = self.store.upsert(lead)
        except ValueError as exc:
            self.failures.record(source_name, "invalid_lead", detail=str(exc))
            return None
        except Exception as exc:
            self.failures.record(source_name, "store_failed",
                                 business=lead.business_name, detail=repr(exc))
            return None

        with self._stats_lock:
            if action == "new":
                self.stats.new_leads += 1
            elif action == "merged":
                self.stats.merged_leads += 1
            self._since_flush += 1
        if threading.current_thread() is threading.main_thread():
            self._maybe_flush()
        return action

    def _maybe_flush(self) -> None:
        """Flush when enough has changed. One flush at a time; never from two threads."""
        with self._stats_lock:
            due = self._since_flush >= self._flush_every
        if due and self._flush_lock.acquire(blocking=False):
            try:
                self._flush()
            finally:
                self._flush_lock.release()

    def _enrich(self) -> None:
        """Fill in contact details for leads we already have.

        Enrichers run in registry order and each picks its own targets, so
        website enrichment goes first -- it often discovers the Instagram
        handle that the Instagram enricher then uses.
        """
        for enricher in self.enrichers:
            if self._stop_requested:
                return
            try:
                targets = enricher.targets(self.store)
            except Exception as exc:
                self.failures.record(enricher.name, "target_selection_failed", detail=repr(exc))
                continue
            targets = self._not_yet_enriched(enricher, targets)
            log.info("%s enrichment: %d candidate leads", enricher.name, len(targets))
            self._run_enricher(enricher, targets)

    def _not_yet_enriched(self, enricher, targets: list) -> list:
        """Skip leads this enricher already went through on an earlier run (Part 2.5)."""
        if self.force_enrich:
            return list(targets)
        reader = getattr(self.store, "enriched_ids", None)
        if not callable(reader):
            return list(targets)
        try:
            done = reader(enricher.name)
        except Exception as exc:
            log.debug("enrichment marks unavailable (ignored): %s", exc)
            return list(targets)
        kept = [(lead_id, lead) for lead_id, lead in targets if lead_id not in done]
        if len(kept) < len(targets):
            log.info("%s enrichment: %d leads already enriched on an earlier run "
                     "(--force-enrich redoes them)", enricher.name, len(targets) - len(kept))
        return kept

    def _run_enricher(self, enricher, targets: Iterable[tuple[int, Lead]]) -> None:
        """Enrich concurrently across hosts (each vendor site is its own host)."""
        def host_of(target):
            try:
                return enricher.target_host(target[1])
            except Exception:
                return enricher.name

        work = HostQueue(targets, host_of)
        total, done = work.total, [0]
        tag = getattr(self.store, "lead_type", None)
        last_note = [time.time()]

        def one(target) -> None:
            if tag and hasattr(self.store, "tag_thread"):
                self.store.tag_thread(tag)
            self._enrich_one(enricher, *target)

        def finished(_target, _result, error) -> None:
            done[0] += 1
            if error is not None:
                log.warning("%s enrichment task failed: %r", enricher.name, error)
            if self.events is not None and (time.time() - last_note[0] > 10 or done[0] == total):
                last_note[0] = time.time()
                self._activity("enrichment", f"{enricher.name} enrichment: {done[0]}/{total}")
            self._maybe_flush()

        run_across_hosts(work.next, work.hosts_of, one, finished,
                         workers=self.enrich_workers, should_stop=lambda: self._stop_requested,
                         thread_name=f"enrich-{enricher.name}")

    def _enrich_one(self, enricher, lead_id: int, lead: Lead) -> None:
        reasons = self.failures.track()
        crashed = False
        try:
            partial = enricher.enrich(lead)
        except Exception as exc:
            crashed = True
            partial = None
            self.failures.record(enricher.name, "enricher_crashed",
                                 business=lead.business_name, detail=repr(exc))
        finally:
            self.failures.untrack()

        pending = getattr(enricher, "pending_text", None)
        if pending and self.ai.uses("extract"):
            with self._stats_lock:
                self._ai_extract_queue.append((lead_id, pending))
            enricher.pending_text = None
        if not crashed and not TRANSIENT_ENRICH_REASONS.intersection(reasons):
            # Done for good: next run skips this lead unless forced.
            marker = getattr(self.store, "mark_enriched", None)
            if callable(marker):
                try:
                    marker(lead_id, enricher.name)
                except Exception as exc:
                    log.debug("could not mark %s enriched (ignored): %s", lead_id, exc)
        if not partial:
            return
        changed = self.store.merge_into(lead_id, partial)
        if changed:
            with self._stats_lock:
                self.stats.enriched += 1
                self._since_flush += 1
            log.debug("enriched %s via %s: %s", lead.business_name,
                      enricher.name, ", ".join(changed))

    # --- AI phases --------------------------------------------------------

    # Sources whose business names come from search-result titles, so are the
    # ones worth sending to the model for cleanup. Local-directory card titles
    # ("X - Caterers in Andheri | Book Now") are just as SEO-shaped.
    _TITLE_DERIVED_SOURCES = {"google_search", "facebook", "linkedin_serp", "indiamart",
                              "sulekha", "justdial"}

    def _ai_phase(self, label: str, phase: Callable[[], None]) -> None:
        """Run one AI phase, tolerating failure the way a dead source is tolerated.

        AI is a bonus, never a dependency: enabling it in config must not put a
        run that has already gathered leads at risk.
        """
        try:
            phase()
        except Exception as exc:
            self.failures.record("ai", f"{label.replace(' ', '_')}_failed", detail=repr(exc))
            log.warning("AI %s failed (%s) -- continuing without it", label, exc)

    # --- venue phases -----------------------------------------------------

    #: Rules verdicts weak enough that the model's reading of the text should
    #: replace them.
    _WEAK_TYPE_REASONS = ("no venue-type evidence", "in description", "homestay / cottage / home")

    def _ai_classify_venues(self) -> None:
        """Ask the model about venues the rules could not confidently type."""
        if not self.ai.uses("venues"):
            return
        candidates = []
        for lead_id, lead in self.store.all_with_ids():
            if lead.record_type == "Brand" or lead.venue_type_source == "ai":
                continue
            reason = lead.venue_type_source or ""
            if lead.venue_type and not any(weak in reason for weak in self._WEAK_TYPE_REASONS):
                continue
            candidates.append((lead_id, lead))
        if not candidates:
            return
        log.info("AI venue classification: %d listings the rules could not type", len(candidates))

        verdicts = self.ai.classify_venues([
            {"id": str(lead_id), "name": lead.business_name,
             "source_category": lead.property_type, "description": lead.description[:500],
             "amenities": lead.amenities[:15], "locality": lead.locality}
            for lead_id, lead in candidates
        ], VENUE_TYPES)

        for lead_id, lead in candidates:
            verdict = verdicts.get(str(lead_id))
            if not verdict:
                continue
            updates: dict = {}
            notes = list(lead.notes)
            venue_type = verdict.get("venue_type")
            if verdict.get("is_venue") is False:
                note = "AI: not a bookable venue"
                if note not in notes:
                    notes.append(note)
                updates.update(venue_type="Not a Venue", venue_type_source="ai")
            elif venue_type in VENUE_TYPES:
                updates.update(venue_type=venue_type, venue_type_source="ai")
            managed_by = (verdict.get("managed_by") or "").strip()
            if managed_by and not lead.managed_by and len(managed_by) <= 40:
                updates["managed_by"] = managed_by
            if lead.event_friendly is None and isinstance(verdict.get("event_friendly"), bool):
                updates["event_friendly"] = verdict["event_friendly"]
            if notes != lead.notes:
                updates["notes"] = notes
            if updates:
                self.stats.ai_updates += len(self.store.update_fields(lead_id, **updates))

    def _venue_rollup(self) -> None:
        """One Brand row per operator, carrying what it runs in the region.

        Properties name their operator in `managed_by` (a platform sets it, or
        the name says "SaffronStays Le Soil"); the brand row is where a sales
        conversation with the operator itself starts.
        """
        from ...packs.hospitality.platforms import BRAND_CATEGORY, REGION_WIDE

        settings = self.scorer.venue_settings
        portfolios: dict[str, list[Lead]] = {}
        for lead in self.store.all_leads():
            if lead.record_type != "Brand" and lead.managed_by:
                portfolios.setdefault(lead.managed_by, []).append(lead)

        for brand_name, properties in portfolios.items():
            configured = settings.brand_by_name(brand_name) if settings else None
            record = Lead(
                business_name=brand_name,
                category=BRAND_CATEGORY,
                locality=REGION_WIDE,
                record_type="Brand",
                property_type="Villa rental operator",
                sources=["rollup"],
            )
            if configured:
                record.website = configured.website
                record.phones = list(configured.phones)
                record.emails = list(configured.emails)
                record.instagram = configured.instagram
            # Brands answer on several lines (StayVista's site lists one number,
            # its llms.txt another), so a phone mismatch must not split them:
            # an existing Brand row with the same name is the same operator.
            lead_id = self._brand_row(brand_name)
            if lead_id is not None:
                self.store.merge_into(lead_id, record)
            else:
                try:
                    lead_id, _, _ = self.store.upsert(record)
                except ValueError:
                    continue

            nightly = sorted(p.price_min for p in properties
                             if p.price_min and p.price_basis == "per night")
            localities = sorted({p.locality for p in properties if p.locality})
            summary = f"Manages {len(properties)} listed properties in the region"
            if localities:
                summary += f" ({', '.join(localities)})"
            if nightly:
                currency = self.config.currency
                summary += (f"; {format_money(nightly[0], currency)}–"
                            f"{format_money(nightly[-1], currency)}/night")
            stored = self.store.get(lead_id)
            if stored is None:
                continue
            notes = [n for n in stored.notes if not n.startswith("Manages ")] + [summary]
            self.store.update_fields(lead_id, notes=notes, record_type="Brand",
                                     venue_type="Brand / Operator")

            # Managed villas hide the owner; the operator's line is how they
            # are booked, so every property it runs carries that number.
            stored = self.store.get(lead_id)
            lines = [phone_display(p) for p in (stored.phones if stored else [])][:2]
            contact = f"{brand_name}: {' / '.join(lines)}" if lines else ""
            if contact:
                for pid, prop in self.store.all_with_ids():
                    if prop.record_type != "Brand" and prop.managed_by == brand_name \
                            and prop.booking_contact != contact:
                        self.store.update_fields(pid, booking_contact=contact)

    def _brand_row(self, brand_name: str) -> int | None:
        from ...core.normalize import normalize_name

        wanted = normalize_name(brand_name)
        for lead_id, lead in self.store.all_with_ids():
            if lead.record_type == "Brand" and normalize_name(lead.business_name) == wanted:
                return lead_id
        return None

    def _ai_polish_names(self) -> None:
        """Clean SERP-derived and keyword-stuffed names; flag non-businesses."""
        if not self.ai.uses("names"):
            self._rule_polish_names()
            return

        candidates = []
        for lead_id, lead in self.store.all_with_ids():
            sources = set(lead.sources)
            # Maps and directory profiles usually give clean names...
            title_derived = ("google_maps" not in sources
                             and bool(sources & self._TITLE_DERIVED_SOURCES))
            # ...but every SEO-titled name in the Mumbai DB (~100) came from
            # Maps, where owners stuff keywords into the listing name itself.
            if title_derived or looks_like_seo_title(lead.business_name):
                candidates.append((lead_id, lead))

        if not candidates:
            return
        log.info("AI name polish: %d title-derived leads", len(candidates))

        verdicts = self.ai.clean_names([
            {"id": str(lead_id), "title": lead.business_name,
             "category": lead.category, "locality": lead.locality}
            for lead_id, lead in candidates
        ])

        for lead_id, lead in candidates:
            verdict = verdicts.get(str(lead_id))
            if not verdict:
                continue
            notes = list(lead.notes)
            updates: dict = {}

            if verdict.get("is_business") is False:
                note = "AI: looks like a category/listicle page, not a single business"
                if note not in notes:
                    notes.append(note)
            else:
                better = (verdict.get("business_name") or "").strip()
                if better and better.lower() != lead.business_name.lower() and len(better) >= 3:
                    notes.append(f"AI renamed from: {lead.business_name}")
                    updates["business_name"] = better

            if verdict.get("matches_category") is False and lead.category:
                note = f"AI: may not actually be a {lead.category} vendor"
                if note not in notes:
                    notes.append(note)

            if notes != lead.notes:
                updates["notes"] = notes
            if updates:
                self.stats.ai_updates += len(self.store.update_fields(lead_id, **updates))

    def _rule_polish_names(self) -> None:
        """Without AI, cut keyword-stuffed names back to the name they start with.

        "Royal Caterers | Best Caterers in Mumbai" -> "Royal Caterers", by the
        same rules that turn a search-result title into a name. A title that
        is all keywords ("Best Caterers in Andheri") is left alone: there is
        no name in it to keep. The original stays in the notes.
        """
        from ...core.normalize import seo_title_head
        from ...sources.google_search import _has_distinctive_token

        renamed = 0
        for lead_id, lead in self.store.all_with_ids():
            better = seo_title_head(lead.business_name)
            if not better:
                continue
            category = next((c for c in self.config.categories if c.name == lead.category), None)
            locality = next((loc for loc in self.config.localities
                             if loc.name == lead.locality), None)
            terms = (lead.category, *(category.queries if category else ()),
                     *(locality.aliases if locality else ()))
            # "Best Caterers in Andheri | Book Now" has no name to keep.
            if not _has_distinctive_token(better, lead.locality, tuple(t for t in terms if t),
                                          [*self.config.place_words, self.config.city]):
                continue
            notes = [*lead.notes, f"renamed by rule from: {lead.business_name}"]
            if self.store.update_fields(lead_id, business_name=better, notes=notes,
                                        _source="rule_polish"):
                renamed += 1
        if renamed:
            log.info("name polish (rules, AI off): %d keyword-stuffed names cleaned", renamed)
            self.stats.ai_updates += renamed

    def _ai_fallback_extraction(self) -> None:
        """Read contacts off pages whose selectors/regexes came up empty."""
        queue = self._ai_extract_queue
        if not queue or not self.ai.uses("extract"):
            return
        log.info("AI fallback extraction: %d pages the scrapers couldn't parse", len(queue))

        results = self.ai.extract_contacts([(str(lead_id), text) for lead_id, text in queue])
        for lead_id, _ in queue:
            record = results.get(str(lead_id))
            if not record:
                continue
            partial = self._lead_from_ai_record(record, self.config.country)
            if partial is None:
                continue
            if self.store.merge_into(lead_id, partial):
                self.stats.ai_updates += 1
        self._ai_extract_queue = []

    @staticmethod
    def _lead_from_ai_record(record: dict, region: str | None = None) -> Lead | None:
        """Turn an AI extraction result into a Lead, re-validating every field.

        Model output goes through the SAME normalisers as scraped data, so a
        malformed or hallucinated phone number is rejected here rather than
        landing in the sheet.
        """
        from ...core.normalize import clean_text, extract_emails, extract_instagram, extract_phones

        def as_text(value) -> str:
            if isinstance(value, (list, tuple)):
                return " ".join(str(v) for v in value if v)
            return str(value) if value else ""

        phones = extract_phones(as_text(record.get("phones")), region)
        emails = extract_emails(as_text(record.get("emails")))
        address = clean_text(record.get("address") or "")[:300]
        instagram = extract_instagram(as_text(record.get("instagram"))) or None
        if instagram is None and record.get("instagram"):
            handle = str(record["instagram"]).lstrip("@").strip()
            instagram = handle.lower() if handle.replace("_", "").replace(".", "").isalnum() else None

        if not (phones or emails or address or instagram):
            return None
        partial = Lead(business_name="")
        partial.phones, partial.emails = phones, emails
        partial.address, partial.instagram = address, instagram
        partial.sources = ["ai_extract"]
        partial.notes = ["contacts read by AI from an unparseable page"]
        return partial

    # --- output -----------------------------------------------------------

    def _flush(self) -> None:
        """Push the current sorted table to CSV and Sheets."""
        with self._stats_lock:
            self._since_flush = 0
        leads = self.store.sorted_leads()
        try:
            write_csv(leads, self.config.path_for("csv_export", "data/leads.csv"), self.profile,
                      self.config.currency)
        except Exception as exc:
            log.warning("CSV write failed: %s", exc)
        self.sheets.sync(leads)

    def _finalise(self) -> None:
        if self._finalised:
            return
        self._finalised = True
        log.info("finalising: rescoring and writing output")
        try:
            self.store.rescore_all()
        except Exception as exc:
            log.warning("rescore failed: %s", exc)

        leads = self._without_suppressed(self.store.sorted_leads())
        self.leads = leads
        csv_path = self.config.path_for("csv_export", "data/leads.csv")
        try:
            write_csv(leads, csv_path, self.profile, self.config.currency)
            self._record_file("csv", csv_path, len(leads))
        except Exception as exc:
            log.error("final CSV write failed: %s", exc)

        failure_rows = self.failures.rows()
        if len(failure_rows) > 1:
            skipped = write_failures_csv(failure_rows, csv_path.with_name("skipped.csv"))
            self._record_file("skipped_csv", skipped, len(failure_rows) - 1)
        self.sheets.sync(leads)
        self.sheets.sync_failures(failure_rows)

        self._close()
        self._report(leads)

    def _without_suppressed(self, leads: list[Lead]) -> list[Lead]:
        """Drop organisations on the run's "exclude" suppression lists (Phase 3).

        Lists in "flag" mode only mark rows, which the API/CLI exports show;
        the run's CSV and Sheet keep their fixed column layout.
        """
        if getattr(self.store, "engine", None) is None:
            return leads
        try:
            from ...services.suppression import ERASURE_LIST, Matcher

            # Erasure requests apply to every run, attached or not.
            names = [*(self.config.get("filters.suppression_lists") or []), ERASURE_LIST]
            matcher = Matcher.for_names(self.store.engine, self.store.workspace_id, names,
                                        region=self.config.country)
        except Exception as exc:
            log.warning("suppression lists not applied: %s", exc)
            return leads
        if not matcher:
            return leads
        kept = [lead for lead in leads if not matcher.excluded(lead)]
        if len(kept) != len(leads):
            log.info("suppression: %d organisation(s) excluded from the output",
                     len(leads) - len(kept))
        return kept

    def _record_file(self, kind: str, path, rows: int | None) -> None:
        run_id = getattr(self.store, "run_id", None)
        if run_id is not None:
            from ...db.runs import add_run_file

            add_run_file(self.store.engine, run_id, kind=kind, path=str(path), rows=rows)

    def _close(self) -> None:
        closers = [self.browser.close, self.http.close, self.ai.close]
        if self._owns_store:
            closers.insert(2, self.store.close)
        for closer in closers:
            try:
                closer()
            except Exception as exc:
                log.debug("cleanup error (ignored): %s", exc)

    def _report(self, leads: list[Lead]) -> None:
        minutes = self.stats.elapsed / 60
        print("\n" + "=" * 68)
        print(f"RUN COMPLETE in {minutes:.1f} min")
        print("=" * 68)
        print(f"  Combinations processed : {self.stats.combos_done}/{self.stats.combos_total}")
        print(f"  Raw leads seen         : {self.stats.leads_seen}")
        print(f"  Unique leads stored    : {len(leads)}")
        print(f"    - new this run       : {self.stats.new_leads}")
        print(f"    - merged into existing: {self.stats.merged_leads}")
        print(f"  Enrichment updates     : {self.stats.enriched}")
        if self.stats.ai_updates:
            print(f"  AI-applied updates     : {self.stats.ai_updates}")
        ai_report = self.ai.report()
        if ai_report:
            print(ai_report)

        if self.stats.per_source:
            print("\n  Raw leads by source:")
            for name, count in sorted(self.stats.per_source.items(), key=lambda kv: -kv[1]):
                print(f"    {name:<16} {count}")

        with_phone = sum(1 for lead in leads if lead.phones)
        with_email = sum(1 for lead in leads if lead.emails)
        multi_source = sum(1 for lead in leads if len(set(lead.sources)) > 1)
        if leads:
            print("\n  Contactability:")
            print(f"    with a phone     : {with_phone} ({with_phone / len(leads):.0%})")
            print(f"    with an email    : {with_email} ({with_email / len(leads):.0%})")
            print(f"    on 2+ sources    : {multi_source} ({multi_source / len(leads):.0%})")

        failure_summary = self.failures.summary()
        if failure_summary:
            total = sum(failure_summary.values())
            print(f"\n  Skipped/failed lookups: {total} (see {self.failures.path})")
            for name, count in list(failure_summary.items())[:10]:
                print(f"    {name:<16} {count}")

        print(f"\n  CSV : {self.config.path_for('csv_export', 'data/leads.csv')}")
        if self.sheets.url:
            print(f"  Sheet: {self.sheets.url}")
        elif self.sheets.unavailable_reason:
            print(f"  Sheet: not written -- {self.sheets.unavailable_reason}")

        if leads and self.config.is_venue_run:
            self._report_venues(leads, self.config.currency)
        elif leads:
            print("\n  Top 10 by legitimacy score:")
            for lead in sorted(leads, key=lambda l: -l.score)[:10]:
                phone = lead.phones[0] if lead.phones else "no phone"
                print(f"    {lead.score:5.1f}  {lead.business_name[:38]:<38} "
                      f"{lead.locality[:14]:<14} {phone}")
        print("=" * 68 + "\n")

    @staticmethod
    def _report_venues(leads: list[Lead], currency: str) -> None:
        properties = [lead for lead in leads if lead.record_type != "Brand"]
        brands = [lead for lead in leads if lead.record_type == "Brand"]
        print(f"\n  Venues: {len(properties)} properties, {len(brands)} brands/operators")
        print("  By tier:")
        for tier in TIERS[1:]:
            count = sum(1 for lead in properties if lead.venue_tier == tier)
            print(f"    {tier:<12} {count}")
        types: dict[str, int] = {}
        for lead in properties:
            types[lead.venue_type or "Unclassified"] = types.get(lead.venue_type or "Unclassified", 0) + 1
        print("  By venue type:")
        for venue_type, count in sorted(types.items(), key=lambda kv: -kv[1]):
            print(f"    {venue_type:<26} {count}")
        priced = sum(1 for lead in properties if lead.price_min)
        print(f"  With a price: {priced} ({priced / max(1, len(properties)):.0%})")
        print("\n  Top 10 venues:")
        for lead in properties[:10]:
            price = (f"{format_money(lead.price_min, currency)}/{lead.price_basis.replace('per ', '')}"
                     if lead.price_min else "no price")
            print(f"    {lead.venue_tier[:7]:<8}{lead.score:5.1f}  {lead.business_name[:32]:<32} "
                  f"{lead.locality[:14]:<14} {price}")

    def _print_plan(self) -> None:
        print("\nDRY RUN -- no requests will be made\n")
        print(f"Sources   : {', '.join(s.name for s in self.sources) or '(none enabled)'}")
        print(f"Enrichers : {', '.join(e.name for e in self.enrichers) or '(none enabled)'}")
        if self.ai.active:
            features = ", ".join(sorted(self.ai.features)) or "all"
            cap = ("no cap (runs until your subscription allowance is spent, "
                   "then continues without AI)"
                   if self.ai.max_cost == float("inf")
                   else f"${self.ai.max_cost:.2f} usage cap")
            print(f"AI        : {self.ai.backend.name} / {self.ai.model}"
                  f" | features: {features} | {cap}")
        else:
            print(f"AI        : {self._ai_status()}")
        print(f"Localities: {', '.join(l.name for l in self.localities)}")
        print(f"Categories: {', '.join(c.name for c in self.categories)}")
        run_once = [s.name for s in self._scoped("run")]
        per_locality = [s.name for s in self._scoped("locality")]
        combo = [s.name for s in self._scoped("combo")]
        if run_once:
            print(f"Once per run : {', '.join(run_once)}")
        if per_locality:
            print(f"Per locality : {', '.join(per_locality)} ({len(self.localities)} localities)")
        print(f"\nCombinations: {self.stats.combos_total}"
              f" x {len(combo)} per-combination sources = "
              f"{self.stats.combos_total * len(combo)} source runs (before category filters)")
        if not self._engine_driven:
            # The engine prints its own estimate (p50/p90, budget) after this.
            estimate = self._estimate_minutes()
            print(f"Rough time estimate: {estimate:.0f}-{estimate * 2:.0f} minutes"
                  " (dominated by the configured delays)\n")
        else:
            print()
        self._close()

    def _estimate_minutes(self) -> float:
        """Very rough: average delay x expected requests per unit of work."""
        total_seconds = 0.0
        for source in self.sources:
            low, high = self.config.delays(source.name)
            per_request = (low + high) / 2
            scope = getattr(source, "scope", "combo")
            if scope == "run":
                # A sitemap pass: one fetch (plus maybe a render) per property.
                total_seconds += per_request * int(source.cfg.get("max_properties", 60)) * 1.5
            elif scope == "locality":
                total_seconds += per_request * 12 * len(self.localities)
            else:
                requests_per_combo = 8 if source.uses_browser else 4
                combos = sum(1 for loc in self.localities for cat in self.categories
                             if cat.runs_in(loc) and cat.runs_source(source.name))
                if self.config.get(f"sources.{source.name}.city_level", False):
                    combos = sum(1 for cat in self.categories if cat.runs_source(source.name))
                total_seconds += per_request * requests_per_combo * combos
        return total_seconds / 60
