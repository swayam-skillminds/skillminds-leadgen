# Skillminds Lead Generation

Finds businesses by category and place, in any country, from free public
sources. It merges what it learns into one deduplicated record per
organisation in a **project**, ranks them with explainable scores, and
exports the table to CSV and a Google Sheet.

**No paid APIs.** Everything here uses public pages, a headless browser, and
the free tier of the Google Sheets API.

It started as a Mumbai event-vendor scraper. That run and the **Nashik
high-end venues** run now ship as presets (`presets/`); see
[Nashik venue run](#nashik-venue-run) below. Being built into a backend
application: see `APP-BUILD-PROMPT.md` and `docs/phase-1-report.md`.

---

## What it collects

Per lead, whatever it can find of:

| | |
|---|---|
| Identity | business name, contact name, category, locality |
| Contact | phone(s), email(s), website, WhatsApp link |
| Social | Instagram handle, Facebook page, public LinkedIn URL |
| Location | full address, Google Maps listing URL |
| Signals | Maps rating + review count, WedMeGood rating + reviews + Featured status |
| Provenance | which sources found it, and the exact URLs |

## Sources

**Discovery** (run per locality × category):

| Source | Method | What it's best at |
|---|---|---|
| `google_maps` | Playwright | The backbone — rating, reviews, phone, website, address |
| `google_search` | requests, heavily paced | Vendor sites and listings Maps misses |
| `wedmegood` | Playwright | Ratings, review counts, Featured status |
| `weddingwire` | Playwright | Wedding vendors |
| `indiamart` | Playwright | Supplier side: catering, decor, sound/lighting |
| `justdial` | Playwright, very slow | Local listings |
| `sulekha` | Playwright, very slow | Local listings |
| `facebook` | SERP → `mbasic` fetch | Often the most complete contact block |
| `linkedin_serp` | `site:linkedin.com/in` search | Public profile URLs of named decision-makers |

**Enrichment** (run per discovered lead, after discovery):

| Enricher | What it does |
|---|---|
| `website` | Fetches the vendor's Contact/About pages for emails, phones, socials |
| `instagram` | Reads bio contact details for leads where a handle was found |

LinkedIn is never logged into or crawled — only public profile URLs that a
search engine has already indexed are recorded.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # deps live in pyproject.toml; add ,ai for the Anthropic provider (SDK)
playwright install chromium
```

### Google Sheets (optional but recommended)

The tool writes a CSV regardless; Sheets is the live, shareable view.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project (free).
2. **APIs & Services → Library** → enable **Google Sheets API** and **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account**. Any name.
4. Open the service account → **Keys → Add key → Create new key → JSON**. It downloads.
5. Save it as `credentials.json` in this folder, or point `.env` at it:
   ```bash
   cp .env.example .env
   # then edit GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/key.json
   ```
6. Create a Google Sheet, then **share it with the service account's email**
   (it looks like `something@project.iam.gserviceaccount.com`) as an **Editor**.
7. Put the sheet's ID in `config.yaml` under `sheets.spreadsheet_id`, or pass
   `--sheet-id` on the command line.

If step 6 is skipped, the tool will try to create its own spreadsheet — but a
service account's Drive isn't visible to you, so sharing an existing sheet is
the path that actually works.

---

## Usage

```bash
# See the plan and a time estimate (p50-p90) without making any requests
python run.py --dry-run

# Start small — one locality, one category, one source
python run.py --locality Andheri --category Caterers --source google_maps

# The best leads obtainable in a time budget (here 15 minutes, a fast skim)
python run.py --time-budget 15 --depth quick

# A ~2-minute sample (one area x one category x top sources) + full-run estimate
python run.py --preview

# A full run (long: budget 12+ hours at the default polite delays)
python run.py

# Discovery only, no per-lead enrichment
python run.py --no-enrich

# Enrich leads you already have
python run.py --enrich-only

# Re-rank and re-export the existing database, no scraping
python run.py --export-only
```

Useful flags: `--no-sheets`, `--no-headless` (watch the browser), `--fast`
(halves every delay — raises your odds of being blocked), `-v` for debug logs.

Run it repeatedly. The project's records persist between runs, so a second run
merges into the first rather than duplicating, and enrichment skips leads it
already went through (`--force-enrich` redoes them).

`Ctrl-C` **pauses**: the work in flight finishes, everything is flushed, and the
run can be resumed from where it stopped (`python run.py --resume <run id>`).
A second `Ctrl-C` stops at once; at most the units in flight are redone on resume.

### Time budget, depth, estimates

- `--time-budget MINUTES` returns the best leads obtainable in that window, not
  an arbitrary prefix: units with the best expected new-leads-per-second go
  first, a unit that can't finish in the time left is never started, and the
  last 90 s are reserved for flushing, scoring and export. The end-of-run report
  lists every skipped unit and why.
- `--depth quick | balanced | deep` scales per-unit thoroughness (results per
  query, scroll rounds, pages; quick also skips enrichment and AI name polish;
  deep adds Instagram). `balanced` is the config's own settings.
- Estimates (`--dry-run`, `python -m leadscraper estimate --config …`) give
  units per source, a p50–p90 range, what fits the budget and what would be
  skipped, and the AI cost. They start from built-in priors and learn from
  every unit you run, so they sharpen with use.
- Units run in parallel across hosts (`--workers`, default 3), never two on the
  same host; enrichment fetches up to 8 vendor sites at once
  (`runtime.enrich_workers`).

### Background runs: the worker

```bash
python run.py --config my-run.yaml --queue          # queue it
python -m leadscraper worker                        # executes queued runs (leave it running)
python -m leadscraper runs list                     # history and status
python -m leadscraper runs events 42 --follow       # live progress (ETA, units, failures)
python -m leadscraper runs pause 42                 # graceful; then: runs resume 42
python -m leadscraper runs cancel 42
python -m leadscraper worker stop                   # graceful; works on every OS
```

A worker that is killed (or a machine that reboots) loses at most the units
that were in flight: restart the worker and it resumes the run from its last
checkpoint.

While a run is active the machine is kept awake (`wakepy`; set
`LEADSCRAPER_KEEP_AWAKE=0` or `worker --no-keep-awake` to turn it off). The
honest limits:

- A sleeping machine runs nothing. The lock only prevents *idle* sleep.
- On macOS, closing the lid on battery forces sleep regardless; so does
  choosing Sleep from the menu, on every OS.
- If the machine sleeps anyway, the run notices on wake, re-validates its
  browsers and connections, doesn't count the gap against its time budget,
  and carries on.
- For truly unattended runs use server mode (an always-on machine): `docker compose up -d`
  (`docs/deploy.md`).

### The workbook

Every run ends with a workbook built from the database: exactly two tabs.

- **Records**: one row per organisation, best first, grouped by place when the run covers several places (`output.sort`), with clickable links, phones as text, and colour scales on the scores.
- **Overview**: what the run did, what it found (new vs updated, breakdowns, the top 10), how complete the data is, and what each column means. When nothing was found, it says why.

It lands next to the run's CSV (`runtime.csv_export`'s folder) as `<project>-run<N>.xlsx`. You can regenerate it at any time without re-scraping:

```bash
python run.py --export-only                          # the whole project's workbook
python -m leadscraper export --run 43                # one run's (also --format csv|json)
```

`output.excel` / `output.json` / `output.csv` in a run config choose the files (the presets keep the legacy CSV). Samples built from fictional data are in `docs/samples/`.

### The HTTP API, server mode, history

```bash
python -m leadscraper api                            # http://127.0.0.1:8000/api/v1/docs
docker compose up -d                                 # API + worker on an always-on machine
python -m leadscraper status                          # health, active and queued runs
python -m leadscraper runs diff 43                   # new / updated / unchanged vs the previous run
python -m leadscraper export --run 43 --scope new    # only what is new, as a workbook
python -m leadscraper bundle export -o laptop.zip --leads --history   # move to another machine
```

Everything the API offers -- saved and versioned configs, estimates, previews,
runs with live progress (Server-Sent Events), organisations with provenance,
exports, suppression lists, schedules, notifications, settings, bundles -- is
listed in `docs/api.md`. The CLI covers the same flows.

### Projects, presets and run configs

A **run config** (`leadscraper/config/schema.py`, JSON Schema via
`python -m leadscraper config schema`) says where to look, what to look for,
which sources to use and how to score. A country, a place and one category
are enough:

```yaml
schema_version: 1
name: Austin dentists
project: austin-dentists
geography: {country: US, places: [Austin]}
lead_types:
  local_business:
    categories: [{name: Dentists}]
```

```bash
python run.py --config my-run.yaml --dry-run          # plan; no requests
python run.py --config presets/nashik-venues.yaml     # a shipped preset
python -m leadscraper presets                         # list presets
python -m leadscraper config validate my-run.yaml
python -m leadscraper db info                         # projects and sizes
```

Each run merges into its **project**, which lives in the application database:
SQLite under your user data directory (WAL mode), or Postgres via
`LEADSCRAPER_DB_URL`. `LEADSCRAPER_DATA_DIR` moves the whole data directory.
The country drives phone parsing, currency, locale and which **packs** of
sources apply. For example, Justdial, Sulekha and IndiaMART run only in India.

The old `config.yaml` / `config.nashik.yaml` still work: they run as the
presets they import to. Their old SQLite stores are imported once, from a
copy (the original is never modified):

```bash
python -m leadscraper migrate-legacy --config config.yaml          # -> "Mumbai event vendors"
python -m leadscraper migrate-legacy --config config.nashik.yaml   # -> "Nashik venues"
```

---

## How ranking works

Each lead gets a 0–100 score from four weighted components (tune the weights
in `config.yaml` under `scoring:`):

| Component | Default weight | What it measures |
|---|---|---|
| `maps_popularity` | 0.40 | Google rating × log-scaled review volume |
| `source_corroboration` | 0.25 | How many independent sources listed the business |
| `directory_signal` | 0.20 | WedMeGood rating, review count, Featured badge |
| `contact_completeness` | 0.15 | How reachable the lead actually is |

Review counts are log-scaled deliberately: the difference between 5 and 50
reviews says much more than the difference between 500 and 550.

Output is sorted by **locality → category → score descending**, so the most
credible vendors in each group surface first. Low-signal listings are kept,
never discarded — they just rank lower.

Changing the weights doesn't require a re-scrape: `python run.py --export-only`
re-scores everything already in the database.

---

## How deduplication works

In priority order:

1. **Shared phone number** — conclusive, merges even across localities (a
   caterer listed in both Andheri and Dadar is one business).
2. **Same Google Maps place** — Maps returns one place to searches run from
   neighbouring localities; the place id (cid) in its URL identifies it.
   Applied when the phones don't contradict.
3. **Identical canonical name** — after stripping legal and category noise, so
   "Sharma Caterers Pvt. Ltd." matches "Sharma Catering Services". Applied only
   when the phones don't contradict and the locality agrees.
4. **Fuzzy name ≥ 92%** within the same locality.
5. **Venue runs:** pins within 250 m with loosely similar names.
6. **Same website domain** (registrable domain; social sites, link-in-bio
   pages, site builders' shared hosts, marketplaces and free email domains
   never count). Applied when phones don't contradict and names are similar.
   Off for venue runs, where an operator's villas share its domain.

A **registry ID** (company number, GSTIN), when a source provides one, comes
before all of these. The rules live in `leadscraper/core/identity.py`.

On a match, fields are **merged into the existing row**: lists are unioned,
empty fields filled, and conflicting ratings resolved in favour of whichever
is backed by more reviews. Nothing is overwritten with a blank. When
enrichment (or an AI rename) gives a record a phone, place or name that
another record has, the two are **consolidated** into one.

---

## Reliability

- Per-host rate limiting with randomised delays, shared across sources (three
  sources hitting `google.com` share one budget).
- Escalating penalties after a 429/403/CAPTCHA, decaying after clean responses.
- Exponential backoff with jitter on transient failures.
- Google Search escalates through three tiers — a cheap `requests` fetch, then
  the headless browser, then DuckDuckGo — so a CAPTCHA costs you a query rather
  than the run.
- DuckDuckGo answers `HTTP 202` with an empty page when it wants you to slow
  down. That's detected as throttling and backed off, not misread as
  "no results".
- `site:` search for directory listings is **opt-in** (`serp_fallback: true`
  per source). Left on, one query per locality × category earned a Google
  CAPTCHA within minutes of every run start and took every SERP-backed source
  down with it.
- **Nothing crashes the run.** Every failure — a blocked host, a login wall, a
  listing whose phone number is hidden behind a click — is written to
  `logs/failures.jsonl` and `data/skipped.csv`, and to a `Skipped` tab in the
  Sheet, for manual follow-up.

The end-of-run report shows what was found, what was skipped, and the top 10
leads by score.

---

## Tuning

Everything lives in `config.yaml`: localities and their search aliases,
categories and their query phrases, per-source delays and page limits, scoring
weights, and output paths. Add a locality or a category there and it flows
through every source automatically.

To go faster, raise the delays' floor at your own risk — the defaults are set
where they are because Justdial, Instagram and Google SERP all block quickly.

---

## Tests

```bash
python -m pytest tests/ -q
```

Covers normalisation (phones, emails, names, ratings) in several countries,
merge semantics and the dedup rules, scoring, the run-config schema and the
legacy importer, the database (migrations, Postgres DDL, provenance, the
legacy-store import), packs, the pipeline's failure handling, and the AI
layer. Everything runs offline against stubs and fixtures in about 12 seconds.
`tests/characterisation/` replays the frozen Mumbai and Nashik stores through
the migration and checks that every score, tier, row and dedup decision is
unchanged.

---

## Optional AI layer

Off by default, opt-in feature by feature, and capped in dollars per run. AI
only ever **adds** information, and never a contact detail the fetched page
doesn't carry: every AI-supplied phone, email or address must appear in the
page text, or it is dropped. A run whose AI fails ends exactly where the same
run without AI ends. The full reference is **[docs/ai.md](docs/ai.md)**.

### Providers

Anthropic (official SDK), OpenAI, Google Gemini, any OpenAI-compatible endpoint
(OpenRouter, Groq, a local Ollama or LM Studio), and the `claude` CLI on your
Claude Code subscription. Each has a cheap **worker** model and a stronger
**judge** model; model ids and prices are data (`leadscraper/ai/defaults.yaml`),
overridable per workspace or per run. List several and they form a **fallback
chain**: one that is out of credit is dropped for the run, and when none is
left the run carries on without AI.

```bash
python -m leadscraper secrets set anthropic-api-key     # keys live in the OS keychain (or env / secret files)
python -m leadscraper ai providers                      # which are usable; keys shown only as yes/no
python -m leadscraper ai test anthropic                 # one tiny connectivity call
```

**Spending money is an explicit choice.** A provider that bills separately is
used only when your config or workspace names it, and never while
`ai.allow_billed_api` is `false` (the Mumbai and Nashik presets keep the
`claude` CLI on your subscription, as before). A stray `ANTHROPIC_API_KEY` in
the environment changes nothing. On the subscription the `$` figures are
list-price equivalents that count against the cap, not an invoice.

### What it does

| Feature | |
|---|---|
| `icp_builder` | "We sell ERP to manufacturers in Pune" → a draft run config, from Skillminds' offerings; returned for you to review, never run (`leadscraper ai icp "..." -o draft.yaml`) |
| `planner` | Areas, search synonyms and title variants for a config; suggestions only |
| `source_discovery` | Proposes extra directories as candidate sources; the ones you approve are read by a generic extractor with self-healing selectors (AI only when a layout is new or changed) |
| `web_search_discovery` | The provider's web search as a discovery source; each business is kept only with a page we fetched ourselves |
| `extraction_fallback` (`extract`) | Reads contacts off pages whose selectors found nothing |
| `normalisation` (`names` ...) | Clean names, industry tags, a company-size estimate (labelled), duplicate adjudication |
| `venue_classification` (`venues`) | Venue types the rules could not settle |
| `lead_summary` | One factual line per organisation; never outreach copy |

An empty `ai.features` means `extract`, `names`, `venues`: what the original
runs used. AI attributes show in the workbook as **Summary (AI)**, **Industry
(AI)** and **Company Size (AI estimate)**.

### Usage

```bash
python run.py --ai                          # enable for this run
python run.py --ai --ai-budget 2.00         # hard USD cap
python run.py --ai --ai-feature extract     # just one feature
python run.py --no-ai                       # force off
python run.py --ai-unlimited                # no cap: until the provider's own quota stops it
```

Or set `ai:` in a run config (`enabled`, `providers`, `features`, `budget_usd`).

### Safeguards

- **Hard budget.** A call that would cross `budget_usd` is not made: AI stops
  mid-run and the run continues. `--ai-budget 0` really means zero. The
  estimate (`--dry-run`, `POST /estimate`) shows the expected AI cost per feature.
- **Cached and ledgered.** Answers are cached (`<data dir>/cache/ai_cache.sqlite3`),
  so a re-run never pays twice; spend is recorded per run, provider and feature
  (`GET /api/v1/ai/usage`).
- **Validated.** Every answer must match a schema; invalid output is retried
  once, then skipped.
- **Never fatal.** Errors, refusals and exhausted quotas are failure records
  under source `ai`; a circuit breaker stops after 4 consecutive failures.
- **Blocked models.** Fable and Mythos families (billed as extra credits) are
  never selected, whatever a config says.

---

## Known behaviour of the sources

Worth knowing before your first run, because these look like bugs and aren't:

- **Google serves a JavaScript-only shell to plain HTTP clients.** The
  `requests` tier of `google_search` will usually return nothing and escalate
  to the browser automatically. That's expected.
- **Google may CAPTCHA your IP** (`/sorry/index`) for `site:` and quoted
  queries, especially from a datacentre or VPN address. When that happens the
  run continues on DuckDuckGo. If you see it constantly, raise
  `sources.google_search.min_delay` or run from a residential connection.
- **Instagram and Facebook often require a login.** Those leads are logged as
  `login_wall` skips with the profile URL recorded, so you can open them by
  hand. Contact details are never guessed at.
- **Justdial hides phone numbers behind a "Show Number" click** and has
  historically obfuscated them with CSS sprites. Both are handled, but when a
  number can't be recovered the listing is logged as `phone_hidden` rather than
  dropped.
- **Search results are full of category pages**, not businesses. Titles like
  "Event Photographer in Mumbai" are rejected and logged as
  `generic_or_unparseable_title` — check `data/skipped.csv` if you think
  something real was filtered out.

---

## Nashik venue run

```bash
python run.py --config config.nashik.yaml --dry-run      # plan + time estimate
python run.py --config config.nashik.yaml                # full run (several hours)
python run.py --config config.nashik.yaml --source stayvista --source saffronstays --no-enrich
python run.py --config config.nashik.yaml --export-only  # re-tier / re-sort, no scraping
```

Venues only, across Nashik district plus Bhandardara, Saputara and Jawhar /
Mokhada. Everything that holds state is separate from the Mumbai run: its own
project ("Nashik venues"; the pre-Phase-1 store in `data/nashik/` is imported
once with `migrate-legacy`), CSV and logs (`data/nashik/`, `logs/nashik/`),
and **two tabs of its own —
`Nashik Venues` and `Nashik Skipped` — in the same spreadsheet**. The Mumbai
`Leads` / `Skipped` tabs are never written.

**Sources.** Google Maps (backbone), Google Search, WedMeGood venues,
Justdial, Facebook, LinkedIn, website + Instagram enrichment, and the stay
platforms:

| Platform | How | Runs |
|---|---|---|
| StayVista, SaffronStays, Elivaas | their sitemaps → every property page in the region; page data for rooms, guests, rating, pin, amenities, event policy; rendered once for the nightly price where it isn't in the HTML | once per run |
| Airbnb, Booking.com | rendered search pages for the places in `platform_searches` | once per locality |

MakeMyTrip refuses automated connections outright, and Lohono / LuxUnlock
had no Nashik-area properties at the time of writing — search hits on them
are still recorded as platform listings. Airbnb and Booking do not expose
owners, so their venues gain contacts only when they dedup with a Maps or
website record.

**One row per venue**, classified into a *Venue Type* (Private Pool Villa,
Villa / Bungalow, Farmhouse / Farm Stay, Vineyard / Winery Resort, Resort,
Glamping / Camp, Heritage Property, Premium Hotel, Hotel, Banquet / Event Lawn,
Boutique Stay / Homestay) by ordered rules, with the AI layer (`venues`
feature) deciding the cases the rules can't. Each operator — StayVista,
SaffronStays, Elivaas, EKOSTAY … — also gets one **Brand** row summarising its
regional portfolio, and every property it runs names it in *Managed By*.

**Tiers describe the venue, not the lead:**

| Tier | Rule |
|---|---|
| Operator | a brand / operator row |
| Luxury | qualifies **and** ≥ ₹25,000/night (or ≥ ₹2,000/plate, or enough premium points) |
| Premium | meets **any one** of: premium platform/operator; rated ≥ 4.3 over ≥ 20 reviews; private pool or ≥ 3 bedrooms; premium hotel or winery resort |
| Standard | evidence, but no criterion met — or a known price under ₹4,000/night, or a budget operator (OYO, Treebo …) |
| Unverified | almost nothing known yet |

Price is never an inclusion criterion; it is the heaviest part of the
*premium signal* (40% of the score), which ranks venues within a tier. All
thresholds live under `venue_tiers:` in the config, and changing them needs
only `--export-only`, not a re-scrape. *Tier Reasons* in each row says why.

**Columns** include price from/to and basis (per night / per plate), price
source, bedrooms, bathrooms, stay and event capacity, hotel stars, pool, lawn,
event-/pet-friendly, amenities, best rating and total reviews across every
source, per-platform ratings, platforms and listing URLs, contacts, pin and
distance from the city, check-in/out, description and a photo URL.

**Dedup differences.** A shared phone only merges records whose names are also
similar (one reservations line fronts dozens of villas); records pinned
within 250 m merge on loosely similar names; distinctive identical names
merge across localities. Localities come from place names in the name, URL or
address, overridden by the pin when the two plainly disagree.

---

## A note on scope

Scraped pages change. When a site redesigns, its scraper will start returning
nothing — that shows up as `no_businesses_parsed` or `no_listings` in
`logs/failures.jsonl` rather than as a crash. The selectors to adjust are at
the bottom of each file in `leadscraper/sources/`.

Respect the sites you're scraping: keep the default delays, don't run several
copies in parallel, and check each site's terms before using this commercially.
