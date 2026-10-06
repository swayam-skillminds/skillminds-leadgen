# Build Prompt — Skillminds Lead Generation Application

> **How to use this file.** Work one phase per Claude Code session. Every session gets
> **Section A (Master Context)** plus **exactly one phase from Section B**. Paste both, or say:
> *"Read APP-BUILD-PROMPT.md. Do Section A + Phase N."* Each phase ends at its acceptance
> checks. Stop there and report; do not start the next phase.
>
> This file **supersedes `REBUILD-PROMPT.md`**. Three parts of that file still apply and are
> referenced below: Part 0 (verified bugs), Part 2 (speed) and Part 3 (time-budget scheduler).
> Its Part 4 (web UI) is replaced: the UI is out of scope for now.

---

# Section A — Master Context (include in every session)

## A.1 What we are building

An existing Python scraper (`leadscraper/`, ~7k LOC, see `CLAUDE.md` and `README.md`) is being
turned into a **backend application that generates B2B leads**:

- **Any country, any city, any category.** Today it is hard-wired to event vendors in Mumbai and venues in Nashik.
- **Many lead types,** not only "businesses that exist". The tool should also find businesses with a
  **reason to buy now**: hiring signals, funding news, public tenders, new registrations, weak digital presence,
  outdated tech, neglected apps.
- **Fully user-configurable:** location, lead types, sources, fields, depth, time budget, AI usage.
- **Usable from any device and any OS:** macOS, Windows and Linux. It runs locally *or* on an always-on server.
- **Final deliverable per run:** a beautified Excel workbook with exactly two tabs, **Overview** and
  **Records**, styled like `Nashik-Venues-Formatted.xlsx`. CSV/JSON/Google Sheets are optional extras.

Users: Skillminds staff only, for now. Skillminds sells custom software, web/app development, ERP,
IT staff augmentation, training and related IT services. Later this may be offered to clients.

## A.2 Hard scope boundaries

1. **No UI in this build.** Deliver a complete, documented HTTP API plus a CLI. A frontend will be
   built later, purely as an API client. Design every endpoint so a UI can be generated against it.
2. **No login yet, but don't paint us into a corner.** Every user-owned row carries a `workspace_id`
   (a single default workspace for now). All request handling goes through one
   `current_workspace` dependency, so auth can be added later in one place.
3. **The tool generates leads. It never contacts them.** Reject outreach and CRM features:
   contact status, email sending, sequences, message or pitch drafting, call logs, pipeline
   stages. Notifications *to our own users* (run finished, etc.) are fine.
4. **Polite, lawful collection only.**
   - Respect per-host rate limits.
   - No CAPTCHA solving, no proxy rotation for evasion.
   - Never log into third-party sites.
   - Never scrape sites whose terms prohibit it when a public API exists. Upwork, Indeed, Glassdoor and
     LinkedIn Jobs are excluded entirely.
   - LinkedIn: only profile/company URLs that a search engine has already indexed.
   - Honour robots.txt for generic website crawling (toggle, on by default).
5. **Contact data honesty.** Contact details are recorded only where they were actually
   published. Nothing is invented. The optional email-pattern feature (Phase 6) is the only
   inference allowed, and it is always labelled.
6. **Existing data is precious.** `data/**/*.sqlite3` and the `.xlsx` files contain real results.
   Migrations and tests work on **copies**, never the originals.

## A.3 Architecture principles

- **API-first, one core.** The FastAPI app, the CLI and the worker are thin layers over the same
  service/core modules. Nothing business-critical lives in a router or a CLI command.
- **Nothing crashes a run** (existing invariant, keep it). Sources yield partial results and never
  raise. Every failure becomes a structured failure record with a reason code.
- **Lead types are isolated plugins.** Each lead type is a self-contained module with its own
  config model, sources, signal definitions, output columns, scoring contribution, fixtures and
  tests. Rules:
  - A lead type depends only on core interfaces, never on another lead type.
  - Enabling, disabling or deleting one lead type never changes another's output.
  - A lead type's failure is contained to its own work units.
  - Lead-type config is namespaced (`lead_types.<key>.*`), including its source settings.
  - Signals and evidence are tagged with the lead type that produced them.
- **Country and vertical packs.** Region- or industry-specific sources live in packs that declare
  where they apply (`applicable_countries`, `applicable_verticals`). Packs that don't apply are
  silently inactive, not failing.
- **Jobs run out-of-process.** A separate worker process takes runs from a DB-backed queue.
  The API process never scrapes. Runs survive API restarts and resume after crashes.
- **Cross-platform by construction.**
  - `pathlib` everywhere.
  - `platformdirs` for data/config/cache locations.
  - No shell-specific code.
  - Graceful stop works on Windows (no reliance on POSIX signals alone).
  - CI on macOS, Windows and Linux.
- **Explainability.** Every score has human-readable reasons. Every fact has provenance:
  source, URL, fetch time.

## A.4 Stack (decided)

| Concern | Choice |
|---|---|
| Language | Python 3.12+ (must also run on 3.14, the current venv) |
| API | FastAPI, pydantic v2, Server-Sent Events for live progress, OpenAPI docs |
| Persistence | SQLAlchemy 2 + Alembic. **SQLite (WAL)** default for local mode, **Postgres** for server mode, same code |
| Jobs | Own worker process + DB-backed queue (no Celery/Redis). Scheduling of recurring runs in the worker |
| Scraping | Playwright (browser context pool), httpx (HTTP/2, pooled), existing per-host `RateLimiter` |
| Parsing | BeautifulSoup/lxml, `phonenumbers` (country-aware), `rapidfuzz`, `tldextract` |
| Excel | openpyxl (add it to dependencies; it is currently missing) |
| Secrets | `keyring` (OS keychain) locally. Env vars / mounted secret file in server mode |
| Keep-awake | `wakepy` |
| Packaging | `pyproject.toml`, Docker image + `docker-compose.yml` (app + worker + optional Postgres), plain `pip install` path |
| Tests | pytest, recorded fixtures (no live network by default), `@pytest.mark.live` opt-in smoke tests |

## A.5 Target package layout

Adapt it if the code argues otherwise, and explain why.

```
leadscraper/
  core/        domain models, identity & dedup, scoring, normalisation, geo, country data
  db/          SQLAlchemy models, repositories, Alembic migrations
  config/      pydantic run-config schema, presets, legacy YAML importer
  engine/      planner, scheduler (time budget), estimator, worker, events, keep-awake
  net/         http client, browser pool, rate limiter, robots.txt, dead-domain cache
  leadtypes/   one sub-package per lead type (see A.7)
  packs/       country packs (in, us, uk, eu, ae, …) and vertical packs (events, hospitality, …)
  ai/          providers/, features/, budget & usage ledger, cache, key storage
  export/      excel (Overview + Records), csv, json, google sheets
  api/         FastAPI routers, schemas, dependencies (current_workspace)
  cli/         CLI over the same services
presets/       shipped run-config presets (YAML)
docs/          sources.md (every source: URL, method, ToS note, last verified date), api.md, lead-types.md
```

## A.6 Core domain model

The model is **organization-centric**. Every lead type resolves its findings to an organization.

- **Workspace → Project → Run.** A project is a long-lived lead database (for example "Pune dentists",
  "UAE Shopify stores"). Repeated runs in a project merge into it rather than duplicating.
- **SavedConfig:** named, versioned run configs, exportable and importable.
- **Organization:** the deduplicated entity. **IdentityKeys:** registrable domain, E.164 phones,
  registry IDs, Maps place IDs, canonical name + locality.
  - Domain is the strongest cross-lead-type key.
  - A blocklist excludes shared hosts from identity: social networks, link-in-bio sites,
    site builders' subdomains, free email domains.
- **Contact:** a phone, email, social link or address. Each carries a `kind`
  (`published` / `inferred`), source and confidence.
- **Person:** a decision-maker (name, title, public profile URL, source).
- **Signal:** dated evidence of buying intent or fit. Fields: `type`, `lead_type`, title, structured
  payload, `observed_at`, `published_at`, optional `expires_at` (for example a tender deadline),
  strength and source URL.
- **Evidence/Provenance:** source, URL and fetched_at for every field written.
- **RunOrganization:** which orgs a run created or updated. This powers "new since last run" diffs.
- **WorkUnit, Failure, AIUsage, SuppressionList/Entry, Schedule.**

Merge semantics carry over from today's `Lead.merge`: union lists, fill blanks, settle conflicting
ratings by higher review count, never overwrite with a blank.

## A.7 The lead-type plugin contract

Each lead type implements roughly the following. Refine it in Phase 1 and document it in `docs/lead-types.md`.

```python
class LeadType(Protocol):
    key: str                                 # "local_business", "hiring", ...
    title: str
    config_model: type[BaseModel]            # its namespaced config section
    applicable_countries: set[str] | None    # None = anywhere
    def sources(self, cfg, geo) -> list[Source]          # incl. pack sources that apply
    def plan(self, cfg, geo, depth) -> list[WorkUnit]    # for scheduler + estimator
    def signal_specs(self) -> list[SignalSpec]           # types, default strength, recency half-life
    def columns(self, cfg) -> list[ColumnSpec]           # Records-tab columns it contributes
    def score(self, org, signals, cfg) -> ScoreParts     # fit/intent contributions + reasons
```

Lead types:

| key | Lead type | Phase |
|---|---|---|
| `local_business` | Businesses by category + location (today's scraper; venues become its hospitality vertical) | 1 |
| `digital_audit` | Weak or missing digital presence | 6 |
| `hiring` | Companies hiring tech/IT/digital roles | 6 |
| `funding_news` | Funding, launches, expansion, transformation news | 6 |
| `decision_makers` | Founder/CTO/IT-head discovery (enrichment over any lead type) | 6 |
| `tenders` | Public IT tenders and RFPs | 7 |
| `registrations` | Newly incorporated companies | 7 |
| `tech_stack` | Companies running a specific or outdated technology | 8 |
| `app_store` | Apps with poor ratings or long-stale updates | 8 |

## A.8 Run config (shape)

A single pydantic model, versioned with `schema_version` and migratable, exposed as JSON Schema to
the future UI. Approximate shape:

```yaml
schema_version: 1
name: "Pune manufacturers — modernisation"
project: "pune-manufacturing"
geography:
  country: IN                    # drives packs, phone parsing, currency
  places: ["Pune"]               # cities/regions; free text, geocoded
  areas: []                      # optional sub-areas; empty = whole place
  radius_km: null
lead_types:
  local_business:
    enabled: true
    categories: ["manufacturing companies", "industrial suppliers"]
    sources: { google_maps: {enabled: true, max_results: 40}, justdial: {enabled: false} }
  digital_audit: { enabled: true, checks: [https, mobile, pagespeed, cms_age] }
  hiring: { enabled: false }
fields: { include: [phones, emails, website, address, socials, decision_makers] }
filters: { min_rating: null, must_have: [phone], exclude_chains: true, suppression_lists: [] }
icp: { industries: [], company_size: null, technologies: [] }
scoring: { weights: {fit: 0.4, intent: 0.4, reach: 0.2} }
depth: balanced                  # quick | balanced | deep
time_budget_minutes: 120         # null = run to completion
ai: { enabled: false, provider: null, features: [], budget_usd: 2.00 }
output: { excel: true, csv: false, json: false, google_sheets: false }
```

Rules:
- Unchecked fields switch off the enrichers that only exist to fill them.
- Sensible defaults: a country + place + one category must be enough to start a good run.

## A.9 Working method (every phase)

1. Read `CLAUDE.md`, this file, and the code the phase touches. Post a short plan before editing.
2. **Re-verify external sources live before coding against them.** URLs, APIs and terms change.
   Record each in `docs/sources.md`: endpoint, method, auth/key needs, rate limits, ToS note,
   date verified.
3. Tests use recorded fixtures. Never hit the network in the default test run.
4. Keep the test suite green throughout. Port existing tests rather than deleting them.
5. At the end of the phase:
   - update `CLAUDE.md` (commands and architecture) and the relevant `docs/`;
   - run the full suite;
   - report what was done, what was verified and how, deviations from this plan with reasons,
     and open issues.
6. Commit in small, coherent steps on a feature branch. Never commit secrets, `.env`,
   `credentials.json`, databases or generated workbooks.

---

# Section B — Phases

## Phase 0 — Safety net and known bugs

**Goal:** a trustworthy baseline before any restructuring.

1. **Version control.** Initialise git if the folder still isn't a repo. Confirm `.gitignore`
   covers credentials, `.env`, `data/`, `logs/`, `*.xlsx` and `.venv/`. If a `credentials.json` is
   anywhere in the tree, stop and tell the user (the old key must be treated as compromised).
2. **Characterisation tests.** Pin current behaviour where the refactor will touch it: dedup/merge
   decisions, scoring output, venue tiering, CSV columns for both profiles. Run them against
   *copies* of `data/leads.sqlite3` and `data/nashik/venues.sqlite3`.
3. **Known bugs.** Work through `REBUILD-PROMPT.md` **Part 0**. Check each item against the
   *current* code first, since some may already be fixed. Fix what remains and add a regression
   test for each.
4. **Dependencies.** Move to `pyproject.toml` (keep `requirements.txt` generated or delete it, but say
   which). Add the missing `openpyxl`.

**Acceptance:**
- Suite green.
- Characterisation tests exist for dedup, scoring, tiering and both export layouts.
- A report lists each Part 0 item as *already fixed / fixed now (test name) / not reproducible*.

---

## Phase 1 — Core refactor: domain model, config schema, plugins, packs

**Goal:** the A.5 layout, the A.6 model and the A.7 contract, with today's functionality living in the
`local_business` lead type.

1. **Persistence.** SQLAlchemy models + Alembic migrations for the A.6 entities, with `workspace_id`
   on every owned table. Use the SQLite WAL default and make sure the Postgres path works.
2. **Identity and dedup.**
   - Port the store's matching rules into `core/`, plus domain and registry-ID keys and the
     shared-host blocklist.
   - Keep the venue-profile dedup rules as options of the hospitality vertical.
3. **Country generalisation.**
   - Phone parsing, currency and address handling are driven by `geography.country`.
   - Remove hard-coded `IN`/`+91`/₹/`mumbai`.
   - Place names are geocoded via Nominatim (polite, cached) for centers and radius.
4. **Packs.** Move existing sources into packs:
   - `in` pack: justdial, sulekha, indiamart.
   - `events` pack: wedmegood, weddingwire.
   - `hospitality` pack: stayvista, saffronstays, elivaas, airbnb, booking, plus the venue
     classification and tiers from `venues.py`.
   - `social` pack: facebook, instagram, linkedin_serp.

   Core sources: google_maps, google_search, website, maps_lookup. Directory URL templates take
   city and category slugs from the pack, not from hard-coded strings.
5. **Run config.**
   - The A.8 pydantic schema, with JSON Schema export.
   - A legacy importer that converts `config.yaml` and `config.nashik.yaml` into presets in
     `presets/`.
6. **Data migration.** A one-off command imports the existing SQLite lead stores (from copies) into
   projects: "Mumbai event vendors" and "Nashik venues".
7. **Scoring v2.** Fit / intent / reachability with per-lead-type contributions, configurable
   weights and human-readable reasons. Today's legitimacy and venue scores become the
   `local_business` fit contribution, so existing rankings are preserved.

**Acceptance:**
- Both legacy presets load.
- Re-exporting the migrated projects reproduces the characterisation results. Allowed
  differences are listed and justified.
- A new run for a non-Indian city (for example "dentists in Austin, US") activates only core + social
  sources, parses US phones, and produces no India-pack failures.
- A test shows that disabling a lead type leaves another lead type's output unchanged.

---

## Phase 2 — Engine: planning, estimation, time budget, resilient jobs

**Goal:** runs that are predictable, interruptible and fast.

1. **Work units** are `(lead_type, source, place/area, category-or-query)`. The planner expands a
   config into units. Port the scheduler design from `REBUILD-PROMPT.md` **Part 3**:
   - priority = expected yield / expected seconds;
   - cold-start priors, then learned per-unit stats;
   - never start a unit that can't finish;
   - a reserved tail for flush/score/export;
   - a checkpoint after every unit.
2. **Depth presets** (quick / balanced / deep), per Part 3.3. They scale each lead type's own knobs.
3. **Estimator.** Before a run, return:
   - units queued, broken down per lead type and source;
   - expected duration as a **p50/p90 range**;
   - what fits inside the time budget, and what would be skipped;
   - AI cost estimate (when AI is on).

   During a run, refine the ETA live from observed rates.
4. **Speed.** Port `REBUILD-PROMPT.md` **Part 2**:
   - parallelise across hosts, never within one;
   - browser context pool;
   - concurrent enrichment;
   - persistent dead-domain cache;
   - skip already-enriched records unless forced.
5. **Worker and lifecycle.**
   - Separate worker process with a DB-backed queue.
   - States: queued → running → pausing/paused → stopping → completed / failed / cancelled.
   - **Pause/resume/cancel** are graceful: in-flight work finishes, then flush.
   - **Crash/reboot recovery:** a restarted worker resumes interrupted runs from their last checkpoint.
   - Ctrl-C in CLI mode shares the same stop path.
6. **Keep-awake (local mode).**
   - While a run is active, the worker holds a `wakepy` keep-awake lock (config toggle, on by
     default), released when no runs are active.
   - Detect sleep anyway (wall-clock jump): re-validate browser contexts and connections, then
     continue.
   - Document the honest limits:
     - A sleeping machine runs nothing.
     - On macOS, closing the lid on battery forces sleep.
     - For truly unattended runs, use server mode (Phase 3).
7. **Preview run.** One area × one category × top sources per lead type, capped at ~2 minutes.
   It returns sample records and a full-run estimate.
8. **Events.** A typed progress event stream: unit started/finished, counts, ETA, current activity,
   failures, AI spend. It is persisted, so history can replay it.

**Acceptance:**
- Killing the worker mid-run and restarting it resumes with at most one unit lost.
- A 15-minute budget run finishes within budget + tail, and its report lists the skipped units.
- Estimator p50 is within ±35% of actual on a recorded-fixture run.
- Pause/resume/cancel are tested.
- Keep-awake is acquired and released correctly (mocked) on all three OSes in CI.

---

## Phase 3 — API, CLI, history, saved configs, portability, ops features

**Goal:** everything a future UI needs, usable today via HTTP and CLI.

1. **HTTP API** (FastAPI, versioned under `/api/v1`, all list endpoints paginated, filterable and sortable):
   - `GET  /health` — Playwright/Chromium present, DB, disk, keys configured, worker alive.
   - `GET  /schema/run-config` — JSON Schema plus UI hints: labels, help text, defaults, enums,
     and which sources/lead types apply to a given country.
   - `GET  /catalog/lead-types`, `/catalog/sources?country=` — what exists, where it applies, ToS
     notes, health.
   - `POST /estimate` — estimate a config without running it.
   - `POST /preview` — preview run.
   - Projects: CRUD.
   - Saved configs: CRUD, versions, clone, import, export.
   - Runs:
     - create (from a config or a saved config), list, get;
     - `pause` / `resume` / `cancel` / `rerun`;
     - `GET /runs/{id}/events` (SSE, live and replay);
     - `GET /runs/{id}/failures`;
     - `GET /runs/{id}/diff?against=` (new / updated / unchanged orgs vs. another run).
   - Organizations: list/filter/sort within a project, get with contacts, people, signals and
     provenance. Erase by domain/phone, for data-subject deletion requests.
   - Exports: `GET /runs/{id}/export.xlsx|.csv|.json`. Exports regenerate from the DB at any time;
     re-exporting never needs a re-scrape. `POST /runs/{id}/export/google-sheets`.
   - Suppression lists: create, import from CSV/XLSX (names, domains, phones), attach to configs.
     Matching orgs are excluded or flagged (configurable).
   - Schedules: recurring runs of a saved config (cron-like). Option to report "new since last
     scheduled run" only.
   - Notifications: per-workspace targets: desktop notification (local mode), generic webhook
     (Slack/Teams compatible), SMTP email to our own users. Events: run completed, failed,
     needs attention.
   - Settings: data directory, retention (auto-purge raw pages/logs after N days, project-level
     lead retention), keep-awake toggle, robots.txt toggle.
2. **CLI** parity for the core flows: run, estimate, preview, status, pause/resume/cancel, export,
   config import/export, bundle import/export, migrate-legacy. `run.py` keeps working as a
   compatibility shim.
3. **History.** Every run stores:
   - its immutable config snapshot;
   - timings, estimate vs. actual, counts per lead type and source;
   - failures;
   - AI usage;
   - generated files.

   Runs can be listed per project, compared, re-run and re-exported.
4. **Source health.** Rolling success/failure/block rates per source from recent runs, exposed via
   the catalog. A source that keeps returning `no_listings` is flagged as likely broken.
5. **Portability ("replicate on any device").**
   - **Bundle export/import:** a zip of saved configs, presets, suppression lists, projects and
     (optionally) leads and history. Secrets are **never** included. Version-checked on import.
   - **Server mode:** `docker compose up` starts API + worker (+ optional Postgres) on any always-on
     machine. Every device then just uses that URL. Document deployment on a small Linux VPS.
   - All local state lives under the `platformdirs` data directory, overridable by env var.
6. **Cross-platform CI.** GitHub Actions matrix on macOS, Windows and Linux: lint, tests, and a
   headless Playwright smoke test against a local fixture server.

**Acceptance:**
- An OpenAPI document covers every endpoint, with examples.
- An end-to-end test drives create config → estimate → run (fixtures) → events → export via the API.
- A bundle exported on one OS imports on another in CI.
- `docker compose up` works and passes `/health`.
- The CI matrix is green.

---

## Phase 4 — Excel output (Overview + Records)

**Goal:** the deliverable. Read `tools/build_nashik_workbook.py` and `Nashik-Venues-Formatted.xlsx`
first. Keep their visual language and generalise the code. The reference workbook has four tabs
(Overview, Venues, Data Quality, Legend). **The new one has exactly two: `Overview` and `Records`.**

1. **Records.**
   - One row per organization. Columns are assembled from:
     - core columns: name, lead types, location, website, phones, emails, socials, maps link;
     - each enabled lead type's `columns()`;
     - only the fields the user selected.
   - Scores: overall, fit, intent, reach, score reasons.
   - Top signals: type, short text, date, link.
   - Decision-makers.
   - Provenance (sources) and first-seen/last-seen dates.
   - Header styling, frozen header and first column, autofilter.
   - Column widths tuned per column type.
   - Clickable hyperlinks.
   - Colour scales on score columns. Signal recency highlighted.
   - Phones and IDs written **as text**. The Sheets USER_ENTERED `#ERROR!` problem noted in the
     existing tool must not recur.
   - Sort order configurable. Default: overall score desc, grouped by place when the run spans
     several places.
   - Inferred contacts (email pattern) sit in their own clearly-labelled column, never inside
     published-contact columns.
2. **Overview** absorbs what used to live in the extra tabs:
   - **Run summary:** project, config name, date, duration, places, lead types, sources used, AI
     on/off and spend.
   - **Results:** totals, new vs. updated since the previous run, counts by lead type, place,
     category, signal type and score band; top 10 leads.
   - **Data quality:** coverage % per field (phone, email, website, decision-maker…).
   - **Column legend:** a compact table explaining the non-obvious columns and how scores work.
3. Deterministic output (stable ordering), generated from the DB only. It works for any lead-type
   mix, including zero results (an Overview explaining why).
4. CSV and JSON exporters share the same column model. Google Sheets export is optional and kept
   behind its existing service-account flow.

**Acceptance:**
- Golden-file tests for workbook structure: tabs, headers, styles, number formats, hyperlinks.
- Fixtures cover: `local_business` only; a three-lead-type mix; venues (hospitality vertical);
  an empty run.
- The user can open the generated sample workbooks, committed under `docs/samples/` from fixture
  data only, with no real leads.

---

## Phase 5 — AI layer: multiple providers, opt-in, budgeted

**Goal:** AI makes runs smarter and more robust. It never makes them fragile, and it never invents facts.

1. **Providers** behind one interface: `anthropic`, `openai`, `gemini`, `openai_compatible`
   (base URL; covers OpenRouter, Groq, local Ollama/LM Studio), and the existing `claude_code_cli`
   backend for local use with a Claude subscription.
   - Each declares capabilities: structured output, tool use, **web search**, context size.
   - A configurable price table, because prices change. Model IDs come from config, not code.
   - Two tiers per provider: a **worker** model (cheap, bulk) and a **judge** model (hard calls).
   - A fallback chain across configured providers on errors/quota. On exhaustion, continue without AI.
2. **Keys.** Stored via `keyring` locally, env/secret file in server mode. Never written to the DB,
   logs, bundles or exports. The API only reports *whether* a key is configured, plus a
   connectivity test.
3. **Budget and caching.**
   - Pre-run cost estimate (included in `/estimate`).
   - A hard per-run USD cap.
   - A usage ledger per run, feature and provider.
   - Response cache keyed by provider + model + prompt hash (generalise `ai_cache.sqlite3`).
4. **Features.** Each one is individually toggleable in config:
   - `icp_builder` — natural-language description of what we sell and to whom → a draft run
     config: lead types, places, categories, roles, signals, filters. Validated against the schema
     and **returned for confirmation, never auto-run**. Ships with Skillminds presets: custom
     software, web/app dev, ERP, staff augmentation, training.
   - `planner` — expands places into areas, categories into query synonyms, roles into title
     variants. Suggests which packs and lead types fit.
   - `source_discovery` — proposes extra country- or industry-specific directories and feeds. They
     land as **candidate sources** the user approves. Approved ones are scraped by a generic
     AI-assisted extractor source.
   - `web_search_discovery` — uses provider web-search tools as a discovery source in any lead
     type. Every returned organization must carry a fetched source URL; anything without one is
     discarded.
   - `extraction_fallback` — extracts structured fields from page text when selectors fail.
   - `normalisation` — clean names, industry tagging, company-size estimate (labelled as an
     estimate), duplicate adjudication (judge tier).
   - `signal_interpretation` — classifies news items and job posts into signal types and strength.
   - `lead_summary` — one line on what the organization does and why it matched. No outreach copy.
5. **Grounding guardrails.**
   - Any AI-supplied contact detail must appear verbatim in the fetched source text, or it is
     dropped.
   - AI-estimated attributes are flagged as such in data and in the workbook.
   - AI output is schema-validated. Invalid output is retried once, then skipped.

**Acceptance:**
- Provider conformance tests (mocked HTTP) for all providers.
- A run with AI on but a failing provider completes identically to AI off.
- The budget cap stops AI mid-run and the run continues.
- A test proves hallucinated phones/emails (absent from source text) are rejected.
- `icp_builder` produces a schema-valid config from a sample brief.

---

## Phase 6 — Lead types, wave 1

Build each as an isolated plugin per A.7: own config, sources, signals, columns, scoring,
fixtures, tests, and a section in `docs/lead-types.md`. Verify every external endpoint live first (A.9).

1. **`digital_audit`.** Audits orgs from any other lead type, or from an **uploaded list of domains
   or businesses**.
   - Checks (each toggleable, each producing a signal):
     - no website;
     - site unreachable;
     - no HTTPS / expiring certificate;
     - not mobile-friendly (viewport);
     - poor PageSpeed (Google PageSpeed Insights API, free; optional key);
     - CMS/framework and version via open-source fingerprint rules, flagged as outdated;
     - stale copyright year;
     - broken homepage links;
     - no contact form;
     - no online booking/ordering where the category expects it;
     - incomplete Maps profile.
   - Produces an audit score and plain-language findings.
2. **`hiring`.** Config: role keywords/families (developer, QA, DevOps, data, IT support,
   digital marketing…), seniority, location or remote, posted-within-days.
   - Sources: public ATS job-board APIs (Greenhouse, Lever, Ashby, Workable, SmartRecruiters),
     company careers pages found via other lead types or search, Hacker News "Who is hiring"
     (Algolia HN API), and country job boards in packs only where terms allow.
   - Signal: role, count, posted date, URL. Several open tech roles = stronger signal.
3. **`funding_news`.** Config: event types (funding round, launch, expansion/new office,
   acquisition, digital transformation, leadership change), keywords, recency window.
   - Sources: Google News RSS queries, regional startup/business news feeds per country pack,
     press-release feeds.
   - Company names are extracted (rules, AI if enabled) and resolved to a domain/organization.
   - Signals carry published date and amount where stated.
4. **`decision_makers`.** An enrichment lead type that runs over orgs from any lead type.
   - Sources: website team/about/leadership pages, company-registry directors (via packs),
     search-indexed LinkedIn profile URLs, public GitHub org owners.
   - Outputs a Person with name, title, profile URL and source.
   - **Email pattern (optional, off by default):**
     - Only when ≥1 published personal email at the same domain reveals the pattern.
     - Applied to named people.
     - MX-record check only. **No SMTP probing.**
     - Its own `Inferred email (unverified)` column. Never merged into published emails, never
       used for dedup.

**Acceptance (each lead type):**
- Fixture-based tests for its sources, signals, columns and scoring.
- An isolation test (enable or disable it alone; other lead types' output unchanged).
- A live smoke test behind `@pytest.mark.live`.
- Workbook columns render.
- `docs/sources.md` entries dated.

---

## Phase 7 — Lead types, wave 2

1. **`tenders`.** Implemented per country pack: India (GeM, CPPP/eprocure), US (SAM.gov API),
   EU (TED API), UK (Find a Tender / Contracts Finder).
   - Config: keywords and/or classification codes (CPV etc.) for IT services, value range,
     closing-after date.
   - Output: buyer organization (`org_type: public_buyer`) plus a tender signal with title, value,
     deadline (`expires_at`), documents URL. Expired tenders drop out of intent scoring.
2. **`registrations`.** Newly incorporated companies.
   - Sources: UK Companies House API (free key; SIC codes + incorporation date), Indian company
     data where an official dataset is available, OpenCorporates (rate-limited) as a generic
     option.
   - Directors become Persons. Signal: incorporated_on, industry code.

**Acceptance:** as in Phase 6, plus a test that tenders/registrations packs stay inactive for
countries they don't cover.

---

## Phase 8 — Lead types, wave 3

1. **`tech_stack`.** Config: target technologies, optionally "outdated only" with version
   thresholds.
   - Candidates come from other lead types, uploaded domain lists, or search footprints
     (for example `"powered by Shopify" <city>`).
   - Each candidate is confirmed by fingerprinting the site itself.
   - Signal: technology, version, outdated flag, evidence.
2. **`app_store`.** Config: keywords/categories, country, rating ceiling, months since last update,
   install range.
   - Sources: Apple iTunes Search API (official) and public Google Play listing pages.
   - Signals: low rating, stale update, review complaints themes (AI if enabled).
   - The developer website resolves to an organization.

**Acceptance:** as in Phase 6.

---

## Definition of done (whole project)

- From the API or CLI, a Skillminds user can:
  1. describe a target, or pick a preset;
  2. adjust places, lead types, sources and fields;
  3. see an estimate, then preview;
  4. run within a time budget, locally (machine kept awake) or on the server;
  5. pause and resume;
  6. come back to the run in history;
  7. download a two-tab workbook matching the reference style;
  8. re-run next month and get only what's new.
- Configs and projects move between machines by bundle, or are shared via server mode.
- The Mumbai and Nashik runs still work as presets.
- No UI, no login, no outreach features. The API is complete enough that a frontend is pure presentation.
