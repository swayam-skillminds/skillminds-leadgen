# Live check 2: four lead types, two countries, a 20-minute budget

Date: 2026-10-13 · Base: `phase-6b/news-people` @ `19d9ae5`, with `fix/live-check` (live check 1) merged in · Fixes: branch `fix/live-check-2`

**Result:** a budgeted run with several lead types was broken on the network this machine is on. Three defects are fixed, each with regression tests; the reruns confirm them live.

**Runs 1 and 2 (Austin, Pune), on the base code:**
- **Three of the four lead types produced nothing.** hiring, funding_news and digital_audit were all empty.
- **Cause:** Google answered 429 from the first request. One SERP unit expected to take ~25 s then sat for 10–18 minutes in the rate limiter's escalating penalty waits. Nothing could stop it, so:
  - the hiring and funding_news units (stage 1) waited for it until the deadline skipped them;
  - post-discovery never ran (careers pages, the audit);
  - in Austin, the abandoned unit was left `running` in a run recorded as `completed`.
- **Separately, Sulekha found nothing in Pune.** Every new config asks it for a capitalised URL, which is its 404 page.

**After the fixes (runs 4 and 5, all fixes in):**
- Google still blocked.
- Both runs completed inside the budget (17.2 and 17.0 min against a 20-minute budget). In each, hiring, funding_news and digital_audit all got their turn.
- Pune's run 4 found 69 new organisations: Sulekha 42, Maps 11, hiring 16.
- **funding_news is still unverified:** GDELT answered 429 to its first query in both runs (section 7).

**Exports:**
- Every run wrote its workbook.
- The final project workbooks (`export --scope project`) hold 80 organisations for Austin and 89 for Pune.
- Each has the Overview and Records tabs, with the hiring and audit columns.

**Suite:** 845 passed, 7 skipped, 16 deselected; it was 837 after merging live check 1. `ruff` is clean, and the characterisation goldens are untouched.

## 1. What was run

| | |
|---|---|
| Places | Austin, US and Pune, IN; each place as a whole (no areas) |
| Categories | Dentists, Software companies (names only; queries default to the name) |
| Lead types | `local_business`, `digital_audit` (`audit: run`), `hiring` (no configured boards: HN and the careers pages), `funding_news` (GDELT) |
| Depth / budget | `quick` / `time_budget_minutes: 20`. Quick means: Maps ≤ 10 per combination, enrichment off, careers pages ≤ 50, 50 GDELT articles a query, 3 links per audited site, PageSpeed off |
| Sources built | **Austin:** `google_maps`, `google_search`, `facebook`, `linkedin_serp`; the India pack is "not applicable — pack 'in' covers IN". **Pune:** the same plus `indiamart`, `justdial`, `sulekha` |
| AI, Sheets | off / off |
| Isolation | Scratch run configs (projects `live-check-2-austin`, `live-check-2-pune`), with `LEADSCRAPER_DATA_DIR` pointed at a scratch directory. The DB, workbooks, logs and caches all landed there |

The real data was not touched. Modification times after the last run:
- the per-user application DB: Oct 7;
- `data/leads.sqlite3`: Sep 18;
- `data/nashik/venues.sqlite3` and `data/ai_cache.sqlite3`: Oct 1.

The configs aren't committed; the table above holds their essentials. This report holds aggregates only, no lead data.

Command: `python run.py --config <scratch>/<city>.yaml -v`. Exports: `python -m leadscraper export --run ID --format xlsx --scope project -o …`.

**The environment shaped every run:**
- **A corporate TLS-inspecting proxy (Zscaler) sits on this network.**
  - Its shared egress addresses are the likely reason Google answered 429 from the first request of every run, and DuckDuckGo answered "202, throttled" soon after.
  - For some domains it presents its own certificate chain, which Python's `certifi` bundle doesn't trust. That is where `SSLError` came from (section 4).
- **Run 1 had a closed lid on battery.** The Mac went into "Clamshell Sleep" for 15.4 minutes even though `caffeinate` was held, which is the documented keep-awake limit. The engine detected the sleep and did not bill it to the budget (19.3 min active, 35.7 min wall).

## 2. The runs

| Run | City | Code | Took (active) | Budgeted estimate p50–p90 | Units | New orgs | Lead types that delivered |
|---|---|---|---|---|---|---|---|
| 1 | Austin | base | 19.3 min (35.7 wall) | 5.3–7.7 min (cold) | 7 done, **1 left `running`**, 2 skipped | 30 | local_business only. HN and GDELT skipped; no audit, no careers pages |
| 2 | Pune | base | 18.6 min | 10.7–15.2 min | 10 done, 6 skipped | 20 | local_business only (Maps). HN, GDELT and the 4 Facebook/LinkedIn units skipped; Sulekha 0; no post-discovery |
| 3 | Austin | fix 1 (cut, abandon) | 18.6 min | 9.5–11.8 min | 4 done, 2 partial (cut), 4 skipped | 50 (+14 updated) | local_business, hiring, digital_audit (9 audited, 6 not checked at the deadline). **GDELT still skipped** → fix 2 |
| 4 | Pune | all fixes | 17.2 min | 14.5–20.2 min | 11 done, 1 partial (cut), 1 failed (GDELT 429), 3 skipped | 69 (+9 updated) | All four ran: local_business 53 new, hiring 16 new, digital_audit 76 audited; funding_news throttled |
| 5 | Austin | all fixes | 17.0 min | 17.2–21.5 min | 5 done, 1 partial (cut), 1 failed (GDELT 429), 3 skipped | 0 (60 updated, 10 unchanged) | All four ran: hiring refreshed 51 postings, digital_audit audited 57 (9 skipped as audited < 7 days ago); funding_news throttled |

Every run ended `completed`, exit 0. Nothing crashed, and each lead type's work was contained. The skipped units in runs 4 and 5 are low-yield SERP units, skipped because the cost model had learned they take minutes on this network. That is the budget doing its job.

## 3. Results per lead type

| Lead type | Austin (runs 1, 3, 5) | Pune (runs 2, 4) |
|---|---|---|
| `local_business` | **30 organisations:** Maps 20 (10 + 10), Google Search 7, Facebook 3 (all run 1, before the block). Re-found unchanged in runs 3 and 5. A phone on 20 of 30 | **73 organisations:** Maps 20 (run 2) + 11 new (run 4), Sulekha 42 new (run 4, after fix 3). A phone on 19 of 20 in run 2 |
| `hiring` | Run 3: HN read 297 ads; 89 postings matched in all (HN, careers pages and the 8 ATS boards they linked); **50 new organisations** (remote-US or Austin tech roles). Run 5 refreshed 51 postings rather than duplicating them. Careers pages: 50 scanned (run 3), 11 (run 5) | Run 4: HN read 263 ads; 29 postings matched in all (5 ATS boards); **16 new organisations**. 27 careers pages scanned |
| `digital_audit` | Run 3: 9 audited, 6 not checked (the deadline came mid-audit). Run 5: 57 audited, 48 findings (no contact form 22, no website 17, incomplete Maps profile 4, stale copyright 2, …) | Run 4: 76 audited, 80 findings, 48 "no website" (Sulekha listings rarely carry one) |
| `funding_news` | Runs 1 and 3: skipped by the budget. Run 5: GDELT 429 on the first query; the breaker stopped it | Run 2: skipped by the budget. Run 4: GDELT 429 on the first query; the breaker stopped it |

**Workbooks** (`<scratch>/data/projects/<project>/…-run<N>.xlsx`, plus the project exports):
- every one has exactly the two tabs;
- runs 1 and 2 show local_business columns only (24 / 23);
- from run 3 on, the hiring and audit columns appear: Site, Open Tech Roles, Role Families, Latest Posting, Jobs Page, Audit Score, Audit Findings, Site Stack, signals, Open Roles, Hiring Sources, Audited (38 columns);
- the project exports hold 80 (Austin) and 89 (Pune) organisations.

## 4. Failures per source (all five runs)

| Source | Reason (count per run) | Verdict |
|---|---|---|
| `google_search` | `generic_or_unparseable_title` ×6 (run 1: directory pages such as "dentists in Austin" lists); `no_results` ×2 / ×1 / ×1 (runs 2, 3, 5). **Plus the time:** 1,109 s for 2 units estimated at 65 s (run 2), 621 s for one estimated at 25 s (run 3) | Google 429 / challenge page, then the DuckDuckGo fallback throttled. Blocked network, not code. The time is the fixed defect (5.1) |
| `facebook` | `no_results` ×1 / ×2 / ×1, `no_name` ×1. Its 2 units took 370 s against 140 s expected (run 1) and 574 s against 324 s (run 4) | Rides on the same blocked SERP |
| `linkedin_serp` | `no_company_in_title` ×1 (run 1); 0 found in every run | Same SERP block; cut or skipped in runs 3–5 |
| `google_maps` | none | Healthy: 10 places per combination in every run, 85–101 s for 2 units |
| `justdial` | `fetch_failed` ×2, runs 2 and 4 (`net::ERR_HTTP2_PROTOCOL_ERROR`) | The block known since Phase 0. Getting past it would need evasion (A.2.4), so it isn't fixable |
| `sulekha` | `no_businesses_parsed` ×2 (run 2); 43 found in run 4 | **Defect, fixed (5.3)** |
| `indiamart` | none; 0 found, 0 s | Not a failure: IndiaMART is opt-in per category (`indiamart: true`). See section 7 |
| `hn_whos_hiring` | none | Healthy: 4.5–4.8 s, 263–297 ads read |
| `greenhouse`, `ashby` | `board_not_found` ×1 each (runs 3, 5) | An HN ad linking boards that don't exist (404 from both APIs). Correctly reported |
| `careers_pages` | `fetch_failed` ×3 / ×2 / ×3, `robots_disallowed` ×1 | Dead domains (`DeadDomainError`), and one `SSLError` caused by the TLS-inspecting proxy (plain `requests` fails the same way; `curl` with the system keychain passes). robots.txt honoured |
| `gdelt` | `rate_limited` ×1 (runs 4, 5) | 429 on the first query. The Phase 6b breaker stopped asking at once, as designed; that cost the news, not time (12 s) |
| `digital_audit` | `blocked` ×1 / ×1 / ×4 (HTTP 403/429), `site_unreachable` ×1 / ×2 / ×1 (dead domains), `robots_disallowed` ×1 | Honest outcomes: refused sites are "not checked" and nothing is written; dead domains are findings |

## 5. What failed, and the fixes (`fix/live-check-2`)

### 5.1 A unit stuck on a throttled host starved every later stage (high)

**Symptom.**
- **Austin, run 1:** `linkedin_serp|Austin|Software companies` (estimated at 25 s) started at 09:10. It spent the rest of the run in pacing waits of 37–200 s per request, as the penalty on www.google.com grew to +225 s. Meanwhile:
  - HN and GDELT, on other hosts and with two workers idle, waited for it: they are stage 1 and start only after every stage-0 unit;
  - at the deadline both were skipped ("time budget spent");
  - post-discovery was skipped, because the deadline had already come, so no careers pages and no audit;
  - after 45 s of grace the unit was abandoned, and its `work_units` row stayed `running` in a run recorded as `completed`.
- **Pune, run 2:** the same with both Google Search units (1,109 s against 65 s). The scheduler then skipped 6 units "would not finish", including HN and GDELT.

**Cause.**
- The budget guarded only the *start* of a unit ("never start what can't finish") and the tail.
- A unit that overruns is cut only between the leads it yields, or once half the tail is spent.
- A unit sitting in `RateLimiter.wait` (`time.sleep(199)`) yields nothing and checks nothing, so it could be neither cut nor stopped. It held its stage open until the deadline.
- An abandoned unit's row was never updated.

**Fix.**
- **The wait asks the engine.** `RateLimiter.interrupt`, set by the engine on budgeted runs, is asked before every paced request and once a second while pacing. When the answer is yes, `wait` raises `UnitCut`. It is a `BaseException`, so sources' own `except Exception` guards don't log it as a failed request.
- **The engine cuts the unit on that thread** only when all of these hold:
  - the run has a budget;
  - the unit has overrun its own estimate;
  - going on would eat into time promised to the work behind it: the post-discovery share, plus the later-stage units that outrank it (5.2), with 30 s of headroom, because the cut lands at the next request.
- **A cut unit keeps what it stored.** It is recorded `partial` and runs again on resume.
- **An abandoned unit is recorded `partial`** ("abandoned: still running … after the stop"), not left `running`.
- **What doesn't change:** unbudgeted runs never cut a unit, and the presets run unbudgeted.

**Live:**
- **Run 3:** the stuck Google Search unit was cut at 621 s ("overran its estimate (25s expected, 621s so far) into the 146s promised to the work behind it"). HN then ran at once and post-discovery ran.
- **Runs 4 and 5:** cuts at 272 s and 313 s, and all four lead types ran.

**Tests** (each of the first two fails on the base code at the asserted point):
- `tests/test_engine.py::test_a_unit_stuck_on_a_throttled_host_is_cut_before_it_starves_later_stages`: a stage-0 unit makes paced requests for 2,400 s against a 30 s estimate. The stage-1 unit must still run, post-discovery must get its 50 s, and nothing may be skipped. Base code: the stage-1 unit is `skipped`.
- `test_an_abandoned_unit_is_not_left_running_in_a_finished_run`: base code `[('running', False)]`, fixed `[('partial', True)]`.
- `test_a_unit_within_its_estimate_or_without_a_budget_is_never_cut`: a guard that passes on both versions.
- `tests/test_network.py::test_a_pacing_wait_ends_as_soon_as_the_engine_cuts_the_unit`: three 1 s slices, not 200 s; a plain sleep without an engine.
- `tests/test_pipeline.py::test_a_unit_cut_by_the_engine_is_partial_and_keeps_what_it_stored`: `partial`, 1 found and stored, no failure recorded although the source wraps its wait in `except Exception`.

### 5.2 Freed time went to another near-empty stage-0 unit, not the later stage (medium)

**Symptom.** In run 3 the cut freed the budget. The scheduler then started `linkedin_serp|Austin|Dentists` (stage 0, expected 42 s, ~0 new) instead of waiting for stage 0 to drain. GDELT (stage 1, 31.5 s, ~3 new) no longer fitted, and was skipped.

**Cause.** The admission check reserved the tail and the post-discovery share, but nothing for later stages. Stages force stage 0 first, so any budget that binds lets stage 0 take all of it, whatever the yield. With a big local_business config and a short budget, hiring and funding_news would be skipped every time, stuck unit or not.

**Fix.**
- **The rule.** Under a budget, a unit must leave time for every queued later-stage unit that **outranks** it (better expected new organisations per second). Those units would have been chosen first had stages not forced the order, so the budget's own principle decides.
- **What it keeps.** A later-stage unit that doesn't outrank the candidate waits its turn, as before. So a pile of low-yield later units can't starve the finders.
- **The cut uses the same measure** (5.1).
- **Unaffected:** runs with one stage (both presets, local_business alone) and unbudgeted runs.

**Live:** in runs 4 and 5, HN and GDELT ran (GDELT then got its 429), and every skip was a SERP unit.

**Test:** `test_a_budget_keeps_time_for_later_stage_units_that_outrank_the_candidate`, with two cases:
- news outranks the SERP unit, so the SERP unit is skipped and news runs. This case fails on the base scheduler.
- it doesn't, so the SERP unit runs and news waits and is skipped. This case passes on both versions.

### 5.3 Sulekha found nothing for any new config (medium)

**Symptom.** Run 2: `sulekha` reported `no_businesses_parsed` for both categories in 6 s.

**Cause.**
- A new run config's category queries default to the category's name, so the listing URL was `https://www.sulekha.com/Dentists/pune`.
- Checked live: that 302s to `/404?reason=page-config-not-found`; `/dentists/pune` is a 200 listing.
- Parsed offline, the saved lowercase page gives 21 structured records.
- The presets' queries happen to be lowercase, so only new configs were hit.

**Fix.** `LocalDirectorySource.lowercase_slugs`, set on Sulekha: its category slugs, configured or derived, are lowercased. Justdial keeps its capitals.

**Live:** run 4 found 43 businesses on Sulekha (42 new) in 12.7 s.

**Test:** `tests/test_directories.py::test_sulekha_listing_urls_are_lowercase_whatever_the_category_says` fails on the base code.

## 6. Estimate vs actual

| | Basis | Budgeted estimate p50 / p90 | Actual (active) | Actual / p50 | New orgs: est → actual |
|---|---|---|---|---|---|
| Run 1, Austin | cold priors | 318 / 461 s | 1,155 s (deadline) | 3.6 | 17.6 → 30 |
| Run 2, Pune | 8 prior, 8 source | 643 / 914 s | 1,118 s (deadline) | 1.7 | 33.9 → 20 |
| Run 3, Austin | 7 learned | 568 / 709 s | 1,116 s (deadline) | 2.0 | 29.9 → 50 |
| Run 4, Pune | 11 learned | 869 / 1,210 s | 1,032 s | 1.19 | 57.1 → 69 |
| Run 5, Austin | 8 learned | 1,031 / 1,290 s | 1,020 s | **0.99** | 43.2 → 0 (all re-found) |

Per source (seconds per run, actual vs expected):

| Source | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 |
|---|---|---|---|---|---|
| `google_maps` (2 units) | 98 vs 146 | 97 vs 108 | 85 vs 114 | 101 vs 113 | 90 vs 102 |
| `google_search` (2) | 71 vs 44 | **1,109 vs 65** | 934 vs 62 (1 cut) | skipped (754 expected) | 738 vs 411 (1 cut) |
| `facebook` (2) | 370 vs 140 | skipped | skipped | 574 vs 324 | 163 vs 69 (1 run) |
| `hn_whos_hiring` | skipped | skipped | 4.6 vs 6.0 | 4.8 vs 5.1 | 4.5 vs 4.9 |
| `gdelt` | skipped | skipped | skipped | 12 vs 31.5 (429) | 11.5 vs 18.5 (429) |
| `sulekha` (2) | — | 7 vs 38 | — | 12.7 vs 17.6 | — |

What these numbers say:
- **Runs 1–3 are dominated by the defect.** Each hit the deadline because a stuck SERP unit held it. Their actual/p50 ratios measure the defect, not the estimator.
- **Once the engine can cut, the estimate is close.** Run 4 was within its p50–p90 range (1.19 × p50, under p90), and run 5 at 0.99 × p50.
- **Maps is steady.** It is the one source not blocked here: 85–101 s for its 2 units in every run. The cold prior (146 s) is pessimistic, as live check 1 found; learned figures converge on ~100 s.
- **Blocked SERP durations are learned.** After runs 2 and 3 the cost model expected 754 s for Pune's two Google Search units and 411 s for Austin's. On a budget it then skips them, which is right while the network is blocked. These figures only fade with more runs once the block lifts.
- **Yield learning crosses places.** HN's yield is learned per source, so Austin's 50 new became Pune's expectation (34). Pune's actual was 16. And as live check 1 noted, re-found organisations count as no yield (run 5: 43 expected, 0 new, 60 updated).
- **The estimator still ignores stages** (Phase 6a open issue) and the new later-stage reservation. Neither moved its figures here, but a budget-bound config with many stage-1 units would.

## 7. Observations, not fixed (not broken, or not code)

- **Google and DuckDuckGo block this network from the first request.** Every SERP-based source (Google Search, Facebook, LinkedIn search) produced nothing after run 1's first minutes.
  - The cut now bounds what a block costs a budgeted run.
  - Unbudgeted runs still wait out the escalating penalties, up to +225 s a request, the documented behaviour.
  - A per-run SERP breaker like GDELT's (stop asking after N consecutive blocks) would be the next step. It's a policy decision, so it isn't made here.
- **funding_news wasn't verified live.** GDELT answered 429 to the first query in both runs that reached it. That matches the Phase 6b open issue, likely aggravated by the proxy's shared egress. A GDELT unit that hits the breaker is `failed`, so a resume doesn't retry it.
- **The TLS-inspecting proxy** makes `certifi`-based requests fail for intercepted domains (`SSLError`). Only one careers page was affected here. If runs from this network matter, the OS trust store (e.g. the `truststore` package) would fix it; that's not a code defect.
- **IndiaMART units are planned and estimated** (2 × 28 s cold, later 9 s) even when no category sets `indiamart: true`. They then return at once, with no request and no failure recorded. That's harmless, but the plan and the estimate over-count them.
- **Two durations in the CLI report.** "RUN COMPLETE in 35.7 min" is the pipeline's wall clock; "completed after 19.3 min" is the engine's active time (run 1, with a 15-minute sleep). Both are true; the first could say "wall".
- **"Combinations processed 0/2"** whenever any unit of a combination was cut or skipped. That's literal, but it reads like nothing was processed.
- **hiring's `signals` stat counts new signals only.** Run 5 refreshed 51 postings and reported "signals 1".
- **Workbook names carry the database-wide run id** (`live-check-2-pune-run2.xlsx` is Pune's first run), as designed in Phase 4.

## 8. Still unverified live

- funding_news end to end (GDELT throttled), and `decision_makers` (not in this check's scope).
- PageSpeed (off at quick depth, and needs a key), balanced depth with enrichment, and AI.
- Unbudgeted multi-lead-type runs on an unblocked network. This network blocks SERP; a check from another connection would show what the SERP sources yield today.

Commits on this branch (after `phase-6b/news-people`):
- `Merge branch 'fix/live-check' into fix/live-check-2` (live check 1's two fixes, which had never reached the phase chain)
- `fix(engine): a unit stuck on a throttled host no longer starves later stages`
- `fix(sulekha): listing slugs are lowercase, so new configs find listings`
- and this report.
