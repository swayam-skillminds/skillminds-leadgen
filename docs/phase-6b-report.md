# Phase 6b report — lead types, wave 1: `funding_news` and `decision_makers`

Date: 2026-10-08 · Branch: `phase-6b/news-people` (see "About the branch") · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 6, items 3 and 4 only

**Result:** Phase 6 items 3 and 4 are complete, so all of Phase 6 is now done.
- The suite is green: 833 passed, 7 skipped, 16 deselected. On `phase-6a/audit-hiring` it was 674 passed, 7 skipped, 12 deselected.
  - The 7 skips are the opt-in Postgres tests plus one characterisation case that doesn't apply, as before.
  - The 4 new deselected tests are the Phase 6b live smoke tests.
  - `ruff` is clean.
- **Live, today:** every external endpoint was checked live before coding against it (A.9.2, section 4).
  - That check decided the defaults: Google News RSS, GlobeNewswire, LinkedIn search and GitHub are **off by default** on terms grounds; GDELT is the default news source.
  - The four live smoke tests pass: GDELT, GlobeNewswire's India feed, GitHub's API, MX records.
  - Three live end-to-end runs (section 5). The first exposed GDELT throttling and people-page false positives, both now fixed.
- **Postgres:** on a throwaway PostgreSQL 16, the four Phase 6 suites (248), the engine suite (48) and the opt-in Postgres tests (6) pass, including migrations 0001 → 0006 over existing rows (section 6).
- **Acceptance:** all five checks pass for both lead types (section 7).
- **Review:** an independent review found 1 high, 9 medium and 5 low issues. All are fixed, each with a regression test; 39 of the new test cases fail on the pre-fix code (section 8).
- **Baselines:**
  - `git diff phase-6a/audit-hiring -- tests/characterisation/golden tests/golden docs/samples` adds only `news_people.json` and `sample-news-and-people.xlsx`;
  - `tests/characterisation` passes against the frozen real-data copies;
  - the five existing golden workbooks and samples are byte-identical.
- **CI has not run** on this branch, because nothing was pushed.

**About the branch.** You asked for `phase-6b/news-people` from `main`. `main` is still at Phase 2: Phases 3–6a live only on their branches, in a line (`main` → `phase-3/api` → `phase-4/excel` → `phase-5/ai` → `phase-6a/audit-hiring`). Phase 6b builds on all of them (services, exports, AI, Phase 6a's attach-don't-rewrite store calls, `Kit`), so I created this branch from `phase-6a/audit-hiring`, as Phases 2, 4, 5 and 6a did. `main` itself is untouched, and once the earlier phases are merged, this branch's diff is exactly Phase 6b.

## 1. `funding_news` (6.3)

**Config:**
- `events`: funding, launch, expansion, acquisition, digital_transformation, leadership_change (default: all);
- `keywords`: extra search words, ANDed with every query;
- `recency_days`: 30;
- `place_filter`: `headline` (default) / `article` / `none`;
- `create_new`: true;
- per-source settings.

**Sources:**

| Source | What | Default |
|---|---|---|
| `gdelt` | GDELT DOC 2.0: worldwide news, searched by keywords per place (and the run's source country and language) | **on** |
| `google_news` | Google News RSS search per place | off: its feed terms forbid our use |
| `press_releases` | GlobeNewswire's feed for the run's country; each release names its issuer (`dc:contributor`) | off: its terms page could not be found |
| `feeds` | Any RSS / Atom URLs the config lists | on, empty |

**Rules** (`events.py`, `amounts.py`):
- **Event:** the headline reports an event the rules know, about a company named right before the verb:
  - "Pune-based fintech startup Ledgerly raises Rs 22 crore in pre-Series A" is Ledgerly's funding round;
  - "Kestrel Robotics opens new R&D centre in Pune" is an expansion;
  - "Brightline Foods appoints Priya Raman as CTO" is a leadership change.
- **Not events:**
  - no company before the verb ("Pune mosque … raises funds for …");
  - a public body (RTO, PMC, ministries, boards);
  - a person with a role ("Zomato CEO … raises");
  - business won or a loan ("bags $1 billion deal", "gets $750 million loan");
  - "raises concerns", "launches campaign", "debuts at discount", "expands its product line".
- **Press releases:** the issuer is the company. When the headline says nothing, the summary's first sentence is read.
- **Amounts and rounds as stated:** "Rs 22 crore", "₹12.5 Cr", "$1.1 Mn", "₹8-10 cr", "EUR 2.5m", "Pre-Series A", "Seed". The value is in the stated currency (a crore is 10^7); nothing is converted.
- **AI:** with `signal_interpretation`, headlines the rules can't read go to the model. A verdict is kept only with a verbatim quote and a company name the item contains (`signals.interpret(with_company=True)`; hiring's prompt is unchanged).

**Signals and scoring:**
- **Signals:** one per event: `funding_round`, `product_launch`, `expansion`, `acquisition`, `digital_transformation`, `leadership_change`.
  - The payload carries amount, round, investors ("led by …"), person and role, the other party, and every outlet that covered it.
  - Stories about one event within 30 days are one signal; a re-run refreshes it.
  - An event keeps the date and headline of its first story.
- **Strength:**
  - by type: funding 0.8, transformation 0.75, expansion and acquisition 0.6, leadership 0.6 (0.75 for a technology role), launch 0.45;
  - +0.05 per further outlet, up to 0.95.
- **Score:** intent = the strongest event, halving every half-life of its type (90 / 60 / 45 days), +0.1 per further kind of event.
- **Columns:** News Event (front, a breakdown), Latest News, News Date, Amount Raised, News Link; News Events and News Sources at the tail.

**Organisations:** a company the project holds gets the signal and keeps its record (rule 6). A new one is created from its name. Its website is set only when a story's link spells the whole name, and never an outlet's or a wire's.

## 2. `decision_makers` (6.4)

An enrichment lead type: no discovery units. In post-discovery (`post_order` 7, after hiring's careers pages and before the audit) it looks into the run's (or project's) organisations, at most once per `refresh_after_days` (30).

**Sources:**

| Source | What | Default |
|---|---|---|
| `team_pages` | The company's own team / leadership / about pages: JSON-LD `Person`, microdata, name-and-title cards, "founded by" prose. robots.txt honoured | **on** |
| `registry` (packs) | UK: Companies House current directors and LLP members (pack `uk`, GB), with the free key `companies-house-api-key` | on; skipped (and said) without the key |
| `linkedin_search` | Search-indexed `linkedin.com/in` profiles whose result title names this company and a leadership title; LinkedIn itself is never fetched | off: Google's terms |
| `github` | Public members of the GitHub org the company's site links (its `blog` must be the company's domain), whose own bio names a leadership title | off: GitHub's AUP |

**Who counts** (`sources/people/roles.py`):
- **Names:** two to four capitalised words (initials and particles allowed; acronyms, role and business words not).
- **Families:**
  - **owner:** founder, owner, partner, chairman, LLP member, "Principal" alone;
  - **executive:** CEO, MD, directors, GM, COO, CFO, president, VPs;
  - **technology:** CTO, CIO, chief digital officer, heads and VPs of engineering, technology, IT or digital;
  - **it:** IT manager, systems manager.
- **Never:** advisors, investors, interns, non-executive directors, product owners, channel partners, art directors, former CEOs.
- **Another employer:** a title naming one ("CIO, Fernwood Retail" in a testimonial) is someone else's decision-maker.

**People and the page:**
- On a site with team or about pages, the homepage's own people don't count (testimonials, authors).
- A card holds one person, and an email or profile link inside it is theirs. A role mailbox is nobody's.
- "Founded by" prose must be about this company.

**Writing:**
- A `people` row of this lead type: name, title, profile URL, source, the page, and an address published for that person.
- The organisation is stamped; its record is never touched.
- A source read in full is a snapshot: someone no longer listed is removed. A page that didn't answer removes nobody.

**The email pattern** (off by default; A.2.5):
- Only from a published *personal* address at the organisation's own domain that is exactly one pattern of a person we know by name: `first.last`, `flast`, `first`, … (12 patterns). Role mailboxes reveal nothing, and disagreeing addresses cancel the pattern.
- Only if the domain's MX records accept mail (a null MX doesn't).
- Only for real person names, never for people found on GitHub, never for anyone whose own address is published.
- Stored as an `inferred` contact naming its person, with the `email_pattern` attribute as its basis.
- Shown only in `Inferred Email (unverified)`; never on the record, never an identity key.
- No SMTP, ever.

**Score:** reach = the core contact reach + 0.15 for a named decision-maker with a title, + 0.05 for a way to reach them (max 1). It needs people, so `score` takes `record=` (a new, optional hook beside `now=`).

**Columns:** the core Decision Makers / Decision Maker Profile / Decision Maker Email (published only) columns, plus DM Roles and People Found; People Sources and Email Pattern at the tail.

## 3. Core changes

| Area | Change |
|---|---|
| Schema | Migration **0006**: `people.lead_type`, `source_url`, `email`, `updated_at`; `contacts.person` (whose inferred address it is) |
| Store | `add_person` (refreshed by name, blanks never overwrite), `people`, `prune_people` (snapshots), `set_inferred_contacts`. A record write keeps every inferred contact (it used to delete any contact not on the record) and upgrades one to published when the record publishes it. Consolidation keeps one row per (lead type, person) |
| Exports | People fold by name across lead types (`columns.people_of`). New optional core column **Decision Maker Email**. Inferred contacts name their person in JSON and the organisation API. `score(..., record=)` |
| Services | `has=email` counts published contacts only. Erasure still matches inferred addresses, deliberately |
| Packs | `Pack.registries` (company registries for decision_makers); a `uk` pack. `packs.registries_for(country)` |
| Network | `HttpClient.get(attempts=)`. News endpoints send the identifying `core.geo.USER_AGENT` (GlobeNewswire resets a browser's) |
| AI | `signals.interpret(with_company=)`: a grounded company name for news items |
| Dependencies | `dnspython` (MX lookups, the system resolver) |
| Pacing | decision_makers and hiring set the shared SERP pacing only when they search |
| Samples | A sixth fixture scenario, `news_people`: fictional Pune firms, their news, a news-only company, team-page and registry people, one labelled inferred email. Golden file and `docs/samples/sample-news-and-people.xlsx` |

## 4. External sources checked live (A.9.2), 2026-10-08

| Endpoint | Live answer | Terms → decision |
|---|---|---|
| GDELT DOC 2.0 `api.gdeltproject.org/api/v2/doc/doc` | 200, `articles[]` {url, title, seendate, domain}; "too short or too long" for a 17-term query; 429 text once it is asked often | "Unrestricted use … commercial", with a citation → **on**. ≤ 1 request / 5 s (paced 6–9 s), queries of ≤ 6 terms, one attempt each, no more GDELT after a throttle |
| Google News RSS `news.google.com/rss/search` | 200 | Its own `<copyright>`: "solely … within a personal feed reader for personal, non-commercial use. Any other use … expressly prohibited" → **off; the owner's call** |
| GlobeNewswire `RssFeed/country/{Country}` | 200 for India, UK, US, UAE; `dc:contributor` on every item; a browser User-Agent refused | robots.txt allows `/RssFeed/`; terms page not found → **off; to review** |
| PR Newswire RSS | 200 | Site terms: personal, non-commercial; no robots → **excluded** |
| Business Wire RSS | "channel … deactivated" | **excluded** |
| Inc42, YourStory (IN) | 200 | Terms forbid automated access → **excluded** (no regional feed ships in a pack) |
| Entrackr (IN) | 404 | **excluded** |
| Companies House API | 401 "Empty Authorization header" | Official, free key, 600 requests / 5 min, real-time lookups → **on with a key** |
| India MCA | — | No free public API → no Indian registry |
| GitHub REST `/orgs`, `/public_members`, `/users` | 200 | AUP: personal information only for the purpose its user authorised; no unsolicited email → **off**, never used for the pattern |
| LinkedIn | — | Never fetched; search-indexed URLs only (A.2.4) → **off** (Google SERP) |
| MX records (dnspython) | example.com: null MX; github.com: hosts | MX only, no SMTP |

Everything is in `docs/sources.md` (endpoint, method, auth, pacing, terms note, date) and `sources/notes.py` (the catalog).

## 5. Live runs

`run.py` against a scratch data directory:
- Pune, India; `funding_news` (GDELT);
- `decision_makers` with the email pattern on;
- three Pune IT companies' sites uploaded through `digital_audit` (Persistent Systems, KPIT Technologies, Zensar Technologies).

| Run | Took (estimate) | What it showed |
|---|---|---|
| 1 | **28.9 min** (1.6–2.6) | GDELT answered 429 to the first query and the client's retry-with-escalating-penalty ran the six queries for half an hour. Two events came through (TCS's Pune lab; "Pune RTO launches …", a public body). People: the real leaders, plus "AI Assistant", "Extensive Partner Ecosystem", a blog's "Principal Software Architect", Zensar's customer testimonials ("Matt Lamb, CIO, Rosendin"), "Kishor Patil Co-founder" and a services page taken for a team page |
| 2 | 0.6 min (0.9–1.3) | After the fixes: **15 leaders, all real** (Persistent's founder-chairman and CEOs; KPIT's co-founder-CEO, president, CTO; Zensar's CEO, CFO, SVPs), none of the junk. GDELT refused the 17-term combined query, which led to the six-term limit |
| 3 | 0.9 min (1.2–1.9) | With every fix: the same 15 people. GDELT 429 on the first query; the breaker stopped it at once instead of retrying |

None of the three sites publishes a personal address, so no address was inferred (`no_personal_address`). That is the honest outcome.

**Fixes from the live runs** (all with fictional regression tests):
- GDELT: one attempt per query, ≤ 6 terms, a breaker, an identifying User-Agent.
- Team links: matched by their last path segment or text.
- Homepage people: counted only when there's no team page.
- Employer and marketing-copy filters; acronyms and role words in names.
- Civic bodies are not companies.

## 6. Postgres

A throwaway PostgreSQL 16 ran in Docker (`postgres:16-alpine`, bound to 127.0.0.1, stopped afterwards). It was run before and after the review fixes.

| Check | Result |
|---|---|
| `test_funding_news.py`, `test_decision_makers.py`, `test_hiring.py`, `test_digital_audit.py` on one shared database | 248 passed |
| Engine suite on a fresh database | 48 passed |
| `-m postgres` (round trip, migrations 0001 → 0006 over existing rows, both real stores) | 6 passed |

## 7. Acceptance checks

| Check (each lead type) | `funding_news` | `decision_makers` |
|---|---|---|
| Fixture tests for sources, signals, columns and scoring | **Pass.** `tests/test_funding_news.py`, 78 cases: amounts and rounds; 42 headline shapes (fictional rewrites of live ones, true and false); event details; RSS, Atom and a hostile XML feed; GDELT queries, text answers, the throttle breaker; Google News; place filter; AI (grounded company); company sites; runs end to end (grouping by event, outlets, attach vs create, workbook), follow-ups, feeds and press releases, `create_new: false` | **Pass.** `tests/test_decision_makers.py`, 80 cases: names; title families; team-page links; JSON-LD, microdata, cards, one-line entries, prose; contacts per person; testimonials and copy; Companies House (match, ambiguity, Basic auth); GitHub; LinkedIn results; the pattern (learning, MX, null MX, no SMTP); runs end to end (people, snapshot, refresh window, fields, registry with and without a key, GitHub never in the pattern); the store (refresh, publish-later, prune, consolidation); score and columns |
| Isolation (enable or disable it alone; others' output unchanged) | **Pass.** `test_enabling_or_disabling_funding_news_leaves_local_business_unchanged`, `test_a_crashing_source_is_contained` | **Pass.** `test_enabling_or_disabling_decision_makers_leaves_local_business_unchanged` (with the email pattern on), `test_a_crash_on_one_organisation_is_contained` |
| Live smoke test behind `@pytest.mark.live` | **Pass.** `test_live_gdelt`, `test_live_globenewswire_country_feed` | **Pass.** `test_live_github_public_members`, `test_live_mx_records` |
| Workbook columns render | **Pass.** Run tests open the exported workbook; the `news_people` golden file pins styles, formats and links | **Pass.** Same, including the Inferred Email and Email Pattern columns |
| `docs/sources.md` entries dated | **Pass.** `gdelt`, `google_news`, `press_releases`, `feeds`, and the five excluded feeds | **Pass.** `team_pages`, `companies_house`, `linkedin_search`, `github`, `email_pattern` |

**What the isolation tests compare:**
- local_business's own rows, score columns and its own evidence;
- for decision_makers, also its published contacts;
- its final leads (CSV / Sheet / report);
- the organisations its enrichment is handed.

Both lead types are run on, off and crashing.

## 8. Review and the fixes it led to

An independent reviewer agent read the diff, reproduced its findings with offline scratch scripts and reported 1 high, 9 medium and 5 low findings. All are fixed. I re-ran the new tests on the pre-fix code: 39 of the new test cases fail there.

| Finding | Fix | Test |
|---|---|---|
| **High:** on a team page, a one-line entry's email went to everyone in its list, and a role mailbox (sales@) could become a person's "Decision Maker Email" | A line's contacts are its own; role mailboxes are nobody's; a block of paragraphs is several lines | `test_a_line_s_contacts_are_its_own_and_role_mailboxes_are_nobodys` |
| `has=email` matched organisations whose only email was inferred | Published contacts only (erasure still covers inferred, deliberately) | `test_has_email_means_a_published_one` |
| The email pattern applied to non-names ("Front Office" → front.office@) | Real person names only, for learning and applying | `test_the_pattern_only_names_people_and_a_failed_read_keeps_it` |
| Consolidation duplicated people (People Found inflated); pruning dropped an inferred address another source still backed | One row per (lead type, name) on fold; prune keeps a name still listed; counts per name | `test_consolidation_keeps_one_person_and_their_inferred_address` |
| Companies House picked a same-named company elsewhere | Several matches: the place decides, else `registry_ambiguous` | `test_companies_house_directors` |
| A news company got an outlet's or investor's site ("Tata Motors" → tatacapital.com) | The domain must spell the whole name; outlets and wires never | `test_a_news_companys_site_must_spell_its_name` (×7), `test_the_outlet_is_never_the_companys_site` |
| Headline false positives: orders and loans as funding; people, questions, commas, parentheses and Title Case phrases as companies; "Nadella As" | Won-not-raised nouns; role words end the subject; stop words, commas, parentheses; 4-word cap in Title Case; person words exclude As/To/… | 16 new cases in `test_headlines_name_an_event_and_its_company`, `test_event_details` |
| Non-buyers as owners or executives (Product Owner, Channel Partner, Former CEO, Art Director…) | Excluded explicitly | 11 new cases in `test_which_titles_are_decision_makers` |
| "Founded by" prose took "Priya Shah. We" and a client's founders | Names stop at a full stop; the clause must be about this company; names matched case-sensitively | `test_founded_by_prose_must_be_about_this_company` |
| A follow-up story made an old event look fresh | The first story's date and headline stay | `test_a_follow_up_story_never_makes_an_event_look_fresh` |
| Low: find-or-create race between concurrent stage-1 units (funding_news, and hiring too) | Under the store's lock, the signal's refresh included | (concurrency; covered by the run tests) |
| Low: "$5M Series A" and "$5M" stories became two signals; AI events never grouped | One funding round per company and batch; AI events by kind | the follow-up test |
| Low: "expands its product line", "enters into agreement", "sets up a fund" were expansion | Excluded | 3 headline cases |
| Low: decision_makers (and hiring) set the shared SERP pacing even when not searching | Only when the source is enabled | `test_serp_pacing_is_left_alone_unless_linkedin_search_runs` |
| Low: a transient team-page failure wiped earlier inferred addresses | Inference skipped on a transient read | `test_the_pattern_only_names_people_and_a_failed_read_keeps_it` |

The reviewer also checked these and found them sound:
- migration 0006 against the models;
- XML safety (entities unresolved, no network);
- every third-party fetch going through the public-only client;
- secret names, and where keys go;
- LinkedIn never fetched; MX only, no SMTP;
- inferred contacts never on the record or identity keys;
- no ReDoS in the rules.

## 9. Intended behaviour changes, with reasons

None of these touch the characterisation results or the existing golden workbooks.

- **A record write no longer deletes inferred contacts.** Before, `_sync_contacts` removed any contact not on the record, so the email pattern's addresses would vanish whenever local_business wrote. Nothing wrote inferred contacts before this phase, so no existing data changes.
- **`has=email` (and `has=…` generally) counts published contacts only.**
- **People rows carry their lead type**, so local_business's `contact_name` rows are now tagged `local_business` (old rows read `""`).
- **The Decision Makers column folds people by name** across lead types (the same person from two sources is shown once).
- **Hiring's `board_search` no longer sets google.com's pacing when it is off**, and hiring's find-or-create runs under the store's lock. Neither changes hiring's output.
- **The ICP builder switches `funding_news` and `decision_makers` on** when a draft names them. Its hiring test now uses `tenders` as the later-phase example.

## 10. Deviations from the plan, with reasons

- **Branch from `phase-6a/audit-hiring`, not `main`** (see the top).
- **Google News RSS is built but off by default.** The plan names it, but its own feed terms forbid any use beyond a personal feed reader. Enabling it is the owner's call, as with `board_search` in Phase 6a. **GDELT is the default news source instead:** open for commercial use, worldwide, searchable by keyword and place.
- **No regional startup feed ships in a country pack.** The obvious Indian ones (Inc42, YourStory) forbid automated access; Entrackr's feed is gone. Instead, `feeds.urls` takes any licensed feed and `press_releases` reads GlobeNewswire per country. The pack mechanism for feeds wasn't built, since it would carry no data.
- **Press releases (GlobeNewswire) are off by default.** Its terms page could not be found; the feed itself carries no restriction and robots.txt allows it.
- **"Public GitHub org owners" are public members whose own bio names a leadership title.** Org owners aren't public without authentication. The source is off by default (GitHub's AUP on personal information) and never feeds the email pattern.
- **Company-registry directors: UK only.** Companies House has a free official API (a key is needed). India's MCA has none, so no Indian registry.
- **The amount's value isn't converted between currencies.** The text is kept as stated, the value is in its own currency, and the column shows the text.
- **`score(..., record=)`** was added to the contract, beside Phase 6a's `now=`, because decision_makers' reach depends on people, which aren't signals.

## 11. Open issues

- **Terms decisions (A.2.4):** `google_news`, `press_releases`, `linkedin_search` and `github` are off by default. Opting in is a per-config decision for the owner. The inherited sources from earlier phases are still open.
- **GDELT throttles harder than it says.** It asks for one request per 5 s, but it answered 429 to single requests 6–30 s apart after a burst of probes from this IP, for over an hour. A run now stops asking at the first throttle (and says so), so the cost is lost news, not time. For heavy use GDELT points to its bulk datasets; a cache of answers per query and day would also help.
- **Companies House key:** `leadscraper secrets set companies-house-api-key`; then a live run would confirm a 200 answer. The fixtures follow the documented shape.
- **Headline rules are heuristic.**
  - A joint announcement ("Acme, Beta Labs partner with AWS") goes to the last-named company only.
  - Global companies in local news ("TCS launches … lab in Pune") are real events but rarely Skillminds targets.
  - The `signal_interpretation` AI feature is the intended second opinion.
- **News-only companies have no website** unless a press release links it. decision_makers and digital_audit then have nothing to read for them.
- **Team pages rendered by JavaScript are invisible** (requests only, no browser). Persistent's and Zensar's leadership pages are server-rendered, so they worked.
- **From earlier phases, still open and still yours:**
  - running `migrate-legacy` into your real data directory;
  - checking the real database for the Phase 2 migration bug;
  - a balanced-depth live check;
  - live AI provider checks;
  - the PageSpeed key;
  - opening the samples in Excel, including the new `sample-news-and-people.xlsx`.
- **CI has not run on this branch.** Merge order: `phase-3/api` → `main`, then `phase-4/excel`, `phase-5/ai`, `phase-6a/audit-hiring`, this branch. `fix/live-check` is still open too.

Commits on this branch (after `phase-6a/audit-hiring`):
- `feat(core): people of a lead type, inferred contacts that survive record writes, export people by name`
- `feat(leadtypes): funding_news and decision_makers (Phase 6, items 3 and 4)`
- `test: funding_news and decision_makers -- fixtures, runs, isolation, live smoke tests`
- `feat(export): a news_people workbook scenario -- funding_news and decision_makers columns, pinned`
- `fix: findings of the live run -- GDELT throttling, testimonials and copy read as people`
- `docs: funding_news and decision_makers (lead types, sources verified 2026-10-08, engine order, API, AI, README, CLAUDE.md)`
- `fix: findings of the Phase 6b review (high, medium, low)`
- `docs: the review fixes (one person per card, registry ambiguity, has= published only, first-story dates)`
- and this report.
