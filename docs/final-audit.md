# Final audit: the whole system against APP-BUILD-PROMPT.md

Date: 2026-10-14 · Base: `main` @ `9fe98c3` (= `origin/main`, tags `phase-0` … `phase-8`) · Read-only: no code was changed.

Each phase had its own review. This is the first review of the system as a whole. It audits `main` against the plan's **Definition of done** and **Section A**, with most attention on A.2 (scope boundaries) and A.3 (lead-type isolation).

**Inputs read:** `APP-BUILD-PROMPT.md`, `CLAUDE.md`, `docs/phase-{0,1,2,3,4,5,6a,6b,7,8}-report.md`, `docs/live-check-{1,2}.md`, `docs/sources.md`. Claims were then checked against the code, the test suite and CI.

## Verdict

The build is functionally complete and well tested offline. Every A.7 lead type exists, the API has 80 operations, and every phase's acceptance tests pass on SQLite. No lead type imports another, and nothing outreach-shaped exists.

Four things stand between `main` and "done":

1. **The browser and HTTP client still carry inherited anti-bot evasion** (H1). No phase report mentions it. It is the exact technique the plan names as against A.2.4 when it rejects ScrapeGraphAI.
2. **Default-on inherited sources contradict the terms/robots policy the later phases applied** (H2). The A.2.4 owner review has been open since Phase 1.
3. **`main`'s CI is red** (H3). A flaky engine test fails on Linux 3.12, so the Phase 3 acceptance check "the CI matrix is green" is not met today.
4. **Much of the system has never run live** (H4): balanced depth with enrichment, the legacy presets end to end, every keyed source, every AI provider, funding_news, and search footprints.

## What I ran (2026-10-14)

| Check | Result |
|---|---|
| `python -m pytest tests/ -q` (default: offline, `live` deselected) | **953 passed, 7 skipped, 25 deselected** in 60 s; re-run in a second session the same day (scratch `LEADSCRAPER_DATA_DIR`): identical, 67 s. The skips are 6 opt-in Postgres tests plus 1 case that doesn't apply. The real-data characterisation layer ran, because the frozen copies are present. The Chromium smoke test ran. |
| `ruff check leadscraper tests tools run.py` | clean |
| `-m postgres` on a throwaway PostgreSQL 16 (Docker, 127.0.0.1, removed afterwards) | **6 passed**, including both real stores migrated into Postgres against the SQLite goldens |
| `tests/test_engine.py` on a fresh Postgres database (`LEADSCRAPER_TEST_ENGINE_DB_URL`) | **1 failed, 54 passed.** `test_estimator_p50_is_within_35_percent_on_a_recorded_fixture_run` fails at its cold-basis precondition (`{'prior': 45, 'source': 15} != {'prior': 60}`). Run alone on another fresh database, it passes (L1). |
| GitHub Actions, latest runs (re-checked in the second session: no newer run) | `main` run 38514545078: **failure** (test, ubuntu-latest, 3.12). The same commit on `fix/ci-after-phase-8` (run 38514137415): success (H3). |
| `-m live` | **Not run.** These are third-party requests, and the Phase 8 report records an accidental round already. The last live results are the ones in the phase reports. |

The second session repeated the default suite, ruff and the CI check, and re-checked the cited evidence for H1–H3 and M1–M5, M8 and L2 against `9fe98c3`; all of it still holds. The two Postgres rows are from the first session and weren't repeated.

No live scraping was done. The only outbound requests were three `robots.txt` fetches (google.com, duckduckgo.com, html.duckduckgo.com) to confirm H2, plus GitHub API reads of the CI runs.

---

## Findings, ranked by severity

Severity reflects the plan. **High** means a hard boundary (A.2) is crossed, or an acceptance check or Definition-of-done item fails today. **Medium** means something promised is missing or doesn't work as described. **Low** covers smaller defects, test hygiene and documentation drift.

### High

#### H1. Anti-bot evasion in the shared browser and HTTP client (A.2.4). No report flags it.

- **Browser fingerprint masking.** `leadscraper/net/browser.py:24–36` injects a `_STEALTH_INIT` script into every page context (`:156–157`). It:
  - hides `navigator.webdriver`;
  - fakes `navigator.plugins` and `navigator.languages`;
  - adds a `window.chrome` stub;
  - patches `permissions.query`.

  Chromium is also launched with `--disable-blink-features=AutomationControlled` (`:136`). The code's own comment says the script "removes the most obvious automation tells".
- **User-Agent rotation.** `leadscraper/net/http.py:27–35` holds a pool of real desktop browser UA strings, "rotating these is the cheapest way to avoid the trivial 'same UA 500 times' fingerprint". A random one is used:
  - per browser context (`browser.py:148`);
  - per HTTP session and request (`http.py:281, 401, 462, 479`).
- **Why this crosses the line.** A.2.4 says "no CAPTCHA solving, no proxy rotation for evasion". Phase 5.6 rejected ScrapeGraphAI because "it ships anti-bot evasion (undetected Playwright/chromedriver …), against A.2.4". Masking `navigator.webdriver` and disabling `AutomationControlled` is what "undetected Playwright" does.
- **Where it applies.** Every Playwright source (Google Maps, Justdial, Sulekha, IndiaMART, WeddingWire, the hospitality platforms) and every `HttpClient` fetch: business websites, careers and team pages, audits, ATS APIs, Google Play listings.
- **Only some sources identify themselves.** Only news, tenders, iTunes, OpenCorporates and the geocoder send the identifying `core.geo.USER_AGENT`. robots.txt is evaluated as `*` (`net/robots.py:32`), so the client never presents a name a site could address.
- **History.** The code is in the baseline import (`016e044`). Phase 1 moved it without comment, and no report or review mentions it.
- **Resolution (owner's call):**
  - remove the stealth script and the AutomationControlled flag;
  - send one honest, identifying User-Agent everywhere, as the Phase 6–8 API clients already do.

  Expect Google, Justdial and Instagram to block sooner. That is the cost A.2.4 accepts.

#### H2. Default-on inherited sources contradict the policy Phases 6–8 applied. The A.2.4 review has been open since Phase 1.

- **The later phases' rule.** Phases 6–8 settled a consistent rule. A source whose terms restrict automated access, or whose robots.txt disallows the path, is **off by default** (`board_search`, `linkedin_search`, `footprints`, `google_news`, `cppp`, `gem` …). Play search was refused outright because robots.txt disallows `/store/search` (Phase 8 §4).
- **The inherited sources break that rule.** For any new config they are **on by default** (`leadtypes/local_business/config.py:137–158`, `DEFAULT_SOURCES`):
  - `google_search`, `facebook` discovery and `linkedin_serp` fetch `https://www.google.com/search`.
    - google.com's robots.txt has `User-agent: *` / `Disallow: /search` (fetched 2026-10-14).
    - `linkedin_serp` is not even flagged: `sources/notes.py:92–95` has `review: False`, although it uses the same mechanism as `linkedin_search`, which is flagged and off.
  - `justdial`: "blocks automated clients" (`docs/sources.md`). It is retried on every Indian run and answered with a 14-byte body or `ERR_HTTP2_PROTOCOL_ERROR` (live check 2). Unflagged.
  - `instagram`: rate-limits fast; public profiles only.
  - `airbnb` and `booking` (hospitality vertical): "terms prohibit scraping", and Booking has an affiliate API.
  - `google_maps`: robots.txt **allows** `/maps/search/` and `/maps/place/`, but the terms restrict automated access and a paid Places API exists.
  - `itunes` and `google_play`: on by default, marked for review (Phase 8).
- **Status.** Every report from Phase 1 to Phase 8 lists the review as "still open and still yours". The full list of flagged sources and their states is in the "Sources flagged for A.2.4" section below.
- **Resolution:** the owner decides per source. The minimum is consistency: Google SERP riders off by default (as `board_search` is), `linkedin_serp` flagged, Justdial off by default.

#### H3. `main`'s CI is red. Phase 3 acceptance ("the CI matrix is green") is not met.

- **Background.** Phases 4/5–8 were pushed only on 2026-10-14, so these are their first CI runs. The first run on `main` failed at collection on Windows (`%-d` in `test_app_store.py`); `9fe98c3` fixed that.
- **The current failure.** Run 38514545078 on `main` fails `tests/test_engine.py::test_without_a_budget_every_unit_runs_in_plan_order` on ubuntu-latest / 3.12 (`order` has an extra `'A'` after `A00…A04`). The same commit passed on `fix/ci-after-phase-8`, and macOS, Windows and Linux 3.14 passed in the same run.
- **Likely cause** (not reproduced locally in 6 tries; it depends on timing):
  - the test just before it, `test_an_abandoned_unit_is_not_left_running_in_a_finished_run` (`tests/test_engine.py:~222`), starts a unit with `"area": "A"` that hangs for 1.5 s;
  - the engine abandons it, but its daemon thread lives on (the Phase 2 open issue "an abandoned unit keeps its daemon thread until the process exits");
  - when it wakes, it calls the module-global `HOOKS["after_unit"]` that the next test has installed.

  The extra item is exactly that unit's area. The Phase 7 and 8 reports saw the same test flake on Postgres and left it "not settled".
- **Resolution:**
  - make the synthetic session ignore hooks from a run that isn't the current one, or join or neutralise abandoned threads between tests;
  - re-run CI until `main` is green.

  In production the stray thread's writes are contained (Phase 2). The defect is in test isolation, but it keeps `main` red.

#### H4. Large parts of the Definition of done have never run live.

Every acceptance check is met offline. The reports and live checks list what was never exercised against real services. None of it was closed later:

| Never verified live | Why it matters | Raised in |
|---|---|---|
| **Balanced depth with enrichment:** the website enricher, maps_lookup, Instagram, concurrent enrichment, `enrichment_marks`. Both live checks ran `quick`, which turns enrichment off. | The website enricher is where emails and socials come from. Phase 0 items 0.6d/0.6e/0.7 (contact names, email coverage, `source_corroboration`) stay "unblocked, not verifiable offline". | Phase 0 §3, Phase 2 §15, live check 1 §6, live check 2 §8 |
| **The Mumbai and Nashik presets end to end** after the refactor | The DoD says "the Mumbai and Nashik runs still work as presets". They are proven offline: characterisation, plan, key-for-key compile. Live, `run.py` on them stops until `migrate-legacy` is run into the real data directory, which was never done. | Phase 1 §14 → Phase 8 §11 |
| **Every AI provider** (Anthropic, OpenAI, Gemini, OpenAI-compatible) | `tests/test_ai_live.py` always skipped (no key). The only real call was one accidental `claude` CLI call. The OpenAI/Gemini model ids and the Anthropic web-search price are unconfirmed. | Phase 5 §13 |
| **Keyed sources:** SAM.gov, Companies House (registrations and decision_makers), PageSpeed, OpenCorporates | No 200 was ever seen; the fixtures follow documented shapes | Phases 6a, 6b, 7 |
| **funding_news end to end** | GDELT answered 429 on every live run from this network. The GlobeNewswire live test failed in the Phase 8 accidental run. | Phase 6b §11, live check 2 §7, Phase 8 §11 |
| **tech_stack `footprints`**, and every SERP source since live check 1 | SERP is blocked from this network. Untested: the real Google → DuckDuckGo path for footprints. | Phase 8 §11, live check 2 §7 |
| **SMTP / webhook notifications, a schedule firing on a real clock, a long container run, desktop notifications on Windows/Linux** | Mocked only | Phase 3 §15 |
| **Opening the workbooks in Excel / LibreOffice**, including filtering Records to see the SUBTOTAL "Filtered view" move | Checked with openpyxl and Quick Look only | Phase 4 §13 → Phase 8 §11 |

**Resolution:** do one live check from an unblocked connection (not this Mac's Zscaler egress) with keys set:
- the Nashik preset at balanced depth with `--time-budget 30`, after `migrate-legacy` into a scratch directory;
- a GB run with Companies House;
- a US run with SAM.gov;
- AI on with one billed provider;
- `pytest -m live`.

### Medium

#### M1. `icp` is drafted by the ICP builder but never acted on; `filters.exclude_chains` isn't implemented (A.8, DoD #1).

- **`icp` is ignored at run time.** `config/schema.py:288–300` (`unapplied()`) still names both. The ICP builder writes `icp.industries`, `company_size` and `technologies` into every draft (`ai/features/icp_builder.py:149`). So "describe a target" yields an ICP that only `compile.py:122` copies into the snapshot.
- **What it could steer.** Nothing filters by it, although usable signals exist: AI industry tags, the AI size estimate, tech_stack's detected technologies.
- **`exclude_chains`.** The schema default is `false` (`:98`), but the A.8 example shows `true`, and there is no chain detection. The Phase 8 app_store `max_ratings` and the tenders/news "national brand" notes are partial, per-lead-type stand-ins.
- **Status.** Open since Phase 1 §14 ("`filters` has no phase in the plan").

#### M2. The preview only knows `local_business` (Phase 2.7, A.3).

- **What the plan says.** Phase 2.7: "one area × one category × top sources **per lead type**".
- **What the code does.** `engine/preview.py:46–49` imports `LOCAL_BUSINESS` directly (the engine depending on one lead type) and takes only its two top discovery sources. Sample records are its legacy `to_row()` columns (`:126–131`), not the Records model.
- **Probe.** For a GB config with only `tenders` + `hiring`, `plan_preview` returns `categories []`, `sources []`.
- **Tests.** Both preview tests (`test_engine.py:1006`, `test_api.py:282`) are local_business-only.
- **Consequence.** DoD #3 ("see an estimate, then preview") holds only for configs that include local_business.

#### M3. `organizations.score` is local_business's score for every organisation (open since Phase 4).

- **The default scorer.** `db/store.py:126–129`: `LeadStore` defaults to `leadtypes.local_business.scoring.Scorer`, so the db layer imports a lead type.
- **Who gets that score.** Every organisation, including a tender's public buyer, an app developer or a news-only company, gets the local_business legitimacy score at write time (`_score`, `:500–512`).
- **Where it is used.** `GET /organizations` sorts and filters on it (`services/organizations.py:51, 111–114, 128`; default `-score`).
- **What differs.** The export's combined, renormalised score (`export/records.score_records`), which is what the workbook shows.
- **Consequence.** A UI's organisation list would rank differently from the deliverable. Raised in Phase 4 §13 and Phase 5 §13; never closed.

#### M4. "Re-run next month and get only what's new" ignores new signals on known organisations (DoD #8).

- **How `scope=new` decides.** It keeps run members whose action is `new` (`services/exports.py:351–360`). A scheduled `only_new` run defaults to it.
- **How a signal is recorded.** A lead type that attaches a signal to an organisation the project already holds stamps it `updated` (`db/store.py:742`). That covers a new tender, job posting, funding round, app finding or incorporation.
- **Consequence.** For every signal lead type, next month's "what's new" export drops the most valuable rows: fresh buying signals on companies already known.
- **Also.** A refreshed posting and a brand-new one are both `updated`, so the diff can't tell them apart either.
- **Resolution (needs design):** a "new signal since run N" scope, or `signals.observed_at` ≥ the previous run.

#### M5. The plain `pip install` path (A.4) loses the presets.

- **Where presets are read from.** They are resolved from the source checkout: `PROJECT_ROOT / "presets"` (`paths.py:17`, `cli/__init__.py:39`, `services/bundle.py:145`).
- **What the wheel ships.** `pyproject.toml` package-data ships only `db` migrations, `ai/*.yaml` and `sources/web/*.yaml`.
- **Consequence.** A non-editable install has no `leadscraper presets`, no `GET /presets` entries, and no shipped presets in bundles. Legacy relative paths (`data/leads.csv`) would resolve inside `site-packages`.
- **The workaround.** The Docker image works only because it uses an editable install (Phase 3 §6).
- **No test.** Nothing builds a wheel and runs from it. Phase 8 found a missing `__init__.py` the same way, by review.

#### M6. Lead-type packs leave India (Skillminds' home market) and other countries thin by default.

These are deviations made for honest reasons (terms, no API), but the result for an Indian run is:

| Lead type | Indian sources by default | Reason |
|---|---|---|
| `tenders` | **none** (CPPP and GeM are built but off) | Their copyright policies ask for permission |
| `registrations` | **none** | No MCA API; data.gov.in's data stops at 2023-11-03 |
| `decision_makers` registry | **none** (UK only) | No free Indian registry |
| `hiring` country job boards | none, anywhere | Naukri and Arbeitnow terms |
| `funding_news` regional feeds | none, in any pack | Inc42 and YourStory terms; GDELT, the default, is throttled from this network |
| `app_store` | country scope needs explicit evidence | A domestic app that names neither its country nor a place, and has no Play twin, is dropped (Phase 8 §10) |

Asking GeM and CPPP for written permission would unlock Indian tenders. That decision has been open since Phase 7.

#### M7. The estimator ignores discovery stages and the later-stage reservation (open since Phase 6a).

- **The gap.** `engine/estimator.py` has no notion of `discovery_stage`. Stage-1 units (hiring, funding_news, tenders, registrations, app_store, uploads) are simulated overlapping stage 0.
- **The live-check-2 reservation isn't modelled either.** That is the rule that, under a budget, keeps time for later-stage units that outrank the candidate.
- **Consequence.** Budgeted multi-lead-type estimates (p50/p90, "what fits", "what is skipped") can be wrong exactly when the budget binds.
- **Status.** Raised in Phase 6a §11 and repeated in live check 2 §6; no later commit touches it.

#### M8. `web_search_discovery` exists only inside local_business (Phase 5.4).

- **What the plan says.** "Uses provider web-search tools as a discovery source **in any lead type**."
- **What exists.** `ai_web_search` is a core `Source` that only local_business builds (`sources/registry.py`, `leadtypes/local_business/estimates.py`). No signal lead type (hiring, funding_news, tenders …) can use it.

### Low

| # | Finding | Evidence | Raised |
|---|---|---|---|
| L1 | **Postgres test isolation.** On one Postgres database the engine suite fails the Phase 2 acceptance test's precondition, because `unit_stats` are workspace-wide and earlier tests' stats leak in (verified today: 1 failed, 54 passed; passes alone). The plan-order test (H3) and `test_us_run_end_to_end…` also flake on shared databases. So the "±35 %" check is demonstrated on SQLite only, while `CLAUDE.md` documents the shared-database command. | `tests/test_engine.py:296–310` | Phase 6a §11, 7 §12, 8 §11 |
| L2 | **Deleting a lead type needs a registry edit,** and one built-in failing to import breaks them all. Built-ins are imported in one unguarded block (`leadtypes/registry.py:38–46`); plugins get per-module containment. Lead-type keys are also hard-coded outside `leadtypes/`: `services/configs.py:61` (which lead types' `targets_file` to refuse; a new upload lead type would be unguarded), the CLI's `--lead-type` choices (`cli/app_cmds.py:611`), and `icp_builder.LATER_PHASES` (stale name: every lead type is built). A.3: "deleting one lead type never changes another's output". | as listed | new |
| L3 | **Bundles omit approved candidate sources and selector sets** (and notification targets, deliberately: webhook URLs are bearer secrets). AI source approvals don't move between machines except via server mode. | `services/bundle.py` | Phase 5 §13 |
| L4 | **Webhook URLs are stored in plain text** in `notification_targets.config` (masked in responses, kept out of bundles). | `db/models.py:~572–584` | Phase 3 §15 |
| L5 | **Residual SSRF limits** on an unauthenticated API: DNS rebinding between check and connect; robots.txt not re-checked after a redirect or for the HTTP fallback origin. | `net/http.py` `PublicOnlyAdapter` | Phase 6a §11 |
| L6 | **Tender closing dates are shown as UTC dates**, a day off for midnight-ish deadlines in some zones. | | Phase 7 §12 |
| L7 | **A Companies House 401 is retried 3 times** before `key_rejected`. | | Phase 7 §12 |
| L8 | **Workbook `created`/`modified`** are local time with a `Z` suffix. | `export/excel.py:522–563` | Phase 6a §11 |
| L9 | **Abandoned units keep their daemon threads** until the process exits; see H3 for a visible consequence. | `engine/` | Phase 2 §15 |
| L10 | **`urllib.robotparser` uses first match,** not RFC 9309's longest match, so it reads more conservatively. | `net/robots.py:28` | Phase 3 §15 |
| L11 | **Erasure doesn't rewrite files already written** (project CSV, `skipped.csv`, logs, earlier exports, a Google Sheet). | | Phase 3 §15 |
| L12 | **IndiaMART units are planned and estimated when no category opts in;** they then return at once. The plan and estimate over-count. | | live check 2 §7 |
| L13 | **No per-run SERP breaker for unbudgeted runs:** a blocked Google costs up to +225 s per request until the 3-failure breaker trips. A budget now bounds it. Policy decision left open. | `sources/google_search.py:75` | live check 2 §7 |
| L14 | **CLI and report wording:** two durations ("RUN COMPLETE in 35.7 min" wall vs "completed after 19.3 min" active); "Combinations processed 0/2" when any unit was cut; hiring's `signals` stat counts new signals only; "not requested" for enrichers turned off by depth. | | live checks 1 & 2 |
| L15 | **`docs/sources.md` "Last verified" has drifted** from its machine twin and the live checks. `google_maps` says "not re-verified" while `sources/notes.py` says live check 1. `google_search`, `facebook`, `linkedin_serp`, `justdial`, `sulekha` (and its lowercase-slug fix) and `indiamart` ran in live check 2 but their rows aren't updated. | `docs/sources.md:22–81` | new |
| L16 | **CI hygiene:** `actions/upload-artifact@v4` targets the deprecated Node 20; `ubuntu-latest` moves to Ubuntu 26 from 2026-10-19. The two Ctrl-C tests skip on Windows, where Ctrl-C/Ctrl-Break is covered only by the shared stop-path tests. | CI annotations | Phase 2 §15 + new |
| L17 | **The TLS-inspecting proxy breaks `certifi`-based requests** for some domains. `truststore` would use the OS store. Environment, not code. | | live check 2 §7 |

### Accepted deviations (recorded, reasoned, not defects)

For completeness: each was justified in its report, and nothing here asks to reopen them.

- **Core sources stay in `sources/`** (A.5), so lead types share them.
- **`requests`, not `httpx` with HTTP/2** (A.4).
- **The engine falls short of REBUILD Part 2's speed targets:** 7.6× enrichment against ≥10×, and 1.2–1.9× discovery against ≥3×, bounded by one Google host.
- **`balanced` depth = the configured settings,** not Part 3.3's numbers.
- **Self-healing selectors are CSS only** (no XPath).
- **Fingerprint rules are our own,** not an open-source set (Wappalyzer went closed; the fork is GPL).
- **Google News, GlobeNewswire, GitHub, LinkedIn search, Workable, SmartRecruiters, CPPP, GeM, OpenCorporates and footprints are off by default.**
- **App complaint themes come from the three reviews Play's listing shows;** Apple's reviews aren't used.
- **The preview is asynchronous** through the queue, and config-assist AI calls are synchronous in the API.
- **Overview figures are values,** with only the "Filtered view" as formulas.
- **The estimator's "recorded fixture" is synthetic.**

---

## Acceptance checks, phase by phase (status on `main` today)

| Phase | Check | Status |
|---|---|---|
| 0 | Suite green; characterisation for dedup, scoring, tiering, both layouts; Part 0 table | **Met.** Characterisation passes against the frozen copies today. Items 0.6d/0.6e/0.7 remain "not verifiable offline" (H4). |
| 1 | Presets load; migrated re-export reproduces the characterisation; Austin US run activates core + social only, US phones, no India-pack failures; disabling a lead type leaves another unchanged | **Met** (SQLite and Postgres) |
| 2 | Worker kill/resume loses ≤ 1 unit | **Met** (a real worker process) |
| 2 | 15-minute budget within budget + tail, skipped units reported | **Met** (fake clock). Live: live check 2 runs 4–5 finished in 17.0–17.2 min against a 20-minute budget. |
| 2 | Estimator p50 within ±35 % on a recorded fixture | **Met on SQLite** (synthetic recording). On a shared Postgres database the test's precondition fails (L1). |
| 2 | Pause/resume/cancel tested | **Met** |
| 2 | Keep-awake (mocked) on three OSes in CI | **Passes on macOS and Windows on `main`; the Linux 3.12 job is red** for an unrelated test (H3) |
| 3 | OpenAPI covers every endpoint with examples | **Met** (80 operations) |
| 3 | API end-to-end: config → estimate → run → events → export | **Met** |
| 3 | Bundle made on one OS imports on another (CI) | **Met** on `main` (bundle-make/check ×3 green) |
| 3 | `docker compose up` passes `/health` | **Met** on `main` (docker job green) |
| 3 | CI matrix green | **Not met on `main`** (H3) |
| 4 | Golden workbook tests; four fixture kinds; samples in `docs/samples/` | **Met** (9 scenarios now; `test_samples_are_current` passes). Never opened in Excel (H4). |
| 5 | Provider conformance (mocked); failing provider = AI off; budget cap; hallucinated contacts rejected; ICP builder valid; page prep ≥ 50 %; self-healing three paths | **Met offline.** No live provider call (H4). |
| 6–8 | Per lead type: fixture tests, isolation, live smoke test, workbook columns, dated `sources.md` | **Met when each phase ran.** The keyed smoke tests skip without keys (SAM.gov, Companies House, OpenCorporates). The GlobeNewswire live test failed in the Phase 8 accidental run. |
| 7 | Tenders/registrations packs inactive where they don't apply | **Met** |

## Definition of done

| Item | Status |
|---|---|
| 1. Describe a target, or pick a preset | **Partly.** The ICP builder works (AI or offering-only), and presets are listed by `GET /presets`. But the drafted `icp` doesn't steer the run (M1), and a plain `pip install` has no presets (M5). |
| 2. Adjust places, lead types, sources, fields | **Met:** saved configs with versions, `/schema/run-config` with UI hints, `fields.include` switching off enrichers |
| 3. See an estimate, then preview | **Partly.** The estimate is met, but stages are ignored (M7). The preview covers local_business only (M2). |
| 4. Run within a time budget, locally kept awake or on the server | **Met** (budget scheduler, cut/abandon, wakepy, Docker/compose) |
| 5. Pause and resume | **Met** |
| 6. Come back to the run in history | **Met:** snapshot, per-source counts, estimate vs actual, events replay, failures, files |
| 7. Download a two-tab workbook matching the reference style | **Met in structure** (golden files); not checked in Excel (H4) |
| 8. Re-run next month and get only what's new | **Partly:** only new *organisations*; new signals on known ones are excluded (M4) |
| Configs and projects move between machines (bundle or server mode) | **Met**, minus AI candidate sources and selector sets (L3) |
| The Mumbai and Nashik runs still work as presets | **Met offline** (characterisation, compile-back, Postgres). Never run live after the refactor; needs `migrate-legacy` into the real data directory first (H4). |
| No UI, no login, no outreach | **Met.** No UI. One `current_workspace` dependency (`api/deps.py`; no router builds a workspace itself). `workspace_id` on every owned table (only `workspaces` and `workers` lack it). No contact status, sending, sequences or pitch drafting; `lead_summary` rejects pitch, CTA and contact text; notifications go only to our own targets. |
| The API is complete enough that a frontend is pure presentation | **Largely met** (80 operations: runs, control, SSE, exports, bundles, schedules, suppression, settings, AI, catalog, health). Two gaps: keys can't be set through the API (by design: Phase 5.2, "the API only reports whether a key is configured"), and the preview gap (M2). |

## Section A compliance

| Clause | Status |
|---|---|
| A.2.1 No UI | Met |
| A.2.2 workspace_id + one `current_workspace` | Met |
| A.2.3 No outreach / CRM | Met |
| A.2.4 Polite, lawful collection | **Not met: H1** (fingerprint evasion, UA rotation) and **H2** (default-on sources against terms or robots.txt; owner review open). Met: per-host rate limits with penalties, no CAPTCHA solving, no proxies, no logins, LinkedIn never fetched, Upwork/Indeed/Glassdoor/LinkedIn Jobs excluded, robots.txt honoured by every generic-site crawler (website enricher, careers and team pages, audit, tech_stack, site_apps, AI web search/directory). Directory and platform sources don't consult robots.txt; A.2.4 scopes robots.txt to generic crawling. |
| A.2.5 Contact data honesty | Met. AI contacts must be grounded on the page; only the email pattern infers, labelled and never published or matched on. |
| A.2.6 Existing data is precious | Met. Originals are refused, only copies imported; no real `.xlsx`/`.csv`/DB is tracked by git (only `docs/samples/*.xlsx`). |
| A.3 API-first, one core | Met (services layer). Exceptions noted: `engine/preview.py` and `db/store.py` import local_business (M2, M3). |
| A.3 Nothing crashes a run | Met (containment tests per lead type and source) |
| A.3 Lead types are isolated plugins | **Met for imports and output:** no lead type imports another (checked across all nine packages); isolation tests compare local_business's rows, final leads and enrichment targets with each other lead type on, off and crashing. **Weaker for "deleting":** L2, and the two core-to-local_business dependencies above. |
| A.3 Packs silently inactive | Met (test per pack, Phase 1 and Phase 7) |
| A.3 Jobs out of process; API never scrapes | Met. The worker queue and the async preview; config-assist AI calls are synchronous model calls, not scraping. |
| A.3 Cross-platform | Met in code (no `os.path`, no `shell=True`, `platformdirs`, stop by DB flag). CI is red on one Linux job (H3). |
| A.3 Explainability | Met (score reasons; evidence per write with source, URL and time; signal `source_url`; AI attributes labelled with their producer) |
| A.4 Stack | Met except `httpx`/HTTP/2 (accepted) and the plain `pip install` path (M5) |
| A.5 Layout | Met, with the documented `sources/` deviation |
| A.6 Domain model | Met: every entity present; signals carry type, lead_type, title, payload, observed/published/expires, strength, source_url; contacts carry kind, source and confidence |
| A.7 Contract | Met, and extended (sessions, stages, `post_order`, `unit_skip_reason`, `score(now=, record=)`) |
| A.8 Run config | Met, except `icp` and `filters.exclude_chains` (M1) |

## Open issues carried in reports and never closed

Each item was traced to the latest report and to `git log` after it. None has a later fixing commit.

| Issue | First raised | Latest mention | Owner |
|---|---|---|---|
| A.2.4 terms review of inherited sources | Phase 1 §14 | Phase 8 §11 | **You** (H2) |
| VayaVia phone-similarity threshold (operator properties merge) | Phase 1 §14 | Phase 3 §15 | You |
| Bridging upserts not consolidated (measured: 1 wrong extra merge; recommended leaving) | Phase 1 §14 | Phase 1 §15 | You |
| Run `migrate-legacy` into the real data directory | Phase 1 §14 | Phase 8 §11 | You |
| Check the real per-user DB for the Phase 2 migration cascade-delete bug | Phase 3 §15 | Phase 8 §11 | You (`db info`, `runs list`). Not checked here, deliberately: it would open the real database. |
| Rotate the old Google service-account key (`eighth-physics-491407-c6`) | Phase 0 §1 | `.env.example:11–13` | You; can't be verified from code |
| Keys: `sam-gov-api-key`, `companies-house-api-key`, `pagespeed-api-key` (+ OpenCorporates, paid) | Phase 6a / 7 | Phase 8 §11 | You |
| Live AI provider checks; unconfirmed default model ids and web-search price | Phase 5 §13 | Phase 8 §11 | You (H4) |
| Anthropic server-side refusal fallback on by default ("say if you'd rather not") | Phase 5 §1 | never answered | You |
| Opening the samples in Excel; checking the Filtered view follows the autofilter | Phase 4 §13 | Phase 8 §11 | You (H4) |
| Balanced-depth live check (enrichment) | live check 1 §6 | Phase 5 §13 | H4 |
| Each approved AI candidate source is an A.2.4 decision (nothing checks its `terms_note`) | Phase 5 §13 | — | You, per approval |
| `organizations.score` not combined across lead types | Phase 4 §13 | Phase 5 §13 | M3 |
| Estimator ignores stages | Phase 6a §11 | live check 2 §6 | M7 |
| `filters.exclude_chains`, `icp` not applied | Phase 1 §6 | Phase 4 §13 | M1 |
| Candidate sources and selector sets not in bundles | Phase 5 §13 | — | L3 |
| Webhook URLs in plain text | Phase 3 §15 | — | L4 |
| Residual SSRF (rebinding, robots after redirect) | Phase 6a §11 | — | L5 |
| Abandoned units keep their threads | Phase 2 §15 | — | L9 / H3 |
| Postgres test isolation (estimator, plan-order, US run) | Phase 6a §11 | Phase 8 §11 ("isn't settled") | L1 / H3 |
| Tender closing dates as UTC dates; Companies House 401 retried ×3 | Phase 7 §12 | — | L6, L7 |
| Workbook dates local time with `Z` | Phase 6a §11 | — | L8 |
| `robotparser` first-match | Phase 3 §15 | — | L10 |
| Erasure doesn't rewrite files already written | Phase 3 §15 | — | L11 |
| IndiaMART over-planned; SERP breaker policy; truststore; CLI wording | live check 2 §7 | — | L12–L14, L17 |
| GDELT throttling (consider a per-query/day cache or bulk datasets) | Phase 6b §11 | live check 2 §7 | Design |
| Heuristic precision: HN roles, news headlines (joint announcements, global companies), app country evidence, three-review complaint samples | 6a / 6b / 8 | — | Informational; the AI second opinion is opt-in |
| News-only companies have no website, so decision_makers and digital_audit can't follow up | Phase 6b §11 | — | Informational |
| Team pages rendered by JavaScript are invisible (requests only) | Phase 6b §11 | — | Informational |

**Closed since raised, for the record:**
- the Phase 0 identity issues (Phase 1);
- the Maps search waste and budgeted tail estimate (live check 1);
- the stuck SERP unit and Sulekha slugs (live check 2);
- the merge order of the branch chain (merged and tagged 2026-10-14);
- unchanged re-finds as run members (Phase 3);
- the fast-loop command that ran live tests (Phase 8, `CLAUDE.md`);
- the Windows `%-d` collection error (`9fe98c3`).

## Sources flagged in `docs/sources.md` for review against A.2.4

"Default" is the state for a new config that leaves `sources` out; the presets set their own.

| Source | Where | Default | Why flagged | Last verified live |
|---|---|---|---|---|
| `google_maps` | core | **on** | Terms restrict automated access; a paid Places API exists. robots.txt allows `/maps/search/` and `/maps/place/`. | live check 2 (2026-10-13; sources.md not updated, L15) |
| `google_search` | core | **on** | Terms restrict automated queries; **robots.txt disallows `/search`**; DuckDuckGo fallback (`html.duckduckgo.com` robots allows `/`) | live check 2: blocked |
| `maps_lookup` (enricher) | core | off | As `google_maps` | never |
| `facebook` | social pack | **on** | Graph API restricted; discovery via Google `/search`, then `mbasic` pages | live check 2: blocked SERP |
| `instagram` (enricher) | social pack | **on** | Graph API restricted; rate-limits fast | never since Phase 0 |
| `linkedin_serp` | social pack | **on** | **Not flagged** (`notes.py` `review: False`), though it uses the same mechanism as the flagged `linkedin_search` | live check 2: blocked SERP |
| `justdial` | in pack | **on** | **Not flagged**; "blocks automated clients"; always blocked when tried | live check 2: blocked |
| `airbnb` | hospitality pack | **on** (vertical) | Terms prohibit scraping | never since Phase 0 |
| `booking` | hospitality pack | **on** (vertical) | Terms prohibit scraping; affiliate API exists | never since Phase 0 |
| `itunes` | app_store | **on** | Apple's terms are written for partners promoting store content; robots.txt disallows `/search*` for crawlers (it is the documented API) | 2026-10-13 |
| `google_play` | app_store | **on** | Listing pages only (robots allows them); Google's terms | 2026-10-13 |
| `workable` | hiring | off | Undocumented widget endpoint | 2026-10-07 |
| `smartrecruiters` | hiring | off | Docs now describe key-based access | 2026-10-07 |
| `board_search` | hiring | off | Google SERP | never |
| `google_news` | funding_news | off | The feed's terms: personal, non-commercial only | 2026-10-08 |
| `press_releases` (GlobeNewswire) | funding_news | off | Terms page not found | 2026-10-08; the live test failed in Phase 8 |
| `feeds` | funding_news | on (empty) | Each feed's terms are the config author's call | n/a |
| `linkedin_search` | decision_makers | off | Google SERP; LinkedIn never fetched | never |
| `github` | decision_makers | off | GitHub's AUP on personal information | 2026-10-08 |
| `cppp` | tenders (in) | off | Copyright policy asks for permission | 2026-10-13 |
| `gem` | tenders (in) | off | Copyright policy asks for written permission | 2026-10-13 |
| `opencorporates` | registrations | off | Free tokens exclude companies; needs a paid key | 2026-10-13 (401 without a token) |
| `footprints` | tech_stack | off | Google SERP | never (SERP blocked here) |
| `ai_directory` | core (AI) | per approval | Each approved candidate is an A.2.4 decision | n/a |

Also listed in `docs/sources.md` "Open questions for the project owner": Google Maps and Search, Airbnb and Booking.com, Instagram and Facebook.

`indiamart` ("IndiaMART runs a seller API, not a search API") and the stay platforms (`stayvista`, `saffronstays`, `elivaas`, sitemap-driven) aren't flagged and haven't been re-verified since Phase 1.

## Suggested order of work

1. **H1:** decide on, and remove, the stealth script and UA rotation.
2. **H2:** decide the A.2.4 list above. At minimum, make the defaults consistent with the Phase 6–8 rule.
3. **H3:** fix the abandoned-thread test leak and get `main` green.
4. **H4:** one live check from an unblocked connection with keys set (balanced depth, a preset after `migrate-legacy` into a scratch directory, GB and US keyed sources, one AI provider, `pytest -m live`). Open the samples in Excel.
5. **M4 and M1:** decide what "only what's new" means for signals, and whether `icp` should steer runs.
6. **M2, M3, M5:** multi-lead-type preview, a combined store score, and packaged presets.
7. **M7, M8**, then the Low list.
