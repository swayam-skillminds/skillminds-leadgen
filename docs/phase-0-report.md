# Phase 0 report — safety net and known bugs

Date: 2026-10-06 · Branch: `phase-0/safety-net` · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 0

**Result:** the suite is green (267 passed, 2 skipped; it was 229 passed, 1 skipped). Characterisation tests now cover dedup, scoring, tiering and both export layouts. Every Part 0 item is accounted for below.

## 1. Version control

- `git init` on `main`, with a baseline commit of the tree as found. The Phase 0 work is on `phase-0/safety-net`.
- `.gitignore` covers:
  - credentials (`credentials*.json`, `*service-account*.json`) and `.env` / `.env.*` (`.env.example` is still allowed);
  - `data/`, `logs/`, and SQLite files including WAL/SHM;
  - `*.xlsx` and `*.csv`, because they hold real leads; Phase 4 will add an exception for `docs/samples/`;
  - `.venv/` and build artefacts.
- No `credentials.json` and no private key are anywhere in the tree. The service-account key lives at `~/.config/lead-scraper/credentials.json`.
  - `.env.example` still asks for the old key (project `eighth-physics-491407-c6`) to be rotated. That's a manual step in the Google Cloud console and can't be verified from here.

## 2. Dependencies

- `pyproject.toml` is now the single dependency list. **`requirements.txt` is deleted, not generated.**
- Install with `pip install -e ".[dev]"`. Extras:
  - `dev`: pytest;
  - `ai`: the `anthropic` SDK, which was already imported lazily.
- `openpyxl` is now declared; the workbook tools already imported it.
- Pytest config moved into `pyproject.toml`, with markers `live` (deselected by default) and `characterisation`.

## 3. REBUILD-PROMPT.md Part 0 — status of every item

Each item was checked against the current code first. "Live" means a single polite GET to that URL on 2026-10-06.

| Item | Status | Evidence / test |
|---|---|---|
| **0.1** Four sources skipped silently | **Already fixed** | `Pipeline._build` logs every registered source as built or NOT built, with the reason. Tests: `test_visibility.py::test_not_requested_source_says_so_and_says_what_was_requested`, `::test_disabled_source_says_so`, `::test_run_start_names_registered_requested_built_and_skipped`. |
| **0.2** WedMeGood slugs 404 | **Already fixed** | Slugs are read from the live listing HTML and fuzzy-matched; the config slugs are corrected and Florists is `null`. Tests: `test_directories.py::test_resolve_slug_*`, `::test_discovered_slugs_*`. Live: `/vendors/mumbai/` → 200, and `/vendors/mumbai/wedding-catering/?page=2` → 200. |
| **0.3** Listing URLs | **Already fixed, or not fixable within A.2.4** | Per directory below. |
| **0.4** SERP fallback trap | **Already fixed** for `directories.py` (`test_serp_fallback_is_off_by_default`). **Fixed now** for Justdial/Sulekha, which still ran one `site:` query per locality × category. | `test_directories.py::test_local_directory_makes_no_serp_query_by_default[justdial,sulekha]`, `::test_local_directory_serp_runs_when_opted_in[…]` |
| **0.5** Dead domains retried | **Already fixed** | DNS failure and loopback-only resolution are terminal and persisted in `dead_domains`. Tests: `test_network.py::test_dns_failure_raises_dead_domain_after_a_single_attempt`, `::test_loopback_only_host_is_dead_without_a_request`, `::test_known_dead_domain_costs_nothing`, `test_hygiene.py::test_dead_domain_round_trip`. The Nashik DB (Oct 1) holds 23 dead domains. |
| **0.6a** "Access denied" stored as a name | **Already fixed** for new data | Tests: `test_hygiene.py::test_upsert_rejects_an_error_title_lead`, `test_char_synthetic.py::test_error_titles_are_refused`. The 6 legacy rows (ids 27–31, 56) stay in the untouched original DB; replaying it rejects all six (pinned in `golden/real_mumbai.json` → `dedup.rejected_ids`). |
| **0.6b** SEO page titles as names | **Fixed now** (two causes) | See the note after this table. Tests: `test_pipeline.py::test_keyword_stuffed_maps_names_reach_ai_name_polish`, `test_core.py::test_merge_rejects_an_seo_title_that_starts_with_our_name`, `test_core.py::test_looks_like_seo_title`. |
| **0.6c** Exact-name duplicates (73 rows) | **Fixed now** (main cause) | See the note after this table. Tests: `test_core.py::test_same_maps_place_merges_across_localities`, `::test_same_maps_place_with_contradicting_phones_stays_separate`, `::test_a_place_learned_through_a_merge_is_indexed`, `::test_place_index_is_backfilled_for_a_store_written_before_it`. |
| **0.6d** `contact_name` is 0 / 1,333 | **Unblocked, not verifiable offline** | `linkedin_serp` now builds and runs (0.1), but it rides on Google SERP. Coverage stays unknown until a live run. |
| **0.6e** Emails at 13.7%; `facebook` `mbasic` never ran | **Unblocked, not verifiable offline** | `facebook` is enabled and now runs. It needs a live run to measure. |
| **0.7** Dead `source_corroboration` component | **No code change, by design** | Weights must not be retuned. 950 of 1,333 Mumbai leads are single-source in the (pre-fix) DB. Whether the spread widens can only be confirmed after a live run. |

### 0.3 per directory

Each was checked live:

- **WeddingWire:** 403 to a plain client. The directory code already falls back to the browser.
- **ShaadiSaga:** the source is deleted (`test_directories.py::test_shaadisaga_source_is_deleted`). Its host is now an alias of `weddingbazaar.com`.
- **Sulekha:** 200, 551 KB. It works.
- **Justdial:** 200 with a 14-byte body, i.e. still blocked. Getting past this would need evasion, which A.2.4 rules out. The block is recorded as a `blocked` or `no_businesses_parsed` failure, never silently.

### 0.6b: SEO page titles

1. **AI name polish skipped every Maps record.** Every SEO-titled name in the Mumbai DB came from Google Maps (about 104 of them), because owners stuff keywords into the listing name. Keyword-stuffed names (`normalize.looks_like_seo_title`) are now sent to AI name polish too. Renames still need an AI backend; without one, names are unchanged.
2. **`Lead.merge` let a stuffed title overwrite a clean name.** "Royal Caterers | Best Caterers in Mumbai" contains the real name, so it passed the extension check. It now becomes an "also known as" note instead.

### 0.6c: exact-name duplicates

**Analysis of a copy of the Mumbai DB.** Every surviving duplicate pair followed the rules as written: 53 pairs were in different localities and 16 had conflicting phones. Underneath:

- 25 Google Maps places were stored once per locality searched (Flora Store six times).
- Shared phones were added by enrichment *after* both records had been stored.

**Fix.** A new rule: same Maps place → same business, using the place's cid. It runs right after the phone rule and requires that phones don't contradict.

**Result.** Replaying the Mumbai copy cuts exact-name duplicate groups from 30 (73 rows) to 3 (6 rows). The 6 remaining rows have different places and phones, so they are genuinely ambiguous.

## 4. Characterisation tests (`tests/characterisation/`)

| Layer | What it pins | Runs |
|---|---|---|
| Synthetic goldens (`test_char_synthetic.py`) | Dedup/merge decisions (8 vendor + 6 venue scenarios); score breakdowns (6 vendor, 8 venue leads); venue type, tier, points and reasons; sort order for both profiles; the exact CSV headers and every rendered cell for both layouts, plus the UTF-8 BOM and timestamp format | Always (CI-safe) |
| Real-data snapshots (`test_char_real_data.py`) | `--export-only` over a copy of each frozen DB: headers, row count, a hash of every row in export order, every score, and every venue tier. Plus a dedup replay of every stored lead into an empty store: counts, merged clusters by original id, rejected ids | When `data/characterisation/*.sqlite3` exist; skipped otherwise |

The details that make the suite trustworthy:

- **The originals are never opened for writing.** Freezing uses the SQLite backup API with the source opened read-only. Each test then works on a further copy, because `LeadStore` writes its schema on open.
- **The originals were not modified.** After freezing, `data/leads.sqlite3` and `data/nashik/venues.sqlite3` still had their modification times of 18 Sept and 1 Oct.
- **Snapshots contain no lead data.** They hold only ids, scores, tiers, counts and 16-hex row hashes; a scan for names, phones and emails finds none.
- **Snapshots are tied to one baseline.** Each records a fingerprint of the frozen DB it was taken from.
- **Code changes are kept separate from config changes.** The tests use frozen copies of `config.yaml` and `config.nashik.yaml` in `baseline_configs/`.
- **Timestamps are pinned as epoch floats.** The rendered "First Seen" and "Last Updated" columns depend on the local timezone.

Baseline numbers, after the Phase 0 fixes. These are what Phase 1 must reproduce:

| | Mumbai vendors | Nashik venues |
|---|---|---|
| Leads exported | 1,333 | 1,005 |
| Replay: stored / replayed | 1,267 / 1,333 | 987 / 1,005 |
| Replay: merged clusters, rejected | 38, 6 | 17, 0 |

To regenerate after an intended change: `CHAR_UPDATE=1 python -m pytest tests/characterisation`.
To re-freeze (only on purpose, since it replaces the yardstick): `python tests/characterisation/freeze.py --force`.

## 5. Deviations from the plan, with reasons

- **No before/after live run of `--locality Andheri`.** REBUILD-PROMPT asks for one after Part 0. It takes hours at polite delays and hits Google, so it was not started without asking. It's the way to verify items 0.6d, 0.6e and 0.7.
- **The existing DBs gain a `place_index` table the next time a run or `--export-only` opens them.** This is derived data, backfilled once; no lead row changes. That's the same way `dead_domains` and the other tables were added before.
- **Justdial behaviour change.** With `site:` lookups opt-in, Justdial only tries its (currently blocked) listing template. The Nashik run got one Justdial lead last time. Set `serp_fallback: true` on the source to get the old behaviour back.
- **Duplicates already in the stores are not merged in place.** The originals must stay untouched (A.2.6). The Phase 1 data migration replays them through the identity rules, and the characterisation replay already shows the result.

## 6. Open issues, carried into Phase 1

- **No consolidation after enrichment.** When enrichment gives two stored records the same phone, nothing merges them, because `merge_into` never re-runs `find_match`. This belongs to the Phase 1 identity work (A.6 identity keys).
- **No domain-based matching yet.** A.6 calls for a registrable-domain key, but it needs the shared-host blocklist first: `sites.google.com`, `instagram.com`, `youtube.com` and a shared florist site all appear as "websites" for unrelated duplicates.
- **The Nashik dedup replay takes about 23 s** (≈1,000 inserts). The venue rules scan and JSON-parse every row per insert, so `pytest -m "not characterisation"` is the fast loop. This is a Phase 2 performance item.
- **SEO names are only cleaned when an AI backend is available.** There's no rule-based fallback.
- **Your global git `user.email` has a trailing colon** (`swayambhosale@skillminds.in:`). This repo sets a clean local value; the global setting was left alone.
