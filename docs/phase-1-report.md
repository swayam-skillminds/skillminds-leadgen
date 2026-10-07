# Phase 1 report — core refactor: domain model, config schema, plugins, packs

Date: 2026-10-06 · Branch: `phase-1/core-refactor` · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 1

**Result:** Phase 1 is complete. The suite is green: 381 passed, 6 skipped (it was 267 passed, 2 skipped). Four of the skips are the opt-in live Postgres tests, which pass against a real PostgreSQL 16 server (section 15).

All four acceptance checks pass. The Phase 0 characterisation baselines hold exactly, now measured *through the new migration*: no golden file changed (`git diff main -- tests/characterisation/golden` is empty). The two Phase 0 open issues that name Phase 1 are resolved, and so is the unassigned SEO-name one.

## 1. Package layout (A.5)

| Old module | Now |
|---|---|
| `models.py`, `normalize.py`, `errors.py`, `parsing.py` | `core/lead.py`, `core/normalize.py`, `core/errors.py`, `core/parsing.py` |
| (new) | `core/identity.py`, `core/country.py`, `core/geo.py`, `core/scoring.py`, `core/ratings.py` |
| `store.py` | `db/store.py`, rewritten on SQLAlchemy; plus `db/models.py`, `db/engine.py`, `db/migrations/`, `db/legacy_import.py`, `db/runs.py` |
| `config.py` | `config/legacy.py` (the `Config` view); plus `config/schema.py`, `importer.py`, `compile.py`, `geography.py` |
| `http.py`, `browser.py`, `ratelimit.py` | `net/` |
| `pipeline.py`, `scoring.py` | `leadtypes/local_business/pipeline.py`, `scoring.py`; plus `plugin.py`, `planning.py`, `config.py` |
| `venues.py` | `packs/hospitality/venues.py` |
| `sources/{local_directories,indiamart}.py` | `packs/india/` (pack key `in`; `in` is a Python keyword) |
| `sources/directories.py` | `packs/events/` |
| `sources/platforms.py` | `packs/hospitality/` |
| `sources/{facebook,instagram,linkedin_serp}.py` | `packs/social/` |
| `export.py`, `sheets.py`, `failures.py`, `ai.py` | `export/csv_writer.py`, `export/sheets.py`, `engine/failures.py`, `ai/assistant.py` |
| `run.py` | `cli/run_cmd.py`; `run.py` is a shim, and `python -m leadscraper` has subcommands |

Modules were moved with `git mv` and their imports rewritten mechanically, so `git log --follow` still finds each file's history. The registry is now assembled from core plus packs, ordered by each class's `priority`. That reproduces the original run order exactly (`tests/test_country.py::test_registry_keeps_the_original_run_order`).

**One deviation from A.5:** the core sources (Google Maps, Google Search, website, Maps lookup) and the `Source` contract stay in `sources/` rather than inside a lead type. Lead types must not depend on one another, and the website fetcher and SERP client will be needed by `digital_audit`, `hiring` and others, so they need a neutral home. `api/` doesn't exist yet; it's Phase 3.

## 2. Persistence (1.1)

- **SQLAlchemy 2 models** for every A.6 entity:
  - `workspaces`, `projects`, `saved_configs`, `runs`;
  - `organizations`, `identity_keys`, `contacts`, `people`, `signals`, `evidence`, `run_organizations`;
  - `work_units`, `failures`, `ai_usage`, `suppression_lists` / `suppression_entries`, `schedules`;
  - plus the original store's operational tables: `seen_urls`, `dead_domains`, `unit_stats`, `enrichment_marks`.
- **`workspace_id` is on every owned table** (`test_every_owned_table_carries_workspace_id`).
- **Alembic.** Baseline revision `0001`; `test_alembic_migrations_build_exactly_the_models` fails if a model change has no migration. A fresh database is created from the models and stamped; an older one is upgraded.
- **SQLite runs in WAL mode** (`test_file_databases_use_wal`) and is the default, under the `platformdirs` user data directory. `LEADSCRAPER_DATA_DIR` or `LEADSCRAPER_DB_URL` override it.
- **Postgres path.**
  - Tested here: every table and index compiles for Postgres, with records as `JSONB`. The full migration renders as offline SQL (`db upgrade --sql`).
  - Live, opt-in (`LEADSCRAPER_TEST_POSTGRES_URL` → a throwaway database): `tests/test_postgres.py` is a round trip (migrate, compare, upsert, merge, consolidate, dead domains, rescore) plus the locality-scan order. `tests/characterisation/test_char_postgres.py` migrates both real stores into Postgres and requires the SQLite goldens exactly. **All pass** on PostgreSQL 16.2, in both a `C.UTF-8` and an `en_US.UTF-8` database (section 15).
- **What every write records.** Beyond the merged record, each write leaves:
  - identity keys, in claim order;
  - contacts, each `published` with its source and a confidence (0.8 for AI-read values, 1.0 otherwise);
  - a person row for a named contact;
  - an evidence row (source, URL, changed fields, time, run);
  - inside a run, run membership (`new` / `updated`).

  `test_writes_record_contacts_evidence_and_run_membership` covers this.
- **The originals are protected in code.** An original lead store is refused before anything touches it, via a read-only check (`test_a_legacy_store_is_refused_and_left_untouched`). That test caught a real risk: the first version's connection pragma (`journal_mode=WAL`) rewrote the file header before the refusal ran. WAL is now switched on only after the database is known to be ours.

## 3. Identity and dedup (1.2)

The original rules moved into `core/identity.py`, behind a `CandidateIndex` protocol that the store answers from indexed tables. Two non-obvious orders had to be reproduced, and both were confirmed against SQLite's actual query plans before porting:

- `phone IN (...)` returns the claimant of the *lexically smallest* phone;
- the fuzzy-name rule walks a locality in `(category, score DESC, id)` order.

**New rules:**

| Rule | Where | Guard |
|---|---|---|
| Registry ID (`Lead.registry_ids`, e.g. GSTIN, company number) | first | none (authoritative) |
| Registrable domain (`tldextract`, bundled suffix list, no network) | **last**, so it can only add merges the original rules would have stored as new; it never moves one | not a shared host; phones don't contradict; name similarity ≥ 60; fewer than 3 organisations already hold the domain; **off for the hospitality vertical** |

The **shared-host blocklist** has two parts:
- Hosts that identify nobody: social, link-in-bio, Google surfaces, directories and marketplaces, free email providers.
- Site builders whose tenants get subdomains that the public suffix list doesn't know. For those the full host is the identity (e.g. `x.grexa.site`).

**Measured on the frozen stores before the guards existed**, a plain domain rule made the 6 extra merges below. All of them were wrong or debatable, and each guard comes from one of them:

| Store | Merges | What they were | Guard |
|---|---|---|---|
| Mumbai | 2 | Unrelated tiffin services and event managers on the same site builder (`grexa.site`) | builder tenancy |
| Nashik | 4 | Distinct villas of one operator (VayaVia), Beyond / The Haven by Sula, two Sahyadri Farms sites, and two Facebook-derived villas whose "website" was `messenger.com` | venue runs off; Messenger blocklisted |

The flower marketplaces from the Phase 0 report (`fnp.com`, `floweraura.com`, `onlineflowerscakesdelivery.com`, …) are blocklisted, and the three-holder guard catches ones not yet on the list.

**With the guards, the domain rule adds no merges on either real store**, so both dedup baselines hold exactly. Its behaviour is pinned by synthetic tests in `tests/test_identity.py`:
- same domain with a similar name merges across localities;
- branches with their own phones stay apart;
- unrelated names stay apart;
- marketplaces are ignored;
- venue runs are off.

## 4. Country generalisation (1.3)

`core/country.py` covers 14 countries (IN, US, GB, AE, AU, CA, SG, IE, NZ, ZA, SA, DE, FR, NL) and gives each run its:
- phone region and digit bounds;
- currency symbols and money format;
- locale, timezone, Google `gl` and DuckDuckGo region;
- postal-code shape and name noise.

Any other ISO code still parses phones, with neutral defaults for the rest.

| Was hard-coded | Now |
|---|---|
| `DEFAULT_REGION = "IN"` in every phone parser | every call passes the run's region (`Source.region`); with none, only `+`-numbers parse |
| `+91` in the phone regex | built from the region's calling code (identical regex for IN, proven in a test) |
| `₹ / INR / Rs` price patterns, `₹` in tier reasons and the roll-up | built from the run's currency; `format_money` |
| `Price From (INR)` header | `Price From ({currency})` |
| `mumbai|india|maharashtra` in name noise and SERP generic words | the country's name, the state (`geography.admin_area`), the run's place words |
| 6-digit PIN + "Maharashtra" in website addresses | the country's postal code + city/state |
| `gl=IN`, `kl=in-en`, `en-IN`, `Asia/Kolkata`, Mumbai geolocation | from the country and the run centre |
| `city` default "Mumbai", centre default Mumbai's | none; geocoded when missing |
| "Indian phone numbers … +91" in AI prompts | the run's country (Mumbai and Nashik prompts are byte-identical, so their AI cache stays valid) |

The implicit defaults of pre-Phase-1 configs (IN, Mumbai, its centre, Maharashtra, "mumbai" as name noise) live in one place, `config/importer.py`. They're written into the presets explicitly, which is how the legacy runs stay identical.

**Geocoding:** `core/geo.Geocoder` calls Nominatim at most once a second with an identifying User-Agent, and caches every answer, misses included. `config/geography.py` fills a run's missing centre and state before the run. Venue runs also get area centres and a radius. Dry runs, exports and both presets make no lookups. The endpoint was verified live once on 2026-10-06 (`docs/sources.md`); tests use a fixture with that response's shape.

## 5. Packs (1.4)

| Pack | Sources | Applies to |
|---|---|---|
| `in` | justdial, sulekha, indiamart | IN |
| `events` | wedmegood, weddingwire | IN, events vertical |
| `hospitality` | stayvista, saffronstays, elivaas (each narrowed to IN), airbnb, booking; plus `venues.py` | hospitality vertical |
| `social` | facebook, instagram, linkedin_serp | anywhere |
| *core* | google_maps, google_search, website, maps_lookup | anywhere |

- A source whose pack doesn't apply is reported as "not built — not applicable — pack 'in' covers IN; this run is in US", never as a failure.
- Directory URL templates take their city segment from the pack (`Pack.city_slug`; e.g. Justdial says "Bangalore" for Bengaluru) and their category slug from `directory_slugs`. WedMeGood and WeddingWire keep probing city variants live, as in Phase 0.

## 6. Run config (1.5)

- **Schema.** `config/schema.py` is the A.8 pydantic model, `schema_version: 1`, with JSON Schema export (`python -m leadscraper config schema`). `lead_types.<key>` is validated against each lead type's own model from the registry, so a new lead type needs no schema edit.
- **Minimal configs work.** A country, a place and one category are enough; sources default to every applicable source at polite delays (`test_country_place_and_one_category_are_enough`).
- **Unchecked fields switch off the sources that only fill them** (A.8). Each enricher, and the decision-maker search, declares `fills`:

  | Source | `fills` |
  |---|---|
  | `website` | emails, phones, socials, address, prices |
  | `maps_lookup` | phones, website, address, ratings |
  | `instagram` | phones, emails, website |
  | `linkedin_serp` | decision_makers |

  Source selection (shared by `plan()` and the pipeline) skips a source when `fields.include` asks for none of its fields, and logs it as "not needed — it only fills …". Discovery sources always run. A config without `fields` keeps everything, so both presets are unaffected (`test_phase1_acceptance.py::test_unchecked_fields_switch_off_the_sources_that_only_fill_them`, `::test_a_run_config_without_fields_keeps_every_source`).
- **Settings not acted on yet are named, not silently dropped.** The schema holds the whole A.8 shape so saved configs never need to change. Today these do nothing at runtime: `filters`, `icp`, `depth`, `time_budget_minutes`, `output.excel` and `output.json`; the CSV is also written even with `output.csv: false`. `RunConfig.unapplied()` lists each one a config sets, with the phase that brings it. The runner logs them as warnings at run start, and `config validate` prints them (`test_config_schema.py::test_settings_this_build_does_not_apply_are_named`).
- **Importer.** `config/importer.py` turns a legacy `config.yaml` into a run config, and `config/compile.py` is its exact inverse into the `Config` view that sources read.
- **Presets.** `presets/mumbai-event-vendors.yaml` and `presets/nashik-venues.yaml` are the imported legacy configs. Tests prove:
  - both load (`test_legacy_preset_loads`);
  - each compiles back to its legacy file **key for key** (`test_preset_compiles_back_to_the_legacy_file_key_for_key`);
  - each matches the importer's output for today's `config*.yaml` (`test_committed_presets_match_the_importer`).

## 7. Data migration (1.6)

`python -m leadscraper migrate-legacy --config config.yaml` (and the same for `config.nashik.yaml`):
1. copies the original with SQLite's backup API, read-only, into `<data_dir>/legacy-copies/`;
2. imports only the copy into the config's project;
3. records the import as a run.

There are two modes:
- **replay** (the default) runs every stored record through today's identity rules, keeping its own timestamps;
- **verbatim** keeps every row and the original phone/place claim order.

Operational state comes along: seen URLs, dead domains, unit stats, and enrichment marks mapped onto wherever each record ended up.

Run end to end against the real stores, in a sandboxed data directory:

| | Mumbai event vendors | Nashik venues |
|---|---|---|
| Legacy rows | 1,333 | 1,005 |
| Organisations after replay | 1,267 (60 merged, 6 rejected bot-wall titles) | 987 (18 merged) |
| Merged clusters | 38 | 17 |
| Carried over | 307 seen URLs | 193 seen URLs, 23 dead domains, 355 enrichment marks |
| Time | 3.1 s | 2.7 s |

The originals' modification times were checked before and after: still 18 Sept and 1 Oct. `test_copy_never_writes_the_original` checks the hash and mtime.

## 8. Scoring v2 (1.7)

`core/scoring.py` combines each lead type's `ScoreParts` (fit, intent and reach, each 0..1, with reasons) using the run's `scoring.weights`:
- A component that no enabled lead type provides drops out, and the remaining weights are renormalised.
- The stronger of two lead types' evidence wins.

For `local_business`:
- *fit* is today's legitimacy score for vendors, or the venue score for venues (/100);
- *reach* is contact completeness;
- reasons read like "Google rating 4.6 from 310 reviews", "listed by 3 sources (…)" and "reachable by phone, email, website".

The legacy presets weight `{fit: 1, intent: 0, reach: 0}`, so **every score and rank is unchanged**: the characterisation suite pins all 2,338 real scores. New configs default to A.8's 0.4 / 0.4 / 0.2. The store keeps `score`, `fit`, `intent`, `reach` and `score_reasons` for every organisation.

## 9. Lead-type contract, runner and CLI

- **`leadtypes/base.py`** is the A.7 contract (`sources`, `plan`, `signal_specs`, `columns`, `score`, `run`; `WorkUnit`, `SignalSpec`, `ColumnSpec`, `LeadTypeResult`), documented in `docs/lead-types.md`.
- **`local_business`** is today's scraper as a plugin. `plan()` and the pipeline share `planning.py`, and a test proves `plan()` lists exactly what executes, in order.
- **`engine/runner.py`** runs every enabled lead type against one project store:
  - it records the run (config snapshot including CLI overrides, per-lead-type stats, status), its failures and its work units;
  - it tags each row with the active lead type;
  - it contains a crashing lead type.
- **CLI**: `python -m leadscraper {run, migrate-legacy, config import-legacy|validate|schema, presets, db upgrade|info}`.
  - `run.py` keeps every old flag.
  - Running a legacy config whose old store hasn't been imported stops with the exact `migrate-legacy` command; it doesn't silently start an empty project. `--allow-empty-project` starts fresh.

## 10. Acceptance checks

| Check | Status | Evidence |
|---|---|---|
| Both legacy presets load | **Pass** | `test_config_schema.py::test_legacy_preset_loads[*]`, `::test_preset_compiles_back_to_the_legacy_file_key_for_key[*]`, `::test_preset_and_legacy_file_run_identically[*]` |
| Re-exporting the migrated projects reproduces the characterisation results | **Pass, no differences** | `tests/characterisation/test_char_real_data.py`: the export layer now re-exports a *verbatim* migration, and the dedup layer is a *replay* migration. Every header, row hash, order, score, tier and dedup decision equals the Phase 0 golden. |
| "Dentists in Austin, US" activates only core + social sources, parses US phones, no India-pack failures | **Pass** | `test_phase1_acceptance.py::test_us_run_activates_only_core_and_social_sources` (selection and plan) and `::test_us_run_end_to_end_parses_us_phones_without_india_pack_failures`. That one is a full offline run through the runner, with every source enabled including India's: the dentist's phone is `+12015550123`, the address ends in a ZIP, and failures come only from core sources (Maps, with no browser in the test). |
| Disabling a lead type leaves another's output unchanged | **Pass** | `::test_enabling_or_disabling_a_lead_type_leaves_local_business_unchanged`. local_business's rows, score parts and evidence are identical alone, with a second lead type enabled, and with it disabled. `::test_a_crashing_lead_type_is_contained` covers a crash. |

Characterisation numbers, the same as the Phase 0 baseline:

| | Mumbai vendors | Nashik venues |
|---|---|---|
| Leads exported (verbatim migration) | 1,333 | 1,005 |
| Replay: stored / replayed | 1,267 / 1,333 | 987 / 1,005 |
| Replay: merged clusters, rejected | 38, 6 | 17, 0 |

## 11. Phase 0 open issues

| Issue | Status |
|---|---|
| No consolidation after enrichment | **Fixed.** `merge_into` and `update_fields` re-check identity when a phone, place, website, registry ID, pin or name changes, and fold the record into what it now matches. The lower id survives; keys (claim order kept), contacts, people, signals, evidence, run membership and enrichment marks follow it. Tests: `test_identity.py::test_enrichment_that_reveals_a_shared_phone_merges_the_records`, `::test_an_ai_rename_that_reveals_a_duplicate_merges_it`. |
| No domain-based matching; needs the shared-host blocklist | **Done.** See section 3. |
| Nashik dedup replay takes ~23 s (Phase 2 item) | **Improved as a side effect.** Indexed identity keys and a bounding-box pin search replace the full-table scans: replay now takes ~3.5 s, and the whole suite ~12 s instead of ~27 s. |
| SEO names only cleaned with an AI backend | **Fixed.** With AI off or unavailable, keyword-stuffed names are cut back to the name they start with (`core.normalize.seo_title_head` plus the run's generic words). The original title is kept in the notes. On the frozen copies: 118 of 120 such Mumbai names and 46 of 46 Nashik names; pure keyword titles are left alone. Tests: `test_core.py::test_seo_title_head_only_cuts[*]`, `test_pipeline.py::test_keyword_stuffed_names_are_cleaned_by_rule_when_ai_is_off`. |
| Global git `user.email` has a trailing colon | Not a code issue; left alone, as in Phase 0. |

## 12. Intended behaviour changes, with reasons

None of these touch the characterisation results, which run the legacy configs through export and replay.

- **Records live in the application database.**
  - **What changes:** the store is the per-user app DB with one project per config, not `data/leads.sqlite3`. `run.py --export-only` on `config.yaml` needs `migrate-legacy` once, and says so.
  - **Why:** A.3 (platformdirs, one DB for local and server mode); the originals must never be written (A.2.6).
- **Name polish cleans SEO names by rule when AI is off.**
  - **What changes:** `test_pipeline.py::test_ai_off_leaves_the_messy_name_untouched` pinned the old behaviour; it is now `test_ai_off_cleans_the_messy_name_by_rule`.
  - **Why:** the Phase 0 open issue.
- **Dead domains are shared across projects in a workspace.**
  - **Why:** a host that doesn't resolve is dead for everyone.
- **Maps lookup's stop words come from the run.**
  - **What changes:** the city, state, country and the run's name noise replace the hard-coded "nashik", "igatpuri", "trimbak", "maharashtra" and "india". For Nashik this adds a few brand words ("stayvista", "saffronstays", "elivaas", "resorts", "mumbai").
  - **Why:** those say where or who runs a venue, not which venue it is. This only affects which Maps result a phone-less venue may match.
- **The AI prompts' phone hint follows the country.** This only changes non-Indian runs.
- **A bare `Config(dict)` no longer defaults to Mumbai.**
  - **What changes:** no city "Mumbai" and no Mumbai centre. Only the legacy importer supplies those.
  - **Why:** "remove hard-coded mumbai"; real configs are unaffected because the presets state them.
- **Tests ported, not deleted:**
  - The phone tests pass `"IN"`.
  - The SERP title test passes the Mumbai run's place words.
  - The prompt byte-identity test states city Mumbai.
  - The platform stubs carry a currency.
  - The enrichment-memo test uses a real organisation (marks now have a foreign key).
  - `test_place_index_is_backfilled…` moved to `test_db.py::test_place_is_indexed_for_a_store_written_before_place_index`, because original stores are now imported, not opened.

## 13. Deviations from the plan, with reasons

- **Core sources stay in `sources/`** (section 1).
- **The `Lead` dataclass is still the organisation record.**
  - `organizations.record` holds it; contacts, keys, people and evidence are normalised beside it.
  - A fully normalised organisation (no record blob) would have needed every source rewritten, and would have put the exact-reproduction guarantee at risk.
  - Venue attributes stay on `Lead`, so core holds hospitality fields. Splitting them out is a Phase 4 question (columns per lead type).
- **Per-field provenance is per write, not per value.**
  - Each write records which source changed which fields, from which URL and when.
  - Contacts record the source that first published each value.
  - Imported legacy rows have no per-field history, so their contacts and evidence say `legacy-import`. That is honest, not invented.
- **Core sources call the hospitality pack's pure text parsers** (prices, amenities) when the vertical is on. This is a known layering compromise, documented in `CLAUDE.md`.
- **The runner is in-process and sequential.** Planning, scheduling, the worker and resumability are Phase 2. `plan()` exists so the planner and estimator can build on it.
- **No `api/` package yet.** It's Phase 3; nothing business-critical lives in the CLI.

## 14. Open issues, carried forward

- **Terms of service vs A.2.4.** `docs/sources.md` flags inherited sources that sit close to "never scrape when a public API exists": Google Maps and Search, Airbnb, Booking.com, Instagram and Facebook. Phase 1 changed none of their behaviour. Whether each stays, moves to an official API, or is retired is your call.
- **Bridging upserts aren't consolidated — measured; recommend leaving them.** An incoming record that matches A by phone and B by place merges into A only, as before. Consolidating after every merging upsert was measured on the frozen stores (section 15): it adds **one** merge on Mumbai and none on Nashik, and that one is wrong. An event planner and a banquet hall share one phone number, but they have different domains and different Maps places. No code change was made; it's still your call.
- **The legacy phone rule merges a venue operator's properties.** On the Nashik replay, VayaVia Divine and VayaVia The Frame share a reservations number and score 70 on name similarity (threshold 60), so they merge. This is unchanged Phase 0 behaviour, pinned by the baseline; raising the threshold is a tuning decision.
- **Migrate the real stores.** `migrate-legacy` was run only into a sandbox. Run it once into your real data directory (it copies the originals; they stay untouched). Until then, `run.py` on the legacy configs stops with that instruction.
- **`filters` has no phase in the plan.** `min_rating`, `must_have` and `exclude_chains` are accepted and reported as not applied. Suggested home: the Phase 4 Records export, since filtering is a view over the project, not a reason to drop data. `suppression_lists` is Phase 3.
- **Phase 2 inherits:**
  - `plan()` and `WorkUnit`;
  - per-unit timing via `store.record_unit` (now actually recorded);
  - the `runs` / `work_units` tables;
  - a DB-backed run record to resume from.

## 15. Continuation (same day): Postgres verified, `fields` honoured

This picks up the open items in section 14.

**Live Postgres.** A throwaway PostgreSQL 16.2 server ran from the `pgserver` wheel in a scratch Python 3.12 venv: no system-wide install, no Docker, TCP on localhost only, deleted afterwards. `psycopg[binary]` (the `postgres` extra) runs on the project's Python 3.14.

| Check | `C.UTF-8` DB | `en_US.UTF-8` DB |
|---|---|---|
| `test_postgres.py` round trip | pass | pass |
| Mumbai, verbatim export + replay dedup vs SQLite goldens | identical | identical |
| Nashik, verbatim export + replay dedup vs SQLite goldens | identical | identical |

**One real divergence found and fixed.** The fuzzy-name rule breaks ties by the locality scan's `(category, score desc, id)` order:
- SQLite compares categories bytewise.
- A Postgres database with a locale collation (the official Docker image defaults to `en_US.utf8`) puts "Decorators" before "DJs".

So a server could have resolved the same tie differently. The real stores happen not to hit it; their category names sort the same both ways. The scan now orders by `category COLLATE "C"` on Postgres. `test_postgres.py::test_locality_scan_orders_categories_bytewise_on_postgres` fails on an `en_US` database without the fix and passes with it. SQLite's SQL is unchanged.

**`fields.include` and unapplied settings.** See section 6. A dry run of "dentists in Austin, US" with `fields: {include: [phones, emails]}` and a 30-minute budget:
- built Google Maps, Google Search and Facebook;
- skipped `linkedin_serp` as "not needed";
- kept the website and Instagram enrichers;
- warned that the time budget, Excel and the CSV switch aren't applied yet.

**Bridging upserts, measured** (scratch only, nothing committed): a replay of both frozen stores with consolidation after every merging upsert.

| Store | Stored today | With consolidation | Extra merges |
|---|---|---|---|
| Mumbai | 1,267 | 1,266 | 1, wrong: an event planner (Andheri) and a banquet hall (Dadar) sharing one phone; different domains and Maps places |
| Nashik | 987 | 987 | 0 |

**Still open, and yours to decide:**
- the terms-of-service review (section 14);
- the VayaVia phone-similarity threshold;
- running `migrate-legacy` into your real data directory. It copies the originals, and wasn't done here because it writes to your per-user application database.

Commits: `fix(db): order the locality scan bytewise on Postgres; real-data characterisation on Postgres`, `feat(config): fields.include switches off sources that only fill unchecked fields; name unapplied settings`, and this report.
