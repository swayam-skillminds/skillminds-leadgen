# Phase 3 report — API, CLI, history, saved configs, portability, ops features

Date: 2026-10-07 · Branch: `phase-3/api` (from `main`) · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 3

**Result:** Phase 3 is complete.
- The suite is green: 457 passed, 8 skipped (it was 429 passed, 8 skipped on `main`). The 8 skips are the opt-in Postgres and live tests, as before.
- The Phase 3 suites also pass against a real PostgreSQL 16 (section 12), and so do the engine suite and the opt-in Postgres tests.
- All five acceptance checks pass (section 10). The CI matrix is green on macOS, Windows and Linux (run 37589649703; and run 37590757044 after the review fixes, both all green).
- The characterisation baselines are untouched: `git diff main -- tests/characterisation/golden` is empty.
- One **pre-existing data-loss bug in the Phase 2 migration path** was found and fixed (section 9). It may already have affected a real database: please check (section 15).

**About the branch.** As asked, `phase-3/api` starts from `main`. `fix/live-check` (live check 1: the estimator's budget-tail fix and the Maps search fix) is still unmerged. It touches different files (`engine/estimator.py`, the runner's `LiveEta` line, `sources/google_maps.py`) and should merge cleanly in either order.

## 1. One core: `leadscraper/services/` (A.3)

A.3 asks for the API, the CLI and the worker to be thin layers over the same core. Every user-facing operation is a function taking `(engine, workspace, …)` in `leadscraper/services/`. Routers and CLI commands only parse input, call one of them, and render the result.

| Module | What it does |
|---|---|
| `workspace.py` | The default workspace. The API resolves it in one place (`api/deps.py: current_workspace`, A.2.2). |
| `projects.py` | CRUD; per-project lead retention (`retention_days`). |
| `configs.py` | Saved configs: versions (never overwritten), clone, rename, validate, import (YAML/JSON or a legacy `config.yaml`), export (secrets scrubbed). |
| `runs.py` | Queue a run (a document or a saved config's version), list, history detail, pause/resume/cancel, rerun, failures, units, events, **diff**. |
| `organizations.py` | List/filter/sort; detail with contacts, people, signals, evidence and runs; **erasure**. |
| `exports.py` | XLSX / CSV / JSON from the database; Google Sheets. |
| `suppression.py` | Lists, entries, CSV/XLSX import, matching. |
| `schedules.py` + `core/cron.py` | Recurring runs (a small, dependency-free cron parser, with timezones). |
| `notifications.py` | Desktop / webhook / SMTP targets; run_completed, run_failed, needs_attention. |
| `settings.py`, `retention.py` | Workspace settings; purge. |
| `health.py`, `catalog.py`, `schema_hints.py` | `/health`; lead types and sources with **source health**; the schema plus UI hints. |
| `estimates.py` | Estimate (dry run); queue a preview. |
| `bundle.py` | Export and import. |
| `errors.py` | `NotFound` / `Conflict` / `Invalid`. The API maps them to 404 / 409 / 422; the CLI prints the message and exits 2. |

## 2. HTTP API (3.1)

FastAPI under `/api/v1`: **70 operations on 49 paths**, documented in `docs/api.md`. Interactive docs are at `/api/v1/docs`; `leadscraper api --openapi FILE` writes the OpenAPI document for client generation.

| Phase 3.1 item | Where |
|---|---|
| `GET /health` (Playwright/Chromium, DB, disk, keys, worker) | `services/health.py`. Chromium is found on disk without launching it, so the check is cheap enough to poll. `down` → 503 (only when the database is unreachable). |
| `GET /schema/run-config` (JSON Schema + UI hints + per-country applicability) | `services/schema_hints.py`: label, help, widget, default, enum, required, wizard step and "advanced" per field path; a minimal example; which lead types and sources apply (and why not) for `?country=&verticals=`; the event schema. |
| `GET /catalog/lead-types`, `/catalog/sources?country=` | `services/catalog.py` plus `sources/notes.py` (endpoint, method, terms note, review flag, last live check: the machine-readable twin of `docs/sources.md`), and **source health** (section 5). |
| `POST /estimate` | The engine's dry run. It makes no requests and no longer leaves an empty project behind (section 13). |
| `POST /preview` | **Queued for the worker** (the API never scrapes). The sample and the calibrated estimate land on the run (`GET /runs/{id}` → `preview`). |
| Projects: CRUD | `/projects`. A run still creates its project on first use. |
| Saved configs: CRUD, versions, clone, import, export | `/configs…`, plus `/configs/validate` and `/configs/import/file`. |
| Runs: create, list, get, pause/resume/cancel/rerun, events (SSE), failures, diff | `/runs…`, plus `/runs/{id}/units` and `/runs/{id}/events/history` (JSON). |
| Organisations: list/filter/sort, detail with provenance, erase | `/organizations…` |
| Exports `.xlsx/.csv/.json`, Google Sheets | `/runs/{id}/export.{fmt}?scope=run\|new\|project`, `/projects/{id}/export.{fmt}`, `POST /runs/{id}/export/google-sheets` |
| Suppression lists: create, import CSV/XLSX, attach to configs, exclude or flag | `/suppression-lists…`. Attached by name in `filters.suppression_lists`. |
| Schedules: cron-like, "only new since last scheduled run" | `/schedules…`. The worker fires them. |
| Notifications: desktop, webhook (Slack/Teams), SMTP | `/notifications…`, plus `/notifications/{id}/test`. |
| Settings: data directory, retention, keep-awake, robots.txt | `/settings`, `/settings/purge`. |

**Conventions:**
- Every list is paged (`limit`/`offset` → `{items, total, limit, offset}`) and sortable (`sort=-score,name`), on fields the list declares and no others.
- Errors are JSON `{detail, errors}`. An invalid config returns the schema's own error list.
- Every request and response model carries an **example**, and `test_openapi_covers_every_endpoint_with_examples` enforces it. Paged responses derive theirs from the item model.

**Live progress (SSE):** `GET /runs/{id}/events` streams the persisted Phase 2 events.
- Each message is `id: seq`, `event: type`, `data: JSON`.
- It replays from `after` or the `Last-Event-ID` header, then follows the run.
- It ends when the run's newest `run_state` is a stopped state *and* the run row agrees, so a replayed old "paused" never ends the stream of a resumed run.
- Keep-alive comments are sent while idle.

**No login yet (A.2.2), so the API guards itself against the two browser attacks that matter on localhost** (section 11):
- only `Host` names in `LEADSCRAPER_ALLOWED_HOSTS` are served (DNS rebinding);
- writes carrying a foreign `Origin` are refused (CSRF);
- uploads are capped.

It binds to `127.0.0.1` by default and warns when told otherwise. `docs/deploy.md` describes a private network or an authenticating reverse proxy for server mode.

## 3. CLI parity (3.2)

`python -m leadscraper …` over the same services (`cli/app_cmds.py`):

| Core flow | Command |
|---|---|
| run, estimate, preview | `run` / `run.py` (unchanged), `estimate`, `preview` (Phase 2) |
| status | `status [--json]`: health checks, active and queued runs |
| pause / resume / cancel | `runs pause|resume|cancel` (Phase 2); new: `runs rerun`, `runs diff`, `runs failures` |
| export | `export --run ID \| --project ID --format xlsx\|csv\|json [--scope run\|new\|project]`. The file is recorded on the run. |
| config import / export | `config save FILE` (YAML/JSON/legacy → versioned), `config list|show|versions|export` |
| bundle import / export | `bundle export -o F [--leads --history --project ID]`, `bundle import F` |
| migrate-legacy | unchanged |
| also | `api [--openapi FILE]`, `projects`, `suppression list|create|import`, `schedules list|create|delete|run`, `purge [--dry-run]`, `secrets set|delete|check` |

`run.py` keeps every old flag; `tests/test_cli.py` still passes unchanged.

## 4. History (3.3)

A run already stored its config snapshot, timings, estimate, failures and units (Phase 2). Phase 3 adds:
- **Counts per source:** `GET /runs/{id}` → `per_source`, which gives, per lead type and source:
  - units, done, failed, skipped;
  - found, new, merged;
  - seconds vs expected.
- **Estimate vs actual** side by side (`estimate`, `actual`), including the skipped units.
- **AI usage:** the runner now writes an `ai_usage` ledger row per lead type when AI was used. The table existed but nothing wrote to it.
- **Generated files:** `runs.files` (migration 0003). The run's CSV and `skipped.csv` are recorded by the pipeline, and CLI exports by `export`.
- **Unchanged re-finds are run members.** `run_organizations.action` gains `unchanged`: found again, nothing to add. A later write in the same run upgrades it to `updated`. Live check 1 (section 5) left this as a design decision. It is what makes "what did this run find" and the diff work.
- **Compare:** `GET /runs/{id}/diff?against=` classifies each organisation:

  | Change | Meaning |
  |---|---|
  | `new` | It did not exist when `against` ended. |
  | `updated` | This run added to it. |
  | `unchanged` | This run found it again with nothing to add. |
  | `only_in_against` | `against` had it; this run did not. |

  `against` defaults to the project's previous finished run.
- **Re-run:** `POST /runs/{id}/rerun` (same snapshot and options; overrides allowed).
- **Re-export:** any time, from the database. A run's `scope=new` export is "re-run next month and get only what's new" (Definition of Done #8). A scheduled `only_new` run defaults to it, against its schedule's previous run.

## 5. Source health (3.4)

`services/catalog.source_health` rolls up the workspace's last 20 finished runs, per source:
- productive / empty / failed units;
- failures by reason and blocks (`blocked`, `login_wall`, …);
- success, failure and block rates.

Statuses:
- **`likely_broken`**: in each of its last three runs the source found nothing and recorded a "nothing parsed" reason (`no_listings`, `no_results`, `no_businesses_parsed`, …). This is Phase 3.4's "keeps returning `no_listings`".
- **`degraded`**: block or failure rate ≥ 30 %.
- `ok`, or `unknown` without data.

Exposed per source in `/catalog/sources`. Test: `test_source_health_flags_a_source_that_keeps_finding_nothing`.

## 6. Portability (3.5)

**Bundles** (`services/bundle.py`): a zip containing `manifest.json`, saved configs (every version), presets (shipped and the user's own), suppression lists, projects, schedules and settings. Optionally it also carries leads (organisations with contacts, people, signals, evidence and identity keys) and history (runs with units, failures, AI usage and memberships).
- **Never secrets:** documents and snapshots go through `core/secrets.scrub`, and notification targets are left out entirely (a webhook URL is a bearer secret). `test_bundle_round_trip_into_a_fresh_database` asserts that an API key and a webhook token are absent from the zip.
- **Version-checked:** an unknown format version, or a schema revision this installation doesn't know (a bundle from a newer version), is refused before anything is written. A zip bomb is refused too.
- **Additive and idempotent:**
  - identical config versions are skipped;
  - lists, projects and presets are matched by name;
  - organisations go verbatim into an empty project, and through the identity rules (`LeadStore.upsert`) into a non-empty one;
  - an already-imported history run is skipped;
  - active runs come in as `failed`, so no worker picks them up;
  - schedules come in **disabled**.
- **Cross-OS:** JSON with ISO-8601 UTC times and `/` paths. CI proves it (section 10).

**Server mode:**
- A `Dockerfile` (python:3.12-slim, Chromium with system libraries, an editable install so presets resolve, non-root user, `/data` volume, healthcheck).
- `docker-compose.yml`: the API, plus a worker that starts after the API is healthy and stops gracefully on SIGTERM, plus a `postgres` profile.
- **`docs/deploy.md`** covers deployment on a small Linux VPS: sizing, `.env`, Postgres, access control (Tailscale/WireGuard, or Caddy with basic auth and SSE-safe proxying), backups, upgrades, scaling workers and secrets.

**State** stays under the `platformdirs` data directory (`LEADSCRAPER_DATA_DIR` overrides it), as since Phase 1. Imported presets go to `<data_dir>/presets`.

**Secrets** (`core/secrets.py`, A.4):
- Lookup order: env `LEADSCRAPER_SECRET_<NAME>` → files in `LEADSCRAPER_SECRETS_DIR` or `/run/secrets` → the OS keychain (`keyring`, a new dependency; disabled in the image).
- Configs and targets hold only secret *names* (`*_secret_name`). The API reports only whether one is configured.

## 7. Cross-platform CI (3.6)

`.github/workflows/tests.yml` (all on GitHub Actions):

| Job | What |
|---|---|
| `lint` | `ruff check` (new; config in `pyproject.toml`: errors and likely bugs, not style). Two existing `raise … from None` fixes in `ai/assistant.py`. |
| `test` × macOS / Windows / Linux 3.12 + Linux 3.14 | The suite, with Chromium installed and `LEADSCRAPER_REQUIRE_BROWSER=1`, so the **headless Playwright smoke test** (`tests/test_browser_smoke.py`) cannot skip. It runs against a local `http.server`: one page builds its listings with JavaScript (proving Chromium executed it), and a bot-check page must be recognised by `check_blocked`. |
| `bundle-make` × 3 OS → `bundle-check` × 3 OS | `tools/ci_bundle.py`: each OS makes a bundle (synthetic leads including non-ASCII names, versioned configs, a suppression list, a run); every OS imports every OS's bundle into a fresh database and compares organisations, contacts, runs, config versions and list entries. |
| `docker` | `docker compose up -d --build`, then poll `/api/v1/health` until the worker is alive. Asserts the database, browser and worker are `ok` and the OpenAPI document is served. |

Actions were bumped to `checkout@v5` / `setup-python@v6` (the Phase 2 report noted Node 20 deprecation warnings).

## 8. Also in this phase

- **robots.txt** (A.2.4: honoured, toggle, on by default; Phase 2 had deferred it). Implemented in `net/robots.py` and used by the website enricher:
  - one paced probe per host per run (`HttpClient.get_once`: no retries, no block penalty);
  - RFC 9309 statuses: 4xx = no rules, 5xx = stay off; 429 also = stay off;
  - a disallowed page is recorded as `robots_disallowed`;
  - toggled by the workspace setting `robots_txt`, or `runtime.robots_txt` in a config.
- **Workspace settings fill what a config leaves unset:** `keep_awake` and `robots_txt` (`services/settings.apply_to_config`, called by the runner).
- **Suppression in the run's own output:** "exclude" lists named in the config, plus the workspace's **Erasure requests** list, are dropped from the run's CSV and Sheet as well (`Pipeline._without_suppressed`). "Flag" lists become a `Suppressed` column in API and CLI exports only, so the run CSV keeps its fixed layout. With no lists, output is unchanged.
- **Notifications** are sent by `runner.execute` after every ordinary run, CLI or worker. A failing target records its own error and never touches the run.

## 9. Database: migration 0003, and a migration bug fixed

**Migration `0003`:**
- new table `notification_targets`;
- `workspaces.settings`, `projects.retention_days`, `saved_configs.description`, `runs.files`;
- schedules gain `name`, `timezone`, `overrides` and `last_run_id`.

Columns get plain literal server defaults, so SQLite adds them in place. `test_alembic_migrations_build_exactly_the_models` passes.

**Fixed: SQLite upgrades cascade-deleted child rows (pre-existing, from Phase 2).**
- **Cause:** Alembic's batch mode rebuilds a SQLite table (copy, `DROP`, rename). With `PRAGMA foreign_keys=ON` (set on every connection), the `DROP` performs the `ON DELETE CASCADE`s. Migration 0002 rebuilt `runs`, so upgrading a 0001 database deleted every run's `work_units`, `failures`, `run_organizations`, `run_events` and `ai_usage`, and nulled evidence's `run_id`. A rebuild of `workspaces` would have emptied the whole database.
- **Why the tests missed it:** the Phase 2 migration test only checked the `runs` row.
- **Fix:** `init_schema` turns foreign keys off for the migration (outside the transaction, where SQLite honours the pragma) and back on after. 0003 also avoids rebuilds altogether.
- **Tests:** `test_upgrading_an_old_database_keeps_every_child_row` and the Phase 2 migration test, which now upgrades through `init_schema`, as the app does. Without the fix, the scenario left 0 projects.

## 10. Acceptance checks

| Check | Status | Evidence |
|---|---|---|
| An OpenAPI document covers every endpoint, with examples | **Pass** | `test_openapi_covers_every_endpoint_with_examples`: all 70 operations have a summary, a tag, a 2xx response with an example for every media type (component examples, inline examples for files and SSE), and an example for every JSON request body. It also asserts that every path Phase 3.1 lists exists. |
| An end-to-end test drives create config → estimate → run (fixtures) → events → export via the API | **Pass** | `test_end_to_end_config_estimate_run_events_export`, with real `local_business` and clocked fixture sources (no network). Steps: save a config; estimate (2 units, no project left behind); queue a run; a **real `Worker`** executes it while the test follows the **SSE stream live**; the replay equals the live stream; `Last-Event-ID` resumes. Then: history detail (per-source counts, membership, files); organisations with provenance; CSV / JSON / XLSX exports (two tabs; phones as text); a re-run; the diff (`new 2, unchanged 6`); a `scope=new` export. The preview (`test_preview_is_queued_…`) and run control (`test_run_control_through_the_api`) are covered too. |
| A bundle exported on one OS imports on another in CI | **Pass** | CI jobs `bundle-make` (Linux, macOS, Windows) → `bundle-check` (each imports all three). Locally: `test_bundle_round_trip_into_a_fresh_database`, and on the Postgres run, Postgres → SQLite. |
| `docker compose up` works and passes `/health` | **Pass** | Locally (Docker Desktop, macOS, arm64): the image built (2.1 GB), the API turned healthy, and then the worker started. `/health` returned **`ok`**: database `sqlite, schema 0003`; browser `Playwright 1.63.0, chromium-1243`; disk ok; worker `1 worker(s) alive`. `POST /estimate` worked through the container, and `docker compose stop worker` stopped gracefully on SIGTERM. In CI: the `docker` job (Linux, amd64). |
| The CI matrix is green | **Pass** | Run 37589649703, all 13 jobs: lint; test on macOS, Windows and Linux 3.12 plus Linux 3.14; bundle-make ×3; bundle-check ×3; docker. After the review fixes: run 37590757044, all 13 green, the docker job included (with the Host allowlist on). |

## 11. Review and the fixes it led to

An independent review of the Phase 3 diff (a separate reviewer agent, reading code) reported 12 findings. Each was checked against the code. Each fix has a regression test where one was practical.

| Finding | Fix | Test |
|---|---|---|
| **Lead retention purged leads still being found:** an `unchanged` re-find doesn't touch `updated_at` (data loss) | Purge only organisations neither updated nor a member of any run since the cutoff | `test_retention_purges_…` (a recent re-find survives) |
| **SSRF and secret exfiltration through notification targets** on an unauthenticated API | Targets may only reference `notify-*` secrets; hosts resolving to loopback, link-local or private ranges are refused unless `LEADSCRAPER_ALLOW_PRIVATE_TARGETS=1`; no redirects; stored errors cut to one line | `test_notifications_…` |
| **Formula injection:** a scraped `=HYPERLINK(…)` became a live formula in the XLSX | XLSX writes every text cell as a literal string; CSV prefixes `'` to formula-looking cells, phones untouched | `test_exports_never_carry_live_formulas` |
| **Browser CSRF / DNS rebinding** against localhost | Host allowlist (`LEADSCRAPER_ALLOWED_HOSTS`); writes with a foreign `Origin` refused | `test_cross_site_writes_and_foreign_hosts_are_refused` |
| **Unbounded uploads; zip bombs** | Upload cap (`LEADSCRAPER_MAX_UPLOAD_MB`, 200); bundle size and ratio limits | `test_uploads_are_capped`, `test_bundles_from_a_newer_version_are_refused` (bomb) |
| Bundle history duplicated on re-import | Skip runs already imported (same ref and creation time) | `test_bundle_round_trip_…` |
| Erasure left the business name in failure rows | Those rows are deleted too; the remaining gaps (files already on disk or in a Sheet) are documented | `test_erase_…` |
| Two processes could migrate at once | Postgres advisory lock; after a lost race, re-check that the schema is current | — (compose already starts the worker after the API) |
| SSE could end at once on a replayed old `paused` / `queued` state | End only when the newest event is a stop *and* the run row has stopped | `test_end_to_end_…` |
| `delete_project` race with a worker claiming a queued run | Check and delete in one transaction; queued runs block deletion | — |
| robots.txt 429 treated as "no rules" | 429 = stay off for this run | `test_statuses_follow_rfc_9309` |
| A schedule firing whose queueing fails is skipped to its next time | **Not changed:** retrying every poll would spin on a persistent error. It is logged; `POST /schedules/{id}/run` re-fires by hand. | — |

## 12. Postgres

As before, a throwaway PostgreSQL 16 ran locally, this time in Docker (`postgres:16-alpine`, bound to 127.0.0.1, removed afterwards):

| Check | Result |
|---|---|
| Phase 3 suites on Postgres (`tests/test_services.py`, `tests/test_api.py`; each test gets an empty schema via `tests/app_db.py`) | 22 passed. The bundle test becomes a Postgres → SQLite round trip. |
| The whole engine suite (`LEADSCRAPER_TEST_ENGINE_DB_URL`) | 48 passed |
| `-m postgres` (round trip, bytewise scan, migrations 0001 → head incl. 0003 over existing rows, both real stores migrated into Postgres against the SQLite goldens) | 6 passed |

## 13. Intended behaviour changes, with reasons

None of these touch the characterisation results.

- **Re-finds are run members (`unchanged`).**
  - **What changes:** `run_organizations` gains rows for organisations a run found again without changing them.
  - **Why:** "what this run found", the diff and `scope=new` need it (live check 1, section 5).
- **Dry runs leave no empty project behind.** `--dry-run`, `estimate` and the preview's full-run estimate used to create the config's project as a side effect (`LeadStore.drop_project_if_unused`).
- **robots.txt is honoured by the website enricher**, on by default (A.2.4). It costs one extra request per business website per run, on that site's own pacing. Sites that disallow their contact pages yield less.
- **Suppression and erasure apply to the run's own CSV and Sheet** ("exclude" lists named in the config, and Erasure requests). Without lists, output is identical.
- **Notifications** go to the workspace's targets after a run. With none configured, nothing happens.
- **Workers execute runs in their own database** (`--db`, or `LEADSCRAPER_DB_URL`) instead of the one a config names. Before, a worker started with `--db X` would still have written a run into the default database.
- **`RunConfig.unapplied()` no longer names `filters.suppression_lists`** (5 notes, was 6).
- **Saved-config documents are stored and compared in JSON form.** A venues preset's integer price-band keys become strings. `venues.py` already reads them with `float(k)`, which resumed runs relied on before.
- **New dependencies:** `fastapi`, `uvicorn`, `python-multipart`, `keyring`, and `tzdata` (Windows only). Dev extras gain `httpx` and `ruff`.

## 14. Deviations from the plan, with reasons

- **The XLSX export is plain**: Overview and Records tabs, phones as text, links, frozen header, autofilter. The formatted deliverable (styles, colour scales, legend, coverage) is Phase 4's job, and its golden tests belong there. The endpoint and the column model are in place for it.
- **The CLI's `export` writes the file; the API streams it.** API exports are not recorded in `runs.files` because nothing is written to disk.
- **Desktop notifications** use the OS's own tools (`osascript`, `notify-send`, a PowerShell balloon tip) rather than a new dependency. They are best-effort, and off in server mode.
- **The cron parser is home-grown** (`core/cron.py`, ~150 lines, tested: names, steps, Vixie day-of-month OR day-of-week, timezones, DST) instead of `croniter`, to avoid a dependency for one function.
- **`/preview` is asynchronous** (202 plus a queued run) rather than returning the sample inline. The API must not scrape (A.3), and a preview takes ~2 minutes.
- **`requests` stays** (A.4 names httpx), for the reason given in the Phase 2 report.
- **A.9.2 (re-verify sources live):** no source was coded against in this phase. The only new outbound traffic is robots.txt (generic, any site) and the user's own notification targets. No live scraping run was made.

## 15. Open issues, carried forward

- **Check the real per-user database for the migration bug (section 9).**
  - If that database was upgraded from revision 0001 to 0002 while it held runs (Phase 2's first run against it), those runs' units, failures, memberships and events were deleted. The runs themselves, and every organisation, contact and evidence row, were not.
  - `python -m leadscraper db info` and `runs list` show what is there.
  - The original lead stores (`data/*.sqlite3`) were never affected: they are only read, from copies.
- **Merge order:** `fix/live-check` and `phase-3/api` are both unmerged. Merging and pushing `main` is yours to do.
- **No authentication** (A.2.2, by design). The guards in section 11 make localhost use safe from browsers, not from other users on a shared network. Server mode needs a private network or an authenticating proxy (`docs/deploy.md`).
- **Webhook URLs are stored in the database in plain text.** They are masked in every API response and left out of bundles. Moving them to a secret reference (`url_secret_name`) is a small follow-up if you want "no bearer secrets in the DB" taken literally.
- **Erasure does not rewrite files already written:** a project's CSV, `skipped.csv`, failure logs, earlier exports, a Google Sheet. The next run or export writes them without the erased organisation.
- **Still not applied at run time:** `filters.min_rating / must_have / exclude_chains`, `icp`, `output.excel / output.json`. The run writes the CSV; the workbook and JSON come from the export endpoints. `RunConfig.unapplied()` names them, as before.
- **Python 3.12's `urllib.robotparser`** resolves Allow/Disallow by first match, not RFC 9309's longest match, so a site that writes `Disallow` before a more specific `Allow` is read more conservatively there.
- **Not exercised live:**
  - SMTP and webhook delivery (mocked in tests);
  - desktop notifications on Windows and Linux;
  - robots.txt against real sites;
  - a scheduled run firing on a real clock;
  - a long run in the container.
- **From earlier phases, still open and still yours:**
  - the terms-of-service review of the inherited sources (now visible per source in `/catalog/sources` as `review: true`);
  - the VayaVia phone-similarity threshold;
  - running `migrate-legacy` into your real data directory;
  - a balanced-depth live check (live check 1, section 6).

Commits on this branch:
- `fix(db): SQLite upgrades no longer cascade-delete child rows`
- `feat(db): migration 0003 -- settings, notification targets, schedules, run files; re-finds are run members`
- `feat(api): services layer and the /api/v1 HTTP API`
- `feat(cli): parity with the API …; tests for the API end to end, services and CLI`
- `feat(net): honour robots.txt when crawling businesses' own sites (A.2.4)`
- `ci: lint, Chromium smoke test, cross-OS bundle round trip, docker compose /health`
- `docs: API reference, deployment (local + server mode), CLAUDE.md for Phase 3`
- `docs: engine (worker housekeeping, queued previews), sources (robots.txt, catalog notes), README (API, server mode)`
- `fix: findings of the Phase 3 review`
- and this report.
