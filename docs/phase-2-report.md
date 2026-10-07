# Phase 2 report — engine: planning, estimation, time budget, resilient jobs

Date: 2026-10-06 · Branch: `phase-2/engine` · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 2

**Result:** Phase 2 is complete.
- The suite is green: 429 passed, 8 skipped (it was 381 passed, 6 skipped).
- The two new skips are opt-in Postgres tests. They pass against a real PostgreSQL 16.2 server, and so does the whole engine suite (section 12).
- The five acceptance checks pass (section 10), and so does the suite in CI on macOS, Windows and Linux (run 37563766674, after the merge to `main`).
- The characterisation baselines are untouched (`git diff c21c737 -- tests/characterisation/golden` is empty).

**About the branch.** `main` holds Phase 0 only; Phase 1 was never merged into it. A branch cut from `main` would have dropped Phase 1, which this phase builds on, so `phase-2/engine` starts from the tip of `phase-1/core-refactor`, which contains `main`. Merging is yours to do (section 15).

## 1. Work units and the scheduler (2.1)

**The engine now owns the unit loop.** Before, `local_business.run()` ran its whole pipeline in one call; now the engine drives it unit by unit.
- A lead type opens a **session** (`leadtypes/base.py`: `LeadTypeSession`) with:
  - `units()`;
  - `run_unit()`, which may run on a worker thread and never raises;
  - `hosts()`, `prior()`, `post_discovery(control)`, `finalize(complete=…)`.
- The engine plans, schedules, checkpoints and finalises.
- `local_business` became `LocalBusinessSession` over the existing `Pipeline`:
  - the Pipeline gained `discovery_units()`, `run_planned_unit()`, `post_discovery()` and a stop flag tied to the run's control;
  - `Pipeline.run()` still runs everything sequentially for direct use and the existing tests.
- A lead type that only has `run()` still works: it is one unit (`MonolithicSession`), which is how the Phase 1 isolation tests' `echo` lead type runs.

**Cost and priority** (`engine/costs.py`): `priority = expected new organisations / expected seconds`.

| Basis | Where it comes from |
|---|---|
| **Prior** (cold start) | `leadtypes/local_business/estimates.py`: requests per unit at the run's depth × the configured polite delays, plus fetch/render time. Yields reflect history (Maps produced ~95% of the original leads), so the cold order is **Maps > directories > SERP**, as Part 3.1 asks (`test_cold_start_priors_order_maps_then_directories_then_serp`). |
| **Learned** | `unit_stats` per `(source, area, category)`: seconds, squared seconds (new column, for variance), new organisations. Shrunk towards the prior while the history is short. |
| **Source** | A unit never run borrows its source's average over other areas and categories. |
| **This run** | Every finished unit is observed at once, so the queue re-sorts on what the run is seeing (`test_the_scheduler_prefers_yield_per_second_and_re_sorts_on_what_it_sees`). |

**Budget rules** (`engine/scheduler.py`, `engine/control.py`):
- Without a budget, units run in **plan order**: exactly the original order, so both legacy presets are unchanged.
- With a budget, units run by priority. A unit whose expected seconds exceed what's left is skipped and the next one that fits is taken (`test_the_scheduler_never_starts_a_unit_that_cannot_finish`).
- **The tail** (`runtime.tail_seconds`, 90 s) is reserved for flush, scoring and export.
- **A share for enrichment** (`runtime.enrichment_share`, 30%, capped at the enrichment estimate) is kept back when an enricher is active. Without it, discovery would eat the whole budget and leave leads with no contacts.
- **A unit that overruns** into the tail is cut (`partial`) once half the tail is spent, so the run ends within budget + tail.
- **A unit that hangs** without yielding is abandoned after a grace period once a stop or the deadline arrives.
- Whole-region units (sitemaps) go first in both modes.
- Skipped units, with reasons, are in `work_units.skip_reason`, `stats.engine.skipped_units`, `unit_skipped` events and the CLI report.

**Checkpoint after every unit:**
- Every planned unit is a `work_units` row: `pending → running → done / partial / failed / skipped`.
- A resumed run skips `done` and `failed` units and re-runs the rest.
- `runs.checkpoint.phase` records whether discovery had finished.
- `runs.active_seconds` carries the budget's clock across pauses and resumes. Finalise time is excluded, so it isn't billed again on every resume.

## 2. Depth presets (2.2)

Each lead type scales its own knobs (`LeadType.apply_depth`). For local_business:

| Depth | Maps results / query | Scroll rounds | Pages (SERP, directories, platforms) | Enrichment | AI names |
|---|---|---|---|---|---|
| quick | ≤ 10 | ≤ 3 | 1 (sitemaps ≤ 30 properties) | off | off |
| balanced | the config's settings, unchanged | | | on | on |
| deep | ≥ 60 | ≥ 20 | ≥ 5 | on, Instagram switched on | on |

- Depth-derived values are not recorded as CLI overrides; the snapshot records `depth` itself.
- Learned stats are kept per depth (`google_maps@quick`; balanced keeps the plain key that earlier runs and imports wrote).
- On the Mumbai preset the estimated time rises quick < balanced < deep (`test_depth_presets_scale_local_business_knobs`).

## 3. Estimator (2.3)

`engine/estimator.py` **simulates the dispatcher**: same order, same one-unit-per-host rule, same worker count, same "never start what can't finish" check. Its answer to "what fits in 15 minutes" is the scheduler's own decision, made in advance. It returns:
- units queued, per lead type and source, with the basis of each estimate;
- discovery, enrichment/AI and total **p50 / p90**:
  - p50 is the simulated wall-clock time;
  - p90 adds the units' spread, plus a correlated term for cold priors (a wrong prior is wrong for every unit of its source);
- with a budget: units that fit and are skipped (by source, and the first 200 by key), expected new organisations, whether enrichment completes, and the budgeted p50/p90;
- the AI cost when AI is on (rough, at list-price equivalent).

**Live ETA:** every `progress` event refines the remaining time. The cost model has already learned from the finished units, and the parallelism actually achieved is measured.

Shown by `--dry-run`, `leadscraper estimate --config … [--json]`, the `plan` event and `runs.estimate` (kept for estimate-vs-actual). Cold estimates of the presets:

| | Units | Full run (p50–p90) | 15-minute budget | 2-hour budget |
|---|---|---|---|---|
| Mumbai vendors | 648 | 13.5–18.7 h, ~1,260 new | 38 units, ~38 new | 347 units, ~378 new |
| Nashik venues | 160 | 4.6–6.4 h, ~966 new | 16 units, ~160 new; enrichment won't finish | 114 units, ~637 new |

The Mumbai full-run figure is a sanity check, not a validation: the original run took "12+ hours" (REBUILD-PROMPT) and produced 1,333 leads. Real per-unit timings don't exist yet. The `unit_stats` tables in both frozen stores are empty, so the priors can't be validated offline. Every real run now records them (section 15).

## 4. Speed (2.4, REBUILD Part 2)

| Item | Status |
|---|---|
| Parallelise across hosts, never within one | **Done.** `engine/parallel.py`: `runtime.discovery_workers` (3) threads. Units whose hosts overlap never run together (`test_work_runs_in_parallel_across_hosts_but_never_two_on_one_host`). Directories drop Google/DuckDuckGo from their hosts unless `serp_fallback` is on. |
| Browser context pool | **Already existed** (`net/browser.py`: thread-affine, `max_contexts`); parallel units now use it. It gained `revalidate()` for after a sleep. |
| Concurrent enrichment | **Done.** Each vendor site is its own host; `runtime.enrich_workers` (8) are fetched at once, Instagram one at a time. `WebsiteEnricher.pending_text` became thread-local. |
| Persistent dead-domain cache | **Already done** in Phase 0/1. |
| Skip already-enriched records unless forced | **Done.** Each enricher marks the leads it has been through (`enrichment_marks`); later runs skip them; `--force-enrich` redoes them. A lead the enricher was turned away from (`blocked`, `fetch_failed`, `login_wall`, …) isn't marked, so it's retried. |

**Measured offline** (scratch scripts; no live requests):

| | Sequential | Phase 2 | Speed-up |
|---|---|---|---|
| Enrichment, 120 simulated sites (3 pages each, delays scaled 1/100) | 15.6 s | 2.05 s (8 workers) / 1.08 s (16) | **7.6×** / 14.4× |
| Discovery, Mumbai preset (estimator model) | 15.4 h | 12.5 h (3 workers) | **1.23×** |
| Discovery, Nashik preset (estimator model) | 5.4 h | 3.0 h | **1.82×** |
| Discovery, a new config with default sources ("dentists in Austin", 5 areas × 2 categories) | 42 min | 22.5 min | **1.87×** (after the follow-up below; 1.0× before) |

**Against Part 2's targets (≥10× enrichment, ≥3× discovery), both fall short, for stated reasons:**
- **Enrichment is bounded by the worker count.** At the default of 8 the gain is 7.6×. `runtime.enrich_workers: 16` gives ~14×. Re-runs also skip what's already enriched, which is where the "5 hours" mostly went.
- **Discovery is bounded by one host.** In the Mumbai preset, Maps, Google Search, Facebook and LinkedIn discovery all go through google.com. "Never within one host" serialises them, and they are most of the work. The Nashik preset paces Maps on its own key (`rate_key: google_maps`), hence its 1.8×.
- **Follow-up (decided after this report's first draft): Maps paces on its own key by default.**
  - New configs that leave `sources` out now get `rate_key: google_maps` (`DEFAULT_SOURCES`), as the Nashik preset already had.
  - The Nashik run is the evidence: Google throttles Maps and web search separately, and a shared key let a SERP CAPTCHA penalty slow every Maps page.
  - Maps then runs beside the SERP sources, each with its own polite delays; for the Austin example above that's 1.87×.
  - The Mumbai preset sets its sources explicitly, so its original pacing is unchanged; adding `rate_key: google_maps` to its `google_maps` section would opt it in.
  - Pinned by `test_maps_paces_apart_from_search_by_default_but_the_legacy_preset_is_unchanged`.

## 5. Worker and lifecycle (2.5)

- **Worker** (`engine/worker.py`, `leadscraper worker`):
  - registers in a new `workers` table and heartbeats there;
  - recovers runs a dead worker left behind: an active state with a stale heartbeat is resumed from its checkpoint; one that was `pausing` becomes `paused`; one that was `stopping` becomes `cancelled`;
  - claims `queued` runs oldest first with one conditional UPDATE (`claim_run`), so several workers can share a database.
- **States:** `queued → running → pausing/paused → stopping → completed / failed / cancelled`.
  - `runs pause|resume|cancel` write requests into `runs.status`; the runner's heartbeat (5 s) sees them.
  - Pause, cancel, the deadline, Ctrl-C and a worker shutdown share **one stop path** (`RunControl`): start nothing new, let work in flight finish, flush. Only the final status differs; a worker shutdown puts the run back to `queued`.
- **Stopping a worker without signals:** `leadscraper worker stop` sets a database flag, so it behaves the same on Windows. Ctrl-C, SIGTERM and Ctrl-Break work too.
- **Ctrl-C in the CLI** pauses through the same path; a second Ctrl-C stops at once (`test_ctrl_c_pauses_through_the_same_stop_path`, `test_a_second_ctrl_c_stops_at_once_and_loses_at_most_the_unit_in_flight`).
- **Ownership:**
  - every execution claims its run atomically, and `run --resume` refuses a run that a live executor holds;
  - heartbeats and the final write only touch the run while this executor still holds it. An executor that stalled and lost the run stops without writing.
- **Containment:** an engine error (the database going away mid-run) marks the run `failed`, never `completed`, and it stays resumable. A bookkeeping failure for one unit costs that unit's checkpoint, not the run.

## 6. Keep-awake (2.6)

- **The lock** (`engine/keepawake.py`): a reference-counted `wakepy` "keep running" lock, acquired by the first active run in a process and released by the last. It is on by default; turn it off with `LEADSCRAPER_KEEP_AWAKE=0`, `worker --no-keep-awake` or `runtime.keep_awake: false`. A failing method logs a warning and never fails a run. `wakepy>=0.9` is a new dependency.
- **Sleep detection:**
  - The heartbeat watches for a wall-clock jump.
  - On wake, each thread relaunches its browser on next use (Playwright objects stay on their own thread) and HTTP sessions are recreated.
  - The part of the gap the budget's monotonic clock counted (only Windows' does) is not billed.
  - A `sleep_detected` event is emitted.
- **The honest limits** are written up in the README and `docs/engine.md`:
  - a sleeping machine runs nothing;
  - on macOS, a closed lid on battery forces sleep regardless;
  - unattended runs belong on the server (Phase 3).
- Verified for real once on this Mac: wakepy chose `caffeinate`.

## 7. Preview (2.7)

`python run.py --preview` / `leadscraper preview --config …` runs one area × one category × the top two discovery sources (Maps first), at quick depth, inside a 2-minute budget with a 15 s tail.
- It writes into a scratch project, `<project> (preview)`, emptied first, so the real project never holds preview rows.
- It returns the sample records and the full-run estimate.
- The estimate is **calibrated** by the preview's own actual/expected ratio, applied to the part of it that still rests on cold priors. The preview's units also feed `unit_stats`.

## 8. Events (2.8)

`engine/events.py`: typed pydantic events with a `type` discriminator (`event_json_schema()` for the future UI):
- `run_state`, `plan`, `unit_started`, `unit_finished`, `unit_skipped`;
- `progress` (counts, elapsed, ETA p50/p90, budget left);
- `activity`, `failure`, `ai_spend`, `sleep_detected`, `run_finished`.

They're numbered per run, persisted in a new `run_events` table by a writer thread (no unit or heartbeat waits on the database), and replayable from any `seq` (`read_events`; `leadscraper runs events ID [--follow] [--json]`). A test proves the replay equals what a live subscriber saw, field for field.

## 9. Data model and CLI

**Alembic `0002`:**
- `runs` gains `request`, `estimate`, `checkpoint`, `worker_id`, `heartbeat_at`, `active_seconds` and `attempts`.
- `work_units` gains `unit_key` (indexed with `run_id`), `new`, `merged`, `seconds`, `expected_seconds`, `expected_new`, `priority` and `skip_reason`.
- `unit_stats` gains `total_sq_seconds`.
- New tables: `run_events`, plus `workers` (process infrastructure, so it's the one table besides `workspaces` without `workspace_id`).
- Server defaults upgrade existing rows on SQLite and Postgres (`test_migration_0002_upgrades_a_database_with_runs_in_it`, and the Postgres twin).

**CLI:**
- `run` gains `--time-budget`, `--depth`, `--workers`, `--force-enrich`, `--resume`, `--queue` and `--preview`.
- New commands: `worker [--once] | stop | list`, `runs list|show|pause|resume|cancel|events`, `estimate` and `preview`.
- `run.py` keeps every old flag.

`LEADSCRAPER_PLUGINS=module` loads extra lead types into any process. The worker-kill test uses it to give a separately started worker its synthetic lead type.

## 10. Acceptance checks

| Check | Status | Evidence |
|---|---|---|
| Killing the worker mid-run and restarting it resumes with at most one unit lost | **Pass** | `test_killing_the_worker_mid_run_and_restarting_it_loses_at_most_one_unit` starts a real `python -m leadscraper worker` process and kills it (SIGKILL / TerminateProcess) after ≥3 units. A second worker recovers the orphaned run after its heartbeat goes stale; every unit runs, at most one runs twice, and `attempts == 2`. Also `test_a_second_ctrl_c_…` (in-process hard stop: exactly one unit re-ran) and `test_stopping_a_worker_requeues_its_run_and_the_next_worker_resumes_it` (graceful: none re-ran). |
| A 15-minute budget run finishes within budget + tail and its report lists the skipped units | **Pass** | `test_a_15_minute_budget_run_finishes_within_budget_plus_tail`: a fake clock, one unit overrunning its estimate; elapsed ≤ 990 s; every skipped unit in `stats.engine`, `work_units` and the printed report. `test_a_unit_that_overruns_into_the_tail_is_cut` and `test_local_business_runs_through_the_engine_within_a_budget` (the real pipeline, Maps first) cover the rest. |
| Estimator p50 within ±35% of actual on a recorded-fixture run | **Pass: +5.1%** | `test_estimator_p50_is_within_35_percent_on_a_recorded_fixture_run`. See the note below. |
| Pause / resume / cancel are tested | **Pass** | `test_pause_then_resume_runs_every_unit_exactly_once` (through the database and the heartbeat), `test_cancel_finishes_in_flight_work_then_flushes`, the Ctrl-C tests, the worker hand-over, `test_a_cancel_recorded_while_the_run_finishes_wins`. |
| Keep-awake is acquired and released correctly (mocked) on all three OSes in CI | **Pass, in CI** | `test_keep_awake_is_held_while_runs_are_active_and_released_after[Darwin, Windows, Linux]`: wakepy is mocked, so the test checks reference counting, held during the run, and released after. Also `…_that_fails_never_fails_the_run` and `…_toggle_is_respected`. `.github/workflows/tests.yml` runs the suite on macOS, Windows and Linux (3.12; plus 3.14 on Linux); all four jobs passed (run 37563766674). |

**Note on the "recorded fixture".** No real per-unit timings exist yet (see section 3), so `tests/fixtures/recorded_units.json` is a fixed recording of a 4-source × 5-area × 3-category job:
- generated once with a seeded log-normal and committed;
- it has two "runs" with realistic per-area and per-category variation.

Run 1 teaches `unit_stats`. The estimate for run 2 is then compared with run 2's actual time on a fake clock: p50 6,826 s against 6,493 s actual (+5.1%), and the actual falls within the p90 (7,325 s). The cold estimate (−7.4%) is not evidence about real sources: I chose those synthetic priors myself.

## 11. Review and the fixes it led to

An independent review of the Phase 2 diff (a separate reviewer agent, code reading only) reported 12 findings. Each fix below has a regression test where one was practical.

| Finding | Fix | Test |
|---|---|---|
| A cancel after a pause (or during a worker shutdown, or while finishing) could be lost, or the run re-queued | A cancel overrides pause and shutdown; `finish_run` turns a pending `stopping` into `cancelled` | `test_a_cancel_overrides_…`, `test_a_cancel_recorded_while_the_run_finishes_wins` |
| An engine exception left the run `completed` with units pending | Caught: `failed`, resumable; per-unit bookkeeping contained | `test_an_engine_error_fails_the_run_instead_of_completing_it` |
| `--resume` had no atomic claim; a run could execute twice | `_claim` via `claim_run` (stale-heartbeat rule for active states) | `test_resuming_a_run_a_live_executor_holds_is_refused` |
| A stalled executor never noticed it lost its run | Heartbeat and final write conditional on `worker_id`; lost → stop silently | `test_an_executor_that_lost_its_run_stops_without_writing` |
| Signal handlers could deadlock on non-reentrant locks | `RLock`s | `test_a_cancel_overrides_…` (request under the lock) |
| A hung unit could hold a pause, cancel or deadline forever | Grace period, then abandon (re-runs on resume) | `test_a_hung_unit_is_abandoned_after_the_grace_period` |
| Resume treated `partial` / budget-`skipped` units as finished | Only `done` / `failed` count as finished | `test_resume_reruns_units_that_were_cut_or_skipped` |
| Settling orphaned runs was unconditional | `settle_run`, conditional on status and staleness | — |
| CSV/Sheets flushes ran on unit threads | Flushes on the dispatcher thread (`session.checkpoint()`) | — |
| Finalise time was billed again on each resume | Elapsed measured before finalising | — |
| Event writes serialised every thread behind the database | Writer thread | — |
| Lead-type tag for non-unit threads ambiguous | Not changed: only unit and enrichment threads write, and both tag themselves | — |

## 12. Postgres

As in Phase 1, a throwaway PostgreSQL 16.2 server ran from the `pgserver` wheel in a scratch Python 3.12 venv, on a local socket only, and was deleted afterwards.

| Check | Result |
|---|---|
| `tests/test_postgres.py` (Phase 1 round trip + bytewise scan + two new Phase 2 tests: migration `0002` over existing rows; a run paused, resumed, events replayed and a claim race) | 4 passed |
| The **whole engine suite** on Postgres (`LEADSCRAPER_TEST_ENGINE_DB_URL`, new opt-in), the real worker-kill test included | 46 passed |

One test had to change for the shared-database mode: it now uses its own source name, so no other test's learned stats apply to it. Migration `0002` also renders as offline SQL (`db upgrade --sql`).

## 13. Intended behaviour changes, with reasons

None of these touch the characterisation results.

- **Ctrl-C pauses instead of cancelling.**
  - **What changes:** the work in flight finishes, everything is flushed, and the run is `paused` and resumable (`run --resume ID` or `runs resume ID`). The command exits 0 with a resume hint. A second Ctrl-C still stops at once.
  - **Why:** Phase 2.5, "Ctrl-C in CLI mode shares the same stop path".
- **Discovery runs up to 3 units at once, on different hosts.**
  - **What changes:** under parallelism, the order in which leads are stored differs from plan order, so which record of a duplicate pair is stored first, and keeps the lower id, can differ. The merged result is the same records.
  - `--workers 1` (or `runtime.discovery_workers: 1`) restores the exact sequential order.
  - `test_plan_lists_the_units_the_pipeline_executes_in_order` was ported: plan order with one worker, the same units exactly once with three.
- **Enrichment skips leads already enriched.**
  - **What changes:** the website enricher used to re-fetch every lead with a website and no email on every run. Now it marks each lead once done; `--force-enrich` restores the old behaviour.
  - **Why:** Part 2.5.
- **Enrichment is concurrent** (8 sites at once). Per-site pacing is unchanged.
- **`--dry-run` prints the estimator's p50–p90 range, budget fit and AI cost** instead of the old "Rough time estimate" line, which direct `Pipeline` use still prints.
- **`depth` and `time_budget_minutes` are acted on**, so `RunConfig.unapplied()` no longer names them. `test_settings_this_build_does_not_apply_are_named` was updated: 8 notes became 6.
- **Paused and queued runs have no `finished_at`.** A run's `worker_id` is cleared when it stops.
- **Test plumbing:**
  - `tests/conftest.py` disables the real OS keep-awake lock in tests;
  - `test_every_owned_table_carries_workspace_id` lists `workers` as unowned, with the reason.

## 14. Deviations from the plan, with reasons

- **`balanced` is each source's configured settings, not Part 3.3's numbers.** Applying 25/8/2 would have changed both legacy presets (Mumbai's Maps setting is 20/6). The shipped defaults are close to that row anyway.
- **The cold-start priors' yields are judgement calls**, set to reproduce Part 3.1's order. Learned stats replace them after the first real run. Their real accuracy is unmeasured (section 15).
- **The "recorded fixture" is a seeded synthetic recording** (section 10). No real per-unit timings exist yet.
- **Part 2's ≥10× enrichment and ≥3× discovery targets are not met by default** (section 4). The discovery limit comes from the per-host rule.
- **`requests` stays; `httpx` with HTTP/2 (A.4) wasn't adopted.** The per-host pooled sessions the plan asks for (Part 2.4) already exist. Swapping the client would touch every source for no Phase 2 acceptance item.
- **The CLI covers what the lifecycle needs.** Full parity (saved configs, bundles, exports) is Phase 3, as is the API.
- **robots.txt handling isn't here.** A.5 lists it under `net/`, but no phase item asks for it before the generic crawler work.

## 15. Open issues, carried forward

- **Merged:** Phases 1 and 2 are on `main` (merge commit "Merge Phases 1 and 2"), and every branch is pushed.
- **No live run yet.** Nothing in Phase 2 made a network request:
  - the priors, the real speed-ups, the block rate under parallel discovery and keep-awake over a long run are unmeasured;
  - a first real run (e.g. `python run.py --config presets/nashik-venues.yaml --time-budget 30`) would populate `unit_stats` and show how far the cold estimate was off.
- **CI passes on all three OSes**, Windows included. The two Ctrl-C tests skip on Windows (`os.kill(SIGINT)` isn't Ctrl-C there), so Windows Ctrl-C/Ctrl-Break is covered only by the shared stop-path tests. GitHub warns that `actions/checkout@v4` and `setup-python@v5` target the deprecated Node 20; bump them when v5/v6 are current.
- **Maps beside SERP (decided):** new configs pace Maps on its own key (section 4). Watch the block rate on the first live runs; setting `rate_key: www.google.com` on `google_maps` restores the shared pacing.
- **Abandoned units.** A unit abandoned after the grace period keeps its daemon thread until the process exits. Its writes, if it ever wakes, are contained, but the unit re-runs on resume.
- **From Phase 1, still open and still yours:**
  - the terms-of-service review of the inherited sources;
  - the VayaVia phone-similarity threshold;
  - running `migrate-legacy` into your real data directory.

Commits on this branch:
- `feat(engine): scheduler, estimator, events and checkpointed units`
- `feat(engine): worker and DB-backed queue, lifecycle, preview, keep-awake; CLI`
- `fix(engine): lifecycle races and robustness from review; docs and CI`
- `test: engine on Postgres (migration 0002, pause/resume, claims); opt-in engine suite on Postgres; CLAUDE.md`
- and this report.
