# UI Build Prompt — Fieldbook (the web UI for the lead generator)

> **How to use this file.** Work one phase per Claude Code session, the same way as
> `APP-BUILD-PROMPT.md`. Every session gets **Section A (Master Context)** plus **exactly one
> phase from Section B**. Say: *"Read UI-BUILD-PROMPT.md. Do Section A + Phase UI-N."*
> Each phase ends at its acceptance checks. Stop there, write `docs/ui-N-report.md`, and do not
> start the next phase. Branch each phase from `main` (`ui-N/<short-name>`).
>
> Every decision in this file was settled with the user over nine design rounds (2026-10-06 to
> 2026-10-15). **Don't re-open them.** If the code makes one impossible, stop and say so in the
> report rather than choosing a substitute silently.

---

# Section A — Master Context (include in every session)

## A.1 What we are building

A web UI over the finished backend (`CLAUDE.md`, `docs/api.md`, `APP-BUILD-PROMPT.md` Phases 0–8):
a FastAPI `/api/v1` (80 operations, SSE run events), a worker and a CLI over one services layer.

- **Users:** Skillminds staff, sales and technical, with **equal weight**. The UI has two modes of
  equal standing over one shared config: a guided **Form** and a full **Config** editor.
- **The deliverable is the workbook** (Overview + Records). In-app results are a check before download,
  not a replacement for it.
- **Devices:** full desktop experience. On a phone, the views needed to *check* things (A.10).
- **Deployment:** the same UI in **local mode** (one user, localhost) and **server mode** (a shared
  always-on machine, `docker compose`). The UI adapts: keep-awake and key entry exist only locally.

## A.2 Hard boundaries

1. **The UI is a pure API client.** No business logic, scoring, dedup or estimation in the
   browser; the UI renders what the API returns. A missing number becomes a backend change
   (UI-0 or a phase's own backend step), never a client-side guess.
2. **No CRM, no outreach.** Records are read-only. The only actions on an organisation:
   - view it;
   - open its sources;
   - add it to a suppression list;
   - export it.

   Never add status, notes, assignment, "contacted" or pipeline fields (`APP-BUILD-PROMPT.md` A.2.3).
3. **No login yet.** The API has none and binds to localhost by default. Don't build auth
   screens; keep every call workspace-agnostic so auth can be added later.
4. **Secrets are write-only and local-only.** No screen ever displays a key value. Keys can be
   entered only in local mode. In server mode, the Keys screen shows status and instructions.
5. **No fake numbers.** Never show a placeholder count, invented trend or decorative chart.
   Loading, empty and error states are real and say what is happening.
6. **English only,** with strings inline (no i18n catalogue).

## A.3 Identity

- **Name: Fieldbook** (a surveyor's record book; the output is a workbook). It's pending a trademark
  and domain check by the user, so keep the product name in **one constant**.
- **Personality: a field instrument.** Calm, precise, restrained colour; typography does the
  work. There is **one signature element**, the territory grid (A.5), and everything else stays quiet.
- **Mark:** a 3×3 survey grid with some cells filled, beside the wordmark "Fieldbook" in the UI typeface.
- **Motion: state changes only.** A cell fills when its unit finishes; a panel slides when it opens.
  No page-load or hover flourishes. Respect `prefers-reduced-motion`.
- **Theme:** light and dark, following the system, with a manual switch.

## A.4 Design tokens and rules

| Token | Light | Use |
|---|---|---|
| `ink` | `#1C2430` | text |
| `paper` | `#FAFBFC` | background (cool; never cream) |
| `rule` | `#D6DCE4` | lines, borders |
| `navy` | `#1F3864` | the **only** accent: primary action, focus, selection (the workbook's header navy) |
| `score-low` / `score-mid` / `score-high` | `#F8C9C4` / `#FFF0C2` / `#C6E7C8` | score fills **only** (the workbook's RAG fills) |

- **Colour = data only.** Apart from navy for interaction, colour appears only where it carries data
  (scores). Run and unit states are shown by **glyph shape + label**, never by colour alone.
- The **dark set** is derived in UI-1 and approved at the specimen. Score fills keep their meaning
  in dark mode.
- **Type:** Atkinson Hyperlegible Next and Atkinson Hyperlegible Mono, **bundled and self-hosted**,
  with Noto fallbacks for Devanagari and Arabic business names. The final pick is made at the
  specimen, from a rendered side-by-side.
- **The workbook relation is "siblings":** the same navy and score colours, so a lead looks the same
  in both. Type and layout are the UI's own.
- **Anti-slop rules** (from `taste-skill`, the only parts of it that apply here):
  - one accent;
  - one corner radius;
  - real empty, loading and error states;
  - no fake numbers;
  - **no em-dashes in UI copy**;
  - no cards inside cards.

## A.5 The signature element: the territory grid

One component, used in four places: **new search** (the plan forming), **estimate** (what fits
the budget), **live run**, and **history** (the run as a record).

- **Stacked blocks, one per enabled lead type,** each with a header. Each block's axes come from
  the per-lead-type **axes descriptor** in `/catalog/lead-types` (built in UI-0). The UI must not
  hard-code lead-type layouts.
  - **Two-axis lead types get a grid:** local_business (areas × categories), registrations
    (places × registers), funding_news (places × sources), tech_stack footprints (places × technologies).
  - **One-axis lead types get a single row of cells:** tenders (portals), hiring (boards, plus one
    Hacker News cell), app_store (keywords).
  - **Post-discovery work** gets a **progress band** under its block, fed by structured
    `post_progress` events: digital_audit, decision_makers, careers pages, tech checks,
    app own-site checks.
- **Odd-scope units** (local_business) go in a **margin row** ("Whole <city>") for run-wide units
  and a **margin column** ("All categories") for area-wide units.
- **Source switcher:** chips above a block (`[All] [Maps] [SERP] …`). With "All", a cell shows its
  combined state; with one source chosen, that source's units only.
- **Mixed cell under "All":**
  - the count of new organisations;
  - the glyph of the least-finished state (running > queued > partial > done);
  - a corner mark if any source failed in it.
- **Cell states** (glyph + label, never colour alone):

  | State | Meaning |
  |---|---|
  | done | finished |
  | running | in progress |
  | queued | waiting |
  | partial | half-filled glyph; "re-runs on resume" |
  | failed | ended with an error |
  | skipped (budget) | hatched; didn't fit the time budget |
  | not run (key missing) | its own glyph; links straight to the key field in Settings |
- **Selection is by whole rows and columns only** (they map to the API's `options.areas` and
  `options.categories`). No per-cell exclusion. Selection exists only where the backend honours
  it (local_business today). Elsewhere the grid is read-only.
- **Accessibility:**
  - arrow keys move between cells;
  - each cell has a screen-reader summary ("Andheri, Caterers: done, 14 new, 1 source failed");
  - row and column headers are real headers.

## A.6 Stack (decided)

| Concern | Choice |
|---|---|
| App | React + TypeScript + Vite, in `web/` in this repo |
| Components | **React Aria Components** (unstyled, accessible): combobox, tag groups, table, timezone-aware date/time pickers, toasts. Styled to the tokens |
| Styling | **Tailwind v4**, with the A.4 tokens as the theme (light and dark sets) |
| API client | **Generated from the OpenAPI document** (`python -m leadscraper api --openapi FILE`); regenerate in CI and fail on drift |
| Server state | TanStack Query (default; change it only with a reason in the report). Live runs via `EventSource` on `/runs/{id}/events` |
| Routing | A typed router; URLs are linkable (an org drawer, a run tab and table filters all live in the URL) |
| Config editor | **CodeMirror 6**: YAML, live JSON-Schema validation, hover help from `/schema/run-config` UI hints, autocomplete |
| Packaging | FastAPI serves the built UI at `/` and keeps `/api/v1/docs`. Built assets ship **inside the Python package**, so pip and Docker users never need Node. Requirement: `pip install` of a release, and `docker compose up`, both serve the UI. UI-1 picks the build route and justifies it |
| Tests | **Vitest + Testing Library** (components); **Playwright e2e against the real API + worker on fixture sources** (the backend's `tests/fake_web.py` fixtures); **axe** plus a manual keyboard pass per screen |

**Skills to use while building:**
- `ui-design` and `product-design` (interaction states, undo, recovery) while building screens;
- `frontend-design` for visual decisions;
- `typography-audit` and `ui-verification` (focus, hit targets, overflow, themes, request
  failures) in every phase's acceptance checks;
- `ui-animation` only for the allowed state-change motion.

## A.7 Information architecture

- **Project-centric.** Projects → runs / records / searches / schedules.
- **Global navigation:** Home · Projects · New search · Lists · Sources · Settings.
- **Present everywhere:**
  - an **inbox bell** with an unread count;
  - a **system status indicator** (worker alive, local/server mode, problems), which links to the
    System page.
- **Home** has two parts:
  - a slim **Start strip**: "Describe what you want" (first card), presets, recent searches, blank;
  - a **status board**: Running now (mini grid, %, ETA p50–p90) · Finished since you last
    looked (counts, workbook button) · Needs attention · Coming up (schedules).
- Not in v1: command palette, compare projects, export-scope chooser.

## A.8 Formatting

- **Numbers and money** follow the **run's country**, like the workbook. Pune: ₹ and Indian grouping
  (1,00,000). Austin: $ and 100,000.
- **Times** are shown in the **viewer's timezone**, with the run's timezone on hover when they differ.
  Schedules are always shown in their own timezone.
- Tender deadlines are shown as dates in the buyer's timezone where the API provides it, otherwise
  labelled UTC (audit L6).

## A.9 Quality bar (every phase)

- **WCAG 2.2 AA.** Keyboard access everywhere, including grid navigation; visible navy focus; axe clean.
- **Every data view has designed loading, empty and error states.** Errors say what failed and
  what to do: e.g. "Google Maps blocked us, retrying in 4 min", never a spinner forever.
- **Large data stays fast:**
  - the Mumbai preset's grid is 648 units (9 areas × 8 categories × 9 sources) and must render and
    update live without jank;
  - records tables page from the server.
- **Backend changes** keep the backend suite and characterisation tests green. They also update
  `docs/api.md` and `CLAUDE.md`, and regenerate the OpenAPI client.

## A.10 Phone (must-work views)

- Run status with pause, resume and cancel.
- Download the workbook.
- Inbox and alerts.
- Run a schedule now, or rerun a run.

Everything else may be desktop-only, but it must not break on a small screen. Show a short "best on
a larger screen" note instead.

## A.11 Working method

1. Read `CLAUDE.md`, this file, `docs/api.md` and the previous `docs/ui-*-report.md`. Post a short
   plan before editing.
2. Backend work follows `APP-BUILD-PROMPT.md` A.9: tests with fixtures, nothing live by default,
   an Alembic migration for every schema change.
3. Commit in small steps on the phase branch. Never commit secrets, real leads, `data/` or
   built artefacts the chosen packaging route doesn't require.
4. At the end:
   - run the backend suite, the UI unit tests, the Playwright e2e suite, axe and lint;
   - write `docs/ui-N-report.md`: what was done, how it was verified, deviations with reasons,
     open issues;
   - update `CLAUDE.md` (commands for `web/`, architecture).

---

# Section B — Phases

## Phase UI-0 — Backend prerequisites

**Goal:** every endpoint and field the screens need exists before any screen is built. Group the
work as below, and test each item.

1. **Secrets API.**
   - `GET /secrets`: an inventory of every known key. Name, used by (sources and providers),
     configured, where from (env / file / keychain), register URL. **Never values.**
   - `PUT /secrets/{name}`: write-only. `DELETE /secrets/{name}`.
   - Writes go to the OS keychain only. They are refused when `LEADSCRAPER_MODE=server` or no
     keychain exists. Names are validated against the known names and the `ai-<site>-…` and
     `notify-…` patterns.
   - The server-mode response includes the exact env var or secret-file name to set.
   - Add PageSpeed and GitHub to the catalog's `key{}`, and derive `/health`'s key list from the
     catalog instead of its hard-coded four.
2. **Inbox.**
   - An `inbox_items` table fed by:
     - run completed, run failed, needs attention;
     - post-discovery skipped;
     - key missing;
     - candidate sources proposed;
     - schedule fired.
   - `GET /inbox` (paged), `POST /inbox/{id}/read`, `POST /inbox/read-all`. The read state is
     shared by the workspace.
3. **Queue and preview.**
   - **Preview pre-emption:** `kind=preview` jumps the queue. The worker pauses the active run at
     its next unit boundary, runs the preview, then auto-resumes the run. Only one run is active
     at a time.
   - **Preview for every lead type** (audit M2): a small slice of each enabled lead type
     (e.g. one portal, one board, the first 5 rows of a list).
   - Fix `docs/deploy.md` to recommend **one worker**: the `RateLimiter` is per process, so two
     workers would pace the same host independently.
4. **Estimate and progress.**
   - `/estimate` returns a **per-unit plan**: `units[]` with key, lead type, source, axis values,
     scope, expected seconds, and `fits` / `skipped_reason` / `unavailable`.
   - The estimator honours lead-type stages when it fits a budget (audit M7).
   - **Structured post-discovery progress:** a `post_progress` event and run stat
     (`lead_type, done, total, status`) alongside the free-text activity.
   - **Unit skip-reason class** (`budget` / `key` / `stop`) in units and events; `partial`
     documented as a state.
   - **Resume accounting** (live check 3, §7): sum session stats across attempts, and re-estimate
     post-discovery on resume, so numbers and ETA are right after recovery.
5. **Catalog for forms.**
   - The **grid-axes descriptor** per lead type in `/catalog/lead-types`: row axis, column axis,
     labels, one- or two-axis, post-discovery-only flag.
   - **Option lists:** tech_stack technologies and version thresholds (from `fingerprints.yaml`),
     ATS kinds, tender CPV/NAICS presets, SIC divisions.
   - **Help text** for every lead-type field that lacks it. Mark `targets_file` hidden in the UI hints.
   - Expose each source's **terms-review flag** and note (`docs/sources.md`, audit H2).
   - Record SERP blocks as `blocked`, so health is honest (live check 3, §7).
6. **Records and comparison.**
   - A paged, sortable `GET /runs/{id}/records` (and a project-level equivalent) built from the
     workbook's `ExportTable`, so in-app scores, order and values **equal the workbook** (audit M3).
   - Each record includes `change`, `top_signal` (with `expires_at`), `lead_types`, and the
     short-set fields.
   - **A new signal on a known organisation counts as "updated"** (with the reason) in
     `/runs/{id}/diff`, `scope=new` exports and only-new schedules (audit M4).
7. **Configs and lists.**
   - A **list-only run** (digital_audit / tech_stack targets only) may omit `geography.places`;
     `country` stays required.
   - **Suppression import `dry_run=true`:** returns detected columns, counts per kind, and
     unclassified rows.
8. **Small items.**
   - `ScheduleOut.next_runs` (the next three dates).
   - Candidate sources gain `discovered_by` (a saved config or project) and are included in bundles
     (audit L3).
   - Webhook URLs are masked in API responses (audit L4).
9. **Serving.** `GET /` serves the SPA (index fallback for client routes); `/api/v1/docs` stays.
   Presets ship as package data (audit M5).

**Acceptance:**
- Each item has tests.
- The OpenAPI document is regenerated, and `docs/api.md` documents every new field and route.
- The backend suite, characterisation tests and lint are green.
- The report maps each item above to its tests.

---

## Phase UI-1 — Foundation, tokens, the grid, the specimen

**Goal:** the visual system and the signature component exist, served by FastAPI. **The phase stops
for the user's sign-off** before any screen is built.

1. **Scaffold `web/`:**
   - Vite, React, TS, Tailwind v4 with the tokens, React Aria Components;
   - the generated API client (with a CI drift check), the router, TanStack Query;
   - Vitest, Playwright, axe.
2. **Packaging:** choose and implement the route that ships built assets in the Python package;
   prove it in the Docker image and a clean `pip install`. Add `web/` build, lint and tests to CI.
3. **Theme:** light and dark token sets, system-following with a manual switch, both fonts
   self-hosted with fallbacks.
4. **The territory grid component (A.5) in full:**
   - axes from the catalog descriptor;
   - stacked blocks, margin row and column, source switcher;
   - every cell state, mixed-cell rule, post-discovery band, row/column selection;
   - keyboard navigation and screen-reader summaries;
   - live updates from SSE events, tested at 648 units.
5. **The mark** (grid mark + wordmark) as an SVG component.
6. **App shell:** navigation, inbox bell (real `/inbox` count), system status indicator
   (real `/health`), theme switch, empty Home.
7. **The specimen page (`/specimen`, dev route):** one rendered page built from the real tokens and
   the real grid component. It shows:
   - the type candidates side by side;
   - the colours in light and dark;
   - every cell state;
   - the mark.

   It is **not** a screen mockup.

**Acceptance:**
- Component tests for the grid's states, selection and keyboard behaviour.
- axe clean on the shell and the specimen.
- The built UI is served at `/` from both Docker and pip.
- **Stop and ask the user to approve the specimen**, choosing type, the dark palette and the mark.
  Record the choices in the report. Screens start only in the next phase.

---

## Phase UI-2 — New search

**Goal:** go from intent to a queued run, with the estimate visible the whole time.

1. **Entry points** (the Start strip):
   - **Describe what you want:** a brief, plus an offering, country and places →
     `POST /ai/icp` → opens the stepper prefilled. It works without AI (offering only). State
     plainly that the `icp` section describes the target but **does not filter results** (audit M1).
   - Also: presets gallery, recent searches, blank.
2. **Stepper + live rail.**
   - Steps: **Basics / Where / What / Filters / Time / AI / Output**, as pages.
   - The right rail always shows:
     - the territory grid forming, from `/estimate` `units[]`;
     - the estimate, p50–p90;
     - budget fit: what fits, what's skipped and why;
     - "Not run" units: key missing, with a link to the key.
3. **Where:** country, places (geocoded), areas. It says "not needed for a list-only search" when
   that's the case.
4. **What:**
   - lead types, each with its settings form generated from the schema hints and the UI-0
     option lists;
   - **lists for digital_audit and tech_stack sit inside their settings:** upload → preview
     (`/audit/targets`) → import;
   - sources per lead type, with inapplicable ones greyed out and the reason shown;
   - terms-review badges.
5. **AI step:**
   - provider status;
   - feature toggles;
   - per-run budget with the estimate's AI cost;
   - **planner suggestions as accept/reject changes** (`/ai/plan`);
   - a **"Find more sources" panel** (`/ai/source-discovery`), where candidates are approved or
     rejected in place.
6. **Form / Config toggle over one document.** The CodeMirror editor validates live against the
   schema. An invalid config blocks switching back to the form, with errors shown on their lines.
7. **Preview in the page** (every lead type, UI-0). Sample results and a corrected estimate replace
   the estimate panel. The user can tweak and re-preview. If a long run is active, say that the
   preview pauses it briefly and resumes it.
8. **Run:** auto-save on Run (a new version if anything changed), then go to the run page.
   Also: config versions with compare, and file import/export.

**Acceptance:**
- e2e: preset → preview → run.
- e2e: describe → draft → edit in Config mode → run.
- e2e: list upload → list-only run.
- The rail updates within a second of a change (debounced).
- axe, keyboard pass, `ui-verification` sweep.

---

## Phase UI-3 — Live run and the report

**Goal:** watch, steer and receive a run.

1. **Run page:**
   - **Numbers on top:** time vs budget, ETA p50–p90, found, new, and the current activity line.
   - Below that, the full-width grid with source chips.
   - Then tabs: **Failures / Units / Estimate vs actual**.
2. **Live updates over SSE,** with replay from `events/history` when reopening. Post-discovery
   bands update from `post_progress`.
3. **Controls without confirmations:**
   - pause shows an **Undo** toast;
   - cancel uses a **5-second undo window** before the request is sent;
   - resume and rerun.
4. **Queue view:** one lane, because the worker runs one run at a time. It shows queued runs and a
   preview cutting in.
5. **A finished run becomes its report:**
   - **download buttons** on top: workbook first, then CSV, JSON, Google Sheets push;
   - the **Overview mirror:** totals, new/updated, top 10, breakdowns, coverage, or why the run is empty;
   - the grid below, as the record.
   - Partial units and skipped post-discovery work are named.

**Acceptance:**
- e2e: run on fixtures → pause → undo → cancel within the window aborts nothing → completes →
  the workbook downloads.
- Reopening a finished run replays correctly.
- The 648-unit grid updates without dropped frames on a mid-range laptop. Measure it and report
  the numbers.

---

## Phase UI-4 — Projects, records, organisation detail

1. **Projects list** with counts and last run.
2. **Project page** opens on a **project overview** (the Overview mirror for the whole project, plus
   project export). Then tabs: runs, searches, schedules.
3. **Records table** (from `/runs/{id}/records` or the project equivalent).
   - Short set: Score (workbook colour scale), Name, Category, Area, Phone, Email, Website,
     Fit / Intent / Reach, **Top signal** (with deadline), **Lead types**, Change.
   - Server-side paging and sorting. Filters live in the URL.
4. **Run comparison:** counts act as filters (new / updated / unchanged / gone since run N).
   "Against" defaults to the previous finished run and can be changed. New signals count as updated,
   with the reason shown.
5. **Organisation drawer** over the table. Arrow keys step through organisations, and the URL is linkable.
   - **Reach first:** name, score, published contacts, website, socials, people.
   - Then score reasons, signals (deadlines, expired ones marked), and AI attributes labelled
     "(AI)" / "(AI estimate)".
   - **Provenance inline per fact:** source and date beside each contact. Inferred emails are
     marked "inferred, unverified" and are never mixed with published ones.
   - The full evidence timeline sits at the bottom.
   - **Only actions:** open source, add to suppression list, export.

**Acceptance:**
- e2e: compare two runs, filter "new", open the drawer, step with arrow keys, add to suppression,
  confirm the export excludes it.
- In-app scores equal the workbook's for the same run (an automated check).

---

## Phase UI-5 — Operations and administration

1. **Home status board in full** (A.7).
2. **Inbox panel:**
   - newest first, grouped Today / Earlier;
   - item types from UI-0, each linking to its run or setting;
   - "Mark all read" (shared state).
3. **Schedules:**
   - a **plain-language builder**, with raw cron behind the Config toggle;
   - the **next three dates**, the timezone, only-new, and "Run now".
4. **Lists:** suppression lists and erasure requests, each with counts and which searches use it.
   - Opening a list shows a searchable entries table.
   - **Import:** preview (detected columns and counts), then import.
   - **Erasure:** dry run, then a single "Erase N organisations" button. No typed confirmation.
5. **Sources:** grouped by lead type, then source.
   - Columns: applicable countries, key needed and set, 7-day health (ok / blocked / broken with
     counts), terms-review badge and note.
   - A **Candidates** tab: all suggested sources, decided or pending, with approve and reject.
6. **Settings:**
   - **General:** data folder, keep-awake (local only), robots.txt.
   - **Keys:**
     - local mode: paste and replace (write-only), plus status;
     - server mode: status plus the exact env var or secret-file name.
   - **AI:** provider order by drag, allow billed APIs, default budget, test button, this month's spend.
   - **Notifications:** targets (desktop on local only, webhook, email), event checkboxes per target,
     "Send test", masked webhook URLs.
   - **Data and retention:** retention days, purge with a dry-run count first.
   - **Bundles:**
     - export: configs, presets and lists always; leads and history optional; size shown;
     - import: preview of what will be added or replaced, then import;
     - "keys are never included".
   - **System:** health, version, local or server mode, worker status.

**Acceptance:** e2e for schedule create → run now; suppression import preview → import; erasure dry
run → erase; key set (local) and server-mode instructions; bundle export → import; candidate approve.

---

## Phase UI-6 — Phone, first run, polish

1. **Phone views (A.10)** work fully. Other screens degrade gracefully.
2. **First-run setup:** system check → keys (local only) → try a preset preview → notifications.
   Each step can be skipped, and the flow can be re-opened from Settings.
3. **Polish sweeps:**
   - `typography-audit` across the app;
   - `ui-verification` (focus, hit targets, overflow, both themes, request failures);
   - every empty, loading and error state reviewed against A.2.5 and A.9;
   - no em-dashes in UI copy.
4. **Performance:** report measured numbers for the 648-unit grid, a 10,000-record table and the SSE
   update rate.
5. **Docs:** `docs/ui.md` covers how to run, build and test the UI and how it's packaged.
   Update `README.md` and `CLAUDE.md`.

**Acceptance:**
- Playwright mobile-viewport e2e for each phone view.
- The first-run flow tested from a clean data directory.
- axe clean across all routes. A full manual keyboard pass is recorded in the report.

---

## Definition of done (UI)

From a browser, locally or on the shared server, a Skillminds user can:
1. start from a description, preset, recent search or blank;
2. shape the search in Form or Config mode with the estimate and grid always visible;
3. preview any lead type;
4. run it within a time budget;
5. watch it on the grid, and pause, undo or cancel it;
6. receive the report and download the workbook;
7. check records that match the workbook exactly;
8. compare runs;
9. manage schedules, lists, sources, keys (locally), AI, notifications and bundles;
10. check and steer a run from a phone.

There is no login, no CRM field, no outreach, no displayed secret and no fake number.
