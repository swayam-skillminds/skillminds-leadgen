# Phase 4 report — Excel output (Overview + Records)

Date: 2026-10-07 · Branch: `phase-4/excel` (see "About the branch") · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 4

**Result:** Phase 4 is complete.
- The suite is green: 482 passed, 8 skipped (it was 457 passed, 8 skipped on `phase-3/api`). The 8 skips are the opt-in Postgres and live tests, as before.
- The export suites, the engine suite and the opt-in Postgres tests also pass against a real PostgreSQL 16 (section 8). That run found a **cross-backend ordering bug** (Postgres JSONB doesn't keep key order), which is now fixed.
- All three acceptance checks pass (section 9). The sample workbooks are in `docs/samples/`.
- An independent review found one crash, which is fixed, and five lesser issues, all fixed (section 10).
- The characterisation baselines are untouched: `git diff main -- tests/characterisation/golden` is empty, and `tests/characterisation` passes against the frozen real-data copies.
- CI has **not** run on this branch, because nothing was pushed (section 13).

**About the branch.** `main` is still at Phase 2: `phase-3/api` was never merged. Phase 4 builds on Phase 3's export service, so `phase-4/excel` was created from `main` and **fast-forwarded to `phase-3/api`**. `main` itself is untouched. Merging `phase-3/api` into `main` then makes this branch's diff exactly Phase 4. `fix/live-check` is still unmerged too; it touches other files.

## 1. What the deliverable looks like

One workbook per run, **exactly two tabs**, in the visual language of `tools/build_nashik_workbook.py` → `Nashik-Venues-Formatted.xlsx`: Arial, navy header row, thin grey rules, banded rows, red–amber–green colour scales, short clickable links.

**Records** (one row per organisation):

| Group | Columns |
|---|---|
| Lead | Score, Name, then the lead types' **front** columns (Category; for venues Tier in its tier colour, Venue Type, Record Type, Managed By) |
| Location | Lead Types, Place (only when the run covers several places), Locality, then the lead types' **body** columns (ratings; venue price, size, flags) |
| Published contacts | Primary Phone, Other Phones, Primary Email, Other Emails, **Inferred Email (unverified)**, Website, Instagram, Facebook, LinkedIn, WhatsApp, Google Maps, the lead types' **contacts** columns (Booking Contact, Listing, Platforms), Address |
| People | Decision Makers ("name (title)"), Decision Maker Profile |
| Scores | Fit, Intent, Reach (0–100), Score Reasons |
| Signals | Top Signal, Signal Date (green within 30 days, amber within 90), Signal Link, Other Signals |
| Detail | the lead types' **tail** columns (amenities, tier reasons, roles hiring …) |
| Provenance | Sources, Src #, Notes, Change (New / Updated / Unchanged in this run), Suppressed, First Seen, Last Seen, Org ID |

The Records tab also has:
- the header row, the score and the name frozen; an autofilter; widths tuned per column kind (with per-column overrides);
- colour scales on Score, Fit, Intent and Reach;
- phones and IDs written as text (`@`);
- every text cell written as a literal string, and links for http(s) URLs only;
- a column only when it applies. Columns the run's `fields.include` leaves out are absent. Optional columns (every lead type's, socials, signals, people, inferred) appear only when some record has a value. An empty export keeps the columns that explain the layout.

**Overview** absorbs the reference's Overview, Data Quality and Legend tabs:
- **Title and KPI tiles:** records, new, updated, with phone, with email, with signal (when the run looks for signals), average score.
- **Run summary:** project, config, run and status, date (in the run's timezone), duration, places and areas, country, lead types, sources used (with found counts), work units (done / failed / skipped), depth and budget, AI on/off and spend (from the `ai_usage` ledger), score weights, scope, suppression and filters, sort order, generated time.
- **Results:** new vs updated vs unchanged; top 10 leads; counts by lead type, by place or locality, by each lead-type breakdown column (category; for venues tier in tier order and venue type), by signal type, and by score band.
- **Data quality:** coverage % per field (any contact, phone, email, website, address, socials, maps, decision-maker, inferred email, rating, price, signal) with a colour scale.
- **Column legend:** every non-obvious column in Records order, including how scores combine.
- **Empty export:** "WHY THERE ARE NO RECORDS", built from the run's own record: nothing new since run N, suppression or filters, a non-completed status with its error, units that never finished or found nothing, skipped units, and the most frequent failures.

Open `docs/samples/` to see all four kinds (section 7).

## 2. One column model for XLSX, CSV and JSON (4.4)

`services/exports.build_table` reads the database into one `ExportTable`, and three renderers draw it:

| Module | Role |
|---|---|
| `export/records.py` | `ExportRecord`: the merged `Lead` plus lead types, signals, people, inferred contacts, first/last seen, the run's change, suppression flags, place, combined score. Scoring and ordering (section 3). |
| `export/columns.py` | `Column` and `assemble()`: core columns + each lead type's `columns()` / `column_values()`, `fields.include`, optional columns, header clashes between lead types (`Category (Hiring)`). |
| `export/table.py` | `ExportTable`, plus the **CSV** (Records headers; dates in the run's local time; formula-looking text quoted with `'`) and **JSON** renderers. The JSON has `columns` (key, header, kind, group), `records` (`values` by column key, scores with reasons and per-lead-type contributions, signals, people, inferred contacts, change, suppressed) and `summary` (the Overview's numbers). |
| `export/summary.py` | The Overview's content as data, shared by the workbook and the JSON. |
| `export/excel.py` | The workbook. |
| `export/outputs.py` | Which files a run writes, and where. |

**The lead-type contract grew** (`leadtypes/base.py`, documented in `docs/lead-types.md`):
- `ColumnSpec` gains `placement` (front / body / contacts / tail), `width`, `number_format`, `breakdown`, `palette` (value → font colour) and `link_label`.
- `LeadType` gains `column_values(org, cfg)`.
- `local_business` now contributes **only its own columns** (`leadtypes/local_business/columns.py`): category and ratings, plus WedMeGood only with the events vertical; on a venue run, the venue itself. The values are read from the legacy row (`Lead.row_for`), so the workbook and the run's CSV never disagree.

**What stays legacy.** The run's own CSV (`runtime.csv_export`, now behind `output.csv`) and its Google Sheet keep `Lead.to_row` / `Lead.to_venue_row` and `db.store.sort_leads`. That is what the Mumbai and Nashik tabs use, and what the characterisation suite pins.

## 3. Scores, signals and order

- **Every lead type an organisation belongs to scores it** (`LeadType.score` gets only that lead type's signals), and `core.scoring.combine` folds the parts with the run's weights.
  - **Weights renormalise per run, not per organisation:** a component some lead type in the export scores counts 0 for an organisation without it.
  - Per organisation, a news-only company with no fit and almost no reach had ranked first on intent alone (80). It now scores 35, and a business with no hiring signal gets intent 0 rather than having its other components scaled up.
- **Reach is core:** an organisation no reach-scoring lead type found gets `core.scoring.contact_reach`, the channel weights `local_business` already used, now one constant in `core`.
- With `local_business` alone, all of this equals the legacy rescore. `test_legacy_rankings_are_unchanged` checks it on the venue scenario's fit-only weights.
- **Signals:** the strongest unexpired one leads ("Top Signal"); strength halves every `SignalSpec.half_life_days`. Recency is highlighted by a conditional format judged when the book is opened (`TODAY()`), so it stays true as the book ages.
- **Order (`output.sort`):**

  | Value | Order |
  |---|---|
  | `auto` (default) | Score, highest first, **grouped by place** when the run covers several places (places in the config's order) |
  | `score` | Score, highest first |
  | `place` | Place, then score |
  | `area` | Place, area, then score |
  | `category` | The first breakdown column (category, or tier for venues), then score |
  | `legacy` | The run profile's own order (`sort_leads`: locality and category, or venue tier) |

  Ties are broken by name, then id, so the order is stable.

## 4. Runs write their deliverable

- `runner.execute` writes the run's files **from the database** once a run has ended (`services/exports.write_run_outputs`):
  - `<project>-run<N>.xlsx` when `output.excel` is on (the default), `.json` when `output.json` is on;
  - next to the CSV (a new config's own project folder) or in `runtime.export_dir`;
  - written through a temporary name, recorded in `runs.files`, and printed by the CLI.
- A paused, cancelled or failed run gets a workbook of what it has; previews and dry runs get none. A failure here is logged and never touches the run.
- `output.csv` now gates the legacy CSV and `skipped.csv`, which used to be "still written until Phase 4". The presets keep `csv: true`, and a config without an `output` section keeps the old behaviour.
- `python run.py --export-only` also writes the project's workbook (and JSON).

## 5. Also in this phase

- **Google Sheets writes `RAW`** (`export/sheets.py`). With `USER_ENTERED`, Sheets parsed "+91 93200 22422" as a formula: the `#ERROR!` phones the old tools worked around. It would also have run a scraped `=HYPERLINK(...)`. Numbers stay numbers. Test: `test_sheets_sync_writes_raw_values`.
- **`filters.min_rating` and `filters.must_have` are applied** to exports (workbook, CSV, JSON), as the Phase 1 report suggested: "filtering is a view over the project".
  - A rating below the minimum, or a missing must-have, leaves the organisation out. Unrated organisations stay. The Overview counts what was left out.
  - `filters.exclude_chains` (there is no chain detection yet) and `icp` remain in `RunConfig.unapplied()`. Every `output.*` setting is now applied, so those notes are gone.
- **UI hints** for `output.*` and the two filters (`/schema/run-config`).
- **OpenAPI examples** for the export responses show the new CSV and JSON shapes.
- The old `tools/build_leads_workbook.py` / `build_nashik_workbook.py` are marked superseded and kept as the style reference (they read the legacy CSVs and carry the editorial "zones").

## 6. Fixed: lead-type order differed on Postgres

- **Cause:** run snapshots are stored as JSON, which on Postgres is JSONB, and JSONB doesn't preserve key order. A snapshot read back there lists `lead_types` in another order than it was written, so the export put the lead types' columns in a different order than on SQLite.
- **Also affected:** a run resumed by a worker on Postgres would open its lead types in that other order.
- **Fix:** `RunConfig` puts `lead_types` into the registry's order (built-ins first, then plugins in registration order), whatever the document says.
- **Tests:** `test_lead_types_keep_one_order_whatever_the_document_says`. The golden tests now pass on both backends.

## 7. Samples (`docs/samples/`)

`tools/build_sample_workbooks.py` builds them from `tests/workbook_fixtures.py`. **Fictional data only:** invented names, `.example` domains, phone numbers in unallocated (India, `0000` exchanges) or fictional (US 555-01xx) ranges, and LinkedIn URLs such as `/in/example-person-0001`.

| File | Scenario |
|---|---|
| `sample-local-business.xlsx` | Dentists and caterers in Pune and Mumbai: 10 records, grouped by place, one failed work unit |
| `sample-three-lead-types.xlsx` | Austin software firms: `local_business` plus two **fixture** lead types (`demo_hiring` with job-posting signals, `demo_funding` with funding rounds), decision-makers and one inferred email |
| `sample-venues.xlsx` | The hospitality vertical with the Nashik preset's settings: tiers, prices in ₹, an operator, a non-venue, AI spend, a skipped unit |
| `sample-empty-run.xlsx` | A run that found nothing, with failures, explaining why |

`.gitignore` excepts `docs/samples/*.xlsx` and nothing else. `test_samples_are_current` fails when a sample no longer matches the golden files.

## 8. Postgres

As before, a throwaway PostgreSQL 16 ran locally in Docker (`postgres:16-alpine`, bound to 127.0.0.1, removed afterwards):

| Check | Result |
|---|---|
| `tests/test_workbook.py`, `tests/test_services.py`, `tests/test_api.py` | 47 passed (after the review fixes). The first attempt failed two workbook tests, which found the bug in section 6. |
| The engine suite (`LEADSCRAPER_TEST_ENGINE_DB_URL`) | 48 passed on a fresh database |
| `-m postgres` | 6 passed |

The opt-in engine suite needs a **fresh** database. Run a second time against the same one, or after the API suite (whose end-to-end run leaves learned `unit_stats` behind), its estimator tests see `learned` where they expect `prior` and fail. This is how the suite has always worked, and Phase 4 doesn't touch `unit_stats`. Run it on its own throwaway database, as `CLAUDE.md` says.

## 9. Acceptance checks

| Check | Status | Evidence |
|---|---|---|
| Golden-file tests for workbook structure: tabs, headers, styles, number formats, hyperlinks | **Pass** | `test_workbook_matches_its_golden_file[…]` pins each workbook as JSON (`tests/golden/workbooks/*.json`): sheet order, active sheet, dimensions, freeze panes, autofilter, tab colours, gridlines, merged ranges, column widths, conditional formats (ranges, formulas, scale colours), and every non-empty cell's value, type, number format, font, fill, alignment and hyperlink. `WORKBOOK_UPDATE=1` accepts an intended change. Readable rule tests sit beside them: `test_exactly_two_tabs_with_the_reference_style`, `test_phones_and_ids_are_text_and_links_are_clickable`, `test_text_is_never_a_formula_and_links_are_http_only`, `test_output_is_deterministic` (two separate databases, identical description), `test_csv_and_json_share_the_records_column_model`. |
| Fixtures cover `local_business` only, a three-lead-type mix, venues, an empty run | **Pass** | The four scenarios of `tests/workbook_fixtures.py`. Each has a golden file and its own tests: sort and grouping, signals / people / inferred email / per-run weights, tier colours and ₹ format, the empty-run explanation. |
| Sample workbooks the user can open, under `docs/samples/`, from fixture data only | **Pass** | Four files, built by `tools/build_sample_workbooks.py`, kept current by `test_samples_are_current`. They were opened with openpyxl and rendered with macOS Quick Look; the Overview of each was checked visually. |

Phase 4's own requirements, item by item:

| Requirement | Where |
|---|---|
| Records columns: core + each enabled lead type's `columns()` + only the selected fields | `export/columns.assemble`; `test_fields_the_run_left_out_have_no_columns` |
| Scores, top signals, decision-makers, provenance, first/last seen | Section 1; `test_three_lead_types_signals_people_and_inferred_contacts` |
| Header styling, frozen header and first column, autofilter, widths per type, hyperlinks, colour scales, signal recency | `export/excel.write_records`; golden files |
| Phones and IDs as text; the Sheets `#ERROR!` problem must not recur | `@` text cells; Sheets now `RAW` |
| Sort configurable; default score desc, grouped by place | `output.sort`; `test_records_sort_by_score_grouped_by_place_and_other_orders` |
| Inferred contacts in their own labelled column | `Inferred Email (unverified)`; asserted absent from the published columns |
| Overview: run summary, results, data quality, column legend | Section 1 |
| Deterministic, from the DB only, any lead-type mix, zero results explained | `test_output_is_deterministic`, the empty scenario |
| CSV and JSON share the column model; Sheets optional behind the existing flow | Section 2 |

## 10. Review and the fixes it led to

An independent review of the Phase 4 diff (a separate reviewer agent, reading code) reported findings in five groups. Each was checked against the code. Each fix has a regression test, and each new test fails without its fix (checked by stashing the fixes).

| Finding | Fix | Test |
|---|---|---|
| **A control character in scraped text broke the whole workbook.** openpyxl raises `IllegalCharacterError` on `\x0b`, `\x0c`, `\x00` …. The store cleans names and addresses, but notes, descriptions and signal titles can carry them, so one bad note meant an API 500 and a run with no workbook. | `_literal` strips them and caps text at Excel's 32,767 characters. Links over 2,000 characters stay text (Excel would ask to repair the file). | `test_control_characters_in_scraped_text_never_break_the_workbook` |
| **`--export-only` scored the workbook with the last run's snapshot**, not the edited config, so "a weights or `venue_tiers` change only needs `--export-only`" held for the CSV and Sheet but not for the workbook. | `build_table` / `export` take a `config` override; `--export-only` passes the live config. | `test_export_only_scores_with_the_current_config` |
| **One failing format stopped the rest**, and the table was built once per format. | `write_files` renders one table into each format separately, logs a failure and removes its `.partial` file. | `test_one_failing_format_does_not_stop_the_others` |
| **Large projects:** facts were read for the whole project and filtered in Python, and `scope=new` bound one parameter per new id (it could pass Postgres's bind limit on a big first run). | Facts are read for the exported organisations only, in chunks of 500. `scope=new` selects the run's members and keeps the new ones. | Existing scope/diff tests, on SQLite and Postgres |
| **Not byte-deterministic:** zip entries were stamped with the wall clock at save time. | Every entry is dated `generated_at`. | `test_the_same_table_gives_the_same_bytes` (shifts the clock between two exports) |
| An unknown timezone name fell back to UTC in Records but was still printed in the Overview; a duplicated line in `base.py`. | `_timezone` validates the name; the line is removed. | `test_an_unknown_timezone_falls_back_to_utc_everywhere` |

## 11. Intended behaviour changes, with reasons

None of these touch the characterisation results.

- **Export CSV and JSON have the Records layout** (`Score,Name,Category,…`), not the legacy one Phase 3's export used. Phase 4.4 asks for one column model. The legacy layout lives on in the run's CSV and Sheet.
- **A run writes its workbook** (`output.excel`, default on), and **`output.csv: false` now means no legacy CSV**. New configs default to `csv: false`, so they get the workbook instead. The presets set `csv: true` and are unchanged.
- **Google Sheets is written `RAW`.** Phones now arrive as text, and nothing scraped runs as a formula.
- **`filters.min_rating` / `must_have` apply to exports.**
- **`local_business.columns()` lists only its own columns,** so `/catalog/lead-types` shows them. Core columns belong to the export.
- **Export scores combine every lead type an organisation belongs to, renormalised per run.** With only `local_business`, scores are identical to before.
- **`RunConfig` orders `lead_types` by the registry** (section 6).
- **`RunConfig.unapplied()`** no longer names `output.*`, `filters.min_rating` or `filters.must_have`.

## 12. Deviations from the plan, with reasons

- **The Overview holds values, not formulas.** The reference workbook computed its Overview with `COUNTIFS` over the data tab. Here the figures are computed from the database when the book is generated:
  - openpyxl cannot store a formula's result, so formulas show as blank or zero in previewers, pandas and some mobile viewers until recalculated;
  - Phase 4.3 asks for output generated from the DB, and testable;
  - it keeps "no live formulas" as a rule for the whole book.

  The cost: filtering Records doesn't update the Overview. The only formulas left are conditional-format rules (banding, signal recency).
- **The reference's editorial "zones" (Nashik territory groups) are not carried over.** They are hand-written groupings for one region. Grouping by place and the place / locality breakdowns are the general form. The old tool still has them.
- **Frozen panes: the header, the score and the name** (`C2`), as the reference froze its leading columns, rather than literally only column A.
- **"Grouped by place" is an ordering** (places in config order, best first within each), not separator rows. Separator rows would break the autofilter and sorting in Excel.
- **The three-lead-type fixture uses two fixture lead types** (`demo_hiring`, `demo_funding`, in `tests/workbook_fixtures.py`) because the real ones are Phase 6. They exercise the real contract: their own columns, signals, scoring, placement and a breakdown.
- **Filters are applied to exports only, not to the run's legacy CSV/Sheet:** the workbook is the deliverable, and those keep their fixed layout and content.
- **A.9.2 (re-verify sources live):** no source was coded against, and no live run was made. This phase only reads the database.

## 13. Open issues, carried forward

- **CI has not run on this branch:** nothing was pushed. The golden descriptions are OS-independent (dates are rendered in the run's timezone, not the machine's; `tzdata` covers Windows), but a CI run on macOS, Windows and Linux should confirm them before merging. Pushing is yours to do or ask for.
- **Merge order:** `phase-3/api` → `main`, then `phase-4/excel`. `fix/live-check` is still open too.
- **Your uncommitted edit to `APP-BUILD-PROMPT.md`** (the Phase 5 "Extraction techniques" section) is left untouched in the working tree. It isn't part of these commits.
- **`organizations.score`** (the API's list sort) is still the store's write-time `local_business` score. For organisations several lead types found, it can differ from the export's combined score. That matters from Phase 6 on, when the store should score with every lead type too.
- **Records rows are 15 pt high,** as in the reference, so wrapped text (score reasons, notes) shows one line until a row is expanded. Previewers that auto-fit, such as Quick Look, show it whole.
- **Not exercised:** opening the samples in Excel and LibreOffice. They were checked with openpyxl and macOS Quick Look. Google Sheets `RAW` was tested against a fake worksheet only.
- **Still not applied:** `filters.exclude_chains`, `icp` (`RunConfig.unapplied()` names them).
- **From earlier phases, still open and still yours:**
  - the terms-of-service review of the inherited sources;
  - the VayaVia phone-similarity threshold;
  - running `migrate-legacy` into your real data directory;
  - checking the real database for the Phase 2 migration bug (Phase 3 report, section 9);
  - a balanced-depth live check.

Commits on this branch (after `phase-3/api`):
- `feat(leadtypes): Records columns per lead type -- placement, formats, breakdowns, column_values`
- `feat(export): the deliverable workbook -- Overview + Records -- with CSV and JSON on one column model`
- `test: golden workbook structure for four fixture runs; sample workbooks in docs/samples`
- `docs: the workbook (API exports, engine run files, lead-type columns, README, CLAUDE.md)`
- `fix: findings of the Phase 4 review`
- and this report.
