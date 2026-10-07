# Phase 6a report — lead types, wave 1: `digital_audit` and `hiring`

Date: 2026-10-08 · Branch: `phase-6a/audit-hiring` (see "About the branch") · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 6, items 1 and 2 only

**Result:** Phase 6 items 1 and 2 are complete. Items 3 (`funding_news`) and 4 (`decision_makers`) were not started, as asked.
- The suite is green: 673 passed, 7 skipped, 12 deselected. On `phase-5/ai` it was 583 passed, 7 skipped, 5 deselected.
  - The 7 skips are the opt-in Postgres tests plus one characterisation case that doesn't apply, as before.
  - The 7 new deselected tests are the Phase 6 live smoke tests.
  - `ruff` is clean.
- **Live, today:** every external endpoint was re-verified live before coding against it (A.9.2, section 4).
  - The live smoke tests pass: one real public board per ATS, the current HN thread, an audit of example.com.
  - A small live end-to-end run completed. It exposed two HN matching problems, now fixed (section 5).
- **Postgres:** on a throwaway PostgreSQL 16, the Phase 6 suites (89), the engine suite (48) and the opt-in Postgres tests (6) pass, including migrations 0001 → 0005 over existing rows (section 6).
- **Acceptance:** all five checks pass for both lead types (section 7).
- **Review:** an independent review found 2 high, 5 medium and 7 low issues. All highs and mediums are fixed, and so are 4 of the lows, each with a test that fails without the fix (section 8). The other three lows are open issues (section 11).
- **Baselines:**
  - `git diff phase-5/ai -- tests/characterisation/golden tests/golden` adds only the new `audit_hiring.json`;
  - `tests/characterisation` passes against the frozen real-data copies;
  - the existing four golden workbooks are unchanged. Their sample files differ only in one pinned date, a fix explained in section 3.
- **CI has not run** on this branch, because nothing was pushed.

**About the branch.** You asked for `phase-6a/audit-hiring` from `main`. `main` is still at Phase 2: Phases 3–5 live only on their branches, in a line (`main` → `phase-3/api` → `phase-4/excel` → `phase-5/ai`). Phase 6 builds on all three (services, exports, AI), so I created this branch from `phase-5/ai`, which is `main` plus Phases 3–5 as a fast-forward. Phases 2, 4 and 5 did the same. `main` itself is untouched, and once Phases 3–5 are merged, this branch's diff is exactly Phase 6a.

## 1. `digital_audit` (6.1)

**Inputs.** It audits:
- organisations other lead types found: this run's (`audit: run`, the default) or the project's (`project`);
- an **uploaded list** of businesses or domains: inline `targets` / `domains`, or a CSV/XLSX `targets_file` from the CLI.

`POST /api/v1/audit/targets` and `leadscraper audit targets FILE` turn an upload into config entries. Nothing is stored or run by them.

**Checks** (each toggleable, each its own signal type; points come off a 100-point audit score):

| Check | Signal | Points | Notes |
|---|---|---|---|
| no website | `no_website` | 60 | A social profile or directory listing isn't a website of its own; a `wixsite`/`blogspot` site is still audited |
| site unreachable | `site_unreachable` | 50 | Doesn't resolve, refuses, or answers 404/5xx on both schemes, after one retry |
| no HTTPS / expiring certificate | `no_https` / `cert_expiring` | 20 / 10 | A TLS handshake reads the certificate (`net/tls.py`); invalid, HTTP-only, or expiring within 21 days |
| not mobile-friendly | `not_mobile_friendly` | 15 | No `width=device-width` viewport |
| poor PageSpeed | `slow_site` | 10 | Google PageSpeed Insights v5, mobile, below 50; at most 25 calls a run; key from the secret `pagespeed-api-key` |
| outdated CMS / framework / server | `outdated_tech` | 15 | Our own dated fingerprint rules (`fingerprints.yaml`, 27 technologies): WordPress < 6.4, Joomla < 5, Drupal < 10, Magento 1, AngularJS, jQuery < 3.5, Bootstrap < 5, PHP < 8.2, Apache 2.2, IIS < 8.5, Flash. Each rule's `eol` note gives the dated reason, and a config can override a threshold |
| stale copyright year | `stale_copyright` | 5 | The latest year next to ©, two or more years old |
| broken homepage links | `broken_links` | 8 | Up to 8 same-site links, HEAD (or a streamed GET), robots.txt honoured for each |
| no contact form | `no_contact_form` | 5 | Homepage, then its contact page. Search boxes and email-only signups don't count; embedded form services do |
| no online booking / ordering | `no_online_booking` | 10 | Only where the category expects it (restaurants, clinics, salons, hotels…; extendable). Booking/ordering platforms and phrases count |
| incomplete Maps profile | `incomplete_maps_profile` | 7 | Only for organisations with a Maps listing: no website, phone, address, rating, or under 10 reviews |

**Outputs:**
- **Findings in plain words,** for example "Outdated technology: WordPress 4.9.8, jQuery 1.12.4, PHP 7.2.34." The end-of-life reasons travel in the signal's payload.
- **An audit score.**
- **The `audit` attribute** (`origin: rule`): score, status, findings, stack, PageSpeed, when.
- **Columns:** Site (front, a breakdown), Audit Score, Audit Findings, Site Stack, PageSpeed, Audited.
- **Score:** intent = the points taken off ÷ 100, halving every 180 days.

**Each audit is a snapshot.** A finding that no longer holds is removed. A site audited within `reaudit_after_days` (7) is skipped unless `--force-enrich` is given.

**Honesty rules:**
- A check that couldn't run says nothing.
- A site that refuses us, robots.txt-blocks the homepage, resolves to an internal address, or whose audit failed or was cut by a stop is "not checked". Nothing is written and the previous audit stands.

## 2. `hiring` (6.2)

**Config:** role families (developer, qa, devops, data, it_support, digital_marketing by default; also design, security, erp, product), extra keywords, seniority, `remote: include | only | exclude`, `posted_within_days` (30), `min_roles`, known `boards` as `kind:token`, and per-source settings.

**Sources:**

| Source | What | Unit |
|---|---|---|
| `greenhouse`, `lever`, `ashby`, `workable`, `smartrecruiters` | One company's public ATS board, read through the JSON the ATS serves for career sites | one per configured board |
| `hn_whos_hiring` | "Ask HN: Who is hiring?" via the Algolia HN API; one ad per company | one |
| `careers_pages` | The run's (or project's) organisations' own sites. An ATS board linked or embedded there is read through its API; otherwise role titles on the careers page count | post-discovery |
| `board_search` | Opt-in: a Google (then DuckDuckGo) search per place for ATS board pages | one per place |

**Rules** (`roles.py`):
- **Role:** the title matches a wanted family as whole words. "Senior PHP Developer" is a developer; "Sales Engineer" and "SEO Services" are nothing.
- **Level and age:** the level is wanted, and the posting is recent enough.
- **Place:** the posting names a run place or area, or is remote and open to the run's country. For an Indian run, "Remote - India", "Remote (Global)" and a plain "Remote" qualify; "Remote (US)", "London, UK or Remote", "Remote but Boston based only" and "in select countries" don't.
- **HN ads:** roles are read from the headline or a role list, not the prose.
- **AI:** with the `signal_interpretation` AI feature, ads the rules can't place go to the model, kept only with a verbatim quote from the ad (Phase 5's `ai.features.signals.interpret`).

**Signals and scoring:**
- **Signals:** one `job_posting` per role, keyed by its URL, so next month's run refreshes the row instead of adding one. The payload carries role, family, seniority, location, match kind, board and posted date.
- **Strength:** 0.45 for one open role, +0.1 for each further one, up to 0.95 ("several open tech roles = a stronger signal").
- **Score:** intent = 0.4 + 0.15 per further role, halving every 30 days after the newest posting. A role not seen for 45 days no longer counts as open.
- **Columns:** Open Tech Roles, Role Families, Latest Posting, Jobs Page, Open Roles, Hiring Sources.

**Not used:** Upwork, Indeed, Glassdoor, LinkedIn Jobs (A.2.4), and no country job board (section 4).

## 3. Core changes

| Area | Change |
|---|---|
| **Records stay their finder's** | A lead type that finds a company the project already holds **attaches** to it: `LeadStore.add_signal` / `stamp` add the lead-type tag, run membership and its own evidence. The record, contacts and score stay untouched; only a company nobody holds is `upsert`ed. `docs/lead-types.md` rule 6 |
| Signals | `signals.key` (migration **0005**): a re-found signal refreshes its row. `prune_signals` for snapshots. Consolidation keeps one copy of a keyed signal |
| local_business's view | `LeadStore.owned_by("local_business")`: its CSV, Sheet, report, rescore and enrichment/AI targets cover only the organisations it found or enriched. Writes and matching see the whole project |
| Engine | **Stages:** a unit waits for every unit of a lower `discovery_stage` (hiring and audit uploads are stage 1, after local_business). **`post_order`:** local_business 0, hiring 5, digital_audit 10. `post_scope = "run"` sizes an assessor's estimate by every lead type's expected new organisations. One `RateLimiter` per run, shared by every lead type |
| Network | `HttpClient.probe` (HEAD, then a streamed GET). `public_only` clients refuse internal addresses on every request and redirect hop (`PublicOnlyAdapter`), and so does `net/tls.py`. `LEADSCRAPER_ALLOW_PRIVATE_TARGETS=1` opts in, as for notifications |
| Shared helpers | `leadtypes/toolkit.Kit` (a session's paced public-only client, failure log, robots.txt cache); `core/tables.py` (CSV/XLSX, now shared with suppression lists) |
| Catalog | `LeadType.catalog_sources()`: `GET /catalog/sources` lists the new sources with terms notes, review flags and last live check (`sources/notes.py`) |
| ICP builder | A draft naming `hiring` or `digital_audit` now switches them on; later-phase lead types are still only noted |
| Exports | Exports pass their `generated_at` to a lead type's `score(..., now=)` and to `column_values` (`ExportRecord.as_of`), so a regenerated export agrees with itself. A signal title already starting with its label isn't prefixed again |
| Workbook (a Phase 4 bug) | openpyxl stamped `docProps/core.xml` "modified" with the wall clock at save, so "same table, same bytes" failed when two saves straddled a second. That date is now pinned to `generated_at` |
| Samples | A fifth fixture scenario, `audit_hiring`: fictional Pune shops, their findings, plus ATS, careers-page and HN postings. Golden structure file and `docs/samples/sample-audit-and-hiring.xlsx` |

## 4. External sources verified live (A.9.2), 2026-10-07

| Endpoint | Live answer | Terms |
|---|---|---|
| Greenhouse `boards-api.greenhouse.io/v1/boards/{t}/jobs` | 200; `jobs[]` with `company_name`, `first_published` | Its docs: board data is public, GET needs no auth |
| Lever `api.lever.co/v0/postings/{t}?mode=json` | 200; `createdAt` in epoch ms; no company name | Published postings, public (its API docs) |
| Ashby `api.ashbyhq.com/posting-api/job-board/{t}` | 200; `isListed`, `isRemote`, `publishedAt` | Public posting API for career sites |
| Workable `apply.workable.com/api/v1/widget/accounts/{t}` | 200 for a real account, 404 otherwise | The JSON behind its documented jobs widget; **not a documented API: to review** |
| SmartRecruiters `api.smartrecruiters.com/v1/companies/{t}/postings` | 200 without auth | Its docs now describe API-key/OAuth access: **to review** |
| HN Algolia `search_by_date` + `items/{id}` | 200; October 2026 thread, 236 ads | Public; ~10,000 requests/hour/IP; ads come from the hiring companies |
| PageSpeed Insights v5 | Keyless: **429 "Quota exceeded … Queries per day"** | Official, free; a key is needed in practice |
| Naukri (IN) | — | Terms forbid automated access: **excluded** |
| Arbeitnow (EU/UK, keyless API) | 200 | Its licence forbids commercial use: **excluded** |

Everything is recorded in `docs/sources.md`, with endpoint, method, auth, pacing, terms note and date.

## 5. A live run

`run.py` against a scratch data directory: Bengaluru, India; the Stripe Greenhouse board; the current HN thread; an audit of example.com. It completed in 0.1 min against an estimate of 0.4–0.5 min, with no failures, and the workbook was written.

| | Stripe (Greenhouse) | HN thread |
|---|---|---|
| Matched | 2 Bengaluru roles: "Engineering Manager, Accounting Products" and a systems administrator | Remote ads |

The run exposed two HN problems:
- **Prose counted as openings.** "QA teams" and "SEO-friendly" in an ad's body counted as roles, so a go-to-market specialist, a growth lead and a "part-time, anything" ad were matched.
- **Restricted "remote" counted as open to India.** "Remote but Boston based only" and "REMOTE in select countries" matched.

Both are fixed (`role_text`, `_RESTRICTED`). The rerun kept 14 companies with real tech roles that are open to India or located in Bengaluru (for example "SingleStore … Remote India", "Oscilar … REMOTE (US/…/India)"). The fixed cases are pinned as tests, rewritten as fictional ads.

## 6. Postgres

A throwaway PostgreSQL 16 ran in Docker (`postgres:16-alpine`, bound to 127.0.0.1, stopped afterwards).

| Check | Result |
|---|---|
| `tests/test_digital_audit.py`, `tests/test_hiring.py` on one shared database | 89 passed. Each run test now has its own project and reads only its own rows |
| Engine suite on a fresh database | 48 passed |
| `-m postgres` (round trip, migrations 0001 → 0005 over existing rows, both real stores) | 6 passed |
| Engine + Phase 1 + workbook + services + API suites on **one** database | 104 passed, 1 failed: `test_us_run_end_to_end_parses_us_phones_without_india_pack_failures` expects exactly one run in its project. It fails identically on `phase-5/ai` (checked in a worktree), so it is pre-existing and listed in section 11 |

## 7. Acceptance checks

| Check (each lead type) | `digital_audit` | `hiring` |
|---|---|---|
| Fixture tests for sources, signals, columns and scoring | **Pass.** `tests/test_digital_audit.py`, 34 cases: fingerprints; page facts; every check, including no website, unreachable, blocked, robots, internal address, a blip retried and a stop mid-site; PageSpeed scores, quota, key never recorded; uploads (CSV, XLSX, bare lists, the API); score and decay; runs end to end, re-audits, refusals keeping the previous audit | **Pass.** `tests/test_hiring.py`, 55 cases: role families, seniority, place/remote (including live-shaped ads); every ATS; board errors; HN parsing; careers pages (ATS embeds, titles, menus, "no openings"); AI interpretation (grounded); runs, refresh not duplicate, stages, shared boards, consolidation |
| Isolation (enable or disable it alone; others' output unchanged) | **Pass.** `test_enabling_or_disabling_digital_audit_leaves_local_business_unchanged`, with an upload in the run, and `test_a_crashing_audit_is_contained` | **Pass.** `test_enabling_or_disabling_hiring_leaves_local_business_unchanged` and `test_a_crashing_source_is_contained` |
| Live smoke test behind `@pytest.mark.live` | **Pass.** `test_live_audit_of_example_com` | **Pass.** `test_live_public_ats_board` × 5 and `test_live_hn_whos_hiring` |
| Workbook columns render | **Pass.** Run tests open the exported workbook and check the headers and values; the `audit_hiring` golden file pins its styles, formats and links | **Pass.** Same, including the Jobs Page hyperlink |
| `docs/sources.md` entries dated | **Pass.** `site_audit`, `pagespeed`, `upload` | **Pass.** The five ATSs, `hn_whos_hiring`, `careers_pages`, `board_search`, and the two job boards excluded |

**What the isolation tests compare:**
- local_business's own rows, score columns and its own evidence;
- **its final leads (CSV / Sheet / report) and the organisations its enrichment is handed** (a recording enricher).

Both lead types are run on, off and crashing. The review showed the first version of these tests was vacuous: they compared only tagged rows, with enrichment off. They now fail without the fix (section 8).

## 8. Review and the fixes it led to

An independent reviewer agent read the diff, reproduced its main findings with scratch scripts (offline) and reported 2 high, 5 medium and 7 low findings. Every fix below has a test that fails on the pre-fix code; I checked by restoring the old files, and 15 of the new tests fail there.

| Finding | Fix | Test |
|---|---|---|
| **High:** hiring's companies and audit uploads flowed into local_business's CSV, Sheet and report, and were enriched by it. My isolation tests missed it | local_business works on `LeadStore.owned_by("local_business")` | The isolation tests now compare its final leads and enrichment targets |
| **High:** when budget skips emptied stage 0, a stage-1 unit was silently left pending and the run still ended "completed" | The scheduler re-evaluates stages after a skip | `test_a_later_stage_runs_when_the_budget_skips_the_earlier_one` |
| SSRF: uploads, HN links and redirects could make the worker fetch internal addresses; the TLS check had no guard | Public-only client for the Phase 6 lead types; TLS guard. A refusal is "not checked", never "unreachable" or a dead domain | `test_public_only_clients_refuse_internal_addresses` (×5), `test_the_tls_check_refuses_internal_addresses`, `test_an_internal_target_is_not_checked_rather_than_unreachable` |
| Careers pages: menu and footer service links became false hiring signals | Navigation, header, footer and forms are ignored; a role noun is required; "no openings" means none | `test_careers_pages_list_roles_not_menus_and_respect_no_openings` |
| A transient refusal or failure wiped the previous audit; one timeout claimed "unreachable" | Not-checked outcomes write nothing; one retry first; stop checks inside a site | `test_a_refused_reaudit_keeps_the_previous_findings`, `test_one_blip_is_retried_before_claiming_unreachable`, `test_a_stop_mid_site_leaves_no_half_audit` |
| A board already read wasn't attached to the organisation whose careers page linked it | One fetch per board per run, cached; postings reach every organisation that asks | `test_a_board_read_in_discovery_still_reaches_the_organisation_whose_careers_page_links_it` |
| `targets_file` in an API config could read the worker's files | Refused in API and saved configs (as `runtime.db_url` already is) | `test_targets_file_and_foreign_secrets_are_refused` |
| Low: the PageSpeed key's secret name was unrestricted, so another secret could be sent to Google | Must start with `pagespeed` | Same test |
| Low: consolidation left duplicate signals | Kept once per (lead type, type, key) | `test_consolidation_keeps_one_copy_of_a_signal` |
| Low: each lead type paced a shared site separately | One `RateLimiter` per run | `test_lead_types_share_one_pace_per_site` |
| Low: the deep preset fetched HN threads whose ads the 30-day window drops | Deep scans more careers pages instead | `test_plan_sources_and_depth` |
| Low: a careers site was marked scanned before its board answered | Marked only when its boards answered | `test_a_site_whose_board_failed_is_scanned_again` |
| Low: the estimator ignores stages; robots.txt isn't re-checked on a redirect; DNS rebinding between check and connect | **Not changed;** open issues (section 11) | — |

## 9. Intended behaviour changes, with reasons

None of these touch the characterisation results or the existing golden workbooks.

- **local_business lists only its own organisations.** Before, a project shared with another lead type would put that lead type's organisations in local_business's CSV, Sheet and report and enrich them. With local_business alone, every organisation is its own (untagged legacy rows count as its own), so presets and imports are unchanged.
- **The four existing sample workbooks changed one date.** `docProps/core.xml` "modified" now equals "created" (the export's `generated_at`) instead of the save time, so the same table gives the same bytes.
- **A signal title that already starts with its type's label is not prefixed again** in Top Signal / Other Signals.
- **The ICP builder switches on `hiring` and `digital_audit`** when a draft names them, instead of noting them as later phases. Its test was updated accordingly.
- **The estimate's per-source lines align across lead types.** A local_business-only plan keeps its layout exactly.

## 10. Deviations from the plan, with reasons

- **Branch from `phase-5/ai`, not `main`** (see the top).
- **Fingerprint rules are ours, not an open-source rule set.** Wappalyzer's rules went closed in 2023, and the maintained fork (webappanalyzer) is GPL-3.0. I wrote 27 rules in the same open pattern style, each outdated threshold with a dated `eol` reason, overridable per config. Coverage is narrower than a full rule set: CMS, e-commerce, site builders, JS libraries, PHP and web servers.
- **No country job board in a pack.** "Only where terms allow": Naukri forbids automated access and Arbeitnow's licence forbids commercial use. None qualified, so there is no pack code.
- **PageSpeed needs a key in practice.** The keyless quota answered 429 on the day. Without the `pagespeed-api-key` secret the check records `pagespeed_quota` once and switches off for the run. No key was available here, so a successful live PageSpeed call was not tested; the mocked response shape follows Google's documentation.
- **`hiring` and `digital_audit` attach rather than merge,** and run their discovery after the finders. This is the only way to keep A.3 isolation ("enabling one never changes another's output") inside the organisation-centric model. The cost: a website or name a hiring source knows isn't added to a record local_business holds, though it stays in the signal's payload. Across runs the model is unchanged: local_business merging into an organisation hiring created last month is the A.6 design.
- **Job sources live in `sources/jobs/`, not in packs.** They apply everywhere and aren't `Source`/`Enricher` classes: they yield postings, not partial leads.
- **The careers-page fallback counts undated titles.** A careers page rarely dates its openings. They match as `site` (the organisation's own page), not by place, and fade like any posting once not seen for 45 days.

## 11. Open issues

- **Your terms decisions (A.2.4):**
  - `workable` (an undocumented widget endpoint), `smartrecruiters` (its docs now describe key-based access) and the opt-in `board_search` (Google SERP);
  - the inherited sources from earlier phases, still open.
- **PageSpeed key:** `leadscraper secrets set pagespeed-api-key`; then a live run would confirm a 200 answer.
- **The estimator ignores stages** (review, low). Stage-1 units are simulated overlapping stage 0, so p50/p90 can be a little low when hiring has many boards.
- **Residual SSRF limits:**
  - the address is checked before connecting, so DNS rebinding between the check and the connection is still possible;
  - robots.txt is not re-checked after a redirect or for the HTTP fallback origin.
- **A pre-existing Postgres test-isolation issue:** `test_us_run_end_to_end_parses_us_phones_without_india_pack_failures` assumes it is the only run in its project, so it fails when several suites share one Postgres database. It fails the same way on `phase-5/ai`, and passes alone or on a fresh database.
- **A pre-existing workbook quirk:** `created` / `modified` are written as local time with a `Z` suffix (openpyxl writes naive times as UTC).
- **HN precision is heuristic.** One ad in the live rerun ("Search Atlas") matched SEO from its role list for a product role. The AI path (`signal_interpretation`) is the intended second opinion.
- **CI has not run on this branch.** Merge order: `phase-3/api` → `main`, then `phase-4/excel`, `phase-5/ai`, this branch. `fix/live-check` is still open too.
- **From earlier phases, still open and still yours:**
  - running `migrate-legacy` into your real data directory;
  - checking the real database for the Phase 2 migration bug;
  - a balanced-depth live check;
  - live AI provider checks;
  - opening the samples in Excel, including the new `sample-audit-and-hiring.xlsx`.

Commits on this branch (after `phase-5/ai`):
- `fix(export): pin the workbook's modified date, so the same table always gives the same bytes`
- `feat(core): signals with stable keys, stamping, post-discovery order, link probes, TLS checks`
- `feat(leadtypes): digital_audit and hiring (Phase 6, items 1 and 2)`
- `feat(export): Phase 6 lead types in the workbook; exports age signals to their own moment`
- `fix(hiring): HN roles from the headline and role lists; restricted remote ads; estimate layout`
- `test: Phase 6 run tests read only their own project, and run on the opt-in engine database`
- `docs: digital_audit and hiring (lead types, sources verified 2026-10-07, API, engine stages, README, CLAUDE.md)`
- `fix: findings of the Phase 6a review (high): local_business's output is its own; stages survive budget skips`
- `fix: findings of the Phase 6a review (medium, low)`
- `docs: the review fixes (owned listings, public-only fetching, not-checked audits, careers precision)`
- and this report.
