# Phase 5 report — AI layer: multiple providers, opt-in, budgeted

Date: 2026-10-07 · Branch: `phase-5/ai` (see "About the branch") · Plan: `APP-BUILD-PROMPT.md` Section A + Phase 5

**Result:** Phase 5 is complete.
- The suite is green: 583 passed, 7 skipped, 5 deselected. On `phase-4/excel` it was 483 passed.
  - The 7 skips are the opt-in Postgres tests plus one characterisation case that doesn't apply.
  - The 5 deselected are the new opt-in live AI tests.
  - `ruff` is clean.
- On a real PostgreSQL 16 (section 8):
  - the AI, services, API and workbook suites pass (110);
  - the engine suite passes on a fresh database (48);
  - the opt-in Postgres tests pass (6), including migrations 0001 → 0004 over existing rows.
- All seven acceptance checks pass (section 9).
- An independent review found five high and four medium issues. All are fixed, and each fix has a regression test that fails without it (section 10).
- The characterisation baselines are untouched:
  - `git diff phase-4/excel -- tests/characterisation/golden tests/golden docs/samples` is empty;
  - `tests/characterisation` passes against the frozen real-data copies;
  - the four `ai/assistant.py` prompts are byte-identical to Phase 4, so the presets' AI cache stays valid.
- **No AI provider was called by any test.** No API key is configured on this machine, so the live tests skip.
  - One real call did happen outside the tests, by accident: a CLI smoke test ran through your local `claude` CLI (section 13).
- CI has **not** run on this branch, because nothing was pushed.

**About the branch.** You asked for `phase-5/ai` from `main`. `main` is still at Phase 2, though: Phases 3 and 4 live only on `phase-3/api` and `phase-4/excel`. Phase 5 builds on Phase 3's services and Phase 4's exports, so I created `phase-5/ai` from `phase-4/excel`, which is `main` plus Phases 3 and 4 as a fast-forward. Phase 4 had to do the same. `main` itself is untouched, and once Phases 3 and 4 are merged, this branch's diff is exactly Phase 5.

## 1. Providers (5.1)

One interface (`ai/providers/base.py`):
- `Provider.complete(AIRequest) → AIResponse` (text, tokens, web searches, source URLs, reported cost);
- declared **capabilities** (structured output, tool use, web search, context size);
- **typed errors** that tell the chain what to do next (table below).

| Kind | Wire format | Structured output | Web search |
|---|---|---|---|
| `anthropic` | Messages API via the **official `anthropic` SDK 1.11** (extra `ai`) | `output_config.format` | `web_search_20260209` (Opus/Sonnet 4.6+), `web_search_20250305` (Haiku 4.5); results from `web_search_tool_result` and citations; `pause_turn` resumed at most twice |
| `openai` | Responses API over `requests` | `text.format` json_schema, strict | `web_search` tool, sources via `include` |
| `gemini` | `generateContent` (v1beta) over `requests` | `responseMimeType` + `responseSchema` (OpenAPI subset) | `googleSearch` tool, grounding chunks |
| `openai_compatible` | Chat Completions at `base_url` (OpenRouter, Groq, Ollama, LM Studio) | `response_format` json_schema, only when the entry declares it | — |
| `claude_code_cli` | `claude -p --output-format json`, tools stripped (the old backend, ported) | JSON + validation | — |

**Two tiers per provider.** A *worker* model handles bulk calls: extraction, tags, summaries, venue typing. A *judge* model handles hard calls: duplicate adjudication and the ICP builder. Defaults:

| Provider | Worker | Judge |
|---|---|---|
| Anthropic, CLI | `claude-haiku-4-5` | `claude-opus-5-5` |
| OpenAI | `gpt-6-luna` | `gpt-6-astra` |
| Gemini | `gemini-3.5-flash-lite` | `gemini-3.1-pro-preview` |

**Model ids and prices are data,** in `ai/defaults.yaml`, which the workspace (`PATCH /settings` → `ai.prices`) or a run config (`ai.prices`) can override.

**Fallback chain** (`ai/client.py`):

| What happens | Effect |
|---|---|
| A provider is out of credit, or its credentials are refused | Dropped for the run |
| A provider errors | Skipped for this call |
| A safety refusal | The item is skipped; the provider stays |
| No provider is left, or `failure_limit` consecutive calls go unanswered | AI stops and the run continues without it |

Two more rules:
- **Refusal fallback.** Claude Opus 5.5 / Opus 5 / Sonnet 5.5 calls opt into Anthropic's server-side refusal fallback (`fallbacks: "default"`), so a declined request is re-run on a fallback model inside the same call. *This is on by default; say if you'd rather not.*
- **Blocked models.** Fable and Mythos are never selected, whatever a config says, as before.

**Where the chain comes from:**
1. the run config's `ai.providers`;
2. the legacy `ai.provider` (the presets);
3. the workspace's providers;
4. the Claude Code subscription.

A provider that **bills separately** is used only when a config or the workspace names it, and never while `allow_billed_api` is explicitly `false` (the presets set that). A stray `ANTHROPIC_API_KEY` changes nothing, and the original tests for this still pass.

## 2. Keys (5.2)

- **Where keys live.** Keys are secrets (`core/secrets.py`, from Phase 3): env `LEADSCRAPER_SECRET_<NAME>` → a secret file → the OS keychain. Each provider also accepts its standard env variable.
- **Where keys never go.** Configs and settings carry only `key_secret_name`. Keys are never in the DB, logs, failure records, bundles or exports, and a settings entry containing a key is refused.
- **What the API shows.** It reports only whether a key is configured and where (`GET /ai/providers`, and `/health` booleans), plus `POST /ai/providers/{name}/test`, a 50-token connectivity call. CLI: `leadscraper secrets set NAME`, `ai providers`, `ai test NAME`.
- **A key goes only to the endpoint it belongs to** (`providers/registry._guard_key`). This matters because the API has no login (A.2.2).
  - A built-in provider's secret is refused for another kind or a custom `base_url`.
  - `key_env` comes from defaults only, and is dropped for a custom `base_url`.
  - `notify-*` secrets are refused.
  - Any other secret is bound to its endpoint by name: `ai-<site>-…`, so `ai-groq-key` only goes to `*.groq.com`.

  Without this, anyone who could reach the API could point an entry at their own server and receive our key. I found the first half of this myself; the review found the rest.

## 3. Budget and caching (5.3)

- **Pre-run cost estimate.**
  - `POST /estimate` and `--dry-run` include `ai`: calls and cost per feature, priced from the table and capped by the budget.
  - Each lead type's part is under `by_lead_type`.
  - A model the table doesn't list is priced at its provider's deliberately high `"*"` rate and flagged `priced: false`.
- **Hard per-run cap** (`ai.budget_usd`).
  - Before each call, the client holds the call's worst case against the cap **under a lock**: the prompt and schema (+30 %), the full answer, and the searches.
  - An answer the remaining budget can't cover in full gets a smaller `max_tokens`. Below 256 tokens the call isn't made, AI stops, and the run continues.
  - One cap is shared by every lead type (one `AIClient` per run, `LeadTypeContext.ai`).
- **Usage ledger.**
  - `ai_usage` rows per run, provider, model and feature: calls, cache hits, failures, tokens, searches, cost.
  - The runner writes deltas after each lead type's AI passes and at the end, so nothing counts twice and a resumed run adds to its own rows.
  - Config-assist calls are recorded with no run.
  - Exposed through `GET /ai/usage` and `GET /runs/{id}`; the Overview names the providers a run used.
- **Response cache.**
  - Keyed by provider kind + model + schema + prompt hash. Only validated answers are stored.
  - The pre-Phase-5 key (model + prompt) is still read for the Claude providers, so the presets' cache stays valid.

## 4. Features (5.4)

Each feature is named in `ai.features`. An empty list means the legacy `extract`, `names` and `venues`, exactly what the presets did; every new feature is opt-in.

| Feature | Where | What |
|---|---|---|
| `icp_builder` | `POST /ai/icp`, `leadscraper ai icp` | A brief becomes a draft config. It starts from a Skillminds offering (`ai/offerings.yaml`: custom software, web/app dev, ERP, staff augmentation, training), uses the judge tier, and is validated against `RunConfig`. If invalid, it is sent back once with the errors. **Returned for confirmation, never saved or run.** Without AI, the offering alone makes the draft. |
| `planner` | `POST /ai/plan`, `leadscraper ai plan` | Areas, query synonyms, title variants, and lead types / packs checked against what exists and applies. Suggestions plus a revised valid config, never applied. |
| `source_discovery` | `POST /ai/source-discovery`; `ai_directory` in runs | Candidate sources are stored as `proposed`. Never proposed: Upwork, Indeed, Glassdoor, LinkedIn, sites with a dedicated source, or (with web search) a site the search didn't return. Approval is `POST /ai/candidate-sources/{id}/decision` or `ai candidates approve ID`. Approved candidates are read by the **generic AI-assisted extractor**, `sources/ai_directory.py`. |
| `web_search_discovery` | `sources/ai_web_search.py` | The provider's web search as a discovery source (section 5). |
| `extraction_fallback` | after enrichment | The old `extract`, now on token-lean Markdown and grounded. |
| `normalisation` | after enrichment | Clean names (the old `names`), industry tags, a company-size **estimate**, and duplicate adjudication by the judge tier: look-alike pairs (same locality, names 80–91 % similar, phones not contradicting) are merged at ≥ 0.85 confidence, the lower id surviving. |
| `venue_classification` | venue runs | The old `venues`. |
| `signal_interpretation` | `ai.features.signals.interpret` | For the Phase 6 signal lead types: only the lead type's own signal types come back, each with a strength and a **verbatim evidence quote**, or it is dropped. |
| `lead_summary` | after enrichment | One factual line: what the organisation does and why it matched. Lines that address anyone, pitch, call to action, or carry a phone, email or link are rejected. |

## 5. Grounding guardrails (5.5)

- **Contact details must be on the page** (`ai/grounding.py`).
  - Phones are compared by the scraper's own phone rules, so formatting may differ but the number may not. Emails may be plain or lightly obfuscated. Addresses and names must appear contiguously. Handles need `instagram.com/<handle>` or `@handle`. Websites need the host as a URL token, and are stored as `scheme://host/`.
  - On a page that lists many businesses, every detail must sit **next to this business's name**, so a model can't hand business B's phone to business A.
  - Everything dropped is recorded as `ai_ungrounded_dropped`.
- **Web-search discovery keeps an organisation only when** its source URL came from the provider's search, we fetched that page ourselves (paced, robots.txt honoured, A.2.4 exclusions applied), and its name is on the page.
- **AI-estimated attributes are flagged** in data (`org_attributes`: `origin: ai`, `estimated: true` for the size band, with the producer `provider/model`) and in the workbook: **Summary (AI)**, **Industry (AI)**, **Company Size (AI estimate)**, with legend lines. They also appear in the API's organisation detail and in bundles.
- **Schema validation.** Every call names a pydantic model. Native structured output is used where declared, JSON + validation elsewhere. Invalid output is retried once with the reason, then skipped (`ai_invalid_output`, which counts towards the circuit breaker).

## 6. Extraction techniques (5.6)

ScrapeGraphAI is not a dependency; everything here is native.

**Token-lean page preparation** (`ai/pageprep.py`):
- **Removed:** scripts, styles, SVG, iframes, navigation and boilerplate (cookie banners, modals, newsletters, breadcrumbs, share bars). A block that links the business's phone, email or profiles is kept anyway; the review of my own test caught a share bar holding the business's Instagram.
- **Output:** compact Markdown, with links only where the target informs (`tel:`, `mailto:`, WhatsApp, social profiles, other sites), plus the business JSON-LD on one line. Repeats are dropped and the result is chunked to the model's context.
- **Grounding text:** the original text, with every href, is kept separately for grounding.

| Fixture page | Raw HTML tokens | Prepared | Saved |
|---|---|---|---|
| `caterer_contact.html` | 23,784 | 150 | 99.4 % |
| `clinic_home.html` | 28,592 | 884 | 96.9 % |
| `directory_v1_page1.html` | 24,575 | 262 | 98.9 % |
| `directory_v1_page2.html` | 24,155 | 178 | 99.3 % |
| `directory_v2_page1.html` | 22,873 | 245 | 98.9 % |
| `directory_v2_page2.html` | 22,637 | 158 | 99.3 % |
| **All** | **146,616** | **1,877** | **98.7 %** |

**Schema-driven extraction** (`ai/schema.py`): the pydantic model becomes a strict JSON Schema (references inlined, all properties required, optional ones nullable, no extra properties), or Gemini's subset.

**Self-healing selectors** (`ai/selectors.py`, migration 0004 table `selector_sets`):
- Selectors are stored per (host, page type), versioned and dated.
- **First contact:** the worker reads the page, then proposes CSS selectors from a trimmed HTML sample. They are accepted only when they reproduce its records on that page *and* on a second page of the same layout.
- **Afterwards** they run by rule, at no AI cost.
- **When stale** (matching nothing): a `selectors_stale` failure goes into source health, the selectors are regenerated once per run, and per-page AI is the fallback (`selectors_failed`).

## 7. Wiring

- **Runs.**
  - The runner builds one `AIClient` per run, hands it to each lead type, publishes `ai_spend` and flushes the ledger.
  - The `local_business` pipeline runs normalisation and summaries after extraction.
  - Name polish falls back to the rules for every name the model didn't answer, which is what makes "failing AI == AI off" hold.
  - The website enricher hands the AI token-lean Markdown and keeps the original page for grounding.
- **Sources.** `ai_web_search` and `ai_directory` are core sources, built only when their AI feature is on (`planning.ai_inactive_reason`), and listed in the catalog with notes.
- **Database.** Migration **0004** adds `org_attributes`, `candidate_sources` and `selector_sets`, plus `ai_usage.cache_hits` / `failures` / `searches`. `LeadStore` gains `set_attribute`, `attributes` and `fold_pair` (judge-approved merges); consolidation carries attributes along.
- **API:** 11 new operations under `/api/v1/ai/…`, 79 in all, every one with examples. **CLI:** `leadscraper ai providers | test | offerings | icp | plan | discover-sources | candidates | usage`.
- **Settings:** `ai.providers`, `ai.prices`, `ai.assist_budget_usd`, `ai.assist_features`.
- **Docs:** new `docs/ai.md`; updates to `docs/api.md`, `docs/sources.md` (AI providers, verified 2026-10-07), `docs/engine.md`, `docs/lead-types.md`, `docs/deploy.md`, `README.md` and `CLAUDE.md`.

## 8. Postgres

A throwaway PostgreSQL 16 ran in Docker (`postgres:16-alpine`, bound to 127.0.0.1, removed afterwards).

| Check | Result |
|---|---|
| `tests/test_ai_{client,features,runs,selectors,guardrails}.py`, `test_services.py`, `test_api.py`, `test_workbook.py` | 110 passed. The first attempt failed two of my AI run tests: on Postgres the engine suite shares one database, and they counted attributes across it rather than per run. The tests were fixed; the code was not at fault. |
| The engine suite on a fresh database | 48 passed |
| `-m postgres` (round trip, migrations 0001 → 0004 over existing rows, both real stores into Postgres) | 6 passed |

## 9. Acceptance checks

| Check | Status | Evidence |
|---|---|---|
| Provider conformance tests (mocked HTTP) for all providers | **Pass** | `tests/test_ai_providers.py`, 27 cases. For each provider: the real request (URL, auth header, model, schema field, web-search tool, the Anthropic refusal-fallback beta header and `fallbacks`), parsing (text, tokens, sources, searches), and error mapping (401/403, 402 / insufficient quota, 429 with retries, 5xx, refusals). Transports: a `requests` transport adapter for OpenAI, Gemini and compatible endpoints; an `httpx2` mock transport under the official Anthropic SDK; a fake subprocess for the CLI. |
| A run with AI on but a failing provider completes identically to AI off | **Pass** | `test_ai_runs.py::test_ai_on_with_a_failing_provider_completes_identically_to_ai_off`: two full engine runs (fixture sources, enrichment, extraction, normalisation and summaries on); every provider call returns HTTP 500; the records are identical field by field. Removing the rule fallback makes it fail (checked). |
| The budget cap stops AI mid-run and the run continues | **Pass** | `test_the_budget_cap_with_a_billed_provider`: priced calls, $0.04 cap; some summaries are written, AI stops (`ai_disabled`), the run completes with all six leads, and ledger cost ≤ cap. `test_concurrent_calls_cannot_overshoot_the_cap` (8 threads) and `test_a_nearly_spent_budget_shortens_the_answer` pin the hard cap. |
| Hallucinated phones/emails (absent from source text) are rejected | **Pass** | `test_ai_guardrails.py::test_hallucinated_phones_and_emails_are_rejected`: through the pipeline, the model returns one real and one invented phone and email (plus an invented handle); only the real ones are stored, and the rest are recorded. Also `test_on_a_listing_page_contacts_must_sit_next_to_the_name` and the web-search tests. |
| `icp_builder` produces a schema-valid config from a sample brief | **Pass** | `test_ai_features.py::test_icp_builder_turns_a_brief_into_a_schema_valid_config`, through the API. The result validates as `RunConfig`; a field that isn't one (`fax`) is dropped; a later-phase lead type is noted, not configured; nothing is saved or run. Also the retry-on-invalid and no-AI-preset cases. |
| Page preparation cuts input tokens ≥ 50 % vs raw HTML, with no loss of target fields | **Pass** | `test_ai_pageprep.py`: 96.9–99.4 % per page, 98.7 % overall (section 6). Name, phone, email (or its obfuscated form), address and website stay in the Markdown, and the phone grounds. |
| Self-healing test: generated and validated on one fixture; reused on a second with zero AI calls; regenerated after a simulated redesign | **Pass** | `test_ai_selectors.py::test_generated_reused_and_regenerated`, counting the model's calls: v1 page 1 + page 2 gives v1 selectors; v1 page 2 is read by rule with no calls; the v2 redesign gives `selectors_stale` then v2 selectors; v2 page 2 is read by rule. Also covered: a second failure in one run falls back to per-page AI, rejected selectors, invented records dropped, and persistence across runs in the DB. A real run reads an approved candidate with them (`test_an_approved_candidate_is_scraped_by_the_generic_extractor`: 14 leads, 3 AI calls). |

## 10. Review and the fixes it led to

An independent review of the Phase 5 diff (a separate reviewer agent, reading code and running the AI tests) reported 5 high, 4 medium and 4 low findings. Each was checked against the code, and each fix has a regression test. The eight new tests fail on the pre-fix code (checked by restoring the old files).

| Finding | Fix | Test |
|---|---|---|
| **`ai_web_search` fetched LinkedIn / Indeed pages** the search returned (A.2.4); a redirect or "next" link could lead `ai_directory` there too | Excluded hosts are never fetched, including after a redirect | `test_web_search_never_fetches_excluded_sites` |
| **Page-level grounding on listing pages**: business B's phone could be attached to A | Details must sit next to the business's name (`SourceText.near`); records read by rule stay structural | `test_on_a_listing_page_contacts_must_sit_next_to_the_name` |
| **Handle / host checks were substring tests** (`@contact`, `a.com` in `banana.com`) | `instagram.com/<handle>` or `@handle`; host as a URL token; site stored as `scheme://host/` | `test_handles_and_hosts_need_a_real_link_on_the_page` |
| **The cap could be overshot**: an unlocked check-then-spend across worker threads, and an answer projected at half the prompt rather than `max_tokens` | The worst case is held under the lock; answers are clamped to what is left | `test_concurrent_calls_cannot_overshoot_the_cap`, `test_a_nearly_spent_budget_shortens_the_answer` |
| **Any non-default secret could be sent to any `base_url`** | Secrets are bound to their endpoint by name (`ai-<site>-…`) | `test_a_key_is_never_sent_to_an_endpoint_it_does_not_belong_to` |
| An Anthropic rate limit dropped the provider for the whole run | It now skips the call; only credit/billing messages drop the provider | `test_anthropic_errors_map_onto_the_chain` |
| Summaries could carry phone numbers, emails or links (ungrounded) | Rejected | `test_summaries_never_carry_contact_details` |
| A model `profile_url` could be `javascript:` | Only http(s) links that are on the page | `test_model_profile_links_must_be_http_links_on_the_page` |
| Invalid output never tripped the circuit breaker | It counts as a failure | `test_invalid_output_counts_towards_the_circuit_breaker` |
| AI failure rows (`ai_call_failed` …) differ between an AI-failing run and an AI-off run's `skipped.csv` / Sheet "Skipped" tab | **Not changed.** The records are identical; the failure log differs only by AI rows, as it did before Phase 5 (the old `_call` wrote `ai_call_failed` too). The acceptance test compares the records. | — |
| Low: the ledger holds its lock during DB I/O; the legacy cache key has no schema name; `ai_directory` may re-read page 2 by AI after rejected selectors (the cache absorbs it) | Left as is; noted | — |

## 11. Intended behaviour changes, with reasons

None of these touch the characterisation results or the golden workbooks.

- **The AI layer is `ai/providers` + `ai/client.py`.** `AIAssistant` keeps its API and its prompts byte for byte. Legacy configs run as before:
  - `provider: claude_cli` maps to `claude_code_cli`, and `anthropic_api` to `anthropic`;
  - `max_cost_usd` and `features: [extract, names(, venues)]` work as before.
- **AI name polish now falls back to the rules** for names the model didn't answer. Before, a failing AI left keyword-stuffed names uncleaned; now the result equals AI off.
- **AI extraction is grounded.** A value not on the page is dropped even when it is well-formed. The old path only re-validated formats. As a result, AI-read addresses are kept only when they appear on the page.
- **`ai.features` and `ai.provider` are validated.** An unknown feature or provider name is now a config error instead of being silently ignored.
- **The `quick` depth** drops `names`, `normalisation` and `lead_summary`. With nothing left it turns AI off, because an empty list would mean the default set.
- **The default judge model is `claude-opus-5-5`.** The presets keep their explicit `claude-opus-5`.
- **The Anthropic provider uses the official SDK 1.x** (extra `ai`, and now dev). The old `anthropic>=0.40` pin is gone.
- **Two new core sources, `ai_web_search` and `ai_directory`,** are registered last in run order and built only with their AI feature. `test_country.py` was updated for them.
- **`/estimate`'s `ai` block** caps the total at the shared budget and adds `cap_usd`.

## 12. Deviations from the plan, with reasons

- **Branch base:** `phase-4/excel`, not `main` (see the top).
- **CSS only, no XPath, for self-healing selectors.** One syntax keeps validation simple, and CSS covers listing layouts (`@attr` reads attributes).
- **The planner is config-time only.** Its suggestions are returned for confirmation and never applied during a run, the same rule as the ICP builder.
- **Signal interpretation is a function, not yet called by any run.** The lead types it serves arrive in Phase 6. It is tested on fixture items.
- **Page-preparation fixtures are fictional pages,** not pages recorded from live sites: the repo must never hold real leads. They are built by `tools/make_ai_fixture_pages.py` with the chrome real sites carry (inline CSS, tag-manager scripts, JSON blobs, menus, cookie banners, pop-ups, SVG, ads, JSON-LD). Real pages vary; the 50 % bar has a wide margin.
- **Gemini uses `generateContent` with `responseSchema`.** The docs now also show an Interactions API with `response_format`, and I could not confirm `responseJsonSchema` in the generateContent reference, so the documented subset is used and validated with pydantic.
- **The budget cap is exact on the answer and estimated on the prompt:** about 4 characters a token, plus the schema, plus 30 %. A provider tokenising far denser could overshoot by a fraction of one call.
  - The `claude` CLI reports its own cost, which includes the CLI's own context, so it can exceed the hold.
- **Local OpenAI-compatible endpoints (private addresses) are allowed.** Ollama and LM Studio are a main use case. Phase 3 refuses private hosts for notification targets; here only an AI call goes there, and a key never does unless it is bound to that endpoint.
- **Config-assist calls are synchronous** in the API, unlike previews. They are single model calls, not scraping, so A.3 ("the API never scrapes") holds.

## 13. Open issues

- **One real AI call was made by accident.**
  - **What happened:** while smoke-testing the CLI in a scratch data directory, `leadscraper ai icp "We sell ERP to manufacturers in Pune"` ran through your local `claude` CLI. It is the default chain when no workspace providers are set.
  - **What it cost:** one judge-tier call (`claude-opus-5-5`) drawn from your Claude Code subscription. The list-price equivalent was $0.03; there is no separate bill.
  - **What it showed:** the CLI provider works end to end, and it returned a valid draft.
  - **Tests are unaffected:** they disable the CLI and every provider key (`tests/conftest.py::_no_real_ai`).
- **Live provider checks are still to do.** No key is configured here, so `tests/test_ai_live.py` skipped.
  - Run `pytest -m live tests/test_ai_live.py` after `leadscraper secrets set anthropic-api-key` (and openai / gemini). It makes one ≤ 50-token call per configured provider, asserted under $0.10 in total.
  - Two values are unconfirmed:
    - the default OpenAI and Gemini model ids, taken from their pricing pages on 2026-10-07;
    - the Anthropic web-search price in `defaults.yaml` ($10 / 1,000 searches), which I did not re-verify.
- **CI has not run on this branch.** The `anthropic` SDK is now in the dev extras, so CI will install it. A run on macOS, Windows and Linux should confirm the new tests.
- **Merge order:** `phase-3/api` → `main`, then `phase-4/excel`, then `phase-5/ai`. `fix/live-check` is still open too.
- **Candidate sources and selector sets are not in bundles yet.** AI attributes are. Approvals don't travel between machines except in server mode.
- **Approving a candidate source is an A.2.4 decision** (its terms are the approver's call). Each candidate carries the model's `terms_note`; nothing checks it.
- **`organizations.score` still doesn't include other lead types,** as noted in the Phase 4 report.
- **From earlier phases, still open and still yours:**
  - the terms-of-service review of the inherited sources;
  - the VayaVia phone-similarity threshold;
  - running `migrate-legacy` into your real data directory;
  - checking the real database for the Phase 2 migration bug;
  - a balanced-depth live check;
  - opening the Phase 4 samples in Excel.

Commits on this branch (after `phase-4/excel`):
- `feat(ai): one provider interface (Anthropic SDK, OpenAI, Gemini, OpenAI-compatible, Claude Code CLI); per-run client with fallback chain, hard USD cap, cache, ledger, schema validation`
- `feat(ai): grounded features in runs -- extraction fallback, normalisation, summaries, web-search discovery, generic directory extractor with self-healing selectors`
- `feat(api): AI providers and keys, ICP builder, planner, source discovery, candidate sources, usage; AI columns in the workbook`
- `fix(ai): a key is only ever sent to the endpoint it belongs to`
- `docs: the AI layer (docs/ai.md), API, sources (AI providers verified 2026-10-07), engine, lead types, deploy, README, CLAUDE.md`
- `fix: findings of the Phase 5 review`
- `test: scope the AI run tests to their run (the engine suite shares one Postgres database)`
- and this report.
