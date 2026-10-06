# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A failure-tolerant Python lead generator. It discovers businesses from free public sources (Playwright + `requests`, no paid APIs), merges them into one deduplicated **organisation** per business inside a **project**, scores and ranks them, and exports to CSV and Google Sheets. It is being turned into a backend application (`APP-BUILD-PROMPT.md`; Phases 0–2 are done, see `docs/phase-*-report.md`). Despite the directory name it does **not** use LangGraph; orchestration is plain Python (`engine/runner.py` drives each lead type's session unit by unit; `docs/engine.md`).

Runs are described by **run configs** (`config/schema.py`, `schema_version: 1`). Two presets reproduce the original runs:
- `presets/mumbai-event-vendors.yaml` (Mumbai event vendors), imported from `config.yaml`.
- `presets/nashik-venues.yaml` (Nashik high-end venues; hospitality vertical), imported from `config.nashik.yaml`. It has its own project, CSV, logs and Sheet tabs (`Nashik Venues` / `Nashik Skipped`), and must never write the Mumbai `Leads` / `Skipped` tabs.

The legacy YAMLs still load: `Config.load` imports them on the fly (`config/importer.py`), and the compiled view equals the file key for key.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # pyproject.toml is the only dependency list; extras: dev, ai, postgres
playwright install chromium

# Tests (all offline; `live` deselected, `postgres` skipped without LEADSCRAPER_TEST_POSTGRES_URL)
python -m pytest tests/ -q                                    # ~35 s (incl. a real worker kill/resume)
python -m pytest tests/test_pipeline.py::test_name -q         # one test
python -m pytest -q -m "not characterisation" --deselect tests/test_engine.py   # fast loop (~6 s)
LEADSCRAPER_TEST_POSTGRES_URL=postgresql+psycopg://USER@HOST:PORT/THROWAWAY_DB \
  python -m pytest -m postgres -q                             # server mode, incl. both real stores (drops app tables!)
LEADSCRAPER_TEST_ENGINE_DB_URL=postgresql+psycopg://…/THROWAWAY_DB python -m pytest tests/test_engine.py -q   # engine suite on Postgres

# Characterisation baseline (see below)
python tests/characterisation/freeze.py                       # freeze DB copies + write real-data snapshots
CHAR_UPDATE=1 python -m pytest tests/characterisation -q      # accept an INTENDED behaviour change

# Running (run.py == python -m leadscraper run; every old flag works)
python run.py --dry-run                                       # plan + time estimate, no requests
python run.py --locality Andheri --category Caterers --source google_maps   # small slice
python run.py --export-only                                   # rescore + re-export the project, no scraping
python run.py --enrich-only | --no-enrich | --no-sheets | --no-headless | -v
python run.py --config presets/nashik-venues.yaml --dry-run   # any preset / run config / legacy YAML
python run.py --time-budget 15 --depth quick                  # best units first; skipped units reported
python run.py --preview                                       # ~2-min sample + full-run estimate
python run.py --resume RUN_ID | --queue | --force-enrich | --workers N

# Engine: estimate, worker, run control (Phase 2)
python -m leadscraper estimate --config FILE [--time-budget M] [--json]
python -m leadscraper worker [--once] | worker stop | worker list
python -m leadscraper runs list | show ID | pause ID | resume ID | cancel ID | events ID [--follow]

# Application database and configs
python -m leadscraper migrate-legacy --config config.yaml     # import data/leads.sqlite3 (a COPY) -> project
python -m leadscraper db info | db upgrade [--sql --db URL]
python -m leadscraper config validate FILE | config schema | config import-legacy SRC -o OUT
python -m leadscraper presets

# Formatted Excel workbooks from the local CSVs (Phase 4 replaces these)
.venv/bin/python tools/build_leads_workbook.py
.venv/bin/python tools/build_nashik_workbook.py
```

Full runs take many hours at the default polite delays (`--dry-run` estimates them). Don't run several copies in parallel, and don't lower delays casually (Justdial, Instagram and Google SERP block quickly). Tests isolate app state via `LEADSCRAPER_DATA_DIR` (`tests/conftest.py`); when you smoke-test by hand, point it at a scratch directory so you never touch the real per-user database.

## Architecture

**Layout** (A.5, with one deviation: core sources stay in `sources/`, see below):

```
core/        lead.py (the Lead record + merge), identity.py (match rules, keys, blocklist),
             normalize.py, country.py, geo.py (+ Nominatim geocoder), scoring.py (fit/intent/reach), ratings.py
db/          models.py (SQLAlchemy, A.6), engine.py (SQLite WAL / Postgres, schema init), migrations/ (Alembic),
             store.py (LeadStore = one project's organisations), legacy_import.py, runs.py (run history)
config/      schema.py (RunConfig), importer.py (legacy YAML -> run config), compile.py (-> Config), legacy.py (Config),
             geography.py (fill centre/state by geocoding)
engine/      runner.py (one project, one Run row; plan -> discover -> post -> finalise), scheduler.py (budget),
             costs.py (priors + learned unit_stats), estimator.py (p50/p90, budget fit), parallel.py (across hosts),
             control.py (pause/cancel/deadline: one stop path), events.py (typed, persisted), worker.py (DB queue),
             keepawake.py (wakepy + sleep detection), preview.py, clock.py, failures.py (FailureLog)
leadtypes/   base.py (the A.7 contract), registry.py, local_business/ (config, plugin, planning, pipeline, session, estimates, scoring)
packs/       in/ (justdial, sulekha, indiamart), events/ (wedmegood, weddingwire),
             hospitality/ (stay platforms + venues.py), social/ (facebook, instagram, linkedin_serp)
sources/     base.py (Source/Enricher contract), registry.py, core sources: google_maps, google_search, website, maps_lookup
net/         http.py, browser.py, ratelimit.py
export/      csv_writer.py, sheets.py            ai/  assistant.py (Phase 5 splits it)
cli/         python -m leadscraper …; run.py is a shim over cli/run_cmd.py
```

**Flow:** `cli/run_cmd.py` (or `engine/worker.py`) → `engine/runner.execute` → each lead type's session (`LOCAL_BUSINESS.open()` → `LocalBusinessSession` over `Pipeline`):
- discovery: the engine schedules the session's units (`planning.plan_discovery` order; by priority under a time budget), in parallel across hosts, checkpointing each in `work_units`;
- for venue runs, AI venue classification and the brand roll-up;
- name polish (AI, or the rule fallback when AI is off);
- enrichment, then AI fallback extraction;
- finalise: rescore, write the CSV, sync Sheets, report.

Output is flushed periodically and on exit. Ctrl-C pauses (in-flight units finish, then flush; resumable); a second Ctrl-C stops at once.

**Engine (Phase 2, `docs/engine.md`):**
- Lead types expose a `LeadTypeSession` (`leadtypes/base.py`): `units()`, `run_unit()` (may run on a worker thread; never raises), `hosts()`, `prior()`, `post_discovery(control)`, `finalize(complete=)`. A lead type with only `run()` is one unit.
- Without a time budget units run in plan order (legacy presets unchanged); with one, by expected new orgs per second, never starting a unit that can't finish, keeping a 90 s tail and a share for enrichment. Skipped units are reported.
- Units sharing a host never run at once (`Source.hosts()`; directories drop Google/DDG unless `serp_fallback`). `runtime.discovery_workers` (3), `runtime.enrich_workers` (8).
- Enrichment marks leads done per enricher and skips them later unless `--force-enrich`; transient failures (`blocked`, `fetch_failed`, …) aren't marked.
- Lifecycle: queued → running → pausing/paused, stopping/cancelled, completed/failed. Requests go through `runs.status`; the runner heartbeats, claims runs atomically, and only writes while it owns the run. Workers recover runs with a stale heartbeat from their checkpoint.
- Depth presets live in each lead type's `apply_depth`; `balanced` changes nothing. Learned `unit_stats` keys carry `@quick`/`@deep`.
- Store writes are tagged per thread (`LeadStore.tag_thread`), because units of different lead types can run side by side.
- `LEADSCRAPER_PLUGINS=module` loads extra lead types in any process (used by the worker-kill test).

**Core invariant: nothing crashes the run.**
- `Source.discover` yields partial `Lead`s and must never raise; problems go to `ctx.failures` with a reason code (`login_wall`, `phone_hidden`, `no_businesses_parsed`, …). That's the JSONL, `skipped.csv`, the Sheet's Skipped tab and, inside a run, the `failures` table.
- A lead type that crashes is contained by the runner; the run is recorded with the error.

Keep this property when you add or modify sources.

**Sources and packs:**
- Each source is one `Source` (discovery) or `Enricher` subclass with a `name`, a `priority` (its run order; the values reproduce the original registry order) and a `scope`: `"combo"` (per locality × category), `"locality"` or `"run"`.
- Core sources live in `sources/`. A source that only makes sense somewhere lives in a pack (`packs/<pack>/__init__.py` declares `applicable_countries` / `applicable_verticals`), and a class can narrow further with its own `applicable_countries` (StayVista: IN).
- `sources/registry.py` assembles core and every pack. `Pipeline._build` reports each entry as built, or not built with its reason (not requested / disabled / not applicable / not needed). An inapplicable pack is silent, never a failure.
- A class that only exists to fill certain fields declares them in `fills` (enrichers; `linkedin_serp` = decision_makers). It isn't built when the run's `fields.include` leaves all of them out (A.8). Discovery sources have `fills = None` and always run.
- Directory city slugs come from the pack (`Pack.city_slug`).
- Site-specific selectors sit at the bottom of each source file.
- Dependencies come in through `SourceContext` (config, `HttpClient`, `RateLimiter`, `FailureLog`, `BrowserSession`, store).
- Core sources still call the hospitality pack's pure text parsers when the vertical is on (a known layering compromise).

**Country:**
- `core/country.py` gives phone region and digit bounds, currency and money format, locale, timezone, Google `gl` and DuckDuckGo region, postal-code shape and name noise.
- Parsers take the run's region (`Source.region`); with none, only `+`-numbers parse.
- Nothing in code assumes India or Mumbai. The implicit defaults of pre-Phase-1 configs (IN, Mumbai, its centre, Maharashtra, "mumbai" as name noise) live only in `config/importer.py` and are written into the presets.
- Missing centres and states are geocoded via Nominatim (cached) before a run (`config/geography.py`).

**Shared infrastructure:**
- `ratelimit.py`: per-host randomised delays, with escalating penalties after a 429, 403 or CAPTCHA.
- `http.py`: retries, plus dead-domain detection persisted in the store, workspace-wide.
- `browser.py`: the Playwright session; locale, timezone and position follow the run.
- SERP use is rationed: `site:` lookups in the directory sources only run with `serp_fallback: true`.

**Data model and identity** (`core/lead.py`, `core/identity.py`, `db/`):
- `Lead.merge` unions lists, fills empty fields, settles conflicting ratings in favour of the higher review count, and never overwrites a value with a blank.
- `IdentityResolver.find_match` order: registry ID → shared phone → same Maps place (phones must not contradict) → identical canonical name → fuzzy name ≥ 92 in the same locality → (venues) pins within 250 m → same registrable domain (blocklisted shared hosts excluded; phones must not contradict; names ≥ 60 similar; fewer than 3 holders; off for venue runs).
- A longer incoming name replaces the stored one only when it *extends* it and isn't keyword-stuffed; otherwise it becomes an "also known as" note.
- `LeadStore` answers the rules' lookups from indexed tables in the original store's row orders (`identity_keys` in claim order; locality scan ordered by category, score desc, id, with categories compared bytewise even on a locale-collated Postgres), so legacy decisions reproduce exactly, on SQLite and Postgres alike.
- Every write records contacts (with source), an evidence row, and run membership. `merge_into` / `update_fields` re-check identity and **consolidate** duplicates (lower id survives).
- The venue profile adds rules via `DedupOptions`.
- Schema changes need an Alembic migration (`db/migrations/versions/`). `tests/test_db.py::test_alembic_migrations_build_exactly_the_models` fails until the migration matches the models.
- An original lead store is refused (checked read-only) and only ever imported from a copy.

**Lead types** (`leadtypes/`, `docs/lead-types.md`): A.7 contract; `local_business` is today's scraper. Lead-type config is `lead_types.<key>`, validated against the registry. The runner tags every row with the active lead type. Rules: no lead type depends on another; enabling, disabling or crashing one never changes another's output (`tests/test_phase1_acceptance.py`).

**Profiles / verticals:**
- `Config.profile` (`venues` when the hospitality vertical is on) and `Config.verticals` drive:
  - column layout (`Lead.to_row` vs `to_venue_row`, with prices in the run's currency) and sort order;
  - dedup options and `Source.assign_locality` / `out_of_region`;
  - which vertical packs apply;
  - the venue logic in `packs/hospitality/venues.py`: price parsing, venue-type classification, brand detection, tiers Operator / Luxury / Premium / Standard / Unverified.
- Scoring (`leadtypes/local_business/scoring.py`) is the legacy component score as **fit**, plus contact completeness as **reach**, combined with the run's `scoring.weights` (`core/scoring.py`). Legacy presets weight fit only, so rankings are unchanged.
- Weights and `venue_tiers` changes only need `--export-only`.

**AI layer** (`ai/assistant.py`):
- Optional: off unless the config enables it or `--ai` is passed; `--no-ai` forces it off.
- Name polish sends title-derived names and keyword-stuffed names. Without AI, keyword-stuffed names are cut back by rule (`core.normalize.seo_title_head`).
- Backends: the `claude` CLI in `--print` mode, or the Anthropic SDK when `ANTHROPIC_API_KEY` is set.
- Cached in `data/ai_cache.sqlite3`, with a USD budget cap; quota errors fall back silently, and every call degrades to `None` on failure.
- `BLOCKED_MODEL_FAMILIES` is never selected.

**Config:**
- `Config` (`config/legacy.py`) is the compiled runtime view: dotted `get`/`set` (CLI flags mutate it and are recorded in the run's snapshot).
- Localities (areas) have aliases, keywords and centres; categories have queries and directory slugs. Anything added there flows through every source.
- A run config leaving out `lead_types.local_business.sources` gets every applicable source at polite defaults (`DEFAULT_SOURCES`).
- The schema holds the whole A.8 shape, but `filters`, `icp` and `output.excel/json` aren't acted on yet (`depth` and `time_budget_minutes` are, since Phase 2). `RunConfig.unapplied()` names each one a config sets; the runner logs them and `config validate` prints them. Drop a note there when you implement one.

## Credentials

The Google service-account key is looked up as: `GOOGLE_SERVICE_ACCOUNT_JSON` (in `.env`) → `~/.config/lead-scraper/credentials.json` → `./credentials.json` (legacy). Keep keys out of the project directory. `.gitignore` excludes credentials, `.env*`, `data/`, `logs/`, SQLite files and `*.xlsx`/`*.csv` (real leads); never commit any of them. The target sheet must be shared with the service account's email as Editor.

## Characterisation tests

`tests/characterisation/` pins the behaviour as of Phase 0.
- **Synthetic goldens** (dedup decisions, scores, tiers, sort order, both CSV layouts) always run.
- **Real-data snapshots** run through the **Phase 1 migration**:
  - a *verbatim* import of each frozen store, re-exported, must reproduce every row, order, score and tier;
  - a *replay* import must reproduce every dedup decision.

  The frozen copies are in `data/characterisation/` (gitignored; skipped when absent). The committed snapshots hold only ids, scores, tiers and hashes.
- Both layers load the frozen configs in `tests/characterisation/baseline_configs/` through the legacy importer.
- A failing characterisation test is a behaviour change to justify (then `CHAR_UPDATE=1`), not a test to edit.

## Scope notes

`APP-BUILD-PROMPT.md` is the build plan (Section A context + Phases 0–8, one phase per session; Phases 0–2 are done). It supersedes `REBUILD-PROMPT.md`, of which only Parts 0, 2 and 3 still apply. Follow them only when asked to do that work.

The hard boundary applies to the project generally: this tool **generates** leads and must not grow outreach/CRM features (contact status, email sending, sequences, and so on). LinkedIn is never logged into or crawled; only search-indexed public profile URLs are recorded. `docs/sources.md` lists every external endpoint with its terms note and last live check; several inherited sources are flagged for review against A.2.4.
