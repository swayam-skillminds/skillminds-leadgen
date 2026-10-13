# Live check 1: first real run after Phases 0–2

Date: 2026-10-07 · Base: `main` @ `cf7281e` · Fixes: branch `fix/live-check`

**Result:** the refactored code works against Google Maps.
- Run 1, on `main`, completed with no failures, no crash and no block. It stored 10 organisations with good field coverage, and the engine recorded everything it should.
- The run exposed **two defects**, fixed with regression tests on `fix/live-check`:
  - every estimate made under a time budget charged the whole 90 s reserved tail as if it were spent;
  - Maps kept running searches after a combination already had all the places it could keep.
- Run 2, on the fix branch, confirmed both fixes live, and confirmed that re-finding the same places doesn't duplicate them.
- The suite is green: 433 passed, 8 skipped (429 passed before the 4 new tests). The characterisation goldens are untouched.

## 1. What was run

| | |
|---|---|
| Scope | Mumbai, area Andheri (2 aliases), category Caterers (3 queries); one unit |
| Sources | `google_maps` only (`--source google_maps`); every other source "not built — not requested" |
| Depth / budget | `quick` (Maps capped at 10 places per combination, 3 scroll rounds; enrichment off) / 15 minutes |
| AI, Sheets | off / off (`--no-sheets`; `output.google_sheets: false`) |
| Pacing | Maps on its own key (`rate_key: google_maps`, the default for new configs), 2.5–6 s delays |
| Isolation | A new run config (project `live-check-1`) in a scratch directory with `LEADSCRAPER_DATA_DIR` pointed at it. DB, CSV, logs and the AI cache all landed there. |

The real data was not touched:
- the per-user application DB (last modified 2026-10-06 16:03);
- `data/leads.sqlite3` (Sep 18), `data/leads.csv` (Sep 17) and `data/nashik/venues.sqlite3` (Oct 1).

Both mtimes were checked after the runs. The scratch config isn't committed; its essentials are in the table above. This report holds aggregates only, no lead data.

Command: `python run.py --config <scratch>/live-check.yaml --source google_maps --no-sheets -v`.

## 2. What was found

### Run 1 (`main`, cold estimate)

| | |
|---|---|
| Status | `completed`, exit 0, `attempts` 1. No failures recorded, no CAPTCHA, no block, no rate-limit penalty. |
| Output | 10 raw leads → 10 new organisations; CSV written (24 columns); Sheets skipped as configured |
| Field coverage | name, address, Maps URL, Maps rating and Maps review count on 10/10; website 9/10; phone 7/10; email, socials and contact name 0/10 (expected: enrichment is off at quick depth) |
| Engine bookkeeping | Correct throughout: `runs` row, one `work_units` row (`done`, 10 new, 81.9 s), `unit_stats` learned under `google_maps@quick`, and a gap-free event stream (`run_state → plan → unit_started → unit_finished → progress → activity × 2 → run_finished → run_state`) |
| Keep-awake | Acquired (`caffeinate`) at start, released at the end |
| Depth preset | Applied and logged: Maps 20 → 10 results, 6 → 3 scroll rounds, enrichment off |

### Run 2 (`fix/live-check`, learned estimate, same scratch DB)

| | |
|---|---|
| Status | `completed`, exit 0, no failures |
| Output | The same 10 places found again → 0 new, 0 merged, **10 unchanged**; still exactly 10 organisations, so no duplicates |
| Requests | 1 search + 10 place pages (run 1: 6 searches + 10 place pages) |
| Estimate | Budgeted p50 equals unbudgeted p50 (118.4 s); live ETA after the last unit was 15 s (run 1: 90 s) |

## 3. Estimate vs actual

| | Basis | Estimate p50 / p90 | Printed by the CLI | Actual | Actual / p50 |
|---|---|---|---|---|---|
| Run 1: the unit | cold prior | 146.5 s | — | 81.9 s | 0.56 |
| Run 1: the run | cold prior | 161.5 / 287.4 s | **3.9–6.0 min** (budgeted, tail included; the defect) | 82.0 s (1.4 min) | 0.51 (0.35 against the printed figure) |
| Run 1: new orgs | cold prior | 4.8 | | 10 | 2.1 |
| Run 2: the unit | learned (run 1) | 103.4 s | — | 45.3 s | 0.44 |
| Run 2: the run | learned (run 1) | 118.4 / 164.8 s | 2.0–2.7 min | 45.4 s (0.8 min) | 0.38 |

What these numbers say:
- **The cold Maps prior is pessimistic at quick depth.** Run 1 took about half the predicted time and found twice the predicted yield. That's one unit, so it's an observation, not a calibration. Learned stats replace the prior per unit, which is what the engine is meant to do.
- **Run 2's error mostly comes from the Maps fix itself.** It learned from run 1, which spent about 30 s on five searches that run 2 no longer makes. The learned figure (103 s) is also shrunk towards the 146.5 s prior after a single run. Later runs of this unit will converge.
- **Run 2's expected yield (8.3 new) vs 0** is expected for an immediate re-run of the same unit. The cost model counts only new organisations as yield, so this run teaches it that re-running the unit pays little. That's correct.
- **Neither run came near the budget**, so the scheduler's skip and cut paths were not exercised live. Only offline tests cover them.

## 4. What failed, and the fixes (`fix/live-check`)

### 4.1 Budgeted estimates billed the reserved tail as time spent

**Symptom.** The pre-run plan, the stored estimate and the final report all said "3.9–6.0 min" for one unit. The same estimate without a budget said 2.7–4.8 min. Adding a budget that cut nothing made the forecast 75 s longer. After the last unit, the live ETA said 90 s remaining; the finalise step took under a second.

**Cause.**
- `engine/estimator.estimate()` added `tail_seconds` (90 s) to the budgeted p50 and p90.
- The runner gave `LiveEta` the tail as its finalise time whenever a budget was set.
- The unbudgeted estimate uses `FINALISE_SECONDS` (15 s).

The tail is a *reservation* (the scheduler keeps it free so flush, scoring and export always fit), not an expected duration. The over-estimate is a constant 75 s, which dominates exactly the short budgeted runs that budgets are for.

**Fix.**
- The budgeted p50 and p90 now add `FINALISE_SECONDS`, like the unbudgeted ones. They are still capped at the budget (p50) and at budget + tail (p90).
- `LiveEta` always uses the estimate's `finalise_seconds`.
- What fits, the reserve for enrichment and the scheduling decisions are all unchanged.

**Tests** (`tests/test_engine.py`; both failed before the fix, with 3990 ≠ 3915 and 90 ≠ 15):
- `test_a_budget_that_fits_everything_does_not_change_the_estimate`;
- `test_a_budgeted_run_s_last_eta_is_finalise_not_the_tail`.

### 4.2 Maps kept searching after the combination was full

**Symptom.** Run 1 made 6 Maps searches (3 queries × 2 locality phrasings) for 10 places. The first search alone supplied all 10, and the other five searches' results were thrown away unread.

**Cause.** `GoogleMapsSource.discover` checks its per-combination cap (`max_results_per_query`, which despite its name counts the whole combination) only before opening a place. It never checks before running the next search.

**Impact.**
- Run 1 spent about 30 s of its 82 s on the five wasted searches.
- Each of those searches is a Google request, on the host this project already treats as the one that blocks first.
- Any combination that fills its cap before its last query wastes requests the same way. That includes the Mumbai and Nashik presets, at every depth.

**Fix.** Return before a search once the combination already has its places. **No lead output changes**: the skipped searches could never contribute a lead. Run 2 confirmed it live: 1 search, then 10 places, in 45 s instead of 82 s.

**Tests** (`tests/test_google_maps.py`, new; both failed before the fix, with 6 searches each):
- `test_maps_stops_searching_once_the_combination_has_its_places`;
- `test_maps_keeps_searching_while_the_combination_is_short`.

## 5. Observations, not fixed (not broken)

- **Unchanged re-finds are invisible in the run's record.** In run 2, all 10 leads matched existing organisations with nothing new to add, so the store returned `unchanged`:
  - the CLI summary reads "stored 10 / new 0 / merged 0", with no "unchanged" line;
  - `run_organizations` has no rows for run 2, because only `new` / `updated` count as membership (Phase 1 design).

  For a future "leads found in this run" view (Phase 3/4) you may want unchanged re-finds recorded as members. That's a design decision.
- **Contactability at quick depth** is phones only (7/10), because enrichment is off by design. A balanced-depth check would exercise the website enricher, concurrent enrichment and `enrichment_marks` live; none of these has been run live yet.
- **The generic CSV still carries the events columns** (WedMeGood Rating, Reviews, Featured), though this config has no events vertical. Per-lead-type columns are Phase 4.
- **A business name kept its trailing city name** (", MUMBAI."), because this scratch config set no `place_noise`. That isn't an SEO title, so the rule-based clean-up rightly leaves it alone.
- **Wording:** with enrichment off by depth, the enricher lines say "not requested — this run asked for nothing". The preceding "enrichment off for this run (depth quick)" line explains it, but the wording could be clearer.

## 6. Still unverified live

- Parallel discovery across hosts, and the block rate with Maps beside SERP: only one source ran.
- The budget's skip and cut paths: the budget never bound.
- Pause and resume, a worker, and keep-awake over a long run.
- Every other source.

Suggested next live check: the Nashik preset with `--time-budget 30` (in a scratch data directory, after `migrate-legacy` into it), as `docs/phase-2-report.md` section 15 proposes. That exercises several hosts, the budget and enrichment.
