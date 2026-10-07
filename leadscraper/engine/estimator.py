"""Before a run: how long, what fits the budget, what it may cost (Phase 2.3).

The estimate simulates the scheduler itself -- the same unit order, the same
"one unit per host at a time" rule, the same number of parallel workers and
the same "never start what can't finish" budget check -- over each unit's
expected cost (``costs.CostModel``). So the answer to "what fits in 15
minutes" is the scheduler's own decision, made in advance.

Ranges: p50 is the simulated wall-clock time. p90 adds the units' spread
(observed variance where history exists) and, for cold-start priors, a
correlated term -- a prior that is off is off for every unit of its source,
so those errors add up rather than cancel.

During a run ``LiveEta`` refines the remaining time from what the run is
actually observing: the cost model learns from every finished unit, and the
parallelism achieved is measured.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .costs import CostModel, UnitCost
from .scheduler import SKIP_BUDGET, Planned

#: z for the 90th percentile of a normal distribution.
Z90 = 1.2816
#: Share of a prior-based total treated as one correlated error.
PRIOR_SYSTEMATIC = 0.3
#: Seconds to rescore and write output, outside a budget's tail.
FINALISE_SECONDS = 15.0


@dataclass
class _Sim:
    makespan: float
    fit: list[Planned]
    skipped: list[Planned]


def order_units(planned: list[Planned], costs: CostModel, order: str) -> list[Planned]:
    if order != "priority":
        return sorted(planned, key=lambda p: (p.unit.scope != "run", p.index))
    return sorted(planned, key=lambda p: (p.unit.scope != "run",
                                          -costs.cost(p.unit).priority, p.index))


def simulate(planned: list[Planned], costs: CostModel, *, workers: int,
             window: float | None) -> _Sim:
    """Discrete-event replay of the dispatcher with expected durations."""
    pending = list(planned)
    running: list[tuple[float, Planned]] = []
    fit: list[Planned] = []
    skipped: list[Planned] = []
    now = 0.0
    workers = max(1, workers)
    while pending or running:
        while len(running) < workers and pending:
            busy = frozenset().union(*(p.hosts for _, p in running)) if running else frozenset()
            choice = next((p for p in pending if not (p.hosts & busy)), None)
            if choice is None:
                break
            pending.remove(choice)
            seconds = costs.cost(choice.unit).seconds
            if window is not None and now + seconds > window:
                skipped.append(choice)      # never start what can't finish
                continue
            running.append((now + seconds, choice))
            fit.append(choice)
        if not running:
            break
        running.sort(key=lambda item: item[0])
        now, _ = running.pop(0)
    return _Sim(makespan=now, fit=fit, skipped=skipped)


def _spread(units: Iterable[Planned], costs: CostModel) -> tuple[float, float]:
    """(sum of expected seconds, sd of that sum)."""
    total = var = prior_total = 0.0
    for planned in units:
        cost = costs.cost(planned.unit)
        total += cost.seconds
        var += cost.sd ** 2
        if cost.basis == "prior":
            prior_total += cost.seconds
    var += (PRIOR_SYSTEMATIC * prior_total) ** 2
    return total, math.sqrt(var)


def _range(makespan: float, units: list[Planned], costs: CostModel) -> tuple[float, float]:
    total, sd = _spread(units, costs)
    if total <= 0:
        return makespan, makespan
    return makespan, makespan * (total + Z90 * sd) / total


def _new(units: Iterable[Planned], costs: CostModel) -> float:
    return sum(costs.cost(p.unit).new for p in units)


def _post(sessions: dict[str, Any], units: list[Planned], costs: CostModel) -> tuple[float, float]:
    mean = var = 0.0
    by_session: dict[str, float] = defaultdict(float)
    for planned in units:
        by_session[planned.session.lead_type] += costs.cost(planned.unit).new
    everyone = sum(by_session.values())
    for key, session in sessions.items():
        # A session that assesses every lead type's organisations (an audit)
        # is sized by the whole run's expected new organisations.
        expected = everyone if getattr(session, "post_scope", "own") == "run" \
            else by_session.get(key, 0.0)
        try:
            m, sd = session.post_estimate(expected)
        except Exception:
            m, sd = 0.0, 0.0
        mean += m
        var += sd ** 2
    return mean, math.sqrt(var)


def estimate(planned: list[Planned], costs: CostModel, sessions: dict[str, Any], *,
             workers: int = 1, budget_seconds: float | None = None, tail_seconds: float = 90.0,
             enrichment_share: float = 0.3, order: str | None = None,
             done_units: int = 0) -> dict[str, Any]:
    """The pre-run answer: units, p50/p90, what fits the budget, AI cost."""
    order = order or ("priority" if budget_seconds else "plan")
    ordered = order_units(planned, costs, order)

    breakdown: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    basis: dict[str, int] = defaultdict(int)
    for p in ordered:
        cost: UnitCost = costs.cost(p.unit)
        row = breakdown[p.unit.lead_type].setdefault(p.unit.source,
                                                     {"units": 0, "seconds": 0.0, "new": 0.0})
        row["units"] += 1
        row["seconds"] += cost.seconds
        row["new"] += cost.new
        basis[cost.basis] += 1
    for sources in breakdown.values():
        for row in sources.values():
            row["seconds"] = round(row["seconds"], 1)
            row["new"] = round(row["new"], 1)

    full = simulate(ordered, costs, workers=workers, window=None)
    disc_p50, disc_p90 = _range(full.makespan, full.fit, costs)
    post_mean, post_sd = _post(sessions, full.fit, costs)
    expected_new = _new(full.fit, costs)
    result: dict[str, Any] = {
        "units_total": len(planned) + done_units,
        "units_queued": len(planned),
        "units_already_done": done_units,
        "workers": workers,
        "order": order,
        "breakdown": {k: dict(v) for k, v in breakdown.items()},
        "basis": dict(basis),
        "discovery": {"p50_seconds": round(disc_p50, 1), "p90_seconds": round(disc_p90, 1)},
        "post": {"p50_seconds": round(post_mean, 1),
                 "p90_seconds": round(post_mean + Z90 * post_sd, 1)},
        "finalise_seconds": FINALISE_SECONDS,
        "p50_seconds": round(disc_p50 + post_mean + FINALISE_SECONDS, 1),
        "p90_seconds": round(disc_p90 + post_mean + Z90 * post_sd + FINALISE_SECONDS, 1),
        "expected_new": round(expected_new, 1),
        "budget": None,
        "ai": _ai(sessions, expected_new),
    }

    if budget_seconds:
        reserve = min(enrichment_share * budget_seconds, post_mean) if post_mean else 0.0
        window = max(0.0, budget_seconds - tail_seconds - reserve)
        sim = simulate(ordered, costs, workers=workers, window=window)
        fit_p50, fit_p90 = _range(sim.makespan, sim.fit, costs)
        fit_new = _new(sim.fit, costs)
        post_fit, post_fit_sd = _post(sessions, sim.fit, costs)
        post_room = max(0.0, budget_seconds - tail_seconds - fit_p50)
        by_source: dict[str, int] = defaultdict(int)
        for p in sim.skipped:
            by_source[f"{p.unit.lead_type}:{p.unit.source}"] += 1
        result["budget"] = {
            "budget_seconds": budget_seconds,
            "tail_seconds": tail_seconds,
            "reserved_for_enrichment_seconds": round(reserve, 1),
            "units_fit": len(sim.fit),
            "units_skipped": len(sim.skipped),
            "skipped_by_source": dict(by_source),
            "skipped": [{"unit": p.key, "reason": SKIP_BUDGET} for p in sim.skipped[:200]],
            "expected_new": round(fit_new, 1),
            "discovery_p50_seconds": round(fit_p50, 1),
            "discovery_p90_seconds": round(fit_p90, 1),
            "enrichment_seconds_available": round(post_room, 1),
            "enrichment_completes": post_fit <= post_room,
            "p50_seconds": round(min(budget_seconds,
                                     fit_p50 + min(post_fit, post_room) + tail_seconds), 1),
            "p90_seconds": round(min(budget_seconds + tail_seconds,
                                     fit_p90 + min(post_fit + Z90 * post_fit_sd, post_room)
                                     + tail_seconds), 1),
            "ai": _ai(sessions, fit_new),
        }
    return result


def _ai(sessions: dict[str, Any], expected_new: float) -> dict[str, Any] | None:
    parts = {}
    for key, session in sessions.items():
        try:
            part = session.ai_estimate(expected_new)
        except Exception:
            part = None
        if part:
            parts[key] = part
    if not parts:
        return None
    # One run, one AI budget (Phase 5): the lead types share the cap.
    caps = [p["cap_usd"] for p in parts.values() if p.get("cap_usd") is not None]
    total = sum(p.get("cost_usd", 0.0) for p in parts.values())
    return {"cost_usd": round(min(total, caps[0]) if caps else total, 3),
            "cap_usd": caps[0] if caps else None,
            "calls": sum(p.get("calls", 0) for p in parts.values()), "by_lead_type": parts}


class LiveEta:
    """The remaining time, refined from what the run observes.

    The queued units' costs come from the run's ``CostModel``, which has
    already observed every finished unit (so a source running slower than
    expected raises the estimate of its remaining units). On top of that this
    measures the parallelism the run actually achieves: seconds of unit work
    done per second of wall-clock discovery.
    """

    def __init__(self, workers: int, post_p50: float, post_p90: float, finalise: float):
        self.workers = max(1, workers)
        self.post_p50 = post_p50
        self.post_p90 = post_p90
        self.finalise = finalise
        self.work_done = 0.0

    def observe(self, seconds: float) -> None:
        self.work_done += max(0.0, seconds)

    def parallelism(self, discovery_wall: float) -> float:
        if discovery_wall <= 0 or self.work_done <= 0:
            return 1.0
        return min(float(self.workers), max(1.0, self.work_done / discovery_wall))

    def remaining(self, queued_mean: float, queued_sd: float, discovery_wall: float,
                  *, budget_left: float | None = None) -> tuple[float, float]:
        parallel = self.parallelism(discovery_wall)
        p50 = queued_mean / parallel + self.post_p50 + self.finalise
        p90 = (queued_mean + Z90 * queued_sd) / parallel + self.post_p90 + self.finalise
        if budget_left is not None:
            p50 = min(p50, max(0.0, budget_left))
            p90 = min(p90, max(0.0, budget_left))
        return p50, p90


def describe_estimate(est: dict[str, Any]) -> str:
    """The estimate as the CLI prints it."""
    def minutes(seconds: float) -> str:
        return f"{seconds / 60:.0f}" if seconds >= 600 else f"{seconds / 60:.1f}"

    lines = [f"Units queued : {est['units_queued']}"
             + (f" ({est['units_already_done']} already done)" if est.get("units_already_done")
                else "") + f"; {est['workers']} parallel worker(s), one per host"]
    # One column for every lead type's "type/source" (a local_business-only plan
    # keeps its original layout: the source padded to 16).
    width = max([len(f"{lt}/") + 16 for lt in est["breakdown"]]
                + [len(f"{lt}/{src}") for lt, srcs in est["breakdown"].items() for src in srcs]
                or [0])
    for lead_type, sources in est["breakdown"].items():
        for source, row in sorted(sources.items(), key=lambda kv: -kv[1]["seconds"]):
            label = f"{lead_type}/{source}"
            lines.append(f"  {label:<{width}} {int(row['units']):>5} units"
                         f"  ~{minutes(row['seconds'])} min of work  ~{row['new']:.0f} new")
    basis = est.get("basis", {})
    lines.append("Basis        : " + ", ".join(f"{n} {kind}" for kind, n in basis.items())
                 + "  (learned = this unit's history; source = its source's; prior = cold start)")
    lines.append(f"Full run     : {minutes(est['p50_seconds'])}-{minutes(est['p90_seconds'])} min"
                 f" (p50-p90); discovery {minutes(est['discovery']['p50_seconds'])}-"
                 f"{minutes(est['discovery']['p90_seconds'])}, enrichment/AI "
                 f"{minutes(est['post']['p50_seconds'])}-{minutes(est['post']['p90_seconds'])};"
                 f" ~{est['expected_new']:.0f} new organisations")
    budget = est.get("budget")
    if budget:
        lines.append(f"Time budget  : {minutes(budget['budget_seconds'])} min"
                     f" (last {budget['tail_seconds']:.0f}s reserved for flush/score/export;"
                     f" {budget['reserved_for_enrichment_seconds'] / 60:.1f} min kept for"
                     " enrichment)")
        lines.append(f"  fits       : {budget['units_fit']} units, ~{budget['expected_new']:.0f}"
                     f" new; finishes in {minutes(budget['p50_seconds'])}-"
                     f"{minutes(budget['p90_seconds'])} min")
        if budget["units_skipped"]:
            by_source = ", ".join(f"{k} {v}" for k, v in budget["skipped_by_source"].items())
            lines.append(f"  skipped    : {budget['units_skipped']} units ({by_source})")
        if not budget["enrichment_completes"]:
            lines.append("  enrichment : will not finish inside the budget; it stops at the"
                         " deadline and resumes on the next run")
    ai = (budget or {}).get("ai") if budget else est.get("ai")
    if ai:
        lines.append(f"AI           : ~{ai['calls']} calls, ~${ai['cost_usd']:.2f}"
                     " (rough, list-price equivalent)")
    return "\n".join(lines)
