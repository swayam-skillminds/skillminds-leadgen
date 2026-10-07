# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A failure-tolerant Python lead generator. It discovers businesses from free public sources (Playwright + `requests`, no paid APIs), merges them into one deduplicated **organisation** per business inside a **project**, scores and ranks them, and exports to XLSX / CSV / JSON / Google Sheets. It is a backend application with an HTTP API, a CLI and a worker over one services layer (`APP-BUILD-PROMPT.md`; Phases 0–5 are done, see `docs/phase-*-report.md`; the API is documented in `docs/api.md`, deployment in `docs/deploy.md`, the AI layer in `docs/ai.md`). Despite the directory name it does **not** use LangGraph; orchestration is plain Python (`engine/runner.py` drives each lead type's session unit by unit; `docs/engine.md`).

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

# Tests (all offline; `live` deselected, `postgres` skipped without LEADSCRAPER_TEST_POSTGRES_URL;
# `browser` skips without Chromium unless LEADSCRAPER_REQUIRE_BROWSER=1, as in CI)
python -m pytest tests/ -q                                    # ~35 s (incl. a real worker kill/resume)
ruff check leadscraper tests tools run.py                     # lint (CI runs it)
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

# API and server mode (Phase 3; docs/api.md, docs/deploy.md)
python -m leadscraper api [--host H --port P] | api --openapi FILE   # /api/v1/docs; never scrapes itself
docker compose up -d [--profile postgres]                     # API + worker (+ Postgres) on a server
python -m leadscraper status | projects | export --run ID --format xlsx|csv|json [--scope run|new|project]
python -m leadscraper runs rerun ID | diff ID [--against ID] | failures ID
python -m leadscraper config save FILE | list | show ID | versions ID | export ID   # saved configs (DB)
python -m leadscraper bundle export -o F [--leads --history] | bundle import F
python -m leadscraper suppression list|create|import · schedules list|create|delete|run · purge · secrets set|check
python tools/ci_bundle.py make DIR | check DIR                # the cross-OS bundle round trip (CI)

# Application database and configs
python -m leadscraper migrate-legacy --config config.yaml     # import data/leads.sqlite3 (a COPY) -> project
python -m leadscraper db info | db upgrade [--sql --db URL]
python -m leadscraper config validate FILE | config schema | config import-legacy SRC -o OUT
python -m leadscraper presets

# The workbook (Phase 4): every run writes it; regenerate from the DB at any time
python run.py --export-only                                   # the project's workbook (+ legacy CSV if output.csv)
python -m leadscraper export --run ID --format xlsx           # one run's Overview + Records
.venv/bin/python tools/build_sample_workbooks.py              # docs/samples/ from fixtures (never real leads)
WORKBOOK_UPDATE=1 python -m pytest tests/test_workbook.py -q  # accept an INTENDED workbook change (then rebuild samples)
# Superseded 4-tab books from the local CSVs (kept as the style reference)
.venv/bin/python tools/build_leads_workbook.py | tools/build_nashik_workbook.py

# AI (Phase 5; docs/ai.md). Tests never reach a provider (mocked HTTP; tests/ai_fakes.py)
python -m leadscraper secrets set anthropic-api-key           # keys: keychain / LEADSCRAPER_SECRET_* / secret files
python -m leadscraper ai providers | test NAME | offerings | usage [--run ID]
python -m leadscraper ai icp "brief" [--offering erp] [--place Pune] [-o draft.yaml]   # a draft, never run
python -m leadscraper ai plan FILE | discover-sources --country IN --category X | candidates list|approve|reject ID
python -m pytest -m live tests/test_ai_live.py -q             # one tiny call per CONFIGURED provider (< $0.10)
.venv/bin/python -I tools/make_ai_fixture_pages.py tests/fixtures/pages   # the fictional AI fixture pages
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
sources/     base.py (Source/Enricher contract), registry.py, core sources: google_maps, google_search, website, maps_lookup,
             ai_web_search + ai_directory (built only with their AI feature)
net/         http.py (+ get_once), browser.py, ratelimit.py, robots.py (robots.txt, RFC 9309)
export/      excel.py (Overview + Records), table.py (ExportTable, CSV, JSON), columns.py (column model),
             records.py (score/place/sort), summary.py (Overview numbers), outputs.py (run files),
             csv_writer.py + sheets.py (the run's legacy-layout CSV and Sheet)
ai/          providers/ (anthropic SDK, openai, gemini, openai_compatible, claude_code_cli; registry),
             defaults.yaml (models, capabilities, prices), settings.py (chain), client.py (per-run: chain,
             cap, cache, ledger, validation), catalog.py (features -> tasks), grounding.py, pageprep.py,
             selectors.py (self-healing), features/ (icp_builder, planner, source_discovery,
             normalisation, signals, summary), assistant.py (local_business tasks; prompts unchanged)
services/    the one core under API, CLI and worker (Phase 3): projects, configs (versioned), runs
             (queue/control/rerun/diff/events), organizations (filter/provenance/erase), exports, suppression,
             schedules, notifications, settings, retention, health, catalog (+ source health), schema_hints,
             estimates (estimate/preview), bundle, ai (providers, config assist, candidates, usage);
             errors.py (NotFound/Conflict/Invalid), workspace.py, query.py
api/         app.py (create_app, /api/v1), deps.py (current_workspace), schemas.py (models + examples),
             routers/ (system, projects, configs, runs, operations, ai)
cli/         python -m leadscraper …; run.py is a shim over cli/run_cmd.py; app_cmds.py = Phase 3 commands
```

**Flow:** `cli/run_cmd.py` (or `engine/worker.py`) → `engine/runner.execute` → each lead type's session (`LOCAL_BUSINESS.open()` → `LocalBusinessSession` over `Pipeline`):
- discovery: the engine schedules the session's units (`planning.plan_discovery` order; by priority under a time budget), in parallel across hosts, checkpointing each in `work_units`;
- for venue runs, AI venue classification and the brand roll-up;
- name polish (AI, then the rule fallback for every name AI did not answer);
- enrichment, then AI fallback extraction (grounded), AI normalisation and lead summaries (Phase 5, opt-in);
- finalise: rescore, write the legacy CSV (`output.csv`), sync Sheets, report;
- then `runner.execute` writes the workbook / JSON from the database (`output.excel`, on by default; `output.json`) and records them in `runs.files`.

Output is flushed periodically and on exit. Ctrl-C pauses (in-flight units finish, then flush; resumable); a second Ctrl-C stops at once.

**Engine (Phase 2, `docs/engine.md`):**
- Lead types expose a `LeadTypeSession` (`leadtypes/base.py`): `units()`, `run_unit()` (may run on a worker thread; never raises), `hosts()`, `prior()`, `post_discovery(control)`, `finalize(complete=)`. A lead type with only `run()` is one unit.
- Without a time budget units run in plan order (legacy presets unchanged); with one, by expected new orgs per second, never starting a unit that can't finish, keeping a 90 s tail and a share for enrichment. Skipped units are reported.
- Units sharing a host never run at once (`Source.hosts()`; directories drop Google/DDG unless `serp_fallback`; Maps with `rate_key: google_maps`, the default for new configs, runs beside SERP; the Mumbai preset keeps shared google.com pacing). `runtime.discovery_workers` (3), `runtime.enrich_workers` (8).
- Enrichment marks leads done per enricher and skips them later unless `--force-enrich`; transient failures (`blocked`, `fetch_failed`, …) aren't marked.
- Lifecycle: queued → running → pausing/paused, stopping/cancelled, completed/failed. Requests go through `runs.status`; the runner heartbeats, claims runs atomically, and only writes while it owns the run. Workers recover runs with a stale heartbeat from their checkpoint.
- Depth presets live in each lead type's `apply_depth`; `balanced` changes nothing. Learned `unit_stats` keys carry `@quick`/`@deep`.
- Store writes are tagged per thread (`LeadStore.tag_thread`), because units of different lead types can run side by side.
- `LEADSCRAPER_PLUGINS=module` loads extra lead types in any process (used by the worker-kill test).

**Services and API (Phase 3, `docs/api.md`):**
- Business logic lives in `services/` only. Routers and CLI commands parse input, call one service function `(engine, workspace, …)` and render the result. Services raise `NotFound` / `Conflict` / `Invalid` (→ 404 / 409 / 422; CLI exit 2).
- Every request resolves its workspace in `api/deps.py: current_workspace` (one default workspace until login exists). The API has no auth: it binds to localhost by default.
- The API never scrapes. `POST /runs` and `POST /preview` insert queued runs. The worker executes them in its own database (`RunRequest.engine`), geocodes API-queued configs, fires due schedules, purges by retention hourly, and stores a preview's sample on the run (`stats.preview`). `runner.execute` notifies the workspace's targets after a run; a failure there is logged, never raised.
- A dry run (estimate) drops the project it created if nothing was written (`LeadStore.drop_project_if_unused`).
- Run membership: `run_organizations.action` is new / updated / **unchanged** (found again with nothing to add; upgraded to updated by a later write). It powers `GET /runs/{id}/diff` and `scope=new` exports.
- Exports are rebuilt from the DB with the run's own config. Suppression lists named in `filters.suppression_lists`, plus the workspace's "Erasure requests" list, apply to exports and to the run's CSV/Sheet (exclude only; flags are API/CLI-export columns).
- Secrets never reach the DB, logs, bundles or exports: `core/secrets.py` (env `LEADSCRAPER_SECRET_<NAME>` → `/run/secrets` / `LEADSCRAPER_SECRETS_DIR` → keyring). Configs refer to secrets by name (`*_secret_name`); `scrub()` removes secret-looking keys from exported documents.
- Saved configs are compared in their stored (JSON) form: YAML integer keys become strings in the JSON column (venue price bands; `venues.py` reads them with `float(k)`).
- Workspace settings (`keep_awake`, `robots_txt`, `retention`) fill what a run config leaves unset (`services/settings.apply_to_config`). robots.txt is honoured by the website enricher (`net/robots.py`), on by default.
- SQLite migrations run with foreign keys off (`db/engine.init_schema`): batch mode rebuilds tables, and a DROP with FKs on cascade-deleted child rows.

**Workbook and exports (Phase 4, `docs/api.md` "Exports"):**
- One `ExportTable` (`services/exports.build_table`: records with contacts, people, signals, inferred contacts, run change, plus the run's facts) is rendered three ways: the workbook (`export/excel.py`), CSV and JSON (`export/table.py`). They share the column model (`export/columns.py`), so a value never differs between formats.
- The workbook has exactly two tabs. **Records**: one row per organisation, in the reference style of `tools/build_nashik_workbook.py` (Arial, navy header, banding, score colour scales, frozen header + score + name, autofilter, short clickable links). **Overview**: run summary, results (totals, new/updated, breakdowns, top 10), data quality, column legend; an empty export says why. Overview figures are values computed from the DB; the only formulas in the book are the Overview's "Filtered view" tiles (`SUBTOTAL` over Records, so they follow the autofilter), each stored with its unfiltered result so non-calculating viewers show real numbers.
- Columns: core ones (name, lead types, place/locality, published contacts, maps, address, decision-makers, Fit/Intent/Reach + reasons, top signal, provenance, Org ID) plus each lead type's own `columns()` / `column_values()`, placed by `ColumnSpec.placement`. `fields.include` drops columns; optional columns (lead-type ones, socials, signals, people, inferred) drop when empty everywhere. Inferred contacts only ever appear in `Inferred Email (unverified)`.
- Text cells are literal strings (never formulas), phones/IDs text (`@`), links http(s) only. The Sheets sync writes `RAW` (USER_ENTERED turned "+91 …" into `#ERROR!`).
- Rows follow `output.sort` (`auto`: score desc, grouped by place when several places; `score`, `place`, `area`, `category`, `legacy` = `db.store.sort_leads`). The run's legacy CSV and its Sheet keep `Lead.row_for` and `sort_leads`, which the characterisation suite pins.
- Golden structure files: `tests/golden/workbooks/*.json` (fixtures in `tests/workbook_fixtures.py`, fictional data, two fixture lead types `demo_hiring`/`demo_funding`). `docs/samples/*.xlsx` must match them (`tools/build_sample_workbooks.py`); `.gitignore` excepts only that folder.

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
- In exports, every lead type an organisation belongs to scores it (`export/records.score_records`). Weights renormalise per run (a component some lead type scores counts 0 where missing), and reach is core (`core.scoring.contact_reach`) for organisations no reach-scoring lead type found. With `local_business` alone this equals the legacy score.
- Weights and `venue_tiers` changes only need `--export-only`.

**AI layer** (Phase 5, `docs/ai.md`):
- Optional and opt-in: off unless the config enables it or `--ai` is passed (`--no-ai` forces it off). Each feature is named in `ai.features`; empty = the legacy `extract`, `names`, `venues` (what the presets use). Features map to tasks in `ai/catalog.py`.
- One `AIClient` per run (`runner._ai_client`, `LeadTypeContext.ai`, `SourceContext.ai`): fallback chain, one hard USD cap (a call that would cross it is never made), cache keyed by provider kind + model + schema + prompt (the pre-Phase-5 key still read for Claude providers), a ledger flushed to `ai_usage` (deltas) per provider/model/feature. Nothing raises to callers; AI off is always a valid outcome.
- Chain: config `ai.providers` → legacy `ai.provider` → workspace `settings.ai.providers` → `claude_code_cli`. A billed provider only when named, never with `allow_billed_api: false`; `BLOCKED_MODEL_FAMILIES` (Fable, Mythos) never selected. Model ids and prices are data (`ai/defaults.yaml`, overridable).
- Keys are secrets referred to by name (`key_secret_name`); never in the DB, logs, bundles or exports; the API shows booleans only. `providers/registry._guard_key`: a built-in provider's secret only goes to its own default endpoint; `key_env` only from defaults; `notify-*` refused.
- **Grounding:** every AI-supplied contact detail must be on the fetched page (`ai/grounding.py`), or it is dropped (`ai_ungrounded_dropped`). AI attributes (industry, size estimate, summary) live in `org_attributes` (`LeadStore.set_attribute`), labelled "(AI)" / "(AI estimate)" in the workbook. Every call is validated against a pydantic model; invalid → retried once, then skipped.
- Name polish: AI first; names it didn't answer are cleaned by rule, so a failing provider ends where AI off ends (`tests/test_ai_runs.py`). The `assistant.py` prompts must stay byte-identical (cache validity).
- The `claude` CLI is a real provider: tests disable it (`tests/conftest.py::_no_real_ai`) and never call any provider; use `tests/ai_fakes.py` (mocked HTTP for requests and the Anthropic SDK's httpx2).

**Config:**
- `Config` (`config/legacy.py`) is the compiled runtime view: dotted `get`/`set` (CLI flags mutate it and are recorded in the run's snapshot).
- Localities (areas) have aliases, keywords and centres; categories have queries and directory slugs. Anything added there flows through every source.
- A run config leaving out `lead_types.local_business.sources` gets every applicable source at polite defaults (`DEFAULT_SOURCES`).
- The schema holds the whole A.8 shape, but `filters.exclude_chains` and `icp` aren't acted on yet (`depth` and `time_budget_minutes` are, since Phase 2; `output.*` and `filters.min_rating`/`must_have` (exports only) since Phase 4). `RunConfig.unapplied()` names each one a config sets; the runner logs them and `config validate` prints them. Drop a note there when you implement one.

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

`APP-BUILD-PROMPT.md` is the build plan (Section A context + Phases 0–8, one phase per session; Phases 0–5 are done). It supersedes `REBUILD-PROMPT.md`, of which only Parts 0, 2 and 3 still apply. Follow them only when asked to do that work.

The hard boundary applies to the project generally: this tool **generates** leads and must not grow outreach/CRM features (contact status, email sending, sequences, and so on). LinkedIn is never logged into or crawled; only search-indexed public profile URLs are recorded. `docs/sources.md` lists every external endpoint with its terms note and last live check; several inherited sources are flagged for review against A.2.4.
