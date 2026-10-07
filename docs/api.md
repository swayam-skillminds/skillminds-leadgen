# HTTP API

The API is what a future UI (and any script) uses. It is a thin layer over
`leadscraper/services/`, the same core the CLI and the worker use. The
API process never scrapes: it reads and writes the database, and a worker
executes the runs it queues.

```bash
python -m leadscraper api                  # http://127.0.0.1:8000/api/v1/docs
python -m leadscraper worker               # in another terminal: executes queued runs
python -m leadscraper api --openapi openapi.json   # the OpenAPI document, for client generation
docker compose up -d                       # both, on a server (docs/deploy.md)
```

- Interactive docs: `/api/v1/docs` (Swagger UI) and `/api/v1/redoc`.
- The OpenAPI document: `/api/v1/openapi.json`. Every endpoint has a summary, a tag, typed
  request and response models and an example; `tests/test_api.py` enforces it.

## Conventions

| | |
|---|---|
| Prefix | Everything is under `/api/v1`. `/health` (unprefixed) is an alias for container health checks. |
| Auth | **None yet** (A.2.2). Every route resolves its workspace through one dependency, `api/deps.py: current_workspace`, so login is added in one place. Until then, serve on localhost or a private network (`docs/deploy.md`). |
| Errors | JSON `{"detail": str, "errors": [...]}`. **404** not found, **409** conflicts with the current state (a duplicate name, pausing a cancelled run), **422** invalid input; a run config that fails the schema lists the schema's errors (`loc`, `msg`, `type`). |
| Lists | Paged: `limit` (1–500, default 50) and `offset`. Responses are `{"items", "total", "limit", "offset"}`. |
| Sorting | `sort=-score,name`: comma-separated fields, with `-` for descending. Each list names its sortable fields in its summary, and any other field is a 422. |
| Times | ISO 8601, UTC. |
| CORS | Off unless `LEADSCRAPER_CORS_ORIGINS=https://ui.example,…` is set. |
| Browser guards | With no login, `Host` must be in `LEADSCRAPER_ALLOWED_HOSTS` (default localhost names; `*` = any), and a write (POST/PATCH/DELETE) carrying a foreign `Origin` is refused (403). Uploads are capped at `LEADSCRAPER_MAX_UPLOAD_MB` (200). |
| Secrets | Never returned: webhook URLs are masked (`https://hooks.slack.com/***`), an SMTP password is a secret *name* plus a `secret_configured` flag, and config exports and bundles drop secret-looking keys. |

## Endpoints

### System and catalog

| | | |
|---|---|---|
| `GET` | `/health` | Database, Chromium, disk, configured keys (booleans only), worker heartbeat. `ok` / `degraded` / `down` (503 only when the database is unreachable). |
| `GET` | `/schema/run-config?country=&verticals=` | The run-config JSON Schema; `ui` hints per field path (label, help, widget, default, enum, required, wizard step, advanced); a minimal `example`; `applicability` (which lead types and sources run in that country, and why not); the `events` schema. |
| `GET` | `/catalog/lead-types?country=` | Each lead type: description, where it applies, signal specs, Records columns, config schema. |
| `GET` | `/catalog/sources?country=&verticals=&kind=&applicable=` | Each source: pack, applicability and reason, scope, `fills`, default settings, endpoint, method, terms note, `review` flag, last live check, and **health** (below). |
| `GET` | `/presets` | Shipped presets and the user's own (imported through bundles). |

**Source health** rolls up the workspace's last 20 finished runs: units, productive / empty / failed units, failures by reason, blocks (`blocked`, `login_wall`, …), and success / failure / block rates. The status is:
- `likely_broken`: in each of the source's last three runs it found nothing and recorded a "nothing parsed" reason (`no_listings`, `no_results`, …);
- `degraded`: a block or failure rate of 30 % or more;
- `ok`;
- `unknown`: no data yet.

### Estimate and preview

| | | |
|---|---|---|
| `POST` | `/estimate` | `{config \| saved_config_id [+version], options}`: units per lead type and source, p50/p90, what fits a time budget, AI cost. A dry run: it makes no requests, needs no worker and leaves nothing behind. |
| `POST` | `/preview` | Queues a ~2-minute sample run (one area × one category × the top sources, quick depth) into a scratch project `<project> (preview)`. **202** with the queued run; when the worker finishes it, `GET /runs/{id}` carries `preview`: sample records, and the full-run estimate calibrated by what the preview saw. |

`options` (run options; all optional): `depth` (quick / balanced / deep), `time_budget_minutes`, `areas`, `categories`, `sources` (`[]` = none), `enrichers` (`[]` = no enrichment), `lead_types`, `force_enrich`, `discovery_workers`.

### Projects and organisations

| | | |
|---|---|---|
| `GET` / `POST` | `/projects` | List (`q`, `sort`) / create (`name`, `description`, `retention_days`). A run creates its project on first use, so creating one first is optional. |
| `GET` / `PATCH` / `DELETE` | `/projects/{id}` | Get with counts / rename, describe, set lead retention / delete with its organisations and history. Refused (409) while a run is active. |
| `GET` | `/projects/{id}/export.{csv\|json\|xlsx}` | The whole project, regenerated from the database. |
| `GET` | `/organizations` | `project_id` or `run_id` (with `change=new\|updated\|unchanged`), `q` (name), `locality`, `category`, `lead_type`, `has=phone&has=email` (website, socials, address), `min_score`, `max_score`, `domain`; sort by score, name, locality, category, created_at, updated_at, fit, intent, reach. |
| `GET` | `/organizations/{id}` | The merged record, contacts (with source, `published`/`inferred`, confidence), people, signals, evidence (source, URL, fields, run, time), identity keys, and the runs that touched it. |
| `POST` | `/organizations/erase` | A data-subject request: `domain` and/or `phone` (`region` for a number without +code), optional `project_id`, `dry_run`. Deletes every matching organisation, in every project, with everything hanging off it. With `suppress` (the default) the value goes on the **Erasure requests** list, which every export applies, so a later run that finds it again never exports it. A shared host (gmail.com, facebook.com) is refused: it identifies nobody. Failure rows naming the business are removed too. Files written before the request (a project's CSV, `skipped.csv`, failure logs, earlier exports, a Google Sheet) are not rewritten: regenerate them, and the next export or run writes them without it. |

### Saved configs

| | | |
|---|---|---|
| `GET` / `POST` | `/configs` | List (latest versions; `q`, `project`, `sort`) / save a validated document. |
| `POST` | `/configs/validate` | Check a document without saving it; lists the settings this build does not act on yet. |
| `POST` | `/configs/import`, `/configs/import/file` | YAML or JSON text, or an upload. A legacy `config.yaml` is converted. An existing name gets a new version. |
| `GET` / `PATCH` / `DELETE` | `/configs/{id}` | Latest (or `?version=`) / rename / delete every version. Runs keep their own snapshots. |
| `GET` / `POST` | `/configs/{id}/versions` | Every version / save a new one (an unchanged document is not a new version). |
| `POST` | `/configs/{id}/clone` | A version as a new saved config. |
| `GET` | `/configs/{id}/export?format=yaml\|json&version=` | Download, secrets removed. |

A saved config's `id` is its first version's row id, stable across versions and renames. A run records the exact version it ran (`saved_config_id` is that version's `version_id`).

### Runs and history

| | | |
|---|---|---|
| `POST` | `/runs` | `{config \| saved_config_id [+version], project_id?, options}` → **201**, `queued`. A worker claims it. `runtime.db_url` is refused (422): runs use the application database. |
| `GET` | `/runs` | `project_id`, `status` (repeatable), `kind` (run / preview / import), `saved_config_id`, `sort`. |
| `GET` | `/runs/{id}` | Everything history keeps: the config snapshot and request; status, timings, attempts, active seconds; estimate vs actual (`estimate`, `actual`); units by status; counts per lead type and **per source** (units, done, failed, skipped, found, new, merged, seconds vs expected); failures by reason and source; organisations by action; AI usage; generated files; a preview's sample. |
| `POST` | `/runs/{id}/pause` `/resume` `/cancel` | Graceful: in-flight work finishes, then everything is flushed. Resume re-queues a paused or failed run from its checkpoint. A transition that does not apply is a 409. |
| `POST` | `/runs/{id}/rerun` | A new queued run with the same snapshot and options (body `options` overrides some). |
| `GET` | `/runs/{id}/failures` | Paged, filter by `source` / `reason`. |
| `GET` | `/runs/{id}/units` | The checkpoint: every work unit's status, timings, yield and skip reason. |
| `GET` | `/runs/{id}/diff?against=&change=` | Each organisation the run touched, as `new` (it did not exist when `against` ended), `updated`, `unchanged` (found again, nothing to add), plus `only_in_against`. `against` defaults to the project's previous finished run. |
| `GET` | `/runs/{id}/events` | **Server-Sent Events**, below. |
| `GET` | `/runs/{id}/events/history?after=` | The same events as a JSON list. |
| `GET` | `/runs/{id}/export.{csv\|json\|xlsx}?scope=run\|new\|project&against=` | Regenerated from the database at any time; re-exporting never re-scrapes. The default scope is `run`, or `new` for a scheduled "only new" run. |
| `POST` | `/runs/{id}/export/google-sheets` | Writes to the run's configured sheet through the service-account flow. 409 with the reason when Sheets is unavailable. |

**Exports** (Phase 4). All three formats render one table through one column model (`export/columns.py`), so a value never differs between them:
- **XLSX** is the deliverable workbook, exactly two tabs. **Records**: one row per organisation; core columns (score, name, lead types, place, locality, published contacts, maps link, address, decision-makers), each lead type's own columns, Fit / Intent / Reach and score reasons, the top signal, provenance (sources, first and last seen, the run's change), the organisation id. **Overview**: run summary, results (totals, new vs updated, breakdowns, top 10), data quality (coverage per field) and the column legend; for an empty export, why it is empty. Phones are text, links clickable (http(s) only), text never a formula. Samples: `docs/samples/`.
- **CSV** has the Records headers and values (dates in the run's local time, formula-looking text quoted with `'`).
- **JSON** has `columns` (key, header, kind, group), `records` (`id`, `values` keyed by column key, `scores` with reasons and per-lead-type contributions, `signals`, `people`, `inferred_contacts`, `change`, `suppressed`) and `summary` (the Overview's numbers).
- Columns the run's `fields.include` leaves out are absent; optional columns (socials, signals, decision-makers, a lead type's columns) appear only when some record has a value.
- Rows follow `output.sort` (default `auto`: score, highest first, grouped by place when the run covers several places).
- Suppression lists attached to the config, plus Erasure requests, apply to all three. `exclude` drops rows; `flag` fills a `Suppressed` column naming the lists. `filters.min_rating` and `filters.must_have` leave organisations out too (the Overview counts them).
- The run's own CSV (`output.csv`, the legacy layout) and its Google Sheet keep the layout the Mumbai and Nashik tabs use.

### Suppression, schedules, notifications

| | | |
|---|---|---|
| `GET` / `POST` | `/suppression-lists` | `{name, mode: exclude\|flag, entries: [{kind?, value}]}`. Attach a list to a config by name in `filters.suppression_lists`. |
| `GET` / `PATCH` / `DELETE` | `/suppression-lists/{id}` | |
| `GET` / `POST` | `/suppression-lists/{id}/entries` | Matching is by canonical name, registrable domain or E.164 phone. A `kind` left out is guessed from the value. |
| `DELETE` | `/suppression-lists/{id}/entries/{entry_id}` | |
| `POST` | `/suppression-lists/{id}/import` | CSV or XLSX upload. Columns headed name / domain / website / phone are read as such; without a header, values are classified one by one. |
| `GET` / `POST` | `/schedules` | `{saved_config_id, cron, timezone, only_new, overrides: {depth, time_budget_minutes}}`. Cron has five fields, names and steps, and `@daily`/`@weekly`/`@monthly`; it is read in the IANA timezone. |
| `GET` / `PATCH` / `DELETE` | `/schedules/{id}` | Disabling clears `next_run_at`. |
| `POST` | `/schedules/{id}/run` | Queue its run now. |
| `GET` / `POST` | `/notifications` | `desktop` (local mode only), `webhook` (`{url}`; Slack/Teams read the `text` field), `email` (`{host, port, security, username, password_secret_name, from, to}`; the secret's name must start with `notify-`). Webhook and SMTP hosts on loopback, link-local or private addresses are refused unless `LEADSCRAPER_ALLOW_PRIVATE_TARGETS=1`. Events: `run_completed`, `run_failed`, `needs_attention` (completed, but a lead type failed, ≥25 % of units failed, or sources were blocked ≥5 times). |
| `GET` / `PATCH` / `DELETE` | `/notifications/{id}` | |
| `POST` | `/notifications/{id}/test` | Sends a test message; `{ok, error}`. |

The worker fires due schedules on every poll. A schedule's run uses the saved config's **latest** version. With `only_new`, the run remembers the schedule's previous run, so its exports default to what is new since then.

### Settings and bundles

| | | |
|---|---|---|
| `GET` / `PATCH` | `/settings` | `retention` (`logs_days`, `events_days`, `seen_urls_days`), `keep_awake`, `robots_txt`. Also shows `data_dir`, the database (password masked) and `mode`, all read-only: they are process settings (`LEADSCRAPER_DATA_DIR`, `LEADSCRAPER_DB_URL`, `LEADSCRAPER_MODE`). Each project has its own `retention_days`. |
| `POST` | `/settings/purge?dry_run=` | Apply retention now. The worker does it hourly. |
| `GET` | `/bundle/export?include_leads=&include_history=&project_id=` | A zip of saved configs (every version), presets, suppression lists, projects, schedules and settings, optionally with leads and history. **No secrets**, and no notification targets. |
| `POST` | `/bundle/import` | Upload a bundle. Version-checked: an unknown format version, or a schema revision from a newer installation, is refused before anything is written. Additive and idempotent. Organisations go verbatim into an empty project and are merged through the identity rules into a non-empty one. Schedules arrive disabled. |

## Live progress (SSE)

`GET /api/v1/runs/{id}/events` streams the run's typed events (`docs/engine.md`):
- each message is `id: <seq>`, `event: <type>`, then `data: <the event as JSON>`;
- it replays from `after` (or the `Last-Event-ID` header a reconnecting `EventSource` sends), then follows the run live;
- it ends after the run's final `run_state` (completed, failed, cancelled, paused or re-queued);
- `follow=false` replays and closes; a comment line keeps idle connections alive.

```js
const source = new EventSource("/api/v1/runs/42/events");
source.addEventListener("progress", (e) => render(JSON.parse(e.data)));
source.addEventListener("run_state", (e) => {
  if (["completed", "failed", "cancelled", "paused"].includes(JSON.parse(e.data).status)) source.close();
});
```

```bash
curl -N http://127.0.0.1:8000/api/v1/runs/42/events
```

## A typical session

```bash
API=http://127.0.0.1:8000/api/v1
# 1. What can run in the US? Draft a config from the example.
curl "$API/schema/run-config?country=US"
# 2. Save it, estimate it, preview it.
curl -X POST $API/configs -H 'content-type: application/json' -d @config.json   # {"document": {...}}
curl -X POST $API/estimate -H 'content-type: application/json' -d '{"saved_config_id": 1, "options": {"time_budget_minutes": 30}}'
curl -X POST $API/preview  -H 'content-type: application/json' -d '{"saved_config_id": 1}'
# 3. Run it; follow it; pause or cancel if needed.
curl -X POST $API/runs -H 'content-type: application/json' -d '{"saved_config_id": 1, "options": {"time_budget_minutes": 30}}'
curl -N $API/runs/2/events
# 4. Look at the results; download the workbook.
curl "$API/organizations?run_id=2&has=phone&sort=-score"
curl -o leads.xlsx $API/runs/2/export.xlsx
# 5. Next month: re-run, and take only what is new.
curl -X POST $API/runs/2/rerun
curl -o new.xlsx "$API/runs/3/export.xlsx?scope=new"
```
