# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A failure-tolerant Python scraper that discovers event-industry businesses from free public sources (Playwright + `requests`, no paid APIs), merges them into one deduplicated record per business in SQLite, scores/ranks them, and exports to CSV and Google Sheets. Despite the directory name, it does **not** use LangGraph — orchestration is the plain `Pipeline` class.

Two run profiles share the codebase, selected by config file:
- `config.yaml` — Mumbai event **vendors** (`profile: vendors`, the default).
- `config.nashik.yaml` — Nashik high-end **venues** (`profile: venues`). Uses separate state: `data/nashik/`, `logs/nashik/`, and its own Sheet tabs (`Nashik Venues` / `Nashik Skipped`). It must never write the Mumbai `Leads` / `Skipped` tabs.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # pyproject.toml is the only dependency list; extras: dev, ai
playwright install chromium

# Tests (all offline — stubs/fakes, no network; `live` tests are deselected by default)
python -m pytest tests/ -q
python -m pytest tests/test_pipeline.py -q                    # one file
python -m pytest tests/test_pipeline.py::test_name -q         # one test
python -m pytest -q -m "not characterisation"                 # fast loop (skips the ~25 s real-data replay)

# Characterisation baseline (see "Characterisation tests" below)
python tests/characterisation/freeze.py                       # freeze DB copies + write real-data snapshots
CHAR_UPDATE=1 python -m pytest tests/characterisation -q      # accept an INTENDED behaviour change

# Running
python run.py --dry-run                                       # plan + time estimate, no requests
python run.py --locality Andheri --category Caterers --source google_maps   # small slice
python run.py --export-only                                   # rescore + re-export DB, no scraping
python run.py --enrich-only | --no-enrich | --no-sheets | --no-headless | -v
python run.py --config config.nashik.yaml --dry-run           # venue profile

# Formatted Excel workbooks from the local CSVs
.venv/bin/python tools/build_leads_workbook.py
.venv/bin/python tools/build_nashik_workbook.py
```

Full runs take many hours at the default polite delays. Don't run several copies in parallel, and don't lower delays casually (Justdial, Instagram and Google SERP block quickly).

## Architecture

**Flow** (`leadscraper/pipeline.py`, `Pipeline.run`): discovery → (venue profile: AI venue classification + brand rollup) → AI name polish → enrichment → AI fallback extraction → finalise (rescore, write CSV, sync Sheets, report). Output is flushed periodically and on exit, including Ctrl-C.

**Core invariant: nothing crashes the run.** `Source.discover` yields partial `Lead`s and must never raise; problems go to `ctx.failures` (`failures.py` → `logs/failures.jsonl`, `data/skipped.csv`, and the Sheet's Skipped tab) with a reason code such as `login_wall`, `phone_hidden`, `no_businesses_parsed`. When a site redesigns, that shows up as a failure reason, not a crash. Keep this property when you add or modify sources.

**Sources** (`leadscraper/sources/`): each module has one `Source` (discovery) or `Enricher` subclass, registered in `sources/__init__.py` (`DISCOVERY_SOURCES`, `ENRICHMENT_SOURCES`). Dependencies come in through a `SourceContext` (config, `HttpClient`, `RateLimiter`, `FailureLog`, `BrowserSession`, store). `Source.scope` sets how often a source runs: `"combo"` (per locality × category), `"locality"`, or `"run"` (for example, sitemap-based stay platforms). Enrichers pick their targets via `targets(store)`. Site-specific selectors sit at the bottom of each source file. A source runs only if it's `enabled` under `sources:` in the config.

**Shared infrastructure**:
- `ratelimit.py` — per-host randomised delays shared across sources, with escalating penalties after 429/403/CAPTCHA. Sources declare hosts via `rate_keys()`.
- `http.py` — retries with backoff, and dead-domain detection (DNS failures are blacklisted after one attempt; persisted in the store).
- `browser.py` — the Playwright session.
- SERP use is rationed: `site:` lookups in the directory sources (`directories.py`, `local_directories.py`) only run with `serp_fallback: true` on that source.

**Data model & dedup** (`models.py`, `store.py`): `Lead.merge` unions lists, fills empty fields, settles conflicting ratings in favour of the higher review count, and never overwrites a value with a blank. `LeadStore.find_match` order: shared phone → same Google Maps place (`place_index`, keyed by the cid parsed from the Maps URL; phones must not contradict) → identical canonical name (`normalize.py` strips legal/category noise) → fuzzy name ≥ 92% in the same locality. A longer incoming name replaces the stored one only when it *extends* it and isn't keyword-stuffed (`normalize.looks_like_seo_title`); otherwise it becomes an "also known as" note. The venue profile adds rules via `DedupOptions`: a shared phone also needs similar names, pins within 250 m merge, and distinctive names merge across localities. The store also tracks seen URLs, enrichment status, and per-unit timing stats used for estimates.

**Profiles branch in several places**: `Config.profile` / `is_venue_run` drives the column layout (`Lead.to_row` vs `to_venue_row`), scoring (`scoring.py`), sort order, dedup options, `Source.assign_locality` / `out_of_region` (geo radius and exclude keywords), and the venue-only logic in `venues.py` (price parsing, venue-type classification, brand detection, tiers Operator/Luxury/Premium/Standard/Unverified). Scoring weights and `venue_tiers:` thresholds are in config. Changing them only needs `--export-only`, not a re-scrape.

**AI layer** (`ai.py`): optional; off unless config enables it (`config.yaml` does) or `--ai` is passed, `--no-ai` forces it off. Name polish sends title-derived names (SERP/directory sources without a Maps record) *and* any keyword-stuffed name, Maps included. Backends: the `claude` CLI in `--print` mode (subscription; slow per call, so it batches) or the Anthropic SDK when `ANTHROPIC_API_KEY` is set. Results are cached in `data/ai_cache.sqlite3`, a USD budget cap disables the layer once hit, and quota errors cause a silent fallback to no-AI. Every call degrades to `None` on failure. `BLOCKED_MODEL_FAMILIES` is never selected, whatever the config says.

**Config** (`config.py`): dotted `get`/`set` (CLI flags in `run.py:apply_overrides` mutate config). Localities have aliases/keywords/centers and categories have queries/directory slugs. Anything added there flows through every source.

## Credentials

The Google service-account key is looked up as: `GOOGLE_SERVICE_ACCOUNT_JSON` (in `.env`) → `~/.config/lead-scraper/credentials.json` → `./credentials.json` (legacy). Keep keys out of the project directory. `.gitignore` excludes credentials, `.env*`, `data/`, `logs/`, SQLite files and `*.xlsx`/`*.csv` (real leads); never commit any of them. The target sheet must be shared with the service account's email as Editor.

## Characterisation tests

`tests/characterisation/` pins today's behaviour so the Phase 1 refactor can prove what it changed. Synthetic goldens (dedup decisions, scores, tiers, sort order, both CSV layouts) always run. Real-data snapshots replay `--export-only` and the dedup rules over frozen copies of the Mumbai and Nashik stores in `data/characterisation/` (gitignored; skipped when absent). The committed snapshots hold only ids, scores, tiers and hashes. Both layers use the frozen configs in `tests/characterisation/baseline_configs/`, not the live YAML. A failing characterisation test is a behaviour change to justify (then `CHAR_UPDATE=1`), not a test to edit.

## Scope notes

`APP-BUILD-PROMPT.md` is the build plan for turning this into a backend application (Section A context + Phases 0–8, one phase per session; Phase 0 is done, see `docs/phase-0-report.md`). It supersedes `REBUILD-PROMPT.md`, of which only Parts 0, 2 and 3 still apply. Follow them only when asked to do that work. The hard boundary applies to the project generally: this tool **generates** leads and must not grow outreach/CRM features (contact status, email sending, sequences, and so on). LinkedIn is never logged into or crawled. Only search-indexed public profile URLs are recorded.
