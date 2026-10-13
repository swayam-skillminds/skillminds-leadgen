# Phase 8 report — lead types, wave 3: `tech_stack` and `app_store`

Date: 2026-10-13 · Branch: `phase-8/techstack-appstore` (see "About the branch") · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 8

**Result:** Phase 8 is complete, and with it every lead type in A.7.
- The suite is green: 953 passed, 7 skipped, 25 deselected. On `phase-7/tenders-registrations` it was 908 passed, 7 skipped, 22 deselected.
  - The 7 skips are the opt-in Postgres tests plus one characterisation case that doesn't apply, as before.
  - The 3 new deselected tests are the Phase 8 live smoke tests.
  - `ruff` is clean.
- **Live, today:** every endpoint was checked live before coding against it (A.9.2, section 4).
  - That check set the defaults. Apple data comes from the iTunes Search API only: Apple's site terms forbid page-scraping, and robots.txt disallows its review feeds. Google Play listing pages are read one by one; robots.txt disallows Play's search, so it is never used.
  - The three live smoke tests pass: an iTunes search, a Play listing, a WordPress site fingerprinted.
  - Three live end-to-end runs (section 5) found six problems, all fixed.
- **Postgres:** on a throwaway PostgreSQL 16 (section 6):
  - the eight Phase 6–8 lead-type suites pass on one fresh database (354);
  - the opt-in Postgres tests pass (6);
  - the engine suite's estimator test fails there on this branch and on the Phase 7 tip alike. A second engine test is flaky there (section 11).
- **Acceptance:** all five Phase 6 checks pass for both lead types (section 7).
- **Review:** an independent review found 2 high, 4 medium and 8 low issues. All are fixed. The 11 new regression tests, and the updated crash test, all fail on the pre-fix code (section 8).
- **Baselines:**
  - `git diff d42aa45 -- tests/characterisation` is empty;
  - the seven existing golden workbooks and samples are byte-identical;
  - the new pair is `techstack_appstore.json` and `sample-tech-stack-and-app-store.xlsx`.
- **CI has not run** on this branch, because nothing was pushed.

**About the branch.** You asked for `phase-8/techstack-appstore` from `main`.
- `main` is still at Phase 2. Phases 3–7 and both live checks live only on their branches, in a line ending at `phase-7/tenders-registrations`.
- Phase 8 builds on all of them (services, exports, AI, the attach-don't-rewrite store calls, `Kit`, digital_audit's fingerprinter), so I created this branch from the Phase 7 tip (`d42aa45`), as Phases 2 and 4–7 did.
- `main` itself is untouched. Once the earlier branches are merged, this branch's diff is exactly Phase 8.

## 1. `tech_stack` (8.1)

**Config:**
- `technologies`: names from the fingerprint table, validated case- and punctuation-blind ("next.js" = Next.js). Empty = any technology, but only outdated versions.
- `outdated_only`: only outdated versions of the named ones count.
- `thresholds`: technology → the oldest version still current (overrides the table).
- `check`: `run` (default) / `project` / `targets`: whose sites besides the uploads.
- Uploads: `targets`, `domains`, and a CSV/XLSX `targets_file` (CLI only, refused through the API, like digital_audit's).
- `recheck_after_days` (30), `create_new`, `max_orgs`, `workers`, per-site delays.
- `sources.footprints`: an opt-in web search (below).

**Candidates, each confirmed on the site itself:**

| Source | What | Default |
|---|---|---|
| `site_fingerprint` | The run's (or project's) organisations' own homepages, in post-discovery (`post_order` 8). robots.txt, then HTTPS, then HTTP | **on** |
| `upload` | The uploaded list, written into the project, then checked | on when given |
| `footprints` | A web search per place and technology for the phrases its users show: `"powered by Shopify" "Pune"`. Each result's homepage is fingerprinted; the technology's own sites, social profiles and directories are never candidates | **off**: Google's terms restrict automated queries |

**The rules** (`sources/web/fingerprints.yaml`, shared with digital_audit, section 3):
- 44 technologies: CMS, shops, site builders, frameworks, libraries, languages, servers.
- 18 are new: Angular (with `ng-version`), Vue.js, React, TYPO3, Ghost, HubSpot CMS, Odoo, Duda, BigCommerce, Salesforce Commerce Cloud, Adobe Experience Manager, SharePoint, Laravel, CodeIgniter, Rails, Django, Classic ASP, ColdFusion.
- New thresholds were checked against endoflife.date's API today:
  - Angular below 20 (19 ended May 2026);
  - Vue 2 (ended Dec 2023);
  - TYPO3 below 13 (12 ended Apr 2026);
  - SharePoint 2013 (ended Apr 2023).
- Existing rules gained `vendor` sites and search `footprints`.

**Signals and scoring:**
- `technology` ("Runs: Shopify") and `outdated_technology` ("Outdated technology: WordPress 4.9.8"), keyed `tech:<name>`. The payload holds version, category, the dated reason, the evidence, and whether the run named it.
- **Snapshot.** Each check replaces the last one's signals. The whole stack is the `tech_stack` attribute.
- **Honesty.** A site that refused us, robots.txt-blocked its homepage, didn't answer or resolved to an internal address writes nothing. A technology seen without a version is never called outdated.
- **Score:**
  - *fit* = 0.6 for running a named technology, +0.1 per further one (max 0.9), halving yearly. None when the run names no technology.
  - *intent* = the strongest outdated version, 0.7 halving every 180 days, +0.05 per further one.
- **Columns:**
  - Technology and Tech Status at the front (breakdowns; Outdated in red);
  - Tech Found, Outdated Tech (with reasons) and Technologies (the whole stack) in the body;
  - Tech Evidence and Tech Checked at the tail.

## 2. `app_store` (8.2)

**Config:**
- `keywords`: App Store searches in the run's storefront.
- `genres` / `exclude_genres` (Games by default).
- `rating_ceiling` (3.5), `min_ratings` (5), `stale_months` (18), `min_complaints` (2).
- `installs` (Play's download band), `max_ratings` (100,000).
- `scope`: `country` (default) / `place` / `storefront`.
- `apps`: store links, Apple ids, Play packages.
- `check_sites`: `run` / `project` / `none`.
- `create_new`, and per-source settings.

**Sources:**

| Source | What | Default |
|---|---|---|
| `itunes` | Apple's iTunes Search API, `/search` by keyword and storefront, `/lookup` by id; keyless; paced 3.5–5 s (Apple: about 20 calls a minute) | **on**, marked for review |
| `google_play` | Public listing pages (`/store/apps/details`): JSON-LD rating and count, "Updated on", the download band, the App support section (developer site, email, phone, legal name, address), and the reviews it shows. It is read without relying on Google's generated class names | **on**, marked for review |
| `listed` | The apps a config names | on when given |
| `site_apps` | App Store / Play badges on the organisations' own homepages (robots.txt honoured), in post-discovery (`post_order` 6) | **on** |

**Where Play listings come from:**
- a package the config or an organisation's site names;
- an App Store app's bundle id, kept only when the listing names the same developer website (the developer's own site, never a shared host).

Discovery and the sites each have `max_listings` Play reads. Play search is never used.

**Which apps count:**
- Not an excluded genre, inside the download band (the Play twin's too), and rated by at most `max_ratings` people (national brands).
- In scope. `country` needs evidence the developer is in the run's country:
  - Play's developer address;
  - the country's domain (`.in`, `.co.uk`);
  - or the app naming the country or a run place.
- A storefront lists apps from everywhere, which is why `country` is the default.
- Apps the config names, or a site links as its own, skip these filters.

**A site-linked app is the organisation's own** when its developer's site is the organisation's, or the app or its developer carries the organisation's name ("Baner Bakes Order", built by an agency). A delivery platform a café links is judged like any other app.

**Findings, one signal per app and type** (keyed `<store>:<app id>:<type>`):

| Type | When | Strength |
|---|---|---|
| `low_rating` | Rated at or below the ceiling by at least `min_ratings` (Apple's "0" for an unrated app is no rating) | 0.5–0.9, lower rating = stronger |
| `stale_app` | No update for `stale_months` | 0.5, +0.05 per further six months (max 0.85) |
| `review_complaints` | At least `min_complaints` low-star reviews on the Play listing naming a theme: crashes, login / OTP, slow, payments / refunds, bugs, hard to use, no support, ads, not updated. Each comes with a quote; reviewers' names are never read. With `signal_interpretation`, reviews the rules can't place go to the model, kept only with a verbatim quote | 0.45 + 0.1 per theme (max 0.75), scaled down for an app rated above the ceiling |

**Snapshots.** An app that has become healthy loses its signals; other apps' signals on the same organisation stay.

**Organisations:**
- The developer's website resolves the app (Apple's `sellerUrl`, Play's developer site).
- One the project holds is stamped, its record untouched (rule 6).
- A developer nobody holds becomes an organisation with the contacts Play's listing publishes for it.

**Score:** *intent* = the worst finding, halving every 120 days, +0.05 per further finding.

**Columns:**
- App Issue at the front (a breakdown: Low rating, Not updated, Complaints);
- App, App Store, App Rating, App Ratings, Installs, App Updated and App Link in the body;
- App Complaints, Flagged Apps and App Developer at the tail.

## 3. Core changes

| Area | Change |
|---|---|
| Fingerprints | `leadtypes/digital_audit/fingerprint.py` + `fingerprints.yaml` moved to `sources/web/` (lead types never import each other). digital_audit keeps a re-export and its detections. Per-rule `verified`, `vendor`, `footprints`; `page_evidence()`; `rule()` / `canonical()` name lookup; an HTML pattern may capture a version (only Angular's does) |
| Uploads | `core/uploads.py`: the upload reader digital_audit had, shared. digital_audit's `parse_targets` wraps it, and its behaviour is the same |
| Store | `set_attribute(note_run=)`: an attribute write can leave run membership alone (section 8) |
| API / services | `targets_file` refused for `tech_stack` as for `digital_audit` |
| CLI | `leadscraper audit targets FILE --lead-type tech_stack`. `run.py` exits 0 for a completed run without local_business; 1 when a lead type failed (section 9) |
| AI | `ai/features/review_themes.py`: grounded complaint themes under `signal_interpretation` (task `signals`, same cap and ledger) |
| Registry | Order: local_business, hiring, funding_news, tenders, registrations, **app_store**, decision_makers, **tech_stack**, digital_audit. `post_order`: app_store 6, tech_stack 8 |
| ICP builder | Every A.7 lead type is now built. The web/app offering adds `tech_stack`; ERP already named it. A draft naming an unknown key is still only noted |
| Catalog | Notes for `site_fingerprint`, `footprints`, `itunes`, `google_play`, `listed`, `site_apps`; `upload` now names both lead types |
| Samples | A ninth fixture scenario, `techstack_appstore`: fictional Pune shops, an outdated WordPress, a current Shopify, a Magento 1 shop found by footprints, a developer with an App Store app and its Play twin, a dentist's app, a bakery's site-linked app. Golden file and `docs/samples/sample-tech-stack-and-app-store.xlsx` |

## 4. External sources checked live (A.9.2), 2026-10-13

| Endpoint | Live answer | Terms → decision |
|---|---|---|
| iTunes Search API `itunes.apple.com/search`, `/lookup` | 200; `trackId`, `sellerName`, `sellerUrl`, `averageUserRating` (0 when unrated), `userRatingCount`, `currentVersionReleaseDate`, `bundleId`, `primaryGenreName`, `description`. A `genreId` filter is ignored, so genres are filtered here | Apple's documentation: "approximately 20 calls per minute"; its terms govern promotional content (none kept). robots.txt disallows `/search*` for crawlers; the API is Apple's "means purposely made available" → **on, marked for review** |
| App Store web pages, customer-review RSS | Pages 200 (a few reviews shown); RSS 200 but empty | Apple's site terms forbid page-scraping; robots.txt disallows `/*/rss/*` → **not used** |
| Google Play `/store/apps/details` | 200: JSON-LD rating 4.49 from 278,696, "Updated on Oct 10, 2026", "10M+ Downloads", the developer's site, email, phone, address, 3 reviews | robots.txt allows it; Google's terms forbid automated access against robots.txt → **on, marked for review** |
| Google Play `/store/search` | 200 | robots.txt **disallows** it → **never used** |
| endoflife.date API | 200 for 17 products (Odoo and Ghost: 404) | Used to date the thresholds, not at run time |
| Google / DuckDuckGo SERP (footprints) | — | Blocked from this network (live check 2), and Google's terms → **off by default**; not run live |

Everything is in `docs/sources.md` (endpoint, method, auth, pacing, terms note, date) and `sources/notes.py` (the catalog).

## 5. Live runs

`run.py` against a scratch data directory. Only `local_business` used `--source google_maps`; the run configs are in the scratchpad, not the repo.

| Run | Took (estimate) | What it showed |
|---|---|---|
| Pune dentists (Maps, quick) + tech_stack (WordPress, Shopify, Wix, Joomla, Drupal, Magento, WooCommerce, PHP, jQuery) + app_store ("clinic", "dentist") | 1.9 min (1.9–2.9) | **tech_stack:** a dentist on WordPress 5.7.2 and PHP 8.1.34 (end of life Dec 2025), others current. **app_store:** apps from the US, Thailand and a solo developer flagged as Indian leads, and "rated 0" for unrated apps |
| Pune again, new project, after the fixes | 1.8 min (1.7–2.2) | **tech_stack:** 8 sites checked, 4 matched; jQuery 3.4.1 and PHP 8.1 flagged. Not checked: one homepage disallowed by robots.txt, one dead domain, one TLS failure through this Mac's proxy. **app_store:** 43 apps read; 19 games and 22 with no sign of India left out; one Indian developer's pet-clinic app flagged for payment and support complaints, though rated 4.49 |
| Austin (US), app_store only ("dentist", "restaurant ordering") | 2.1 min (1.1–1.3) | 89 apps, 50 out of the country, 7 developers flagged, but mostly national brands (Subway, Sonic, Raising Cane's, Rappi with 2.3 million ratings). `run.py` exited 1 although the run completed |

**Fixes from the live runs** (with regression tests):
- **Scope by country.** `country` became the default, needing evidence the developer is in the run's country; `storefront` keeps everything.
- **Apple's 0 rating** for an unrated app is no rating.
- **`max_ratings` (100,000)** leaves out national brands.
- **Complaints on a well-rated app count for less.** Play shows the harshest reviews first.
- **App Updated is written as noon UTC**, so the run's zone keeps the calendar day.
- **`run.py`'s exit code** is fixed for runs without local_business.

The Austin estimate was low: cold priors assumed 10 Play reads per keyword. The prior now uses the Play budget per keyword.

## 6. Postgres

A throwaway PostgreSQL 16 ran in Docker (`postgres:16-alpine`, bound to 127.0.0.1, stopped afterwards).

| Check | Result |
|---|---|
| The eight Phase 6–8 lead-type suites on one fresh database | 354 passed |
| The same suites on a database a previous session had used | Failed at first: my run tests found their own earlier projects (`tmp_path` names repeat). Fixed with a per-session suffix; 44 passed |
| `-m postgres` (round trip; migrations 0001 → 0006 over existing rows; both real stores) | 6 passed |
| The engine suite on fresh databases | `test_estimator_p50_is_within_35_percent…` failed in all 4 runs on this branch and both on the Phase 7 tip. `test_without_a_budget_every_unit_runs_in_plan_order` failed in 2 of 4 runs here, 0 of 2 there, and on Phase 7's base. Both pass alone (section 11) |

## 7. Acceptance checks

| Check (each lead type) | `tech_stack` | `app_store` |
|---|---|---|
| Fixture tests for sources, signals, columns and scoring | **Pass.** `tests/test_tech_stack.py`, 19 cases (1 live). Covered: the new rules and their dates; config validation; which technologies count; one site's check (statuses, HTTP fallback, robots, blocks, dead and internal hosts, stops); planning; a run over the finders' sites (signals, rule 6, workbook columns, recheck window, snapshot); any-outdated; uploads; footprints confirmed by fingerprint; scoring, columns, depth; the review's regressions | **Pass.** `tests/test_app_store.py`, 28 cases (2 live). Covered: iTunes search and lookup; a Play listing; store links and app refs; complaint themes; the assessment; filters and scope; grounded AI themes; a run (iOS twin, attach vs create, Play contacts, workbook columns, a fixed app losing its signal); listed apps; a blocked store; `run.py`'s exit code; scoring, columns, depth; the review's regressions |
| Isolation (enable or disable it alone; others' output unchanged) | **Pass.** `test_enabling_or_disabling_tech_stack_leaves_local_business_unchanged`, `test_a_crashing_check_is_contained`, and between assessors: `test_review_a_check_that_finds_nothing_does_not_join_the_run` | **Pass.** `test_enabling_or_disabling_app_store_leaves_local_business_unchanged`, `test_a_crashing_assessment_is_contained` (discovery and site links) |
| Live smoke test behind `@pytest.mark.live` | **Pass.** `test_live_fingerprint_of_a_wordpress_site` | **Pass.** `test_live_itunes_search`, `test_live_google_play_listing` |
| Workbook columns render | **Pass.** The run test opens the exported workbook; the `techstack_appstore` golden file pins styles, formats and links | **Pass.** Same |
| `docs/sources.md` entries dated | **Pass.** `site_fingerprint`, `footprints` (not live: SERP blocked here) | **Pass.** `itunes`, `google_play`, `listed`, `site_apps`, and the sources not used |

**What the isolation tests compare:**
- local_business's own rows, score columns and own evidence;
- its final leads;
- the organisations its enrichment is handed.

Each lead type is run on, off and crashing. The assessor-to-assessor case (tech_stack against digital_audit's `run` scope) came from the review and has its own test.

## 8. Review and the fixes it led to

An independent reviewer agent read the diff and reproduced its findings with offline scratch scripts. It reported 2 high, 4 medium and 8 low findings, all fixed. The 11 new regression tests, and the updated crash test, fail on the pre-fix code (checked by stashing the source fixes).

| Finding | Fix | Test |
|---|---|---|
| **High:** every site tech_stack checked joined the run's membership, even with no match. With `check: project`, that widened digital_audit's `audit: run` to the whole project, and the run's diff and `scope=new` exports | `set_attribute(note_run=False)` when nothing matched | `test_review_a_check_that_finds_nothing_does_not_join_the_run` |
| **High:** a short Play review took a neighbour's text or the reviewer's name. A 5-star review could count twice as a 1-star login complaint | The review's own box (the largest ancestor with one rating); the text is what follows its date | `test_review_short_reviews_keep_their_own_text_and_never_a_name` |
| Medium: any app a site linked was credited to that organisation, filters bypassed (a café's delivery platform) | Its own only by developer site or name; others are judged like found apps | `test_review_site_links_own_apps_only_and_after_the_search` |
| Medium: an app the search passed over was marked seen, so the site linking it never got its signal | Seen per (app, organisation) | Same test |
| Medium: one Play budget, spent on twins of apps then dropped, exhausted silently | Separate budgets for discovery and sites; size checks before twin lookups; `budget_spent` recorded once | `test_review_a_spent_search_budget_leaves_the_sites_their_own` |
| Medium: `installs` never filtered (only the App Store listing was checked) | Applied to the Play twin too | `test_review_the_download_band_applies_to_play_twins` |
| Medium: `leadtypes/app_store/__init__.py` was missing, so a wheel install would fail to load every lead type | Added | `test_review_every_package_directory_is_a_package` |
| Low: Play's developer could be an email address | Never an email or phone | `test_review_the_developer_is_never_an_email` |
| Low: Preact detected as React | Not after a word character | `test_review_preact_is_not_react` |
| Low: `run.py` exited 0 when one of several lead types failed | Exits 1 | `test_review_run_py_exits_1_when_a_lead_type_failed` |
| Low: a twin "confirmed" through a shared host (facebook.com, linktr.ee) | The developer's own site only | `test_review_a_twin_needs_the_developer_s_own_site` |
| Low: one app's crash in post-discovery cost the rest | Contained per app (`assess_failed`) | `test_a_crashing_assessment_is_contained` (updated) |
| Low: a configured app not in the store vanished silently | `app_not_found` recorded | `test_review_a_missing_listed_app_is_recorded` |
| Low: deep depth lowered a larger `max_results` | `max()` | — (the field's limit is 200, so nothing visible changed) |

The reviewer also checked these and found them sound:
- no lead type imports another;
- thread tagging;
- rule 6 (upserts only after `find_match` under the store lock);
- lock order;
- per-app and snapshot pruning;
- the recheck window;
- every fetch on the public-only client, with robots.txt for site fetches;
- Play search, Apple pages and review feeds never fetched;
- only published contacts;
- the `targets_file` refusal on every path;
- the upload reader;
- the fingerprint move;
- scoring isolation by lead type;
- grounded AI themes;
- the edited earlier tests.

## 9. Intended behaviour changes, with reasons

None of these touch the characterisation results or the seven existing golden workbooks.

- **The fingerprint table is shared and larger.** digital_audit's Site Stack can now list the 18 new technologies, and its `cms_age` check flags the new outdated ones (Angular below 20, Vue 2, TYPO3 below 13, SharePoint 2013). Its detections of the earlier 26 are unchanged.
- **`run.py` exit codes.** A completed run without local_business exits 0 (it exited 1, a pre-existing quirk). Without local_business, a run where a lead type failed exits 1.
- **`audit targets` takes `--lead-type`.** The default output is unchanged (`digital_audit`).
- **The ICP builder's offerings.** Web/app development adds `tech_stack`, and a signal "outdated CMS". The two tests that used `tech_stack` as the not-yet-built example now use a made-up key.
- **`set_attribute` gains `note_run`.** The default (True) is the old behaviour.

## 10. Deviations from the plan, with reasons

- **Branch from the Phase 7 tip, not `main`** (see the top).
- **Search footprints are built but off by default.** The plan names them, but they are Google SERP queries, which Google's terms restrict (the same call as `board_search` and `linkedin_search`). They could not be run live here: SERP is blocked from this network. Paid "who uses X" APIs (BuiltWith, Wappalyzer, PublicWWW) weren't used.
- **No Apple reviews.** The plan asks for complaint themes. Apple's review RSS is disallowed by robots.txt and its web pages by its site terms, so themes come from the reviews Play's listing shows: three "most relevant" ones, which skew negative. That is why complaints on a well-rated app count for less.
- **Google Play is reached without search.** Listings come from the config, organisations' own sites, or an App Store app's bundle id, confirmed by the same developer website. An Android-only app is found only through a site or the config.
- **`country` scope is evidence-based, and the default.** Apple's API doesn't say where a developer is. Without the filter, the India storefront put foreign developers in Indian runs; with it, a domestic app that names neither its country nor a place, and has no Play twin, is left out. `storefront` keeps everything.
- **`max_ratings` defaults to 100,000.** National brands aren't Skillminds' buyers. Set `null` to keep them.
- **The fingerprinter lives in `sources/web/`, not in a lead type**, so both lead types can use it (A.3). A site both digital_audit and tech_stack check is fetched by each; they share no state.
- **A checked site with nothing wanted isn't a tech_stack lead.** It isn't stamped and doesn't join the run; only the attribute records the check, for the recheck window.

## 11. Open issues

- **One accidental round of live traffic.** A fast test loop with `-m "not characterisation"` replaced the default `not live`. The opt-in live smoke tests of earlier phases ran once: single requests to GDELT, GlobeNewswire, GitHub, the ATS boards, TED, Find a Tender, Contracts Finder, CPPP and GeM. The GlobeNewswire test failed, presumably on this network. `CLAUDE.md`'s fast-loop line now says `-m "not characterisation and not live"`.
- **Terms decisions (A.2.4):**
  - `itunes` and `google_play` are on by default, marked for review;
  - `footprints` is off;
  - earlier phases' list still stands.
- **Footprints are unverified live** (SERP blocked here). The tests stub the SERP client, so the real Google → DuckDuckGo path with this lead type is untested. A run on another connection would confirm it.
- **Country evidence is a heuristic.** Words, ccTLD and the Play address miss domestic apps that say nothing; for the US, "USA" is rarely written.
- **Review complaints rest on three reviews** Play chooses. `min_complaints` and the rating scaling temper it, but a sample of three is small.
- **A matched organisation joins the run** (as with every attaching lead type since Phase 6), so digital_audit's `run` scope also audits project organisations tech_stack matched with `check: project`. That is the A.6 design; only the no-match case was a leak.
- **Pre-existing on Postgres:** in a fresh engine database, the estimator test fails on this branch and the Phase 7 tip alike. The plan-order test is flaky there: 2 of 4 runs failed here, 0 of 2 on the Phase 7 tip, and it failed on Phase 7's base. Both pass alone, and the failing assertion is about learned `unit_stats` (`{'prior': 45, 'source': 15}` vs `{'prior': 60}`). This looks like stats leaking between tests in one database; it isn't settled.
- **CI has not run on this branch.** Merge order: `phase-3/api` → `main`, then `phase-4/excel`, `phase-5/ai`, `phase-6a/audit-hiring`, `phase-6b/news-people`, `fix/live-check`, `fix/live-check-2`, `phase-7/tenders-registrations`, this branch.
- **From earlier phases, still open and still yours:**
  - keys to configure: `sam-gov-api-key`, `companies-house-api-key`, `pagespeed-api-key`;
  - running `migrate-legacy` into your real data directory;
  - checking the real database for the Phase 2 migration bug;
  - live AI provider checks;
  - opening the samples in Excel, including the new `sample-tech-stack-and-app-store.xlsx`.

Commits on this branch (after `d42aa45`):
- `feat(core): shared fingerprint rules (+18 technologies, dated), shared upload reader, tech_stack uploads refused through the API`
- `feat(leadtypes): tech_stack and app_store (Phase 8)`
- `test: tech_stack and app_store -- rules, site checks, stores, themes, runs, isolation, live smoke tests`
- `feat(export): a techstack_appstore workbook scenario -- tech_stack and app_store columns, pinned`
- `fix: findings of the live runs -- app scope by country, unrated apps, national brands, complaints on well-rated apps, calendar dates, run.py's exit code`
- `docs: tech_stack and app_store (lead types, sources verified 2026-10-13, engine order, API, AI, README, CLAUDE.md)`
- `test: the techstack_appstore golden workbook takes App Updated at noon UTC`
- `fix: findings of the Phase 8 review (high, medium, low)`
- `docs: app_store's own-app rule, Play budgets and twins after the review`
- `test: tech_stack and app_store run tests use this session's own projects (a reused Postgres database keeps others)`
- and this report.
