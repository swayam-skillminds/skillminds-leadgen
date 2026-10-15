# Live check 3: server mode, seven lead types, two countries, through the API

Date: 2026-10-14/15 · Base: `main` @ `9fe98c3` · Fixes: branch `fix/live-check-3`

**Result:** server mode works end to end. `docker compose up` gave a healthy API and worker. Both runs were created, followed and exported through the API alone, and both workbooks downloaded.

**Runs 1 and 2:**
- **Pune (run 1)** completed on its deadline with 79 new organisations.
- **Austin (run 2)** completed inside its budget with 72 new organisations. Docker Desktop was quit twice during this run (section 1). The worker recovered it from its checkpoint each time, and it still finished at 971 active seconds of 1,200.

**Two genuine defects are fixed,** each with a regression test that fails on the base code:
- **SERP circuit breaker (medium).** It was process-wide, so in a worker one run's block carried into the next run (5.1).
- **Silent post-discovery skip (medium).** A lead type whose post-discovery the deadline never reached was dropped without a word. In Pune that was the audit (5.2).

**Suite:** 955 passed, 7 skipped, 25 deselected; it was 953 before the two new tests. `ruff` is clean, and the characterisation goldens are untouched.

**This machine was not behind the corporate proxy** (Google's own certificate chain, no system proxy). Google and DuckDuckGo still refused search from this connection, and GDELT still answered 429 (section 4). So SERP sources and `funding_news` remain unverified live, now on two networks.

## 1. What was run

| | |
|---|---|
| Stack | `docker compose -p livecheck3 up -d` from this tree (image built locally: Python 3.12-slim, Playwright 1.64 / Chromium 1248, arm64), SQLite on a fresh project-scoped volume `livecheck3_leadscraper-data` |
| Places | Pune, IN and Austin, US; each place as a whole (no areas) |
| Categories | Dentists, Software companies (as live check 2) |
| Lead types | `local_business`, `digital_audit` (`audit: run`), `hiring`, `funding_news` (GDELT), `tenders` (defaults), `tech_stack` (`technologies: [WordPress, Shopify, Magento 1]`, `check: run`), `app_store` (`keywords: [dental clinic, restaurant ordering]`) |
| Depth / budget | `quick` / `time_budget_minutes: 20` |
| Keys, AI, Sheets | no keys configured (`/health`: all false), AI off, Sheets off |
| API calls | `GET /health` → `POST /estimate` (both) → `POST /runs` (both, queued; one worker runs them in turn) → `GET /runs/{id}`, `/units`, `/failures`, `/events/history`, `/catalog/sources` → `GET /runs/{id}/export.xlsx` |

**Isolation.** Nothing touched the real data:
- The first `up` used a bind mount of a scratch directory. I took it down before any run, because SQLite in WAL mode shared by two containers over a macOS bind mount is a corruption risk. The fresh named volume above is the scratch data directory, and it is what `docs/deploy.md` describes.
- Modification times afterwards: the per-user application DB, Oct 7; `data/leads.sqlite3`, Sep 18; `data/nashik/venues.sqlite3` and `data/ai_cache.sqlite3`, Oct 1.
- The configs aren't committed; the table above holds their essentials. This report holds aggregates only.

**Interruptions.**
- **Docker Desktop was quit from its menu twice during run 2,** at 14:08:39 and 14:17:20 UTC. Docker's log shows `POST /app/quit` (`actionMenuQuit`); it was not a crash.
- **I first misread the first quit as a kill** (its log ends with `signal: killed` for Docker's own child processes) and restarted Docker. After the second quit I asked before restarting it. The run finished the next morning.
- **The interruptions tested recovery.** Each time Docker came back, the worker found run 2 with a stale heartbeat and resumed it from its checkpoint (`attempts` 3):
  - first with 7 units already done, re-running the Maps unit that had been in flight;
  - then in post-discovery.
- **The machine's sleep and the overnight gap were not billed** to the budget: 971 active seconds.

## 2. Server mode: compose, health, API

- **Image.** It built in ~4 minutes. `docker compose ps`: `api` healthy, `worker` healthy.
- **Health.** `GET /api/v1/health` (and the unprefixed `/health`, 200) reported `ok`:
  - database: SQLite, schema 0006;
  - browser: Playwright 1.64.0 with chromium-1248;
  - disk: OK;
  - keys: none configured, all optional;
  - worker: 1 alive.

  For the first second after `up` it said `degraded` / "no worker is running", while the worker was still starting.
- **`POST /estimate`.** It returned the plan, the p50/p90 figures and the budget fit without a worker. For Austin it listed `unavailable: tenders|sam_gov` with the "key not configured … get one at https://sam.gov" reason.
- **`POST /runs`.** 201 `queued` for both. The worker claimed them in order and ran them one at a time, as designed.
- **Exports.** `GET /runs/{id}/export.xlsx` returned 200 with the XLSX content type. Pune: 40 KB, 79 rows, 50 columns. Austin: 44 KB, 72 rows, 54 columns.
  - Each workbook has exactly the two tabs.
  - The Overview figures are values; the filtered-view tiles are `SUBTOTAL` formulas.
  - Austin has the audit columns (Site, Audit Score, Audit Findings, Site Stack, Audited). Pune doesn't: its audit never ran (5.2).
  - Both have the tech_stack, app_store and hiring columns.
- **Stdout after a restart.** The restarted worker container's output stopped reaching `docker logs` after Docker Desktop came back, a Docker Desktop quirk. The run's event stream in the database (`/events/history`) was complete throughout. Only the log-based timings below come from the first process.

## 3. The runs

| Run | City | Status | Active | Estimate at start, p50–p90 | Units | New orgs | Delivered |
|---|---|---|---|---|---|---|---|
| 1 | Pune | completed, deadline reached | 1,197 s | 461–643 s (18 cold priors) | 16 done, 1 partial (cut), 1 failed (GDELT 429) | 79 | local_business 62, hiring 16, app_store 1; tech_stack checked 27 sites (6 WordPress, 1 outdated). **digital_audit: not run** |
| 2 | Austin | completed, 3 attempts | 971 s | 951–1,506 s (3 learned, 9 per source; 1,290 s p90 under the budget) | 11 done, 1 failed (GDELT 429), 1 skipped (SAM.gov key) | 72 | local_business 19, hiring 50, app_store 2; tech_stack 13 WordPress; digital_audit audited 67 (54 findings) |

## 4. Results per source

| Lead type / source | Pune (run 1) | Austin (run 2) | Verdict |
|---|---|---|---|
| `google_maps` | 2 units, 20 found, 20 new, 109 s | 2 units, 20 found, 19 new, 283 s | **Healthy.** Austin's units were slow: Maps place pages timed out and retried while the Mac was under heavy load (load average ~20) |
| `sulekha` | 43 found, 42 new, 22 s | — (India pack) | **Healthy.** The lowercase-slug fix from live check 2 holds |
| `justdial` | 0; `fetch_failed` ×2 | — | Blocked, as known since Phase 0 (not fixable without evasion) |
| `indiamart` | 0, no request | — | Opt-in per category; none set it (as live check 2) |
| `google_search` | 0; Google `/sorry` challenge, then 429 | 0 in 0.01–0.06 s (breaker) | **Blocked on this network too:** Google `/sorry` / 429 from the first rendered query, DuckDuckGo 202 "anomaly" challenge, later 403. Plain `curl` from the Mac gets DDG's challenge page too |
| `facebook` | 0; one unit 494 s in penalty waits | 0 in 0.01–0.2 s (breaker) | Rides on the same blocked SERP |
| `linkedin_serp` | 0; 1 cut by the engine at 42 s | 0; one unit 332 s, then the breaker | Same |
| `hn_whos_hiring` | 265 ads read, 16 new orgs, 4.4 s | 299 ads read, 50 new orgs, 7.6 s | **Healthy** |
| ATS boards / careers pages | 30 postings matched in all, 5 boards; 31 careers pages scanned | `greenhouse` / `ashby` `board_not_found` ×2 each (boards an HN ad links that don't exist); 10 careers pages in the last attempt | Healthy; dead domains → `fetch_failed` |
| `gdelt` | `rate_limited` (429) on the first query, 10 s | the same, 14 s | **Not the proxy:** `curl` from this connection also gets 429 (after 12 s). The breaker stopped at once, as designed. funding_news is still unverified live |
| `ted` / `find_a_tender` / `sam_gov` / `cppp` / `gem` | No portal applies to IN unless CPPP/GeM are opted in (their terms ask for permission), so no unit was planned | `sam_gov` skipped: "key not configured …", listed in the estimate's `unavailable` | Correct behaviour. **tenders is unverified live:** it needs a SAM.gov key, or an EU/UK place |
| `itunes` | 2 units, 50 apps, 62 s | 2 units, 49 apps, 53 s | **Healthy** |
| `google_play` / own-site store links | 20 listings read (quick budget spent: `budget_spent` recorded), 5 matched to an iOS app; 27 sites read; 1 app flagged (review complaints); 30 skipped as not in country, 8 by genre | 49 sites read, 4 store links found on sites, all 4 apps healthy; workbook: 1 "Not updated", 1 "Complaints" | Healthy; the country scope did its job |
| `site_fingerprint` (tech_stack) | 27 checked, 6 WordPress (1 outdated), 4 not checked (`blocked` ×2, robots ×1, unreachable ×1), 48 without a website | 13 WordPress; 49 skipped on resume as "checked < 30 days ago"; `blocked` ×10 | Healthy; refusals honestly "not checked" |
| `digital_audit` | **not run** (5.2) | 67 audited, 5 not checked (`blocked`), 16 with no website. Findings: no contact form 26, no website 16, incomplete Maps profile 5, no online booking 3, stale copyright 2, outdated technology 1, certificate expiring 1; 20 "nothing to fix" | Healthy when it gets its turn |

## 5. What failed, and the fixes (`fix/live-check-3`)

### 5.1 The SERP circuit breaker outlived its run (medium)

**Symptom.**
- Pune ended with two consecutive empty searches on the breaker. The cut LinkedIn search never counted.
- Austin ran in the same worker process. Its **first** failed search logged "search engines unreachable after **3** consecutive failures … disabling the SERP fallback for the rest of this run".
- Austin's Google Search and Facebook units, and its second LinkedIn unit, then finished in 0.01–0.2 s with nothing, without asking either engine (expected: 88–228 s each).

**Cause.**
- `_SerpHealth` was a module-level singleton (`SERP_HEALTH`), and nothing in the application ever reset it. Only `tests/test_phase1_acceptance.py` reset it, by hand.
- That is harmless for `python run.py`, which is one process per run.
- A worker (`docker compose`, `leadscraper worker`) executes run after run in one process. Once tripped, search stayed off for every later run until the worker restarted, days later on a server, even after the block lifted. A partly failed run also counted toward the next one's trip.
- Whether a run searched at all depended on how long its worker had been up. The third attempt of run 2, in a fresh process, started with a clean breaker.

**Fix.** The breaker is per run. `serp_health(ctx)` keys a `_SerpHealth` on the run's `RateLimiter`, which:
- every SERP caller already reaches through `ctx.limiter` (the pipeline, hiring's `board_search`, tech_stack's footprints and decision_makers' LinkedIn search share it);
- the runner creates fresh for each run.

A caller without a limiter falls back to the module's `SERP_HEALTH`. Within a run, behaviour is unchanged.

**Test:** `tests/test_core.py::test_serp_breaker_trips_per_run_not_per_worker_process`.
- Run A trips its breaker and stops calling.
- Run B, with its own limiter, must ask Google and DuckDuckGo again, and needs its own three failures to trip.
- On the base code run B makes 0 calls.

### 5.2 A post-discovery lead type the deadline never reached vanished without a trace (medium)

**Symptom.**
- Pune's discovery spent the budget down to the post-discovery reserve: the SERP units sat in Google's and DDG's penalty waits until the engine's cut point.
- Post-discovery then ran in `post_order`: hiring 87 s, app_store 92 s, tech_stack 122 s. tech_stack's site checks crossed the deadline at 1,110 s and finished in the tail.
- `digital_audit`, last in order, never started.
- The run said nothing about it: `lead_types.digital_audit` was all zeros with `failed: false`, there was no event and no log line, and the Overview still listed "Digital audit" among the lead types.
- Run 2, with time to spare, audited 67 organisations, so the lead type itself works.

**Cause.** The post-discovery loop `break`s at the first session it finds the run stopping. Discovery units that don't run are recorded `skipped` with a reason ("skipped units are reported"); post-discovery sessions had no equivalent.

**Fix.** Each session the deadline (or a pause or cancel) reaches before its turn is recorded:
- a warning in the log ("digital_audit: post-discovery not run (time budget spent)");
- an `activity` event with that text, which the SSE stream and `/events/history` carry;
- `stats.<lead type>.post_discovery = "not run: time budget spent"` and `stats.engine.post_skipped` on the run, so `GET /runs/{id}` says why.

`docs/engine.md` says so.

**Test:** `tests/test_engine.py::test_post_discovery_the_deadline_never_reached_is_reported`.
- The first session's post-discovery runs past the deadline, so the second never starts.
- The second must carry the reason in the run's stats and an event; the first, which did run, must not.
- The base code raises `KeyError: 'post_discovery'`.

**Not changed: why the audit was starved.**
- The post-discovery reserve is fixed when the run starts: `min(share × budget, post p50)`, here 140 s.
- The cold priors expected 23 new organisations, and discovery found 79 (Sulekha alone: 0.5 expected, 42 found). Post-discovery work grows with the organisations found, so it took ~300 s.
- That's calibration of cold priors, plus a design question: should the reserve be resized as yield comes in, or the audit run before tech_stack? It is not a breakage, so neither is done here (section 7).

## 6. Estimate vs actual

| | Basis | Estimate p50 / p90 | Actual (active) | Actual / p50 | New orgs: est → actual |
|---|---|---|---|---|---|
| Pune, `POST /estimate` and run 1 start | 18 cold priors | 461 / 643 s | 1,197 s (deadline) | 2.6 | 23.1 → 79 |
| Austin, `POST /estimate` (before run 1) | 12 cold priors | 410 / 576 s | — | — | 21.6 |
| Austin, run 2 start (after Pune) | 3 learned, 9 per source | 951 / 1,506 s (1,290 s under the budget) | 971 s | **1.02** | 33.0 → 71 |

Per source (seconds, actual vs expected):

| Source | Pune | Austin |
|---|---|---|
| `google_maps` (2 units) | 109 vs 146 | 283 vs 116 (machine under load) |
| `google_search` (2) | **209 vs 44** | 0.1 vs 176 (breaker, 5.1) |
| `facebook` (2) | **534 vs 140** | 0.2 vs 455 (breaker) |
| `linkedin_serp` (2) | 91 vs 49 (1 cut) | 332 vs 82 |
| `sulekha` (2) | 22 vs 38 | — |
| `justdial` (2) | 20 vs 44 | — |
| `hn_whos_hiring` | 4.4 vs 6.0 | 7.6 vs 5.0 |
| `gdelt` | 10 vs 31.5 (429) | 14 vs 17 (429) |
| `itunes` (2) | 62 vs 80 | 53 vs 68 |
| post-discovery | ~301 vs 140 (p90 186); the audit not run | hiring 19 + app_store 58 + tech_stack 12 + audit 125 s in the last attempt |

What these numbers say:
- **Pune's 2.6 × p50 is the budget doing its job on a blocked network.** The SERP units overran (Facebook: one unit 494 s against 70 s), and the engine let them run only until the time promised to later stages and post-discovery was at stake. Then it cut one (`linkedin_serp`, "into the 257 s promised to the work behind it"). Every stage-1 unit still ran, and the run ended at 1,197 of 1,200 s.
- **Cold yield priors are far too low for directories and HN.** Sulekha was expected to find 0.5 and found 42; the run overall expected 23 and found 79. That is what starved post-discovery (5.2).
- **Learned figures work across places.** Austin's estimate, made after Pune, landed at 1.02 × p50, with the SERP durations learned from Pune's blocks. The breaker made Austin's SERP units fast, which offset slow Maps. One run per place is still an observation, not a calibration.
- **A resume during post-discovery estimates the remainder as 15 s.** The last attempt's plan event shows p50 15 s with no post reserve; the remaining post-discovery took 214 s. That's harmless here (the budget had room), but a resumed run's ETA is wrong.

## 7. Observations, not fixed (not broken, or not code)

- **Search is refused from this connection too.**
  - Google answered its `/sorry` challenge, then 429; DDG answered 202 "anomaly", then 403.
  - Live check 2 blamed the corporate proxy. This network has no proxy, so the block follows the address range or the automated pattern, not only the proxy.
  - SERP-based sources (`google_search`, `facebook`, `linkedin_serp`, and the opt-in `board_search`, `linkedin_search`, `footprints`) remain unverified live.
- **GDELT throttles this connection** with 429 even for a single `curl`. funding_news is still unverified end to end (also open since Phase 6b).
- **Source health calls the blocked SERP sources "ok".** `GET /catalog/sources` shows `google_search`, `facebook` and `linkedin_serp` as `ok` with a 0 % block rate.
  - `SerpClient` swallows `BlockedError` and the sources record `no_results`, which health counts as "empty", not "blocked".
  - After three such runs they would turn `likely_broken`, which is also wrong.
  - Recording a SERP block as `blocked` would make health honest. It touches every SERP source's failure codes, so it's left as a recommendation.
- **Run stats after a resume cover the last attempt only.** `GET /runs/2` → `lead_types.hiring.postings_seen` is 17 (the third attempt), while HN read 299 ads in the second; `local_business.new` reads 0. The per-source counts (`per_source`, from the checkpoint) and `organizations` are cumulative and right. Summing session stats across attempts would need a design choice.
- **The post-discovery reserve isn't resized when discovery out-yields its estimate** (5.2). Options: re-size it from organisations found so far, or order the audit before tech_stack (both are `post_scope: run` over the same sites).
- **tenders had nothing to do in either place.** India's portals are opt-in (CPPP, GeM); SAM.gov needs a key. The estimate's `unavailable` list and the unit's skip reason say exactly that.
- **The Pune Overview says "Work units 16 of 18 done, 1 failed".** The cut (`partial`) unit is not named; it is in `/runs/1/units`.

## 8. Still unverified live

- `funding_news` (GDELT 429 on both networks), `tenders` (no key; no EU/UK place), `registrations` and `decision_makers` (not in scope; keys).
- SERP sources on a network that search engines don't refuse.
- PageSpeed (off at quick depth, needs a key), balanced depth with enrichment, AI.
- Postgres in server mode (`--profile postgres`); this check used SQLite on the volume.

Commits on this branch (after `main` @ `9fe98c3`):
- `fix(serp): the search circuit breaker is per run, not per worker process`
- `fix(engine): post-discovery the deadline never reached is reported, not dropped`
- and this report.
