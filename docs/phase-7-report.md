# Phase 7 report — lead types, wave 2: `tenders` and `registrations`

Date: 2026-10-13 · Branch: `phase-7/tenders-registrations` (see "About the branch") · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 7

**Result:** Phase 7 is complete.
- The suite is green: 908 passed, 7 skipped, 22 deselected. On `fix/live-check-2` it was 845 passed, 7 skipped, 16 deselected.
  - The 7 skips are the opt-in Postgres tests plus one characterisation case that doesn't apply, as before.
  - The 6 new deselected tests are the Phase 7 live smoke tests.
  - `ruff` is clean.
- **Keys:** sources that need a key are skipped with a clear "key not configured" status when it's missing (section 3):
  - SAM.gov and Companies House need free keys; OpenCorporates needs a token and is opt-in.
  - Where to register for each key is in section 4 and in `docs/sources.md` ("Keys: where to register").
- **Live, today:** every endpoint was checked live before coding against it (A.9.2, section 5).
  - The keyless live smoke tests pass: TED, Find a Tender, Contracts Finder, CPPP, GeM.
  - The keyed ones skip without a key: SAM.gov, Companies House, OpenCorporates.
  - Five live end-to-end runs (section 6). They exposed a time-zone bug in the store and two GeM problems, all fixed.
- **Postgres:** on a throwaway PostgreSQL 16 (section 7):
  - the Phase 6 and 7 suites pass (304);
  - the opt-in Postgres tests pass (6);
  - the engine suite has one failure that also happens on `fix/live-check-2` (pre-existing, section 12).
- **Acceptance:** all checks pass for both lead types, including "packs stay inactive for countries they don't cover" (section 8).
- **Review:** an independent review found no high issues, 3 medium and 6 low. All mediums and 3 lows are fixed, each with a regression test; the 6 new tests all fail on the pre-fix code (section 9).
- **Baselines:**
  - `git diff fix/live-check-2 -- tests/characterisation/golden` is empty;
  - `tests/characterisation` passes against the frozen real-data copies;
  - the six existing golden workbooks and samples are byte-identical. The new pair is `tenders_registrations.json` and `sample-tenders-and-registrations.xlsx`.
- **CI has not run** on this branch, because nothing was pushed.

**About the branch.** You asked for `phase-7/tenders-registrations` from `main`.
- `main` is still at Phase 2. Phases 3–6b and both live checks live only on their branches, in a line ending at `fix/live-check-2` (which merges `fix/live-check` too).
- Phase 7 builds on all of them (services, exports, AI, Phase 6's attach-don't-rewrite store calls, `Kit`), so I created this branch from `fix/live-check-2`, as Phases 2, 4, 5, 6a and 6b did.
- `main` itself is untouched. Once the earlier branches are merged, this branch's diff is exactly Phase 7.

## 1. `tenders` (7.1)

**Config:**
- `cpv` (EU/UK) and `naics` (US) codes, as prefixes. Defaults are IT services 72, software 48, computer equipment 302, IT training 80533; NAICS 5415, 518210, 611420.
- `keywords`: for portals with no codes (India).
- `match`: `auto` / `codes` / `keywords` / `any`.
- `min_value` / `max_value`, in the tender's own currency.
- `closing_after` (default: still open now), `posted_within_days` (30).
- `planned`: prior-information, pre-solicitation and sources-sought notices.
- `scope`: country, or the run's places.
- `create_new`.

**Portals, one country pack each** (`Pack.tenders`; a portal outside its pack's countries is not built):

| Portal | Pack | What | Default |
|---|---|---|---|
| `ted` | `eu` (new; EU 27 + IS, LI, NO) | TED Search API: an expert query by country, notice type, CPV and date | **on**, no key |
| `find_a_tender` | `uk` | Find a Tender's OCDS release packages, cursor pages, sorted here (no CPV filter in the API) | **on**, no key |
| `contracts_finder` | `uk` | Contracts Finder search API, by CPV, open and pipeline notices | **on**, no key |
| `sam_gov` | `us` (new) | SAM.gov Get Opportunities v2; one request a run (10 a day without a SAM.gov role) | **on, needs a free key** |
| `cppp` | `in` | India's CPPP latest-active-tenders list (HTML, ten a page; never the CAPTCHA search) | **off**: its copyright policy asks for permission |
| `gem` | `in` | GeM's public bid listing (the page's own form token, a full-text search per keyword, newest bids first) | **off**: its copyright policy asks for written permission |

**Which notices count** (`session.wanted`):
- **Code or words.** With `auto`, a portal that classifies decides by code, as a prefix (`72000000` = every IT service). India's portals, or a run that names no codes, decide by keyword (whole words: "server" doesn't match "Serverless").
- **Still open.** Not closed by `closing_after`.
- **Value.** Inside the range; a notice stating no value is kept.
- **Recent.** Published within the window (for undated planned notices).
- **Place.** With `scope: place`, it names a run place.

**Organisations:**
- **The buyer is the organisation.** One the project holds keeps its record (rule 6). One nobody holds is created with:
  - the contacts the notice publishes;
  - the portal's buyer id as a registry id (`ted_buyer`, normalised; `gb_fts`; `us_sam_office`).
- **Its website is its own site only.** An e-procurement portal's address, which hundreds of buyers share, is never anyone's website (`own_site`).
- **Every buyer gets `org_type: public_buyer`.** It is an attribute, never a change to the record.

**Signals and scoring:**
- **Types:** `tender` (open) or `tender_planned`.
- **Key:** `portal:notice id`, so a re-run refreshes the row instead of adding one.
- **`expires_at`** is the closing date.
- **Payload:** notice id, portal, type, value as stated, deadline, documents link, codes and what matched, contact point, places.
- **Strength:** 0.75 when found by code, 0.65 by words only, 0.5 for a planned notice.
- **Score:** *intent* = the strongest open tender, halving every 60 days (planned: 90) since publication, + 0.05 per further open tender.
  - **A tender past its closing date counts nothing.** An export regenerated after the deadline agrees: `test_an_export_after_the_deadline_drops_the_tender`.
- **Columns:** Org Type (front, a breakdown), Open Tender, Tender Closes, Tender Value, Tender Link, Tender Documents; Open Tenders, Tenders, Tender Portals, Tender Codes and Tender Contact at the tail.

## 2. `registrations` (7.2)

**Config:**
- `incorporated_within_days` (30).
- `industry_codes`: prefixes; empty = every industry.
- `exclude_codes`: 99999 dormant, 74990 non-trading.
- `company_types`.
- `scope: place` (default): the registered office is in a run place.
- `create_new`.
- `directors` (on), `max_director_lookups` (100).

**Registers:**

| Register | Where | Default |
|---|---|---|
| `companies_house` | pack `uk`: the advanced company search (`incorporated_from/to`, `location` = one unit per place, `sic_codes`), directors from `/officers` | **on, needs a free key** (the same secret decision_makers uses) |
| `opencorporates` | any country (generic) | **off**: free tokens only for public-benefit users; commercial use needs a paid key |

**India: no register.** The MCA has no free API. data.gov.in's newest company master data ("Registrars of Companies (RoC)-wise Company Master Data", refreshed July 2026) says "The data upto 3rd November 2023", so there are no new incorporations to find.

**Organisations and people:**
- **Organisations.** A company the project holds keeps its record (rule 6). A new one carries its register number as a registry id (`uk_company`).
- **Directors become people of this lead type.** These are current directors and LLP members, with resigned ones left out.
  - A re-read is a snapshot: a director who resigned goes.
  - None are read with `fields.include` without `decision_makers`, or at quick depth.
- **Signal:** `incorporation`, with `published_at` = the incorporation date. The payload holds the number, type, status, industry codes with their division names (`sic.py`), the registered office and the register page.
- **Score:** *intent* = 0.7, halving every 90 days of the company's age; nothing after about a year.
- **Columns:** Industry (front, a breakdown), Incorporated, Company No., Company Type, Register Link; Industry Codes and Register at the tail. Directors appear in the core Decision Makers columns.
- **Top Signal** reads "Newly incorporated: 26 Sep 2026 (computer programming and consultancy)".

## 3. "Key not configured": how a missing key shows

A source that needs a key and has none is **skipped, never tried**:

| Where | What it says |
|---|---|
| Dry run / `POST /estimate` | `Not run : 1 unit(s)` / `tenders/sam_gov: key not configured: SAM.gov (US federal opportunities) needs the secret sam-gov-api-key (leadscraper secrets set sam-gov-api-key); get one at https://sam.gov (sign in, then Account Details → Public API Key)`; JSON `unavailable[]` |
| The run | The unit is recorded `skipped` with that reason (`work_units.skip_reason`, `unit_skipped` event, `stats.engine.skipped_units`, the CLI's "skipped units" list and a warning line). One `key_not_configured` failure per source. The run still **completes** |
| `GET /catalog/sources` | `key: {secret_name, configured: false, register_url}` (never the key); `health.status: key_not_configured` whatever the history |
| `GET /health` | `keys.configured`: `companies_house_api_key`, `sam_gov_api_key`, `opencorporates_api_token`, `pagespeed_api_key` (booleans) |

**How it works:**
- **The engine hook.** `LeadTypeSession.unit_skip_reason(unit)` is new in the contract. A unit it names is planned but not scheduled or simulated. A resumed run asks again, so setting the key and resuming runs it.
- **decision_makers' registry** used to record `key_missing`. It now records the same `key_not_configured` reason, with the register link.
- **Keys never leak.**
  - SAM.gov and OpenCorporates take the key in the query string, so it is redacted from every error, both as typed and in its URL-quoted forms.
  - Their requests are never retried, so `with_retry` writes no log line quoting the key.
  - Companies House sends its key in a header.
  - Secret names are bound to their portal: a SAM.gov key secret must start with `sam-gov`, and so on.

## 4. Keys: where to register

| Secret | Used by | Where to get it | Cost / limits |
|---|---|---|---|
| `sam-gov-api-key` | tenders: `sam_gov` (US) | **https://sam.gov**: create a login.gov account, sign in, open **Account Details**, enter your password, **Request Public API Key** | Free. 10 requests/day for a non-federal user without a SAM.gov role (1,000 with a role). A run makes one request by default |
| `companies-house-api-key` | registrations: `companies_house`; decision_makers: the UK registry | **https://developer.company-information.service.gov.uk**: register, **create an application**, add a **REST** API key | Free. 600 requests per 5 minutes |
| `opencorporates-api-token` (opt-in) | registrations: `opencorporates` | **https://opencorporates.com/api_accounts/new** | Free only for journalists, NGOs and academics. Skillminds would need a **paid** plan |
| `pagespeed-api-key` (from Phase 6a, still open) | digital_audit: `pagespeed` | Google Cloud console: enable the PageSpeed Insights API, create an API key | Free quota |

Set one with `python -m leadscraper secrets set <name>`, which uses the local keychain. In server mode, use `LEADSCRAPER_SECRET_<NAME>` or a secret file.

## 5. External sources checked live (A.9.2), 2026-10-13

| Endpoint | Live answer | Terms → decision |
|---|---|---|
| TED `POST api.ted.europa.eu/v3/notices/search` | 200, notices with buyer, contacts, deadline `2026-11-17+01:00`, values, CPV, documents. An unknown field is a 400 listing all 1,830 fields | "Primarily targeted at data reusers … does not require authentication" → **on** |
| Find a Tender OCDS `ocdsReleasePackages` | 200, cursor pages, parties with contact points | Open Government Licence v3.0 → **on** |
| Contracts Finder `api/rest/2/search_notices/json` | 200, CPV-filtered open notices | OGL v3.0 → **on** |
| SAM.gov `opportunities/v2/search` | An empty 404 without a valid key. The docs list 404 as "No data found" and 10 requests/day for keys without a role | Official, free key → **on with a key**; one request a run |
| CPPP latest active tenders | 200, ten a page, IST times; no robots.txt | Copyright policy: reproduction "after taking proper permission" → **off; the owner's call** |
| GeM `all-bids` + `all-bids-data` | 200; "software" found 573 ongoing bids | robots.txt allows it; copyright: "may not be reproduced … without due permission in writing" → **off; the owner's call** |
| Companies House `advanced-search/companies` | 401 "Empty Authorization header"; parameters from the API reference | Official, free key → **on with a key** |
| OpenCorporates `companies/search` | 401 "Invalid Api Token" | Free tokens only for Permitted Users; "corporations … are not" → **off** |
| data.gov.in (company master data) | The newest dataset ends 3 Nov 2023 | Stale → **no Indian register** |

All of these are in `docs/sources.md` (endpoint, method, auth, pacing, terms note, date) and in `sources/notes.py` (the catalog).

## 6. Live runs

`run.py` against a scratch data directory, no keys configured:

| Run | Took (estimate) | What it showed |
|---|---|---|
| Dublin (IE), TED | 0.0 min (0.3–0.4) | 75 notices read. **57 open IT tenders and 6 planned ones** from 55 buyers (HSE, Revenue, Central Bank, councils, universities, ESB), with values, deadlines and buyer emails |
| London (GB), Find a Tender + Contracts Finder + registrations | 0.1 min (0.5–0.7) | 137 releases + 13 notices read. **18 IT tenders** (NHS boards, Scottish universities, councils, colleges). Companies House **skipped: "key not configured …"** |
| Austin (US), SAM.gov | 0.0 min (0.2) | The only unit **skipped: "key not configured …"**. The run completed and nothing was asked |
| Pune (IN), CPPP + GeM opted in | 0.3 min (0.8–1.2) | GeM: 13 IT bids. CPPP: 50 active central tenders read, **none IT** by keyword (works, garden upkeep, supplies) |
| Pune again, after the fixes | 0.3 min | GeM: 17 IT bids, newest first, deadlines correct, no "NA" buyer |

**Fixes from the live runs** (with regression tests):
- **Signal times are stored in UTC.**
  - SQLite kept an aware time's wall clock and dropped its offset, so GeM's 1 pm IST deadlines read back 5.5 hours late. TED's `+01:00` dates were off by an hour.
  - `LeadStore.add_signal` now converts to UTC. Every earlier caller already passed UTC.
- **GeM's "NA".** It writes a missing department as the literal "NA"; that now counts as no name, and the ministry is the buyer.
- **GeM's sort.** Searches ask for the newest bids first. "End date, oldest" filled the pages with bids closing within the hour.

**What the live data says about quality:**
- TED's CPV filter is precise. A few notices carry an IT code beside a non-IT main subject: "Occupational Health Services" with a software lot, "Design Team" for an innovation hub.
- Some notices state placeholder values (€1, €340). They are shown as stated.

## 7. Postgres

A throwaway PostgreSQL 16 ran in Docker (`postgres:16-alpine`, bound to 127.0.0.1, stopped afterwards).

| Check | Result |
|---|---|
| `test_tenders.py`, `test_registrations.py` and the four Phase 6 suites on one shared database | 304 passed |
| `-m postgres` (round trip; migrations 0001 → 0006 over existing rows; both real stores) | 6 passed |
| The engine suite on a fresh database | 54 passed, 1 failed: `test_estimator_p50_is_within_35_percent_on_a_recorded_fixture_run`. It passes alone, and **fails the same way on `fix/live-check-2`** (which also fails `test_without_a_budget_every_unit_runs_in_plan_order` there). Pre-existing; see section 12 |

## 8. Acceptance checks

| Check (each lead type) | `tenders` | `registrations` |
|---|---|---|
| Fixture tests for sources, signals, columns and scoring | **Pass.** `tests/test_tenders.py`, 45 cases (4 of them live). Covered: matching (prefixes, whole words), dates (TED zones, IST, US formats), values; each portal's parsing and paging (TED queries by code / words / either, Find a Tender cursors and statuses, Contracts Finder criteria, SAM.gov's key, 404 and redaction, CPPP pages, GeM's form token); which notices count; runs end to end (signals, `org_type`, attach vs create, workbook columns, refresh not duplicate); scoring and expiry; columns; depth; the review's regressions | **Pass.** `tests/test_registrations.py`, 23 cases (2 of them live). Covered: the Companies House search (parameters, Basic auth, 404, officers), OpenCorporates (token, redaction); SIC divisions; which companies count; runs end to end (signals, directors as people, resigned directors pruned, workbook columns, refresh); `fields.include` without decision_makers reads no directors; score and columns; depth |
| Isolation (enable or disable it alone; others' output unchanged) | **Pass.** `test_enabling_or_disabling_tenders_leaves_local_business_unchanged`, `test_a_crashing_portal_is_contained` | **Pass.** `test_enabling_or_disabling_registrations_leaves_local_business_unchanged`, `test_a_crashing_register_is_contained` |
| Live smoke test behind `@pytest.mark.live` | **Pass.** `test_live_ted`, `test_live_find_a_tender_and_contracts_finder`, `test_live_indian_portals`. `test_live_sam_gov` skips without a key | `test_live_companies_house_incorporations` and `test_live_opencorporates` skip without a key (none was available) |
| Workbook columns render | **Pass.** The run tests open the exported workbook. The `tenders_registrations` golden file pins styles, formats and links | **Pass.** Same |
| `docs/sources.md` entries dated | **Pass.** All six portals | **Pass.** `companies_house`, `opencorporates`, and the Indian datasets not used |
| **Phase 7: packs stay inactive for countries they don't cover** | **Pass.** `test_tender_packs_stay_inactive_where_they_dont_apply` (9 countries: IE, DE, NO → TED; GB → the two UK portals; US → SAM.gov; IN → opt-in only; AU, AE, CA → nothing; catalog applicability and reasons) | **Pass.** `test_registration_packs_stay_inactive_where_they_dont_apply` (GB → Companies House; IN, US, IE, AU → only the opt-in OpenCorporates, so no units) |
| The missing-key status | **Pass.** `test_a_missing_key_is_a_skipped_unit_and_the_run_completes` (skipped unit, reason, one failure, never asked; with the key the same run asks), `test_dry_run_estimate_lists_units_that_will_not_run` | **Pass.** `test_a_missing_key_is_a_skipped_unit_and_the_run_completes` (both places skipped, said once) |

## 9. Review and the fixes it led to

An independent reviewer agent read the diff and reproduced its findings with offline scratch scripts. It reported no high, 3 medium and 6 low findings. The six regression tests below all fail on the pre-fix code (checked by stashing the fixes).

| Finding | Fix | Test |
|---|---|---|
| **Medium:** TED's buyer id is free text ("20110042" vs "IE 20110042"). With the new registry-contradiction rule, one buyer became two organisations | Ids normalised (letters and digits, no country prefix) | `test_review_a_ted_buyer_id_written_two_ways_is_one_buyer` |
| **Medium:** the run's buyer cache, keyed by name, put same-named buyers in two towns on one organisation | Only buyers with a portal id are cached | `test_review_same_named_buyers_in_two_towns_stay_two` |
| **Medium:** after the first keyless run, the catalog's health said `ok`, not "key not configured" | `key_not_configured` whenever no key is configured | `test_review_catalog_still_says_key_not_configured_after_a_keyless_run` |
| Low: `cpv: []` with keywords kept nothing (portals were queried by words, but notices were judged by code) | `auto` decides by code only where the run names codes of that scheme | `test_review_no_codes_named_means_words_decide` |
| Low: the block-page check could take a JSON tender about "access denied" or reCAPTCHA for a bot wall | A JSON answer is never a block page | `test_review_a_json_answer_mentioning_access_denied_is_not_a_block_page` |
| Low: redaction was literal; a URL-quoted key would slip through | Quoted forms redacted too | `test_review_a_key_is_redacted_as_a_url_quotes_it` |
| Low: closing dates show as UTC dates, a day off for some zones | **Not changed** (section 12) | — |
| Low: a Companies House 401 is retried 3 times before `key_rejected` | **Not changed** (section 12) | — |
| Low: the docs still said `key_missing` | Already fixed in the docs commit | — |

The reviewer also checked these and found them sound:
- the engine hook: removed before scheduling and simulation; counted; skipped with the reason; asked again on resume; contained if it fails;
- no key reaches the database, logs, failures, catalog or API;
- rule 6 and isolation;
- the registry-contradiction rule's effect on consolidation;
- lock order (no deadlock);
- pagination termination;
- date handling and expiry scoring;
- pack scoping.

## 10. Intended behaviour changes, with reasons

None of these touch the characterisation results or the six existing golden workbooks.

- **Identity: two different registry ids in one scheme are never a match** (`core/identity.py`), whatever the phone, name or domain. Two councils behind one procurement helpdesk number stayed one organisation in the first fixture run. Only records with registry ids are affected, and no earlier data has any.
- **`LeadStore.add_signal` stores times in UTC.** Earlier lead types already passed UTC, so their rows are unchanged.
- **decision_makers' missing-key reason** is `key_not_configured` (was `key_missing`), and its detail names where to register. Its test was updated.
- **`HttpClient` gains `post`.** It shares the host's session and cookies, with `get`'s pacing, retries and block handling. A JSON response is never taken for a block page.
- **The catalog lists two `companies_house` entries:** decision_makers' and registrations', told apart by `lead_type`. Key-needing sources carry `key`.
- **The ICP builder's offerings:** custom software adds `tenders`; web/app development adds `registrations`. Tests that used `tenders` as the later-phase example now use `tech_stack`.
- **The `in` pack also carries CPPP and GeM, and two packs are new** (`eu`, `us`). `test_country.py` lists them.

## 11. Deviations from the plan, with reasons

- **Branch from `fix/live-check-2`, not `main`** (see the top).
- **India's portals (CPPP, GeM) are built but off by default.** Neither has an API, and both copyright policies ask for permission before reproducing their content (GeM: "in writing in advance"). Enabling them is your call, as with Google News and Workable earlier. With both off, an Indian run has no tender source.
- **SAM.gov makes one request a run by default.** A personal key without a SAM.gov role allows 10 requests a day. NAICS codes are filtered here, not with one request per code.
- **Find a Tender can't filter by code.** A run reads up to 20 pages of releases updated in the window and keeps the IT ones. Contracts Finder and TED filter at the source.
- **No Indian registrations source.** The plan says "where an official dataset is available": the MCA has no free API, and data.gov.in's company master data stops at 3 November 2023.
- **OpenCorporates is built, but off and unverified.**
  - Its free tokens exclude companies, so Skillminds needs a paid key.
  - Without a token, it couldn't be checked whether a search without a name term works; the code leaves the term out.
- **`org_type: public_buyer` is an organisation attribute** (`org_attributes`, `origin: rule`), not a new column. That keeps rule 6 (the buyer's record is never rewritten) and needs no migration.
- **Directors are people of the `registrations` lead type**, read from Companies House's officers list. They are not duplicated into decision_makers, though both appear in the same Decision Makers columns.
- **A "key not configured" unit counts as `skipped`.** It is not `failed`: nothing was attempted, and the run completes.

## 12. Open issues

- **Keys to configure:** `sam-gov-api-key`, `companies-house-api-key` (section 4). Then `pytest -m live tests/test_tenders.py tests/test_registrations.py` and a GB/US run would confirm a 200 from both. The fixtures follow the documented response shapes, but no 200 was seen here.
- **Terms decisions (A.2.4):** `cppp`, `gem` and `opencorporates` are off by default. The inherited sources and Phase 6's opt-in sources are still open.
- **Pre-existing on Postgres:** in a shared engine database, `test_estimator_p50_is_within_35_percent_on_a_recorded_fixture_run` fails on this branch and on `fix/live-check-2`. The base also fails `test_without_a_budget_every_unit_runs_in_plan_order` there. Both pass alone. These look like learned `unit_stats` leaking between tests in one database; not caused by Phase 7.
- **Not changed from the review:**
  - closing dates are shown as UTC dates, a day off for a midnight-ish deadline in some zones;
  - a Companies House 401 is retried 3 times before being recorded as `key_rejected`.
- **Tender relevance is code-based.** A notice whose lots include an IT code beside a non-IT subject is kept (section 6), and placeholder values (€1) are shown as stated. `match: codes` with narrower CPV prefixes tightens it.
- **CPPP found no IT tenders in 50.** Its list is mostly works and supplies, and keywords only see titles. GeM's full-text search finds far more; both are opt-in.
- **Find a Tender notice pages answer 403 to curl** (they open in a browser). The links in the workbook are correct.
- **CI has not run on this branch.** Merge order: `phase-3/api` → `main`, then `phase-4/excel`, `phase-5/ai`, `phase-6a/audit-hiring`, `phase-6b/news-people`, `fix/live-check`, `fix/live-check-2`, this branch.
- **From earlier phases, still open and still yours:**
  - running `migrate-legacy` into your real data directory;
  - checking the real database for the Phase 2 migration bug;
  - live AI provider checks;
  - the PageSpeed key;
  - opening the samples in Excel, including the new `sample-tenders-and-registrations.xlsx`.

Commits on this branch (after `fix/live-check-2`):
- `feat(core): POST requests, units a session can't run, contradicting registry ids`
- `feat(leadtypes): tenders and registrations (Phase 7)`
- `test: tenders and registrations -- portals, registers, runs, isolation, missing keys, live smoke tests`
- `feat(export): a tenders_registrations workbook scenario; offerings name the new lead types`
- `fix: findings of the live run -- signal times stored as UTC, GeM's 'NA' buyer and sort`
- `docs: tenders and registrations (lead types, engine's units that can't run, API, README, CLAUDE.md)`
- `fix: findings of the Phase 7 review (medium, low)`
- `fix(net): a response without headers is checked for a block page as before`
- and this report.
