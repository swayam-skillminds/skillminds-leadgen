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
pip install -e ".[dev]"      # deps live in pyproject.toml; add ,ai for the Anthropic SDK backend
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
# See the plan and a time estimate without making any requests
python run.py --dry-run

# Start small — one locality, one category, one source
python run.py --locality Andheri --category Caterers --source google_maps

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
merges into the first rather than duplicating, and `Ctrl-C` flushes everything
gathered so far before exiting.

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

Off by default. When on, AI only ever **adds** information — it never deletes a
lead, and everything it returns is re-validated by the same normalisers that
scraped data goes through, so a malformed or invented phone number is rejected
before it reaches the sheet.

### Auth and billing: subscription only, by default

| Backend | Auth | Billing |
|---|---|---|
| `claude_cli` **(default)** | Your Claude Code subscription, via `claude --print` | **Draws on your existing subscription allowance. No separate bill.** |
| `anthropic_api` | `ANTHROPIC_API_KEY` | Pay-as-you-go API billing, **separate** from the subscription |

The default is pinned to `claude_cli`, and `ai.allow_billed_api` is `false`.
While that flag is off, a backend that bills separately is **never selected** —
not by `auto`, not even if you name it explicitly, and not if an
`ANTHROPIC_API_KEY` happens to be sitting in your environment. Spending money
has to be a deliberate choice, so an accidental key can't quietly start
charging you. There are tests for exactly this.

To opt in to per-call API billing, set both `backend: "anthropic_api"` and
`allow_billed_api: true`.

**About the dollar figures.** On the subscription backend the `$` amounts in
the run report are list-price *equivalents* that the CLI reports for the tokens
used — they are an accounting figure, not an invoice. What you actually consume
is your subscription allowance, the same bucket your interactive Claude Code
sessions draw from. The practical limit is therefore your plan's usage limits,
not a credit-card charge: a very large run can use up enough of your allowance
to slow your own interactive sessions. `max_cost_usd` doubles as the throttle
for that — it caps token spend regardless of which backend is in use.

Each CLI invocation spawns a process, which is why records are sent in batches
of 12 rather than one at a time; that's what makes it economic.

### What it does

| Feature | When it runs | What it fixes |
|---|---|---|
| `extract` | A page fetched fine but the selectors/regexes found nothing | Reads the contacts straight out of the page text. This is the answer to scraper brittleness — a site redesign stops costing you that lead. Failure-triggered, so volume is naturally low. |
| `names` | After discovery, on leads whose names came from search-result titles | Turns `"Studio Fine Arts \| Best Wedding Photographer in Powai Mumbai"` into `"Studio Fine Arts"`, and flags titles like `"Event Photographer in Mumbai"` as category pages rather than businesses. |
| `dedup` | Ambiguous duplicate pairs (opt-in, not on by default) | Adjudicates cases the phone/fuzzy-name rules can't settle. |

### Usage

```bash
python run.py --ai                          # enable for this run
python run.py --ai --ai-budget 2.00         # hard USD cap
python run.py --ai --ai-feature extract     # just one feature
python run.py --no-ai                       # force off
```

Or set `ai.enabled: true` in `config.yaml` to make it the default.

### Running until the allowance runs out

```bash
python run.py --ai-unlimited
```

No usage cap. AI runs until your Claude Code subscription allowance is
exhausted, at which point the scraper **detects it, switches itself to non-AI
mode, and finishes the run normally**. You get a line in the summary saying
when and why AI stopped, and the leads gathered before and after are all in the
same output. Losing AI degrades the run; it never ends it.

A circuit breaker does the same after `failure_limit` consecutive AI failures
(default 4), so a broken backend can't spend hours spawning dead processes.

### Model families that are blocked

Fable and Mythos models bill as **external credits** rather than drawing on a
subscription allowance, so they are refused in code — `safe_model()` swaps any
`claude-fable-*` / `claude-mythos-*` id for the default worker model and logs
it, no matter what `config.yaml` says. Bulk work uses `claude-haiku-4-5`;
duplicate adjudication uses `claude-opus-5`.

### Safeguards

- **Hard budget.** `max_cost_usd` (default $5) disables the layer once hit —
  mid-run, not at the end. `--ai-budget 0` really does mean zero.
- **Cached.** Responses are content-addressed in `data/ai_cache.sqlite3`, so a
  re-run never pays for the same call twice.
- **Never fatal.** A backend that times out, errors, or returns non-JSON is
  logged to `logs/failures.jsonl` under source `ai` and the run continues.
- **Model choice.** Bulk extraction and classification use `claude-haiku-4-5`;
  only duplicate adjudication uses `claude-opus-5`. Both configurable.

Rough usage: about **$0.003 per record** in list-price terms, so a run touching
1,000 leads consumes ~$1.60-equivalent of subscription allowance and ~15 minutes
of AI time on the CLI backend.

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
