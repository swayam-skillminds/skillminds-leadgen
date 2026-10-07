"""Phase 2: planning, estimation, time budget, resilient jobs. All offline.

Time is a ``FakeClock`` wherever a test is about durations: a 15-minute budget
run takes milliseconds and is exactly reproducible. Lifecycle tests (pause,
cancel, Ctrl-C, worker shutdown) use real, short units, because the pause and
cancel requests travel through the database and the run's heartbeat.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_fixtures import HOOKS, LEAD_TYPE, RECORDED, SyntheticSession, synthetic_config  # noqa: E402
from leadscraper.config import Config  # noqa: E402
from leadscraper.config.compile import compile_run_config  # noqa: E402
from leadscraper.config.schema import RunConfig  # noqa: E402
from leadscraper.core.lead import Lead  # noqa: E402
from leadscraper.db.engine import open_database  # noqa: E402
from leadscraper.db.models import Worker as WorkerRow  # noqa: E402
from leadscraper.db.runs import (  # noqa: E402
    claim_run, get_run, request_control, unit_rows, update_run,
)
from leadscraper.engine import events as ev  # noqa: E402
from leadscraper.engine.clock import FakeClock  # noqa: E402
from leadscraper.engine.control import RunControl  # noqa: E402
from leadscraper.engine.costs import CostModel, Tally, UnitStatsReader, blend, prior_cost  # noqa: E402
from leadscraper.engine.estimator import LiveEta, describe_estimate  # noqa: E402
from leadscraper.engine.parallel import EXHAUSTED, HostQueue, run_across_hosts  # noqa: E402
from leadscraper.engine.runner import RunRequest, execute, request_from_row  # noqa: E402
from leadscraper.engine.scheduler import SKIP_BUDGET, Planned, Scheduler  # noqa: E402
from leadscraper.engine.worker import Worker, request_worker_stop, submit_run  # noqa: E402
from leadscraper.leadtypes import registry  # noqa: E402
from leadscraper.leadtypes.base import WorkUnit  # noqa: E402
from leadscraper.leadtypes.local_business import estimates  # noqa: E402
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod  # noqa: E402
from leadscraper.paths import PROJECT_ROOT, default_db_url  # noqa: E402
from leadscraper.sources.base import Enricher, Source  # noqa: E402

MUMBAI = PROJECT_ROOT / "presets" / "mumbai-event-vendors.yaml"


@pytest.fixture
def synthetic():
    registry.register(LEAD_TYPE)
    HOOKS.clear()
    yield
    HOOKS.clear()
    registry.unregister("synthetic")


def compiled(document: dict) -> Config:
    return compile_run_config(RunConfig.model_validate(document))


def app_engine():
    return open_database(default_db_url())


def units(n: int, *, seconds: float = 120.0, new: int = 2, host: str = "h", source="src"):
    return [{"source": source, "area": f"A{i:02d}", "category": "C", "host": host,
             "seconds": seconds, "new": new} for i in range(n)]


# --- the time budget (acceptance: "a 15-minute budget run finishes within budget +
#     tail, and its report lists the skipped units") -------------------------------------

def test_a_15_minute_budget_run_finishes_within_budget_plus_tail(synthetic, capsys):
    clock = FakeClock()
    plan = units(20, seconds=120.0)
    plan[2]["seconds"] = 400.0                       # one unit overruns its estimate
    config = compiled(synthetic_config("budget", budget=15, units=plan,
                                       priors={"src": [120.0, 2.0]}))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=clock,
                                 discovery_workers=1))

    assert outcome.status == "completed"
    assert outcome.elapsed_seconds <= 15 * 60 + 90          # budget + tail
    assert outcome.skipped and all(reason in (SKIP_BUDGET, "time budget spent")
                                   for _, reason in outcome.skipped)
    engine = app_engine()
    row = get_run(engine, outcome.run_id)
    summary = row["stats"]["engine"]
    assert summary["units_skipped"] == len(outcome.skipped) == 20 - summary["units_done"]
    assert {u["unit"] for u in summary["skipped_units"]} == {k for k, _ in outcome.skipped}
    rows = unit_rows(engine, outcome.run_id)
    assert Counter(r["status"] for r in rows) == {"done": summary["units_done"],
                                                  "skipped": summary["units_skipped"]}
    assert all(r["skip_reason"] for r in rows if r["status"] == "skipped")
    engine.dispose()

    from leadscraper.cli.run_cmd import report_outcome
    report_outcome(outcome)
    printed = capsys.readouterr().out
    assert f"skipped units: {len(outcome.skipped)}" in printed
    assert outcome.skipped[0][0] in printed


def test_a_unit_that_overruns_into_the_tail_is_cut(synthetic):
    clock = FakeClock()
    plan = units(3, seconds=120.0, new=50)
    plan[1]["seconds"] = 5_000.0                     # wildly over its estimate
    config = compiled(synthetic_config("overrun", budget=15, units=plan,
                                       priors={"src": [120.0, 2.0]}))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=clock,
                                 discovery_workers=1))
    assert outcome.elapsed_seconds <= 15 * 60 + 90
    engine = app_engine()
    statuses = {r["unit_key"]: r["status"] for r in unit_rows(engine, outcome.run_id)}
    engine.dispose()
    assert "partial" in statuses.values()


def test_without_a_budget_every_unit_runs_in_plan_order(synthetic):
    order: list[str] = []
    HOOKS["after_unit"] = lambda session, unit, i: order.append(unit.area)
    plan = units(5)
    config = compiled(synthetic_config("nobudget", units=plan))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 discovery_workers=1))
    assert outcome.status == "completed" and not outcome.skipped
    assert order == [u["area"] for u in plan]


def test_the_scheduler_prefers_yield_per_second_and_re_sorts_on_what_it_sees():
    control = RunControl(budget_seconds=10_000, tail_seconds=0)

    def unit(source):
        return WorkUnit("t", source, "A", "C")

    priors = {"fast": prior_cost(10, 5), "slow": prior_cost(100, 5)}
    costs = CostModel(None, lambda u: priors[u.source], lambda u: (u.source, "A", "C"))
    scheduler = Scheduler([Planned(unit("slow"), None, 0), Planned(unit("fast"), None, 1)],
                          costs, control, order="priority")
    assert scheduler.next_ready(set()).unit.source == "fast"    # 0.5/s beats 0.05/s
    costs.observe(unit("fast"), 1000.0, 0)                       # ...until it is seen to be awful
    scheduler = Scheduler([Planned(unit("slow"), None, 0), Planned(unit("fast"), None, 1)],
                          costs, control, order="priority")
    assert scheduler.next_ready(set()).unit.source == "slow"


def test_the_scheduler_never_starts_a_unit_that_cannot_finish():
    control = RunControl(budget_seconds=300, tail_seconds=90, clock=FakeClock())
    big, small = WorkUnit("t", "big", "A", None), WorkUnit("t", "small", "A", None)
    costs = CostModel(None, lambda u: prior_cost(500 if u.source == "big" else 100, 1),
                      lambda u: (u.source, "A", "*"))
    skipped = []
    scheduler = Scheduler([Planned(big, None, 0), Planned(small, None, 1)], costs, control,
                          on_skip=lambda p, reason: skipped.append((p.unit.source, reason)))
    assert scheduler.next_ready(set()).unit.source == "small"   # took the next one that fits
    assert skipped == [("big", SKIP_BUDGET)]
    assert scheduler.next_ready(set()) is EXHAUSTED


def test_whole_region_units_go_first_even_by_priority():
    sitemap = WorkUnit("t", "sitemap", None, None, "run")
    maps = WorkUnit("t", "maps", "A", "C")
    costs = CostModel(None, lambda u: prior_cost(1000 if u.source == "sitemap" else 10, 5),
                      lambda u: (u.source, "*", "*"))
    scheduler = Scheduler([Planned(maps, None, 0), Planned(sitemap, None, 1)], costs,
                          RunControl(), order="priority")
    assert scheduler.next_ready(set()).unit.source == "sitemap"


# --- the estimator (acceptance: "p50 within +/-35% of actual on a recorded-fixture run") --

def recorded(name: str, attempt: int) -> dict:
    return synthetic_config(name, recorded=str(RECORDED), attempt=attempt, priors={
        "maps": [200.0, 10.0], "serp": [60.0, 2.0], "directory": [40.0, 3.0],
        "platform": [100.0, 5.0]})


def test_estimator_p50_is_within_35_percent_on_a_recorded_fixture_run(synthetic):
    # Run 1 (the recording's first run) teaches unit_stats; the estimate for
    # run 2 is then compared with what run 2 actually took.
    cold = execute(RunRequest(config=compiled(recorded("rec", 1)), dry_run=True,
                              print_plan=False, discovery_workers=1)).estimate
    execute(RunRequest(config=compiled(recorded("rec", 0)), install_signals=False,
                       clock=FakeClock(), discovery_workers=1))
    warm = execute(RunRequest(config=compiled(recorded("rec", 1)), dry_run=True,
                              print_plan=False, discovery_workers=1)).estimate
    actual = execute(RunRequest(config=compiled(recorded("rec", 1)), install_signals=False,
                                clock=FakeClock(), discovery_workers=1))

    assert cold["basis"] == {"prior": 60}
    assert warm["basis"] == {"learned": 60}
    error = abs(warm["p50_seconds"] - actual.elapsed_seconds) / actual.elapsed_seconds
    assert error <= 0.35, (warm["p50_seconds"], actual.elapsed_seconds)
    assert warm["p90_seconds"] >= warm["p50_seconds"]
    assert actual.elapsed_seconds <= warm["p90_seconds"]


def test_estimator_simulates_parallel_hosts_and_reports_the_budget(synthetic):
    def estimate(**kwargs):
        return execute(RunRequest(config=compiled(recorded("par", 0)), dry_run=True,
                                  print_plan=False, **kwargs)).estimate

    one, three = estimate(discovery_workers=1), estimate(discovery_workers=3)
    # maps + serp share "google"; directory and platform each have a host of their own.
    assert three["discovery"]["p50_seconds"] < one["discovery"]["p50_seconds"]
    google_lane = sum(row["seconds"] for source, row in three["breakdown"]["synthetic"].items()
                      if source in ("maps", "serp"))
    assert three["discovery"]["p50_seconds"] >= google_lane     # that lane is serial
    budget = estimate(discovery_workers=3, time_budget_minutes=15)["budget"]
    assert budget["units_fit"] + budget["units_skipped"] == 60
    assert budget["units_skipped"] and budget["p50_seconds"] <= 15 * 60
    text = describe_estimate(estimate(discovery_workers=3, time_budget_minutes=15))
    assert "Time budget" in text and "skipped" in text


def test_live_eta_measures_the_parallelism_achieved():
    eta = LiveEta(workers=3, post_p50=0, post_p90=0, finalise=0)
    assert eta.remaining(900, 0, 0) == (900, 900)
    eta.observe(300)
    eta.observe(300)                                      # 600 s of work in 300 s of wall
    assert eta.remaining(900, 0, 300)[0] == pytest.approx(450)


def test_progress_events_carry_a_live_eta_that_learns(synthetic):
    seen: list = []
    # Its own source name: no other test's learned unit_stats apply.
    config = compiled(synthetic_config("eta", units=units(4, seconds=100.0, source="eta"),
                                       priors={"eta": [10.0, 1.0]}))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 discovery_workers=1, sinks=[seen.append]))
    progress = [e for e in seen if e.type == "progress"]
    assert [p.units_done for p in progress] == [1, 2, 3, 4]
    # Planned at 10 s a unit; after one unit took 100 s the remaining three
    # are expected to take far longer than the 30 s the plan said.
    assert outcome.estimate["discovery"]["p50_seconds"] == pytest.approx(40)
    assert progress[0].eta_p50_seconds > 3 * 50
    assert progress[-1].eta_p50_seconds < progress[0].eta_p50_seconds


def test_learned_costs_blend_history_with_the_prior():
    prior = prior_cost(100, 10)
    one = blend(prior, Tally(runs=1, seconds=40, sq_seconds=1600, new=4), "learned")
    many = blend(prior, Tally(runs=10, seconds=400, sq_seconds=16_400, new=40), "learned")
    assert 40 < one.seconds < 100 and 40 < many.seconds < one.seconds
    assert many.sd > 0 and many.basis == "learned" and many.runs == 10


# --- depth presets ----------------------------------------------------------------------

def test_depth_presets_scale_local_business_knobs():
    balanced = Config.load(MUMBAI)
    assert estimates.apply_depth(balanced, "balanced") == []     # the preset, untouched

    quick = Config.load(MUMBAI)
    estimates.apply_depth(quick, "quick")
    assert quick.get("sources.google_maps.max_results_per_query") == 10
    assert quick.get("sources.google_maps.scroll_rounds") == 3
    assert quick.get("sources.google_search.pages") == 1
    assert quick.get("sources.wedmegood.max_pages") == 1
    assert quick.get("runtime.enrichment") is False
    assert not quick.overrides                         # derived from depth, not a CLI flag

    deep = Config.load(MUMBAI)
    estimates.apply_depth(deep, "deep")
    assert deep.get("sources.google_maps.max_results_per_query") == 60
    assert deep.get("sources.google_maps.scroll_rounds") == 20
    assert deep.get("sources.wedmegood.max_pages") == 5
    if deep.get("sources.instagram") is not None:
        assert deep.get("sources.instagram.enabled") is True

    seconds = {}
    for depth in ("quick", "balanced", "deep"):
        est = execute(RunRequest(config=Config.load(MUMBAI), dry_run=True, print_plan=False,
                                 depth=depth)).estimate
        seconds[depth] = est["p50_seconds"]
    assert seconds["quick"] < seconds["balanced"] < seconds["deep"]


def test_quick_depth_switches_enrichment_off(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"slow_site": SlowSiteEnricher})
    config = Config(pipeline_config(tmp_path))
    config.set("runtime.enrichment", False, record=False)
    pipe = pipeline_mod.Pipeline(config, localities=config.localities,
                                 categories=config.categories)
    assert pipe.enrichers == []
    pipe._close()


def test_cold_start_priors_order_maps_then_directories_then_serp():
    est = execute(RunRequest(config=Config.load(MUMBAI), dry_run=True,
                             print_plan=False)).estimate
    rate = {source: row["new"] / row["seconds"]
            for source, row in est["breakdown"]["local_business"].items()}
    directories = ("justdial", "sulekha", "indiamart", "wedmegood", "weddingwire")
    serp = ("google_search", "facebook", "linkedin_serp")
    assert all(rate["google_maps"] > rate[d] for d in directories)
    assert all(rate["google_maps"] > rate[s] for s in serp)
    best_directory = max(rate[d] for d in directories)
    assert all(best_directory > rate[s] for s in serp)


# --- parallel across hosts, never within one ----------------------------------------------

def test_work_runs_in_parallel_across_hosts_but_never_two_on_one_host():
    items = [(host, i) for host in "abc" for i in range(4)]
    active: Counter = Counter()
    peak_by_host: Counter = Counter()
    peak = [0]
    lock = threading.Lock()

    def work(item):
        host = item[0]
        with lock:
            active[host] += 1
            peak_by_host[host] = max(peak_by_host[host], active[host])
            peak[0] = max(peak[0], sum(active.values()))
        time.sleep(0.02)
        with lock:
            active[host] -= 1
        return host

    done = []
    queue = HostQueue(items, lambda item: item[0])
    run_across_hosts(queue.next, queue.hosts_of, work,
                     lambda item, result, error: done.append(item), workers=3)
    assert sorted(done) == sorted(items)
    assert set(peak_by_host.values()) == {1}              # never two on one host
    assert peak[0] == 3                                    # three hosts at once


def test_a_graceful_stop_lets_in_flight_work_finish():
    stop = threading.Event()
    finished = []

    def work(item):
        stop.set()                                         # a stop arrives mid-item
        time.sleep(0.02)
        return item

    queue = HostQueue([(h, 0) for h in "abcdef"], lambda item: item[0])
    run_across_hosts(queue.next, queue.hosts_of, work,
                     lambda item, result, error: finished.append(result),
                     workers=2, should_stop=stop.is_set)
    assert 1 <= len(finished) <= 2 and None not in finished
    assert queue.remaining() == 6 - len(finished)


# --- concurrent enrichment and the enrichment memo (Part 2) -------------------------------

def pipeline_config(tmp_path) -> dict:
    return {
        "localities": [{"name": "Andheri"}],
        "categories": [{"name": "Caterers", "queries": ["caterers"]}],
        "sources": {"slow_site": {"enabled": True, "min_delay": 0, "max_delay": 0}},
        "runtime": {"db_path": str(tmp_path / "leads.sqlite3"),
                    "failure_log": str(tmp_path / "failures.jsonl"),
                    "csv_export": str(tmp_path / "leads.csv"), "enrich_workers": 8},
        "sheets": {"enabled": False},
    }


class SlowSiteEnricher(Enricher):
    """Each lead's site is its own host; every fetch takes 0.1 s."""

    name = "slow_site"
    calls: list[str] = []
    blocked: set[str] = set()

    def target_host(self, lead):
        return lead.website

    def enrich(self, lead):
        SlowSiteEnricher.calls.append(lead.business_name)
        time.sleep(0.1)
        if lead.business_name in SlowSiteEnricher.blocked:
            self.fail("blocked", business=lead.business_name)
            return None
        found = Lead(business_name=lead.business_name)
        found.emails = [f"hello@{lead.website.split('//')[1]}"]
        return found


def seed(store, count: int) -> None:
    for i in range(count):
        store.upsert(Lead(business_name=f"Vendor {i}", locality="Andheri", category="Caterers",
                          website=f"https://vendor{i}.test", phones=[f"+91982000{i:04d}"]))


def test_enrichment_runs_across_hosts_concurrently_and_skips_enriched_leads(tmp_path,
                                                                             monkeypatch):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"slow_site": SlowSiteEnricher})
    SlowSiteEnricher.calls, SlowSiteEnricher.blocked = [], {"Vendor 7"}
    config = Config(pipeline_config(tmp_path))

    def run(force: bool = False) -> float:
        pipe = pipeline_mod.Pipeline(config, localities=config.localities,
                                     categories=config.categories, source_names=[],
                                     install_signals=False, force_enrich=force)
        if not pipe.store.count():
            seed(pipe.store, 8)
        started = time.monotonic()
        pipe.run()
        return time.monotonic() - started

    elapsed = run()
    assert sorted(SlowSiteEnricher.calls) == [f"Vendor {i}" for i in range(8)]
    assert elapsed < 0.8 * 0.1 * 8                         # 8 hosts at once, not one by one

    # Second run: only the lead that was turned away (blocked) is tried again.
    SlowSiteEnricher.calls = []
    run()
    assert SlowSiteEnricher.calls == ["Vendor 7"]

    # --force-enrich redoes every lead.
    SlowSiteEnricher.calls = []
    run(force=True)
    assert sorted(SlowSiteEnricher.calls) == [f"Vendor {i}" for i in range(8)]


# --- lifecycle: pause / resume / cancel / Ctrl-C ------------------------------------------

def lifecycle_config(name: str, tmp_path: Path, count: int = 12, **section) -> Config:
    return compiled(synthetic_config(
        name, units=units(count, seconds=1.0, new=1),
        real_seconds=0.03, log_path=str(tmp_path / f"{name}.log"), **section))


def executions(tmp_path: Path, name: str) -> Counter:
    path = tmp_path / f"{name}.log"
    return Counter(path.read_text().split()) if path.exists() else Counter()


def request_after(action: str, at: int):
    def hook(session, unit, index):
        if index == at:
            request_control(session.store.engine, session.context.run_id, action)
    return hook


def test_pause_then_resume_runs_every_unit_exactly_once(synthetic, tmp_path):
    HOOKS["after_unit"] = request_after("pause", 3)
    config = lifecycle_config("pause", tmp_path)
    paused = execute(RunRequest(config=config, install_signals=False, discovery_workers=1))

    assert paused.status == "paused"
    engine = app_engine()
    rows = unit_rows(engine, paused.run_id)
    done = [r for r in rows if r["status"] == "done"]
    assert 3 <= len(done) < 12
    assert all(r["status"] == "pending" for r in rows if r["status"] != "done")
    assert get_run(engine, paused.run_id)["status"] == "paused"
    replay = [e.type for e in ev.read_events(engine, paused.run_id)]
    assert replay[-1] == "run_state" and "run_finished" in replay

    HOOKS.clear()
    assert request_control(engine, paused.run_id, "resume") == "queued"
    resumed = execute(request_from_row(get_run(engine, paused.run_id)))
    assert resumed.status == "completed" and resumed.run_id == paused.run_id
    counts = executions(tmp_path, "pause")
    assert len(counts) == 12 and set(counts.values()) == {1}   # none lost, none twice
    row = get_run(engine, paused.run_id)
    assert row["attempts"] == 2 and row["status"] == "completed"
    assert row["stats"]["engine"]["units_done"] == 12
    engine.dispose()


def test_cancel_finishes_in_flight_work_then_flushes(synthetic, tmp_path):
    HOOKS["after_unit"] = request_after("cancel", 2)
    config = lifecycle_config("cancel", tmp_path, post_seconds=50)
    outcome = execute(RunRequest(config=config, install_signals=False, discovery_workers=1))
    assert outcome.status == "cancelled"
    engine = app_engine()
    statuses = Counter(r["status"] for r in unit_rows(engine, outcome.run_id))
    assert statuses["skipped"] == 12 - statuses["done"]
    assert all(reason == "run cancelled" for _, reason in outcome.skipped)
    assert get_run(engine, outcome.run_id)["finished_at"] is not None
    engine.dispose()


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) is not Ctrl-C on Windows")
def test_ctrl_c_pauses_through_the_same_stop_path(synthetic, tmp_path):
    def interrupt(session, unit, index):
        if index == 2:
            os.kill(os.getpid(), signal.SIGINT)
    HOOKS["after_unit"] = interrupt
    config = lifecycle_config("ctrlc", tmp_path)
    outcome = execute(RunRequest(config=config, install_signals=True, discovery_workers=1))
    assert outcome.status == "paused"
    assert len(executions(tmp_path, "ctrlc")) == 2        # the unit in flight finished


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) is not Ctrl-C on Windows")
def test_a_second_ctrl_c_stops_at_once_and_loses_at_most_the_unit_in_flight(synthetic,
                                                                         tmp_path):
    def interrupt_twice(session, unit, index):
        if index == 2:
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGINT)
    HOOKS["after_unit"] = interrupt_twice
    config = lifecycle_config("hard", tmp_path)
    outcome = execute(RunRequest(config=config, install_signals=True, discovery_workers=1))
    assert outcome.status == "paused" and "interrupted" in outcome.error
    engine = app_engine()
    statuses = [r["status"] for r in unit_rows(engine, outcome.run_id)]
    assert statuses.count("running") == 1                 # the unit in flight
    HOOKS.clear()
    resumed = execute(request_from_row(get_run(engine, outcome.run_id)))
    engine.dispose()
    assert resumed.status == "completed"
    counts = executions(tmp_path, "hard")
    assert len(counts) == 12 and sum(counts.values()) == 13      # exactly one unit re-ran


# --- the worker and the queue ------------------------------------------------------------

def test_a_worker_takes_queued_runs_and_two_workers_never_claim_the_same_run(synthetic,
                                                                            tmp_path):
    run_id = submit_run(RunRequest(config=lifecycle_config("queued", tmp_path, count=3),
                                   discovery_workers=1))
    engine = app_engine()
    assert get_run(engine, run_id)["status"] == "queued"
    assert claim_run(engine, run_id, "worker:a") is True
    assert claim_run(engine, run_id, "worker:b") is False         # atomic
    update_run(engine, run_id, status="queued", worker_id=None, attempts=0)

    worker = Worker(engine=engine, name="w1", poll_seconds=0.01, heartbeat_seconds=0.05,
                    install_signals=False)
    assert worker.run(once=True) == 1
    row = get_run(engine, run_id)
    assert row["status"] == "completed" and row["attempts"] == 1
    with engine.connect() as conn:
        worker_row = conn.execute(select(WorkerRow.__table__).where(
            WorkerRow.__table__.c.name == "w1")).mappings().first()
    assert worker_row["status"] == "stopped"
    engine.dispose()


def test_stopping_a_worker_requeues_its_run_and_the_next_worker_resumes_it(synthetic,
                                                                         tmp_path):
    engine = app_engine()
    first = Worker(engine=engine, name="first", poll_seconds=0.01, heartbeat_seconds=0.05,
                   install_signals=False)
    HOOKS["after_unit"] = lambda session, unit, index: first.stop() if index == 2 else None
    run_id = submit_run(RunRequest(config=lifecycle_config("handover", tmp_path),
                                   discovery_workers=1))
    first.run(once=True)
    assert get_run(engine, run_id)["status"] == "queued"            # handed back

    HOOKS.clear()
    second = Worker(engine=engine, name="second", poll_seconds=0.01, heartbeat_seconds=0.05,
                    install_signals=False)
    second.run(once=True)
    assert get_run(engine, run_id)["status"] == "completed"
    counts = executions(tmp_path, "handover")
    assert len(counts) == 12 and set(counts.values()) == {1}
    engine.dispose()


def test_worker_stop_is_a_database_flag_not_a_signal():
    engine = app_engine()
    worker = Worker(engine=engine, name="flagged", poll_seconds=0.02, heartbeat_seconds=0.02,
                    install_signals=False)
    thread = threading.Thread(target=worker.run)
    thread.start()
    assert wait_for(lambda: request_worker_stop(engine, "flagged") == 1, timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
    engine.dispose()


def wait_for(predicate, timeout: float = 30.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def test_killing_the_worker_mid_run_and_restarting_it_loses_at_most_one_unit(tmp_path):
    """Acceptance: a real worker process, killed (SIGKILL / TerminateProcess)."""
    import subprocess

    env = {**os.environ, "LEADSCRAPER_PLUGINS": "engine_fixtures",
           "LEADSCRAPER_KEEP_AWAKE": "0",
           "PYTHONPATH": os.pathsep.join([str(PROJECT_ROOT), str(Path(__file__).parent)])}
    registry.register(LEAD_TYPE)
    try:
        config = compiled(synthetic_config(
            "killed", units=units(10, seconds=1.0, new=1), real_seconds=0.3,
            log_path=str(tmp_path / "killed.log")))
        run_id = submit_run(RunRequest(config=config, discovery_workers=1))
    finally:
        registry.unregister("synthetic")
    engine = app_engine()

    def worker(name: str):
        return subprocess.Popen(
            [sys.executable, "-m", "leadscraper", "worker", "--once", "--name", name,
             "--poll", "0.1", "--heartbeat", "0.2", "--stale-after", "1"],
            env=env, cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def done_units() -> int:
        return sum(1 for r in unit_rows(engine, run_id) if r["status"] == "done")

    first = worker("first")
    try:
        assert wait_for(lambda: done_units() >= 3), "the first worker never got going"
    finally:
        first.kill()
        first.wait(timeout=10)
    assert get_run(engine, run_id)["status"] == "running"           # orphaned

    time.sleep(1.5)                                                  # its heartbeat goes stale
    second = worker("second")
    try:
        assert wait_for(lambda: get_run(engine, run_id)["status"] == "completed", timeout=60)
    finally:
        second.wait(timeout=30)
    counts = Counter((tmp_path / "killed.log").read_text().split())
    assert len(counts) == 10                                         # every unit ran
    assert sum(counts.values()) - 10 <= 1                            # at most one ran twice
    row = get_run(engine, run_id)
    assert row["attempts"] == 2 and row["worker_id"] is None
    engine.dispose()


# --- keep-awake (acceptance: acquired and released correctly, mocked, on every OS) ----------

class FakeMode:
    entered = 0
    exited = 0

    def __init__(self, active: bool = True):
        self.active = active
        self.active_method = type("M", (), {"name": "fake"})()

    def __enter__(self):
        FakeMode.entered += 1
        return self

    def __exit__(self, *exc):
        FakeMode.exited += 1
        return False


@pytest.mark.parametrize("system", ["Darwin", "Windows", "Linux"])
def test_keep_awake_is_held_while_runs_are_active_and_released_after(system, monkeypatch,
                                                                     synthetic):
    import platform

    from leadscraper.engine.keepawake import KeepAwake

    monkeypatch.setattr(platform, "system", lambda: system)
    FakeMode.entered = FakeMode.exited = 0
    lock = KeepAwake(enabled=True, factory=FakeMode)

    assert lock.acquire() and lock.acquire()          # two runs
    assert FakeMode.entered == 1 and lock.holders == 2
    lock.release()
    assert FakeMode.exited == 0                        # one run still active
    lock.release()
    assert FakeMode.exited == 1 and not lock.active
    lock.release()                                     # an extra release is harmless
    assert FakeMode.exited == 1

    held_during: list[bool] = []
    HOOKS["after_unit"] = lambda session, unit, index: held_during.append(lock.active)
    config = compiled(synthetic_config(f"awake-{system.lower()}", units=units(2)))
    execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                       keep_awake=lock, discovery_workers=1))
    assert held_during == [True, True]
    assert FakeMode.entered == 2 and FakeMode.exited == 2 and lock.holders == 0


def test_keep_awake_that_fails_never_fails_the_run(synthetic):
    from leadscraper.engine.keepawake import KeepAwake

    def broken():
        raise RuntimeError("no D-Bus session")

    lock = KeepAwake(enabled=True, factory=broken)
    config = compiled(synthetic_config("awake-broken", units=units(2)))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 keep_awake=lock, discovery_workers=1))
    assert outcome.status == "completed" and lock.holders == 0


def test_keep_awake_toggle_is_respected(synthetic, monkeypatch):
    from leadscraper.engine.keepawake import KeepAwake

    FakeMode.entered = 0
    monkeypatch.setenv("LEADSCRAPER_KEEP_AWAKE", "0")
    lock = KeepAwake(factory=FakeMode)
    config = compiled(synthetic_config("awake-off", units=units(1)))
    execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                       keep_awake=lock))
    assert FakeMode.entered == 0


def test_sleep_is_detected_from_a_wall_clock_jump():
    from leadscraper.engine.keepawake import SleepWatch

    class Clock:
        mono = 0.0
        wall_time = 0.0

        def now(self):
            return self.mono

        def wall(self):
            return self.wall_time

    clock = Clock()
    watch = SleepWatch(clock, interval=5, threshold=30)
    clock.mono, clock.wall_time = 5, 5
    assert watch.tick() is None
    clock.mono, clock.wall_time = 10, 605              # slept ~10 minutes (macOS/Linux)
    assert watch.tick() == (595, 0)
    clock.mono, clock.wall_time = 615, 1205            # Windows: monotonic kept counting
    assert watch.tick() == (595, 600)


def test_after_a_sleep_the_run_revalidates_and_does_not_bill_the_gap(synthetic, monkeypatch):
    from leadscraper.engine import runner as runner_mod

    revalidated = []

    class OneSleep:
        def __init__(self, clock, *, interval, threshold=30.0):
            self.fired = False

        def tick(self):
            if self.fired:
                return None
            self.fired = True
            return (600.0, 120.0)

    monkeypatch.setattr(runner_mod, "SleepWatch", OneSleep)
    monkeypatch.setattr(SyntheticSession, "revalidate",
                        lambda self: revalidated.append(True), raising=False)
    HOOKS["after_unit"] = lambda session, unit, index: time.sleep(0.25)   # let it tick
    seen: list = []
    config = compiled(synthetic_config("slept", units=units(2, seconds=200.0)))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 sinks=[seen.append], discovery_workers=1,
                                 heartbeat_seconds=0.05))
    assert revalidated == [True]
    assert any(e.type == "sleep_detected" and e.gap_seconds == 600.0 for e in seen)
    assert outcome.elapsed_seconds == pytest.approx(400.0 - 120.0)


# --- events ------------------------------------------------------------------------------

def test_events_are_typed_persisted_and_replay_exactly(synthetic):
    live: list = []
    config = compiled(synthetic_config("events", units=units(3)))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 sinks=[live.append], discovery_workers=1))
    engine = app_engine()
    replay = ev.read_events(engine, outcome.run_id)
    tail = ev.read_events(engine, outcome.run_id, after=replay[-3].seq)
    engine.dispose()
    assert [e.seq for e in replay] == list(range(1, len(replay) + 1))
    assert [(e.seq, e.type) for e in replay] == [(e.seq, e.type) for e in live]
    for a, b in zip(live, replay):
        assert a.model_dump(exclude={"at"}) == b.model_dump(exclude={"at"})
    kinds = Counter(e.type for e in replay)
    assert kinds["unit_started"] == kinds["unit_finished"] == 3
    assert {"run_state", "plan", "progress", "activity", "run_finished"} <= set(kinds)
    assert replay[1].type == "plan" and replay[1].units_total == 3
    # Catching up after a given seq, as a reconnecting SSE client will.
    assert [e.seq for e in tail] == [replay[-2].seq, replay[-1].seq]
    assert all(ev.describe(e) for e in replay)
    assert "unit_finished" in json.dumps(ev.event_json_schema())


def test_a_crashing_unit_is_contained_and_reported(synthetic, monkeypatch):
    def explode(self, unit):
        raise RuntimeError("synthetic explosion")
    monkeypatch.setattr(SyntheticSession, "run_unit", explode)
    seen: list = []
    config = compiled(synthetic_config("failing", units=units(2)))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 sinks=[seen.append]))
    finished = [e for e in seen if e.type == "unit_finished"]
    assert len(finished) == 2 and finished[0].status == "failed"
    assert "explosion" in finished[0].error
    assert outcome.status == "completed"               # contained


# --- local_business through the engine -----------------------------------------------------

CLOCK = FakeClock()


def clocked_source(name: str, seconds: float, leads: int, host: str):
    def discover(self, locality, category) -> Iterator[Lead]:
        for i in range(leads):
            CLOCK.sleep(seconds / leads)
            digits = sum(ord(c) for c in f"{name}{locality.name}{category.name}") * 100 + i
            yield self.make_lead(business_name=f"{name} {locality.name} {category.name} {i}",
                                 locality=locality.name, category=category.name,
                                 phones=[f"+9198{digits % 10**8:08d}"])

    return type(f"Clocked_{name}", (Source,), {
        "name": name, "discover": discover, "rate_keys": lambda self: [host]})


def local_business_config(name: str, sources: dict) -> Config:
    return compiled({
        "name": name, "project": name,
        "geography": {"country": "IN", "places": ["Mumbai"], "center": [19.07, 72.87],
                      "admin_area": "Maharashtra",
                      "areas": [{"name": a} for a in ("Andheri", "Bandra", "Dadar", "Powai")]},
        "lead_types": {"local_business": {
            "categories": [{"name": "Caterers"}, {"name": "Decorators"}],
            "sources": {n: {"enabled": True, "min_delay": 3, "max_delay": 6} for n in sources}}},
        "fields": {"include": ["phones"]},
    })


@pytest.fixture
def clocked_sources(monkeypatch):
    from leadscraper.sources import registry as source_registry

    def install(registry_: dict) -> dict:
        monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", registry_)
        monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
        monkeypatch.setattr(source_registry, "DISCOVERY_SOURCES", registry_)
        return registry_
    return install


def test_local_business_runs_through_the_engine_within_a_budget(clocked_sources):
    registry_ = clocked_sources({
        "google_maps": clocked_source("google_maps", 150, 12, "www.google.com"),
        "justdial": clocked_source("justdial", 20, 1, "www.justdial.com"),
        "google_search": clocked_source("google_search", 60, 1, "www.google.com")})
    started: list[str] = []
    config = local_business_config("lb-budget", registry_)
    outcome = execute(RunRequest(config=config, install_signals=False, clock=CLOCK,
                                 time_budget_minutes=15, discovery_workers=1,
                                 sinks=[lambda e: started.append(e.unit)
                                        if e.type == "unit_started" else None]))
    assert outcome.status == "completed"
    assert outcome.elapsed_seconds <= 15 * 60 + 90
    assert started[0].startswith("local_business|google_maps|")     # Maps first, cold
    assert outcome.skipped
    pipeline = outcome.contexts["local_business"].pipeline
    assert pipeline.stats.new_leads > 0
    assert outcome.results["local_business"].units_done == len(started)


def test_preview_samples_one_area_and_category_into_a_scratch_project(clocked_sources):
    from leadscraper.db.store import LeadStore, store_target
    from leadscraper.engine.preview import preview

    registry_ = clocked_sources({
        "google_maps": clocked_source("google_maps", 40, 6, "www.google.com"),
        "justdial": clocked_source("justdial", 10, 2, "www.justdial.com"),
        "google_search": clocked_source("google_search", 20, 1, "www.google.com")})
    config = local_business_config("lb-preview", registry_)

    result = preview(config, clock=CLOCK)
    assert result.status == "completed"
    assert result.area == "Andheri" and result.category == "Caterers"
    assert result.sources == ["google_maps", "justdial"]          # top two, Maps first
    assert result.records and result.records[0]["Business Name"]
    assert result.estimate["units_queued"] == 4 * 2 * 3
    assert result.calibration is not None and "calibrated" in result.estimate
    assert result.seconds <= 120 + 15

    target, kwargs = store_target(config)
    real = LeadStore(target, **kwargs)
    assert real.count() == 0                                       # the real project is untouched
    real.close()
    again = preview(config, clock=CLOCK)                           # starts from empty again
    assert len(again.records) == len(result.records)


# --- schema migration ----------------------------------------------------------------------

def test_migration_0002_upgrades_a_database_with_runs_in_it(tmp_path):
    from alembic import command
    from sqlalchemy import text

    from leadscraper.db.engine import alembic_config, init_schema, make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0001")
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) "
                          "VALUES (1, 'default', '2026-10-01')"))
        conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug, description, "
                          "created_at, updated_at) VALUES (1, 1, 'p', 'p', '', '2026-10-01', "
                          "'2026-10-01')"))
        conn.execute(text("INSERT INTO runs (id, workspace_id, project_id, status, "
                          "config_snapshot, kind, stats, error, created_at) VALUES "
                          "(1, 1, 1, 'completed', '{}', 'run', '{}', '', '2026-10-01')"))
        conn.execute(text("INSERT INTO unit_stats (workspace_id, source, area, category, runs, "
                          "total_seconds, total_new_leads, last_run) VALUES "
                          "(1, 'google_maps', 'A', 'C', 2, 100.0, 10, 0)"))
    init_schema(engine)                     # how the application upgrades a database
    run = get_run(engine, 1)
    assert run["request"] == {} and run["attempts"] == 0 and run["active_seconds"] == 0
    assert UnitStatsReader(engine, 1).unit("google_maps", "A", "C").runs == 2
    engine.dispose()


# --- CLI -------------------------------------------------------------------------------------

def test_cli_estimate_and_runs_commands(synthetic, capsys):
    from leadscraper.cli import main

    config = compiled(synthetic_config("cli", units=units(3)))
    outcome = execute(RunRequest(config=config, install_signals=False, clock=FakeClock(),
                                 discovery_workers=1))
    capsys.readouterr()
    assert main(["runs", "list"]) == 0
    assert f"{outcome.run_id:>5}  completed" in capsys.readouterr().out
    assert main(["runs", "show", str(outcome.run_id)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["units"] == {"done": 3} and shown["summary"]["units_done"] == 3
    assert main(["runs", "events", str(outcome.run_id)]) == 0
    assert "run finished: completed" in capsys.readouterr().out
    assert main(["runs", "pause", str(outcome.run_id)]) == 2      # completed: nothing to pause
    assert "cannot pause" in capsys.readouterr().err

    assert main(["estimate", "--config", str(MUMBAI), "--time-budget", "15", "--json"]) == 0
    estimate = json.loads(capsys.readouterr().out)
    assert estimate["budget"]["budget_seconds"] == 900 and estimate["units_queued"] == 648


def test_run_dry_run_prints_the_estimate(capsys):
    from leadscraper.cli.run_cmd import main as run_main

    assert run_main(["--config", str(MUMBAI), "--dry-run", "--time-budget", "30", "--depth",
                     "quick", "-q"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Time budget  : 30 min" in out and "Full run" in out


# --- regressions from the Phase 2 review --------------------------------------------------

def test_a_cancel_overrides_an_earlier_pause_or_shutdown():
    control = RunControl()
    assert control.request("pause")
    assert not control.request("pause")
    assert control.request("cancel")                    # upgrades the pause
    assert control.final_status() == "cancelled"
    assert not control.request("shutdown")              # nothing overrides a cancel
    with control._lock:                                 # a signal handler on the same thread
        assert not control.request("pause")             # re-entrant: no deadlock


def test_a_cancel_recorded_while_the_run_finishes_wins(synthetic, tmp_path):
    def cancel_then_pause(session, unit, index):
        if index == 2:
            engine = session.store.engine
            request_control(engine, session.context.run_id, "pause")
            session.control.request("pause")             # the heartbeat saw the pause...
            update_run(engine, session.context.run_id, status="stopping")   # ...then a cancel
    HOOKS["after_unit"] = cancel_then_pause
    outcome = execute(RunRequest(config=lifecycle_config("late-cancel", tmp_path),
                                 install_signals=False, discovery_workers=1,
                                 heartbeat_seconds=60))
    assert outcome.status == "cancelled"
    engine = app_engine()
    assert get_run(engine, outcome.run_id)["status"] == "cancelled"
    engine.dispose()


def test_resuming_a_run_a_live_executor_holds_is_refused(synthetic, tmp_path):
    from leadscraper.engine.runner import RunBusyError

    HOOKS["after_unit"] = request_after("pause", 1)
    outcome = execute(RunRequest(config=lifecycle_config("busy", tmp_path, count=3),
                                 install_signals=False, discovery_workers=1))
    engine = app_engine()
    from datetime import datetime, timezone
    update_run(engine, outcome.run_id, status="running", worker_id="worker:alive",
               heartbeat_at=datetime.now(timezone.utc))
    HOOKS.clear()
    with pytest.raises(RunBusyError):
        execute(request_from_row(get_run(engine, outcome.run_id)))
    assert get_run(engine, outcome.run_id)["worker_id"] == "worker:alive"
    engine.dispose()


def test_an_executor_that_lost_its_run_stops_without_writing(synthetic, tmp_path):
    def taken_over(session, unit, index):
        if index == 2:
            update_run(session.store.engine, session.context.run_id, worker_id="worker:other")
            time.sleep(0.2)                                # the heartbeat notices
    HOOKS["after_unit"] = taken_over
    outcome = execute(RunRequest(config=lifecycle_config("taken", tmp_path),
                                 install_signals=False, discovery_workers=1))
    assert outcome.status == "lost"
    engine = app_engine()
    row = get_run(engine, outcome.run_id)
    engine.dispose()
    assert row["status"] == "running" and row["worker_id"] == "worker:other"


def test_an_engine_error_fails_the_run_instead_of_completing_it(synthetic, monkeypatch):
    from leadscraper.engine import runner as runner_mod

    def broken(*args, **kwargs):
        raise RuntimeError("database went away")
    monkeypatch.setattr(runner_mod, "plan_units", broken)
    outcome = execute(RunRequest(config=compiled(synthetic_config("broken-db", units=units(2))),
                                 install_signals=False, clock=FakeClock()))
    assert outcome.status == "failed" and "database went away" in outcome.error
    engine = app_engine()
    assert get_run(engine, outcome.run_id)["status"] == "failed"
    engine.dispose()


def test_a_hung_unit_is_abandoned_after_the_grace_period():
    release = threading.Event()
    stop = threading.Event()

    def work(item):
        stop.set()
        release.wait(10)                                    # hangs until released
        return item

    queue = HostQueue([("a", 0), ("b", 0)], lambda item: item[0])
    started = time.monotonic()
    abandoned = run_across_hosts(queue.next, queue.hosts_of, work,
                                 lambda *_: None, workers=1, should_stop=stop.is_set,
                                 grace_seconds=0.2, poll_seconds=0.05)
    release.set()
    assert abandoned == [("a", 0)]
    assert time.monotonic() - started < 2


def test_resume_reruns_units_that_were_cut_or_skipped(synthetic, tmp_path):
    HOOKS["after_unit"] = request_after("pause", 3)
    paused = execute(RunRequest(config=lifecycle_config("rerun", tmp_path, count=6),
                                install_signals=False, discovery_workers=1))
    engine = app_engine()
    done = [r for r in unit_rows(engine, paused.run_id) if r["status"] == "done"]
    from leadscraper.db.runs import mark_unit
    mark_unit(engine, paused.run_id, done[0]["unit_key"], status="partial")
    HOOKS.clear()
    request_control(engine, paused.run_id, "resume")
    execute(request_from_row(get_run(engine, paused.run_id)))
    engine.dispose()
    counts = executions(tmp_path, "rerun")
    assert counts[done[0]["unit_key"]] == 2 and len(counts) == 6


def test_ai_spend_is_reported_after_the_ai_passes(clocked_sources, monkeypatch):
    from leadscraper.ai import AIAssistant
    from leadscraper.leadtypes.local_business.session import LocalBusinessSession

    registry_ = clocked_sources({"google_maps": clocked_source("google_maps", 10, 1,
                                                               "www.google.com")})
    config = local_business_config("lb-ai", registry_)
    config.set("ai.enabled", True)
    seen: list = []
    original = LocalBusinessSession.__init__

    def with_spend(self, context):
        original(self, context)
        self.pipeline.ai = AIAssistant(config, self.pipeline.failures, backend=None)
        self.pipeline.ai.enabled = True
        self.pipeline.ai.stats.cost_usd, self.pipeline.ai.stats.calls = 0.0123, 2

    monkeypatch.setattr(LocalBusinessSession, "__init__", with_spend)
    execute(RunRequest(config=config, install_signals=False, clock=CLOCK,
                       discovery_workers=1, sinks=[seen.append]))
    spend = [e for e in seen if e.type == "ai_spend"]
    assert spend and spend[0].cost_usd == 0.0123 and spend[0].calls == 2


def test_maps_paces_apart_from_search_by_default_but_the_legacy_preset_is_unchanged():
    from leadscraper.leadtypes.local_business.session import LocalBusinessSession
    from leadscraper.engine.runner import LeadTypeContext

    def maps_hosts(config):
        session = LocalBusinessSession(LeadTypeContext(config=config, store=None, run_id=None,
                                                       dry_run=True))
        unit = next(u for u in session.units() if u.source == "google_maps")
        hosts = session.hosts(unit)
        session.close()
        return hosts

    fresh = compiled({"name": "Austin", "project": "austin-hosts",
                      "geography": {"country": "US", "places": ["Austin"],
                                    "center": [30.27, -97.74], "admin_area": "Texas"},
                      "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}}})
    assert "www.google.com" not in maps_hosts(fresh)              # runs beside SERP sources
    assert "www.google.com" in maps_hosts(Config.load(MUMBAI))     # the original pacing
