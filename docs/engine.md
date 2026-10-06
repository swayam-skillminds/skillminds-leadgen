# The engine: planning, time budget, jobs

How a run is planned, scheduled, executed, paused, resumed and reported
(Phase 2). Code: `leadscraper/engine/`.

## A run, end to end

```
config ──► depth preset (each lead type's apply_depth)
       ──► sessions: every enabled lead type's work units
       ──► estimate: units, p50/p90, what fits the budget, AI cost   (runs.estimate, "plan" event)
       ──► work_units rows, all "pending"                            (the checkpoint)
       ──► discovery: scheduler + dispatcher                          (unit_started / unit_finished / progress)
       ──► post-discovery: enrichment, AI                             (unless stopping)
       ──► finalise: rescore, CSV, Sheets                             (always)
       ──► runs.status + stats.engine summary                         ("run_finished")
```

## Work units and the scheduler (`scheduler.py`, `costs.py`)

A work unit is `(lead type, source, area, category)`; `WorkUnit.key` is
`lead_type|source|area|category`. Scope `run` units (sitemaps) cover the whole
region, `locality` units one area, `combo` units one area × category.

Each unit has an expected cost (`UnitCost`: seconds, new organisations, spread):

1. **Cold start:** the lead type's `prior()`. For local_business that's the
   requests a unit makes at the run's depth × the configured polite delays,
   with yields that reflect history (Maps produced ~95% of the original leads),
   so the cold order is Maps > directories > SERP.
2. **Learned:** `unit_stats` (per workspace): every unit's seconds, squared
   seconds and new organisations, per `(source, area, category)`. Shrunk
   towards the prior while there is little history (`PRIOR_WEIGHT`); a unit
   never run borrows its source's average over other areas.
3. **Within the run:** every finished unit is observed immediately, so the
   queue re-sorts on what this run is seeing.

`priority = expected new / expected seconds`.

| | Without a time budget | With a time budget |
|---|---|---|
| Order | plan order (exactly the original scraper's) | priority, best first |
| Skips | none | a unit whose expected seconds exceed what's left is skipped, and the next one that fits is taken |

In both modes, `run`-scope units go first. The budget is
`time_budget_minutes` (or `--time-budget`). From it the engine reserves:

- **a tail** (`runtime.tail_seconds`, 90 s) for flush, scoring and export;
- **a share for enrichment** (`runtime.enrichment_share`, 30%, capped at the
  enrichment estimate) when any enricher is active.

A unit that overruns its estimate into the tail is cut (status `partial`) once
half of the tail is spent, so a run finishes within budget + tail. A unit that
hangs without yielding is abandoned after a grace period once a stop or the
deadline arrives (`runtime.stop_grace_seconds`, 120 s, or half the tail on a
budgeted run); it stays `running` in the checkpoint and runs again on resume.
Units that don't run are listed with their reason in
`stats.engine.skipped_units`, in `work_units.skip_reason`, as `unit_skipped`
events and in the CLI report.

## Parallel across hosts, never within one (`parallel.py`)

`run_across_hosts` runs units on `runtime.discovery_workers` threads (default
3). A unit's hosts come from its source (`Source.hosts()`, which defaults to
its rate-limiter keys), and two units that share a host never run at the same
time. Maps, Google Search, Facebook and LinkedIn discovery all go through
google.com, so they take turns; Justdial, Sulekha, IndiaMART, WedMeGood,
WeddingWire and the stay platforms run beside them. The dispatcher is the
calling thread: checkpoints, events and CSV/Sheets flushes happen there, in
order. With one worker everything runs inline, in plan order.

Enrichment uses the same dispatcher. Each vendor site is its own host, so
`runtime.enrich_workers` (default 8) sites are fetched at once. Instagram is
one host, so its profiles go one at a time. A lead an enricher has been
through is marked (`enrichment_marks`) and skipped on later runs, unless the
run uses `--force-enrich` / `runtime.force_enrich`. A lead the enricher was
turned away from (`blocked`, `fetch_failed`, `login_wall`, …) is not marked,
so the next run tries it again.

## Depth (`leadtypes/*/apply_depth`)

| Depth | Maps results/query | Scroll rounds | Pages | Enrichment | AI names |
|---|---|---|---|---|---|
| `quick` | ≤ 10 | ≤ 3 | 1 | off | off |
| `balanced` | the config's own settings (unchanged) | | | on | on |
| `deep` | ≥ 60 | ≥ 20 | ≥ 5 | on, with Instagram | on |

Learned stats are kept per depth (`google_maps@quick`, plain `google_maps` for
balanced), because a quick unit costs a fraction of a deep one.

## The estimator (`estimator.py`)

It simulates the dispatcher with expected durations: same order, same "one
unit per host" rule, same worker count, same budget check. It returns:

- units queued, per lead type and source (seconds of work, expected new);
- the basis of each estimate (prior / source / learned);
- discovery, post-discovery and total **p50 / p90**:
  - p50 is the simulated wall-clock time;
  - p90 adds the units' spread, plus a correlated term for priors (a wrong
    prior is wrong for every unit of its source);
- with a budget: units that fit, units skipped (by source, and the first 200
  by key), expected new organisations, whether enrichment completes, and the
  p50/p90 of the budgeted run;
- the AI cost, when AI is on (rough, list-price equivalent).

During a run each `progress` event carries a refined ETA. The cost model has
by then learned from every finished unit, and the parallelism actually
achieved is measured.

## Lifecycle (`db/runs.py`, `control.py`, `worker.py`)

```
queued ──► running ──► completed | failed
              │ ├──► pausing ──► paused ──(resume)──► queued
              │ └──► stopping ──► cancelled
              └── (executor died: stale heartbeat) ──► resumed by a worker
```

- **One stop path.** Pause, cancel, the deadline, Ctrl-C and a worker shutting
  down all mean: start nothing new, let the work in flight finish, then flush.
  Only the final status differs: `paused`, `cancelled`, `completed`, or back to
  `queued` for a worker shutdown. A cancel overrides an earlier pause or
  shutdown, and a cancel recorded in the database while the run was finishing
  still wins.
- **Requests through the database.** `runs pause|cancel` set `pausing` /
  `stopping`; the runner's heartbeat (`runtime.heartbeat_seconds`, 5 s) sees
  it. `runs resume` puts a paused or failed run back to `queued`.
- **Ownership.** Every execution claims the run with one conditional UPDATE
  (`claim_run`). Heartbeats and the final write only touch the run while
  `worker_id` is still this executor. An executor that finds it lost the run
  (another worker recovered it after a long stall) stops without writing.
- **Checkpoint.** Every planned unit is a `work_units` row. It is `pending`,
  then `running`, then `done` / `partial` / `failed` / `skipped`. A resumed
  run skips `done` and `failed` units and re-runs everything else: `running`
  (in flight when the executor died), `partial` (cut mid-way) and `skipped`
  (the budget had run out). `runs.checkpoint.phase` records whether discovery
  had finished. `runs.active_seconds` carries the budget's clock across
  resumes, excluding finalise time, and `runs.attempts` counts executions.
- **The worker** registers in `workers` and heartbeats there. It first
  recovers runs a dead worker left behind:
  - an active run with a stale heartbeat (`--stale-after`, 60 s) is resumed;
  - one that was `pausing` becomes `paused`;
  - one that was `stopping` becomes `cancelled`.

  Each of these is a conditional update. It then claims `queued` runs, oldest
  first, so several workers can share a database. Stop it with `worker stop`
  (a database flag; no signals needed, so it works the same on Windows),
  Ctrl-C, SIGTERM or Ctrl-Break.
- **CLI runs** execute in-process (`cli:<host>:<pid>`). Ctrl-C pauses; a second
  Ctrl-C stops at once. A crashed CLI run is not taken over by a worker; resume
  it with `runs resume` (it goes to a worker) or `run --resume` (in-process).
  `run --resume` refuses a run another executor holds with a live heartbeat.
- **An engine error** (the database unreachable mid-run, say) marks the run
  `failed` with the error, and it stays resumable. Bookkeeping that fails for
  a single unit is logged and costs that unit's checkpoint, not the run.

## Events (`events.py`)

Every event is a pydantic model with a `type` discriminator (JSON Schema:
`event_json_schema()`), numbered per run (`seq`) and stored in `run_events` by
a writer thread, so no unit or heartbeat waits on the database to publish.
Replay with `read_events(engine, run_id, after=seq)`; the CLI's view is
`leadscraper runs events <id> [--follow] [--json]`.

| type | carries |
|---|---|
| `run_state` | status, reason |
| `plan` | units total / already done, breakdown per lead type and source, estimate |
| `unit_started` | unit, lead type, source, area, category, expected seconds |
| `unit_finished` | status, found, new, merged, seconds, error |
| `unit_skipped` | reason |
| `progress` | units done/total/skipped, new, merged, elapsed, ETA p50/p90, budget left |
| `activity` | phase, text ("website enrichment: 40/310") |
| `failure` | source, reason code, detail, URL |
| `ai_spend` | cost, cap, calls (reserved; Phase 5 emits it from the usage ledger) |
| `sleep_detected` | gap seconds |
| `run_finished` | final status, summary (skipped units included), error |

## Keep-awake (`keepawake.py`)

While at least one run is active, the process holds one `wakepy` "keep
running" lock. It works on macOS (an IOKit assertion, as `caffeinate`),
Windows (`SetThreadExecutionState`) and Linux (a D-Bus inhibitor, when a
session bus exists). The lock is released when the last run ends. If no
method works, the run logs a warning and carries on.

It's on by default. To turn it off, set `LEADSCRAPER_KEEP_AWAKE=0`, pass
`worker --no-keep-awake`, or set `runtime.keep_awake: false`.

**Sleep detection:**

- The heartbeat expects a tick every few seconds. A wall-clock jump of 30 s
  or more beyond that means the machine slept (or the process was suspended).
- The run then re-validates: each worker thread relaunches its browser on
  next use, and HTTP sessions are recreated.
- The part of the gap the budget's monotonic clock counted is discounted.
  That's none on macOS and Linux, where monotonic time stops during sleep;
  on Windows it keeps counting.
- A `sleep_detected` event records it.

**Honest limits:**

- A sleeping machine runs nothing. The lock only prevents *idle* sleep.
- On macOS, closing the lid on battery forces sleep regardless of any lock;
  so does an explicit Sleep, on every OS.
- For truly unattended runs, use server mode (Phase 3): an always-on machine
  running the worker.

## Preview (`preview.py`)

One area × one category × the top two discovery sources of each lead type,
at `quick` depth, inside a 2-minute budget (15 s tail). It runs in a scratch
project, `<project> (preview)`, which is emptied first. It returns:

- the sample records, best first;
- the full-run estimate;
- the preview's actual/expected ratio, which scales whatever part of the
  estimate still rests on cold priors.

The preview's units also land in `unit_stats`, under the `@quick` depth key.
