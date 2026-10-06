# Rebuild Prompt — Lead Generator Application

> Paste this whole file as the prompt for a fresh Claude Code session in this repo.

---

## Context

You are working in an existing, working Python codebase (~7,100 LOC) at the repo root. It is a
Mumbai event-industry lead scraper: Playwright + requests, per-host rate limiting, SQLite store,
legitimacy scoring, Google Sheets export, 815 lines of passing tests.

**Do not rewrite from scratch. Refactor.** The core is good — the merge/dedup semantics in
`models.py`, the per-host `RateLimiter`, the failure-tolerant pipeline design, and the scoring
model are all worth keeping. The problems are specific and listed below.

Read `README.md`, `config.yaml`, `leadscraper/pipeline.py`, `leadscraper/sources/__init__.py`
and `leadscraper/sources/directories.py` before starting.

---

## Goal

Turn this into a **local web application** that generates B2B leads for **any city and any
business category**, finishing within a **user-specified time budget**.

### Hard scope boundary — read this twice

This tool **generates leads. It does not contact them.**

Do **not** add, and actively reject if tempted: contact/outreach status fields, CRM features,
email sending, call logging, sequences, templates, reply tracking, "contacted on" timestamps,
pipeline stages, or anything resembling sales workflow. The deliverable ends when a ranked,
deduplicated, exportable lead list exists. Anything past that is out of scope.

---

## Part 0 — Verified bugs. Fix these first; they are the difference between 1,300 leads and 5,000

Each of these was confirmed against the live sites and the run logs. Do not re-litigate them,
but **do re-verify the URLs before coding**, since they may have changed again.

### 0.1 — Four sources have never executed

Every run in `logs/` starts with:

```
run start: 9 localities x 8 categories = 72 combinations;
sources: google_maps, wedmegood, weddingwire, shaadisaga, indiamart, sulekha
```

`google_search`, `justdial`, `facebook`, `linkedin_serp` are absent — all four are
`enabled: true` in config, and there are zero `"disabled in config"` lines in any log. They were
excluded by the explicit-request branch in `pipeline.py:117`, which skips **silently**:

```python
if name not in wanted:
    continue          # no log line, no trace
```

**Fix:** make that branch log at INFO. A source that doesn't run must never be invisible.
~900 lines (`google_search.py` 417, `local_directories.py` 263, `facebook.py` 152,
`linkedin_serp.py` 97) have never run in production — expect them to be broken and budget for it.

### 0.2 — WedMeGood category slugs are wrong (4 of 8 → 404)

`listing_template` is `https://www.wedmegood.com/vendors/mumbai/{slug}/?page={page}`.
Verified against live site:

| Config category | Current slug | Status | Correct slug |
|---|---|---|---|
| Venues | `wedding-venues` | 200 OK | — |
| Caterers | `caterers` | **404** | `wedding-catering` |
| Decorators | `wedding-decorators` | 200 OK | — |
| Photographers | `photographers` | **404** | `wedding-photographers` |
| DJs / Sound & Lighting | `dj` | **404** | `djs` |
| Event Planners | `wedding-planners` | 200 OK | — |
| Florists | `florist` | **404** | no such category — set null |
| Entertainment / Performers | `wedding-entertainment` | 200 OK | — |

Full live slug list for `/vendors/mumbai/`: `bartenders-wedding, beauty-and-wellness,
bridal-makeup, bridal-wear, djs, family-makeup, groom-wear, mehendi-artists, pre-wedding-shoot,
pre-wedding-shoot-locations, sangeet-choreographers, trousseau-packers, wedding-accessories,
wedding-cakes, wedding-cards, wedding-catering, wedding-decorators, wedding-entertainment,
wedding-favors, wedding-jewellery, wedding-pandits-priests, wedding-photographers,
wedding-planners, wedding-venues`.

Pagination via `?page=N` works. **Scrape this list at runtime rather than hardcoding it** —
that's what broke.

### 0.3 — Listing URLs: live status of every directory

| Source | URL pattern in code | Live result |
|---|---|---|
| `wedmegood` | `/vendors/mumbai/{slug}/?page={page}` | 404 (wrong slug, see 0.2) |
| `weddingwire` | `/{slug}/mumbai?page={page}` | **403** — bot wall, needs real browser |
| `shaadisaga` | `/{slug}/mumbai?page={page}` | **no connection** — site is gone |
| `sulekha` | `/{query}/mumbai` | **200, 701 KB** — works |
| `justdial` | `/Mumbai/{query}-in-{locality}` | **200 but 14-byte body** — blocked |

`www.shaadisaga.com` now resolves to `www.weddingbazaar.com.edgekey.net` — it was absorbed into
WeddingBazaar. **Delete the ShaadiSaga source entirely.**

### 0.4 — The SERP fallback is a trap, not a feature

`directories.py:_collect_profile_urls` tries the listing page first and falls back to
`site:` search. Because the listing URLs above are broken, *everything* fell through to search —
and Google serves `/sorry/index` CAPTCHA within ~120 seconds of run start, every run, after
which the DuckDuckGo fallback returns HTTP 202 (throttle) with escalating penalties.

The result: 181 × `no_listings`, 57 × `blocked`, and the whole system degraded to a Google Maps
scraper (1,260 of 1,333 leads).

**Fix the listing paths. Do not add a paid SERP API to paper over this.** Brave Search API is no
longer free ($5/1k requests, card required, $5/month credit ≈ 1,000 queries). If a SERP is
genuinely needed after the listing paths work, use **self-hosted SearXNG in Docker** — free,
aggregates and rotates across engines, which is exactly what the DuckDuckGo fallback fails at.

### 0.5 — Dead domains are retried three times each

`logs/enrich.log` is full of `NameResolutionError` retried 3× with exponential backoff:
`deyzevents.com`, `annapurnacaterers.in`, `khushi-events.grexa.site`, `maheshcaterers.co.in`,
dozens more. A DNS failure is **deterministic** — retrying costs ~15s per domain for nothing.
One site (`expertevents.in`) resolves to `127.0.0.0` and burns two 30-second connect timeouts.

**Fix:** treat NXDOMAIN / DNS failure / loopback-resolution as terminal. Never retry. Persist a
`dead_domains` table in SQLite and skip those hosts on all future runs.

### 0.6 — Data hygiene

Against the current 1,333-lead DB:

- **6 leads are named `"access denied"`** — a scraper error string stored as a business name,
  which then survived dedup. Add a rejection list for error-page titles (`access denied`,
  `403 forbidden`, `just a moment`, `attention required`, `are you a robot`, etc.).
- **86 names are SEO page titles**, e.g. `"Vaviya Events – Event Management Company in Mumbai |
  Corporate Event Planner in Andheri | Event Organisers in Western Suburbs"`. The AI `names`
  feature in `ai.py` does exactly this job and is **switched off**. Wire it into the default
  pipeline for SERP-derived names.
- **73 rows are exact-name duplicates** that survived dedup (30 distinct names).
- **`contact_name` is 0/1,333.** `linkedin_serp.py` exists to fill it and has never run (see 0.1).
- `emails` is 13.7%; `facebook.py`'s `mbasic` path is often the richest contact block for Indian
  SMBs and has also never run.

### 0.7 — Scoring has a dead component

`source_corroboration` is weighted 0.25, but 1,094 of 1,333 leads have exactly one source, so a
quarter of the score is near-constant. **Do not retune the weights** — it self-corrects once the
other sources actually run. Just confirm afterwards that corroboration spread has widened.

---

## Part 1 — Generalize to any city, any category

Today `city: "Mumbai"` is config, but `mumbai` is hardcoded into every directory
`listing_template`, and the categories are wedding-specific.

### 1.1 — Core vs. packs

Restructure sources into two tiers:

**Core (always available, works anywhere):**
- `google_maps` — the engine. Handles any city × any category natively. Already your best source.
- `website` — enrichment, domain-agnostic.
- `serp` — generic discovery, off by default (see 0.4).

**Packs (conditionally activated):**
- `pack: india` — `justdial`, `sulekha`, `indiamart`. Active when country is India.
- `pack: weddings` — `wedmegood`, `weddingwire`. Active only when the chosen categories overlap
  the wedding/event vertical.
- `pack: social` — `facebook`, `instagram`, `linkedin_serp`. Region-agnostic.

Each pack declares, in its own file:

```python
applicable_countries: set[str] | None   # None = anywhere
applicable_verticals: set[str] | None   # None = any
category_slug_map: dict[str, str]       # canonical category -> site slug
city_slug: Callable[[str], str]         # "Navi Mumbai" -> "navi-mumbai"
```

A run for *"dentists in Pune"* activates core + india + social. A run for *"caterers in Mumbai"*
additionally activates weddings. **Packs that don't apply must be silently inactive, not failing.**

### 1.2 — Category input

The user types free-text categories (`"dentists"`, `"gyms"`, `"banquet halls"`). For each:

1. Feed it straight to Google Maps as the search term — this always works.
2. Resolve it against each active pack's `category_slug_map` via exact match, then fuzzy
   (`rapidfuzz`, already a dependency), then give up and skip that pack for that category.
3. Ship a `presets/` directory with ready-made category bundles (`events-india.yaml`,
   `hospitality.yaml`, `healthcare.yaml`) so common cases are one click.

### 1.3 — Geography input

- `country` drives phone normalization region (currently `+91`/IN is hardcoded in
  `normalize.py` — make it a parameter, default IN) and pack activation.
- `city` is free text.
- `areas` is an optional list of sub-localities with aliases. Empty means city-wide.
- Derive `city_slug` per pack rather than hardcoding `mumbai` in templates.

---

## Part 2 — Speed

Current state: a full run is 12+ hours, fully serial. Enrichment alone ran **5+ hours** for
~1,300 leads.

**The key enabling fact: `RateLimiter` is already per-host.** Running N workers against N
*different* domains is safe today and requires no redesign of the limiter. Parallelize **across**
hosts, never within one.

1. **Concurrent enrichment** — thread pool, default 8 workers, configurable. Each task acquires
   its host's rate-limit slot. This alone should take enrichment from 5 hours to under 20 minutes.
2. **Browser context pool** — replace the single Playwright page with a pool of contexts (default
   3, configurable). Maps scraping for different area×category pairs runs in parallel.
3. **Dead-domain cache** (see 0.5) — persistent, checked before any fetch.
4. **Connection reuse** — a `requests.Session` per host with a pooled adapter.
5. **Skip already-enriched leads** on re-runs unless `--force` / a UI toggle says otherwise.

Target: **≥10× faster enrichment**, ≥3× faster discovery, with no increase in block rate. Measure
and report both before/after.

---

## Part 3 — The time-budget scheduler (the most important new component)

The user gives a wall-clock budget: 15 minutes, 2 hours, overnight. The tool must return **the
best leads obtainable in that window** — not an arbitrary truncated prefix of the work.

### 3.1 — Work units and priority

A work unit is `(source, area, category)`. Maintain a `unit_stats` table in SQLite recording, per
unit type, the observed **duration** and **yield** (new leads) of past executions.

```
priority = expected_new_leads / expected_seconds
```

- **Cold start:** static priors, ordered `google_maps` > directories > serp. Maps produced 95% of
  leads historically; it must always run first.
- **Warm:** learned per-unit stats, so the scheduler adapts to which areas and categories are
  actually productive in this user's configuration.
- Re-sort the queue after every completed unit.

### 3.2 — Deadline behaviour

- Never start a unit whose expected duration exceeds the remaining budget; take the next one that
  fits.
- **Reserve a hard tail** (default 90s) for final flush, scoring, and export. Never spend it.
- Checkpoint to SQLite after every unit — a killed process must lose at most one unit.
- On deadline: stop cleanly, finish in-flight work, flush, score, export, and report what was
  skipped and why.
- Existing Ctrl-C flush semantics must keep working and must share this code path.

### 3.3 — Depth presets

A separate axis from time budget. Scales per-unit thoroughness:

| Preset | max_results/query | scroll_rounds | pages | enrichment | AI names |
|---|---|---|---|---|---|
| Quick | 10 | 3 | 1 | off | off |
| Balanced *(default)* | 25 | 8 | 2 | on | on |
| Deep | 60 | 20 | 5 | on + Instagram | on |

Quick × 15 minutes = a fast wide skim. Deep × overnight = the exhaustive run. Both must work.

### 3.4 — Estimation up front

Before the run starts, show the user: units queued, estimated total time at the chosen depth, and
what will realistically be reached inside the budget. Extend the existing `--dry-run` logic.

---

## Part 4 — The web application

FastAPI backend, browser UI on `localhost`. **No npm, no build step** — vanilla JS or HTMX served
from `static/`, so a teammate needs only Python.

### 4.1 — Endpoints

```
GET  /                          UI
POST /api/runs                  start a run; body = the settings object; returns run_id
GET  /api/runs                  list past runs with summary stats
GET  /api/runs/{id}/stream      Server-Sent Events: progress, ETA, counts, current unit, log tail
GET  /api/runs/{id}/leads       paginated, sortable, filterable JSON
POST /api/runs/{id}/stop        graceful stop — flush and export, do not kill
GET  /api/runs/{id}/export.xlsx reuse tools/build_leads_workbook.py
GET  /api/runs/{id}/export.csv
POST /api/runs/{id}/sheets      push to Google Sheets
GET  /api/presets               category bundles
GET  /api/health                dependency check: Playwright, Chromium, creds, disk
```

### 4.2 — Settings form

Country · City · Areas (add/remove free text, optional) · Categories (free text + preset
bundles) · **Time budget** (slider: 15m / 1h / 4h / overnight / custom) · **Depth** (Quick /
Balanced / Deep) · Sources (auto-selected by pack, individually overridable) · Output targets
(SQLite always, plus xlsx / csv / Sheets) · Advanced (worker count, browser contexts, headless,
AI on/off + budget).

Settings must be saveable as named profiles and reloadable.

### 4.3 — Live run view

Progress bar with **units completed / total**, elapsed and remaining, running lead count, current
unit, live-updating top-10 by score, skip counter linking to reasons, and a tailing log pane.
A visible **Stop** button that triggers the graceful path in 3.2.

### 4.4 — Results view

Sortable/filterable table: score, name, category, area, phone, email, website, socials, rating,
reviews, source count. Filter by "has phone", "has email", minimum score, category, area. Export
respects the active filters.

---

## Part 5 — Team packaging

Each teammate runs it on their own machine.

- `setup.sh` — venv, deps, `playwright install chromium`, `.env` scaffold, health check. One command.
- `.env.example` covering credentials paths and optional keys. **Never commit `credentials.json`.**
- **Immediate action:** `credentials.json` currently sits in the repo root as a live Google
  service-account private key for project `eighth-physics-491407-c6`. `.gitignore` covers it but
  this is not a git repo, so nothing enforces that. Move it out of the repo, document the path in
  `.env`, and tell the user to rotate the key.
- Conservative defaults so nobody gets their IP blocked. Warn loudly if delays are lowered.
- Detect and warn when a second instance is already running against the same database.

---

## Part 6 — Tests

Keep the existing 815 lines green throughout. Add:

- Scheduler: deadline is never exceeded; tail reserve is never consumed; priority ordering is
  correct; a killed run resumes losing at most one unit.
- Pack activation: correct packs for `(country, categories)`; inapplicable packs are inactive,
  not erroring.
- Dead-domain cache: NXDOMAIN recorded once, never retried.
- Concurrency: the store stays consistent under parallel writes; dedup still holds.
- Error-title rejection: `"access denied"` and friends never become a business name.
- API: each endpoint, plus SSE emitting progress events.

Everything must run offline with stubs, as the current suite does.

---

## Acceptance criteria

1. `./setup.sh && python -m app` opens a working UI on localhost.
2. A run for **"dentists in Pune"** returns real leads — proving genuine generalization.
3. A **15-minute** budget returns leads and terminates within 15 minutes ±30 seconds.
4. An **overnight** budget runs to completion and stops cleanly.
5. Enrichment throughput improves ≥10× versus the current serial baseline, measured and reported.
6. WedMeGood yields materially more than its current 13 leads for the same Mumbai run.
7. `contact_name` coverage is greater than zero.
8. No lead is ever named `"access denied"`.
9. Full test suite green.
10. **No outreach, contact-tracking, or CRM functionality exists anywhere in the codebase.**

---

## Working method

Work in phases and **stop for review after each**: Part 0 → Part 2 → Part 1 → Part 3 → Part 4 →
Part 5. Part 0 is highest value per hour and unblocks measurement of everything else. Do not
start the web app until the engine is fixed and fast — a UI over a broken scraper just hides the
problem behind a progress bar.

After Part 0, re-run a single locality (`--locality Andheri`) and report the before/after lead
counts per source, so the gains are measured rather than assumed.
