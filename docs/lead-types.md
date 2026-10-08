# Lead types

A **lead type** is one way of finding organisations worth selling to:
businesses by category and place (`local_business`), companies hiring tech
roles (`hiring`, Phase 6), public tenders (`tenders`, Phase 7), and so on.
Each one is a self-contained plugin, and all of them resolve what they find
to the same deduplicated **organisation** in a project.

Built today: **`local_business`**, **`hiring`**, **`funding_news`**,
**`decision_makers`** and **`digital_audit`** (Phase 6). The rest of the A.7
table arrives in Phases 7 and 8, each as its own plugin under this contract.

## The contract (`leadscraper/leadtypes/base.py`)

```python
class LeadType(ABC):
    key: str                                   # "local_business"
    title: str
    description: str
    config_model: type[BaseModel]              # validates lead_types.<key> in a run config
    applicable_countries: frozenset[str] | None   # None = anywhere

    def sources(self, cfg, geo=None) -> dict[str, list[str]]   # {"discovery": [...], "enrichment": [...]}
    def plan(self, cfg, geo=None, depth="balanced") -> list[WorkUnit]
    def signal_specs(self) -> list[SignalSpec]   # type, title, default strength, half-life
    def columns(self, cfg) -> list[ColumnSpec]   # its own Records columns (not the core ones)
    def column_values(self, org, cfg) -> dict    # Phase 4: values for them, keyed by ColumnSpec.key
    def score(self, org, signals, cfg) -> ScoreParts   # fit / intent / reach, 0..1, with reasons
    # (it may also take now= and record=: the export's moment and its whole ExportRecord)
    def run(self, context) -> LeadTypeResult     # execute its units; must not raise
    # Phase 2: how the engine drives it
    def open(self, context) -> LeadTypeSession   # default: run() as a single unit
    def apply_depth(self, cfg, depth) -> list[str]   # scale its own knobs for quick/deep
    # Phase 6
    post_order: int = 0                         # post-discovery order: assessors (audit) last
    def catalog_sources(self) -> list[dict]     # its own sources for GET /catalog/sources
    # score() may also take now=: exports pass the moment they describe

class LeadTypeSession(ABC):                    # one lead type's share of one run
    def units(self) -> list[WorkUnit]          # discovery units, plan order
    def run_unit(self, unit) -> UnitOutcome    # never raises; may run on a worker thread
    def hosts(self, unit) -> frozenset[str]    # units sharing a host never run at once
    def stats_key(self, unit) -> (source, area, category)   # its unit_stats row
    def prior(self, unit) -> UnitCost          # cold-start seconds and new organisations
    def post_estimate(self, expected_new) -> (mean, sd)     # enrichment / AI seconds
    def ai_estimate(self, expected_new) -> dict | None
    def post_discovery(self, control) -> None  # enrichment, AI; watch control.stop_requested
    def finalize(self, *, complete) -> None    # always called, even after pause/cancel
    def close(self) -> None
    def result(self) -> LeadTypeResult
    # Phase 6
    discovery_stage: int = 0                   # its units wait for every lower stage's
    post_scope: str = "own"                    # "run": post_estimate sized by every lead type
```

| Type | Fields |
|---|---|
| `WorkUnit` | `lead_type`, `source`, `area`, `category`, `scope` (`run` / `locality` / `combo`). `key` is a stable id. |
| `SignalSpec` | `type`, `title`, `default_strength` (0..1), `half_life_days` (None = never ages), `description` |
| `ColumnSpec` | `key`, `header`, `kind` (`text` `number` `url` `phone` `email` `date` `score` `flag` `money`), `description` (the Overview legend), `field` (the `fields.include` toggle, or None), `placement` (`front` after the name, `body` before the contacts, `contacts` after them, `tail` after scores and signals), `width`, `number_format`, `breakdown` (the Overview counts by it), `palette` (value → font colour), `link_label` |
| `ScoreParts` (`core/scoring.py`) | `lead_type`, `fit`, `intent`, `reach` (each 0..1 or None = "not my concern"), `reasons` |
| `LeadTypeResult` | `units_planned`, `units_done`, `new`, `merged`, `failed`, `error`, `stats` |
| `UnitOutcome` | `found`, `new`, `merged`, `status` (`done` / `partial` / `failed`), `error` |
| `UnitCost` (`engine/costs.py`) | `seconds`, `new`, `sd`, `basis` (`prior` / `source` / `learned`), `runs`; `priority` = new per second |

`cfg` is whatever the lead type reads its settings from. For `local_business`
that's the compiled `Config` view (see below). A new lead type would normally
take its own `config_model` instance.

## Rules every lead type keeps

1. **It depends only on core interfaces** (`core`, `sources`, `packs`, `db`),
   never on another lead type.
2. **Enabling, disabling or deleting it never changes another's output.**
   `tests/test_phase1_acceptance.py` proves this for `local_business`. With a
   second lead type enabled, disabled, or crashing, local_business's rows,
   score parts and evidence are byte-for-byte the same.
3. **A failure inside it is contained to its own work units.**
   `engine/runner.py` records the error and the other lead types still run.
4. **Its config is namespaced:** `lead_types.<key>.*`, its source settings
   included.
5. **Everything it writes is tagged with its key.** The store stamps
   `organizations.lead_types`, `evidence.lead_type`, `signals.lead_type` and
   `run_organizations.lead_type` with the active lead type.
6. **It never rewrites another lead type's record** (Phase 6). A lead type
   that finds a company the project already holds attaches its signals to
   that organisation (`LeadStore.add_signal`, `stamp`: the tag, run
   membership and its own evidence) and leaves the record, its contacts and
   its score alone. Only a company nobody holds yet becomes a new
   organisation (`upsert`). Lead types that work this way run their discovery
   at `discovery_stage = 1`, after the finders (stage 0), so within a run
   they see what the finders found instead of racing them for a record.

   Lead types that *assess* other lead types' organisations (an audit, a
   careers-page scan) take those organisations as their input, so their own
   output naturally changes with the finders. What never changes is the
   finders' output: `tests/test_digital_audit.py` and `tests/test_hiring.py`
   prove local_business's rows, score columns and its own evidence are
   identical with either lead type on, off or crashing -- its rows, score
   columns and evidence, its final leads (CSV, Sheet, report) and the targets
   its enrichment is handed. local_business works on
   `LeadStore.owned_by("local_business")`: every listing it goes through is
   narrowed to the organisations it found or enriched (untagged legacy rows
   count as its own), while writes and identity matching see the whole
   project.

## How a run uses it

```
RunConfig ──validate──► lead_types.<key> checked against registry.config_model(key)
          ──compile───► Config (the runtime view)
engine/runner.py: one project store, one Run row (or a resumed one)
   for each enabled lead type:  apply_depth(cfg, depth); session = open(context)
   planned = every session's units()  ──► work_units rows (pending) = the checkpoint
   estimate (engine/estimator.py)     ──► runs.estimate, the "plan" event
   scheduler + dispatcher: run_unit() per unit, in parallel across hosts
       each finished unit: work_units row, unit_stats, cost model, events
   post_discovery(control) per session, unless stopping
   finalize(complete=…) and close() per session, always
   finish_run(status, stats per lead type + "engine" summary)
```

`context` carries the run's `control` (`engine/control.py`: pause, cancel,
shutdown and the time-budget deadline as one stop path) and `events` (the
run's `EventBus`). A lead type that only implements `run()` still works: its
whole run is one unit (`MonolithicSession`). Extra lead types can be loaded
into any process, a separately started worker included, with
`LEADSCRAPER_PLUGINS=module.path` (the module exposes `LEAD_TYPES` or
`LEAD_TYPE`).

Scoring combines every lead type's `ScoreParts` with the run's weights
(`scoring.weights: {fit, intent, reach}`), see `core/scoring.py`:

- A component that no enabled lead type provides drops out, and the
  remaining weights are renormalised. A run with no intent signals isn't
  capped at 60.
- When two lead types score the same component, the stronger evidence wins
  (max), because they assess different things.
- Reasons from every lead type are kept, in order.

## `local_business`

Businesses by category and location. This is the original scraper, wrapped:

| Part | Where |
|---|---|
| Plugin object | `leadtypes/local_business/plugin.py` (`LOCAL_BUSINESS`) |
| Config model | `leadtypes/local_business/config.py` (`LocalBusinessConfig`), plus `DEFAULT_SOURCES` for configs that leave `sources` out |
| Source selection and unit order | `leadtypes/local_business/planning.py`, shared by `plan()` and the pipeline, so what's reported is what runs (`test_plan_lists_the_units_the_pipeline_executes_in_order`). A source whose `fills` the run's `fields.include` leaves out is skipped as "not needed" |
| Execution | `leadtypes/local_business/pipeline.py` (`Pipeline`): discovery → (venues: AI classification and brand roll-up) → name polish → enrichment → AI extraction → finalise. `session.py` (`LocalBusinessSession`) exposes it to the engine unit by unit; `Pipeline.run()` still runs the whole thing sequentially for direct use |
| Costs and depth | `leadtypes/local_business/estimates.py`: cold-start priors per source (requests × configured delays; yields that put Maps before directories before SERP), enrichment and AI estimates, and the depth presets |
| Scoring | `leadtypes/local_business/scoring.py` (`Scorer`) |
| Hospitality vertical | `packs/hospitality/venues.py`: venue types, tiers, prices |

Its config section:

```yaml
lead_types:
  local_business:
    enabled: true
    verticals: [events]            # events | hospitality; hospitality = the venue layout and tiers
    categories:
      - name: Caterers
        queries: [catering services, wedding caterers]   # default: [name]
        directory_slugs: {wedmegood: wedding-catering}
        sources: []                # only these sources (empty = all)
        only_areas: []             # only these areas (empty = all)
        indiamart: true            # pack-specific keys pass through
    sources:                       # leave out = every applicable source at polite defaults
      google_maps: {enabled: true, min_delay: 2.5, max_delay: 6.0, max_results_per_query: 25}
    scoring: {weights: {maps_popularity: 0.4, …}}    # the legacy component weights (its fit)
    dedup: {phone_match_min_similarity: 60, domain_match: true, …}
    hospitality: {brands: […], venue_tiers: {…}, premium_platforms: […], platform_searches: {…}}
```

- **Signals:** none. Ratings and listings are fit evidence, not a reason to
  buy now.
- **Score:**
  - *fit* is the legitimacy score for event vendors, or the venue score for
    the hospitality vertical (/100);
  - *reach* is contact completeness;
  - *intent* isn't provided.

  The imported legacy presets weight `{fit: 1, intent: 0, reach: 0}`, so
  their rankings equal the original ones exactly (the characterisation
  suite pins every score). Reasons look like "Google rating 4.6 from 310
  reviews", "listed by 3 sources (google_maps, justdial, wedmegood)" and
  "reachable by phone, email, website".
- **Columns** (`local_business/columns.py`): only its own. A vendor run
  contributes Category (front, a breakdown) and the Maps ratings, plus the
  WedMeGood columns when the events vertical is on. A venue run contributes the
  venue: Tier (front, coloured per tier, a breakdown), Venue Type, Record Type,
  Managed By, distance, prices in the run's currency, size, flags, ratings,
  booking contact, listing and platforms, then amenities, tier reasons and the
  rest at the tail. Values come from the legacy row (`Lead.row_for`), so the
  workbook and the run's CSV never disagree. The legacy run CSV keeps the full
  `Lead.to_row` / `Lead.to_venue_row` layouts.

## `digital_audit` (Phase 6.1)

Organisations with a weak or missing digital presence. It audits the
organisations other lead types found and any uploaded list of businesses or
domains; it finds no businesses itself.

| Part | Where |
|---|---|
| Plugin, columns, score | `leadtypes/digital_audit/plugin.py` (`DIGITAL_AUDIT`, `post_order = 10`) |
| Config | `leadtypes/digital_audit/config.py` (`DigitalAuditConfig`) |
| The checks | `checks.py`: pure functions, page facts in, one plain-language finding out |
| Fetching | `auditor.py`: one TLS handshake, the homepage (HTTPS, then HTTP), at most one contact page, a HEAD per checked link; robots.txt for every page and link; paced per site |
| Fingerprints | `fingerprints.yaml` (dated rules, ours) + `fingerprint.py` |
| PageSpeed | `pagespeed.py`: Google PageSpeed Insights v5, capped per run, off for the run on quota or a refused key |
| Uploads | `targets.py` (inline `targets` / `domains`, a CSV/XLSX `targets_file`); `POST /api/v1/audit/targets` and `leadscraper audit targets FILE` turn a file into config entries |
| Engine session | `session.py`: one `upload` unit (stage 1) when there are uploads; the audit itself in post-discovery, several sites at once |

```yaml
lead_types:
  digital_audit:
    checks: [no_website, unreachable, https, mobile, pagespeed, cms_age,
             copyright, broken_links, contact_form, booking, maps_profile]   # default: all
    audit: run                 # run | project | targets: whose organisations besides uploads
    targets: [{name: Brightwater Dental, website: brightwater-dental.example, category: Dentists}]
    domains: [oldmill-bistro.example]
    targets_file: null         # CSV/XLSX (CLI): name, website, phone, category, place
    reaudit_after_days: 7      # audited more recently: skipped (--force-enrich re-audits)
    max_links: 8               # homepage links checked for breakage
    cert_expiry_days: 21
    stale_copyright_years: 2
    min_reviews: 10            # Maps profile
    booking_categories: {tattoo: [booking]}    # added to the built-in table
    outdated: {php: "8.3"}     # override a fingerprint threshold
    pagespeed: {key_secret_name: pagespeed-api-key, strategy: mobile, threshold: 50,
                max_per_run: 25}
    workers: 6
```

**Checks, signals and points off the audit score** (100 = nothing to fix):

| Check | Signal type | Points | Finding, for example |
|---|---|---|---|
| `no_website` | `no_website` | 60 | No website of its own (only instagram.com). |
| `unreachable` | `site_unreachable` | 50 | Website unreachable: the domain doesn't resolve. |
| `https` | `no_https` / `cert_expiring` | 20 / 10 | No HTTPS: its HTTPS certificate is invalid (certificate has expired). |
| `mobile` | `not_mobile_friendly` | 15 | Not mobile-friendly: the homepage has no viewport tag… |
| `pagespeed` | `slow_site` | 10 | Slow site: PageSpeed 31/100 on mobile, largest paint 6.4 s (below 50). |
| `cms_age` | `outdated_tech` | 15 | Outdated technology: WordPress 4.9.8, jQuery 1.12.4, PHP 7.2.34. |
| `copyright` | `stale_copyright` | 5 | Stale copyright: the footer still says 2017, 9 years ago. |
| `broken_links` | `broken_links` | 8 | 1 broken link on the homepage (of 4 checked): … (HTTP 404). |
| `contact_form` | `no_contact_form` | 5 | No contact form on the homepage or its contact page… |
| `booking` | `no_online_booking` | 10 | Customers of restaurants expect to book or order online; the site offers neither. |
| `maps_profile` | `incomplete_maps_profile` | 7 | Incomplete Maps profile: no website, no reviews (only 3). |

- **Honesty.** A check that could not run says nothing. A site that refuses
  us (401/403/429, a bot wall), whose robots.txt disallows the homepage, that
  resolves to an internal address, or whose audit failed or was cut by a stop
  is "not checked": nothing is written and the previous audit stands. One
  dropped connection is retried before "unreachable" is claimed. A Maps check needs a Maps listing; a
  version check needs a version (a CMS seen without one is in the stack, not a
  finding). Booking is only expected where the category says so.
- **Fingerprints** are our own rules in the open pattern style (meta
  generator, HTML, script/link URLs, headers), not a copy of a GPL set. Each
  `outdated` rule says why, with a date (`eol`), so a reviewer can re-check it.
- **Snapshot.** Each audit replaces the last one's signals on that
  organisation (`prune_signals`); its summary is the `audit` attribute
  (`origin: rule`): score, status, findings, stack, PageSpeed, when.
- **Score:** *intent* = the points the findings took off / 100, halving every
  180 days of the audit's age. No fit, no reach.
- **Columns:** Site (front, a breakdown, coloured), Audit Score, Audit
  Findings, Site Stack, PageSpeed, Audited (tail).
- **Depth:** quick checks 3 links and skips PageSpeed; deep checks 20 links.

## `hiring` (Phase 6.2)

Companies hiring tech, IT and digital roles.

| Part | Where |
|---|---|
| Plugin, columns, score | `leadtypes/hiring/plugin.py` (`HIRING`, `post_order = 5`) |
| Config | `leadtypes/hiring/config.py` (`HiringConfig`) |
| Role rules | `roles.py`: families as whole-word title patterns, seniority, place / remote matching |
| Job sources | `sources/jobs/ats.py` (Greenhouse, Lever, Ashby, Workable, SmartRecruiters), `sources/jobs/hn.py` (HN "Who is hiring?" via Algolia), `sources/jobs/base.py` (postings, ATS boards found in links and embeds) |
| Engine session | `session.py`: units `ats` (one per configured board), `hn_whos_hiring`, `board_search` (opt-in, per place), all at stage 1; `careers_pages` in post-discovery |

```yaml
lead_types:
  hiring:
    roles: [developer, qa, devops, data, it_support, digital_marketing]
    # also: design, security, erp, product
    keywords: [odoo]           # extra title words that count ("custom")
    seniority: []              # intern | junior | mid | senior | lead | manager
    remote: include            # include | only | exclude
    posted_within_days: 30
    min_roles: 1
    boards: [greenhouse:acme, lever:acme-labs, ashby:acme]
    sources:
      ats: {enabled: true, max_postings: 500,
            kinds: [greenhouse, lever, ashby]}   # + workable, smartrecruiters: opt-in
      hn_whos_hiring: {enabled: true, months: 1}
      careers_pages: {enabled: true, audit: run, max_orgs: 200, rescan_after_days: 7}
      board_search: {enabled: false}       # SERP for ATS board pages naming the place
```

- **Which postings count.** The title matches a wanted family ("Senior PHP
  Developer" is a developer, "Sales Engineer" nothing); its level is wanted;
  it is recent enough (an undated careers-page title counts); and it names one
  of the run's places or areas, or (unless `remote: exclude`) is remote and
  open to the run's country: "Remote - India", "Remote (Global)" and a plain
  "Remote" are; "Remote (US)", "London, UK or Remote", "Remote but Boston based
  only" and "in select countries" are not, for an Indian run.
- **HN ads** name their roles in the headline (or a role list); prose
  mentioning QA or SEO doesn't count. With the `signal_interpretation` AI
  feature, ads the rules can't place go to the model, kept only with a
  verbatim quote from the ad.
- **Careers pages** of the run's organisations (`audit: run`) or the
  project's: an ATS board the site links or embeds is read through the ATS's
  API (once per run, however many organisations link it); otherwise tech job
  titles on the careers page itself count, each with its own link. Only the
  page's content counts (navigation, header, footer and forms name services,
  not openings), a title must name a role ("Senior PHP Developer", not "SEO
  Services"), and a page saying it has no openings has none. A site whose
  board didn't answer is scanned again next run.
- **Organisations.** A company the project already holds gets the signals and
  keeps its record (rule 6); a new one is created from the board's company
  name (and the HN ad's site).
- **Signals:** one `job_posting` per role, keyed by its URL (a re-run
  refreshes it), with role, family, seniority, location, how it matched, the
  board and the posted date. Strength grows with the company's open roles:
  0.45 for one, +0.1 each, up to 0.95.
- **Score:** *intent* = 0.4 + 0.15 per further open role (max 1), halving
  every 30 days after the newest posting; a role not seen for 45 days no
  longer counts as open.
- **Columns:** Open Tech Roles, Role Families, Latest Posting, Jobs Page;
  Open Roles and Hiring Sources at the tail.
- **Opt-in:** Workable and SmartRecruiters boards are read only when their ATS is
  in `sources.ats.kinds` (their public endpoints aren't documented as public
  APIs); a board of another ATS is skipped and noted (`source_not_enabled`),
  whether configured or found on a careers page. `board_search` is off unless
  enabled.
- **Not used:** Upwork, Indeed, Glassdoor, LinkedIn Jobs (A.2.4). No country
  job board qualified yet: Naukri forbids automated access and Arbeitnow's
  licence forbids commercial use (`docs/sources.md`).
- **Depth:** quick scans at most 50 careers pages, deep up to 500. (More HN
  threads only help with `posted_within_days` above 30.)

## `funding_news` (Phase 6.3)

Companies in the news with a reason to buy now: funding rounds, launches,
expansion and new offices, acquisitions, digital transformation and leadership
changes.

| Part | Where |
|---|---|
| Plugin, columns, score | `leadtypes/funding_news/plugin.py` (`FUNDING_NEWS`) |
| Config | `leadtypes/funding_news/config.py` (`FundingNewsConfig`) |
| Headline rules | `events.py`: event verbs, the company before them, exclusions; `amounts.py`: amounts and rounds as stated |
| News sources | `sources/news/gdelt.py` (GDELT DOC 2.0), `google_news.py` (Google News RSS), `feeds.py` (GlobeNewswire per country; any RSS/Atom), `base.py` (`NewsItem`, a hardened feed parser) |
| Engine session | `session.py`: units `gdelt` and `google_news` (one per place), `press_releases` (one), `feeds` (one per URL), all at stage 1 |

```yaml
lead_types:
  funding_news:
    events: [funding, launch, expansion, acquisition, digital_transformation,
             leadership_change]          # default: all
    keywords: []               # extra search words, ANDed with every query
    recency_days: 30
    place_filter: headline     # headline | article | none
    create_new: true           # a company nobody holds becomes an organisation
    sources:
      gdelt: {enabled: true, max_records: 100, min_delay: 6, max_delay: 9}   # >= 5 s
      google_news: {enabled: false}      # its feed terms forbid it: the owner's call
      press_releases: {enabled: false}   # GlobeNewswire's country feed: to review
      feeds: {enabled: true, urls: []}   # RSS / Atom feeds you are entitled to read
```

- **Which stories count.** The headline (for a press release, also the first
  sentence of its summary) reports an event the rules know, about a company
  named right before the verb: "Pune-based fintech startup Ledgerly raises
  Rs 22 crore in pre-Series A" is Ledgerly's funding round; "Pune mosque … raises
  funds for flood relief" names no company and "Sensex raises concerns" no
  event. Descriptors ("fintech startup", "Pune-based"), possessives
  ("Bengaluru's") and prefixes ("Exclusive:") are dropped; public bodies (RTO,
  PMC, ministries, police, courts, boards) are not companies, nor is a person
  with a role ("Zomato CEO … raises"). Orders, contracts and loans a company
  "bags" or "gets" are not funding. A press release's issuer
  (`dc:contributor`) is the company whatever the grammar.
- **Place.** With `place_filter: headline` (the default) a story counts only
  when its headline or a feed's summary names one of the run's places or
  areas. GDELT matches the whole article text, so `article` keeps GDELT's
  place-scoped results too (noisier); `none` keeps the country's news.
- **AI.** With the `signal_interpretation` feature, headlines the rules can't
  read go to the model, kept only with a verbatim quote and a company name the
  item itself contains.
- **Organisations.** A company the project already holds gets the signal and
  keeps its record (rule 6); a new one is created from its name (and, for a
  press release, the issuer's own site among the release's links), unless
  `create_new: false`.
- **Signals:** one per event, typed `funding_round`, `product_launch`,
  `expansion`, `acquisition`, `digital_transformation` or `leadership_change`.
  The payload carries the amount as stated (`{text, value, currency}`; a crore
  is 10^7), round, investors ("led by …"), person and role (leadership), the
  other party (acquisition, partner) and every outlet that covered it. Stories
  about the same event (company, kind, detail; any funding news of a company)
  within 30 days are one signal: a re-run refreshes it, and the event keeps the
  date and headline of its first story. Strength: the type's default (funding 0.8, digital
  transformation 0.75, expansion and acquisition 0.6, leadership 0.6 or 0.75
  for a technology role, launch 0.45) plus 0.05 per further outlet, up to 0.95.
- **Score:** *intent* = the strongest event's strength, halving every
  half-life of its type (funding, acquisition, transformation 90 days;
  expansion, leadership 60; launch 45), plus 0.1 for each further kind of
  event (max 1); an event older than four half-lives no longer counts.
- **Columns:** News Event (front, a breakdown), Latest News, News Date, Amount
  Raised, News Link; News Events and News Sources at the tail.
- **GDELT etiquette.** At most one request every 5 seconds (the config can't
  go lower), one attempt per query (a 429 is not retried), queries of at most
  six OR terms (GDELT refuses longer ones), and no more GDELT in a run once it
  throttles. News endpoints get an identifying User-Agent.
- **Depth:** quick reads 50 GDELT articles per query, deep 250.
- **Not used:** Inc42 and YourStory (their terms forbid automated access), PR
  Newswire (personal, non-commercial use only), Entrackr (its feed is gone).
  No regional feed qualified for a country pack; `feeds.urls` takes yours.

## `decision_makers` (Phase 6.4)

Who to talk to at the organisations any lead type found: founders, owners,
CEOs and MDs, CTOs and heads of technology, IT heads. An enrichment lead type:
it finds no organisations.

| Part | Where |
|---|---|
| Plugin, columns, score | `leadtypes/decision_makers/plugin.py` (`DECISION_MAKERS`, `post_order = 7`) |
| Config | `leadtypes/decision_makers/config.py` (`DecisionMakersConfig`) |
| Names and titles | `sources/people/roles.py`: role families, `person_name`, `employer_of` |
| People sources | `sources/people/team_pages.py`, `linkedin.py` (search-indexed profiles), `github.py` (org members); `packs/uk/companies_house.py` (directors) |
| Email pattern | `email_pattern.py`: the pattern, MX records; nothing else |
| Engine session | `session.py`: no units; everything in post-discovery, several sites at once |

```yaml
lead_types:
  decision_makers:
    scope: run                 # run | project: whose organisations
    roles: [owner, executive, technology, it]
    max_people: 5              # per organisation, owners and chief executives first
    max_orgs: 200
    refresh_after_days: 30     # looked into more recently: skipped (--force-enrich re-reads)
    workers: 4
    sources:
      team_pages: {enabled: true, max_pages: 3}
      registry: {enabled: true, key_secret_name: companies-house-api-key}   # packs (UK)
      linkedin_search: {enabled: false}   # SERP: Google's terms restrict automated queries
      github: {enabled: false, token_secret_name: github-token}   # GitHub's AUP: opt-in
    email_pattern: {enabled: false}
```

- **Team pages.** The homepage, then up to `max_pages` team / leadership /
  management / board pages and then about / company pages it links (a link
  counts by its last path segment or its text). People are read from JSON-LD
  `Person` objects, microdata, name-and-title cards ("Anika Desai" /
  "Co-founder & CEO", or "Sanjay Kulkarni, IT Manager") and "founded by …"
  prose; navigation, header, footer and forms are ignored. The homepage's own
  people count only when a site has no team or about page: on bigger sites
  they are testimonials, authors and customers. A card holds one person; an
  email or LinkedIn link inside it (or inside a one-line "Name, Title" entry)
  is theirs, and a role mailbox (info@, sales@ …) is nobody's. "Founded by"
  prose counts only when it is about this company ("Google, which was founded
  by …" on a vendor's page isn't).
- **Who counts.** A name is two to four capitalised words (initials and
  particles allowed, acronyms, role words and business words not). A title is
  short, isn't marketing copy, and falls in a wanted family: **owner**
  (founder, owner, partner, chairman, LLP member, "Principal" alone),
  **executive** (CEO, MD, directors, GM, COO, CFO, president, VPs),
  **technology** (CTO, CIO, chief digital officer, heads/VPs of engineering,
  technology, IT, digital) or **it** (IT manager, systems manager). Advisors,
  investors, mentors, interns and non-executive or independent directors don't.
  A title naming another employer ("CIO, Fernwood Retail" on a vendor's
  testimonials) is someone else's.
- **Registries (packs).** UK runs: Companies House directors and LLP members
  who haven't resigned, for an active company whose registered name matches
  (similarity ≥ 90), with the free key in the secret `companies-house-api-key`.
  Several active companies of that name: the one whose registered address names
  the organisation's locality or postcode, else nobody (`registry_ambiguous`).
  Without the key it says so once (`key_missing`) and skips. India: the MCA
  has no free public API, so no Indian registry yet.
- **LinkedIn (opt-in).** A search for `site:linkedin.com/in "<company>"
  (founder OR CEO OR CTO …)`; a result counts when its title names this
  company and a decision-maker's title. Profile URLs only: LinkedIn itself is
  never fetched or logged into (A.2.4).
- **GitHub (opt-in).** The org the company's site links, only when the org's
  own `blog` is the company's domain; public members whose own profile bio
  names a leadership title. Never used for the email pattern.
- **Writing.** Each person is a `people` row of this lead type (name, title,
  profile URL, source, the page, an address published for them), refreshed by
  name. The organisation is stamped (tag, evidence, run membership) and its
  record left alone (rule 6). A source read in full is a snapshot: someone it
  no longer lists is removed, with their inferred address; a page that didn't
  answer removes nobody and is read again next run.
- **The email pattern** (A.2.5, off by default). Only when a published
  personal address at the organisation's own domain is exactly one pattern
  (`first.last`, `flast`, `first`, …) of a person we know by name; role
  mailboxes (info@, sales@ …) reveal nothing and disagreeing addresses cancel
  it. Then, if the domain's MX records say it takes mail (a null MX says it
  doesn't), named people without a published address get one, stored as an
  `inferred` contact naming its person (confidence 0.6, or 0.75 from two
  examples). Only real person names count, for learning and for applying ("Front
  Office" as a record's contact name is nobody); a run whose team page didn't
  answer keeps the addresses inferred before. No SMTP, ever. Inferred addresses appear only in `Inferred Email
  (unverified)`, are never on the record, never identity keys, and become
  published contacts if the address is later published. The pattern itself is
  the `email_pattern` attribute (`origin: rule`), shown in Email Pattern.
- **Score:** *reach* = the core contact reach + 0.15 for a named
  decision-maker with a title + 0.05 when there is a way to reach them (a
  profile, or an address published for them); max 1. Without people it adds
  nothing (core reach applies). It needs the whole record, so its `score`
  takes `record=`.
- **Columns:** the core Decision Makers, Decision Maker Profile and Decision
  Maker Email (published for them only) columns, plus DM Roles and People
  Found; People Sources and Email Pattern at the tail.
- **Fields.** A run whose `fields.include` leaves out `decision_makers` gets
  nothing from it; the email pattern also needs `emails`.
- **Depth:** quick reads one team page each for at most 50 organisations,
  deep up to six for up to 500.

## The export (Phase 4)

`export/columns.py` assembles the Records columns for each export: the core
columns every lead type shares (score, name, lead types, place, locality,
published contacts, the maps link, address, decision-makers; Fit, Intent,
Reach and reasons; the top signal; provenance), and each lead type's own
columns where its `placement` puts them. Keys are namespaced
(`local_business.tier`); a header two lead types share gets the lead type's
title appended. Optional columns -- every lead type's, socials, signals,
decision-makers, inferred contacts -- are left out of an export where no
record has a value.

Scoring in an export: every lead type an organisation belongs to scores it,
and `combine` folds the parts with the run's weights. Weights renormalise per
*run*: a component some lead type in the export scores counts 0 for an
organisation without it. Reach is a property of every organisation, so an
organisation no reach-scoring lead type found gets the core
`contact_reach` (the same channel weights `local_business` uses by
default). With only `local_business`, this is exactly its legacy score.

Signals in an export: the strongest unexpired one leads ("Top Signal"); a
signal's strength halves every `half_life_days` of age. `ScoreParts` from a
lead type's `score()` get only that lead type's signals.

## AI for lead types (Phase 5)

A session gets the run's AI client as `context.ai` (None when AI is off). A
lead type that reads news items or job posts can have them classified with
`leadscraper.ai.features.signals.interpret(context.ai, items, self.signal_specs())`:
only its own signal types come back, each with a strength and a verbatim
evidence quote (an answer whose quote isn't in the item is dropped). Items
without a verdict fall back to the lead type's own rules. Any contact detail
AI reads must go through `ai.grounding.ground_contacts` against the fetched
page. AI attributes go to `LeadStore.set_attribute` (never into published
contacts) and show in the workbook's AI columns. Its usage is in the run's
ledger under the feature it used.

## Adding a lead type

1. Create `leadscraper/leadtypes/<key>/` with a `config.py` (pydantic model
   with `enabled: bool = True`) and a `plugin.py` defining a `LeadType`
   subclass and a module-level instance.
2. Register it in `leadtypes/registry.py:_load_builtins`. The run-config
   schema and its JSON Schema pick it up automatically.
3. Put its sources in `leadscraper/sources/` if they apply everywhere, or in
   a pack if they only make sense in some countries or verticals.
4. Write through `context.store`; the store tags rows with the key.
5. Give it `columns()` and `column_values()` for its own facts (not names,
   contacts or scores: those are core), with `description`s for the legend.
6. Tests: fixture-based tests for its sources, signals, columns and score,
   plus an isolation test like `test_enabling_or_disabling_a_lead_type_leaves_local_business_unchanged`.
   To see it in the workbook, add it to a scenario in `tests/workbook_fixtures.py`
   (the `audit_hiring` and `news_people` scenarios show the real Phase 6 lead
   types).
7. If it finds companies other lead types may also hold, resolve them with
   `find_match` first and only attach to a match (rule 6), and set
   `discovery_stage = 1`. If it assesses others' organisations, do that in
   `post_discovery`, set `post_order` and `post_scope = "run"`.
8. Network access: `leadtypes/toolkit.Kit` gives a session the run's shared
   rate limiter, a public-only HTTP client (internal addresses refused, every
   redirect hop included; bound to the dead-domain cache), a failure log and a
   robots.txt cache; `tests/fake_web.py` serves fixtures to it in tests.
9. People and inferred contacts (Phase 6.4) go through `LeadStore.add_person`,
   `prune_people` and `set_inferred_contacts`, never through the record: a
   record write keeps every inferred contact and never stores one as published.
