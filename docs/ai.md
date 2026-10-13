# The AI layer

Optional, opt-in and budgeted. AI makes runs smarter and more robust; it never
makes them fragile, and it never invents facts. A run with AI on and every
provider failing ends exactly where the same run with AI off ends (tested).

Code: `leadscraper/ai/`. Plan: `APP-BUILD-PROMPT.md` Phase 5.

```
ai/providers/     one interface; anthropic (SDK), openai, gemini, openai_compatible, claude_code_cli
ai/defaults.yaml  default models, capabilities and list prices (data, not code)
ai/settings.py    config + workspace settings -> the provider chain, features, cap, prices
ai/client.py      the run's client: chain, budget, cache, ledger, schema validation
ai/cache.py, ai/ledger.py, ai/prices.py, ai/schema.py, ai/catalog.py (features -> tasks)
ai/grounding.py   ai/pageprep.py   ai/selectors.py
ai/features/      icp_builder, planner, source_discovery, normalisation, signals, summary
ai/assistant.py   the local_business batch tasks (extract, names, venues, dedup), prompts unchanged
sources/ai_web_search.py, sources/ai_directory.py
services/ai.py, api/routers/ai.py, `python -m leadscraper ai ...`
```

## Providers

| Kind | API | Auth | Structured output | Web search | Bills separately |
|---|---|---|---|---|---|
| `anthropic` | Messages API via the official `anthropic` SDK (extra `ai`) | key | `output_config.format` | server tool `web_search_20260209` (Opus/Sonnet 4.6+), `web_search_20250305` (Haiku 4.5) | yes |
| `openai` | Responses API, `POST /v1/responses` | key | `text.format` json_schema, strict | `web_search` tool | yes |
| `gemini` | `models.generateContent` (v1beta) | key (`x-goog-api-key`) | `responseMimeType` + `responseSchema` | `googleSearch` tool | yes |
| `openai_compatible` | Chat Completions at `base_url` (OpenRouter, Groq, Ollama, LM Studio) | key, or none for a local endpoint | only with `structured_output: true` on the entry | no | remote: yes; local: no, and free |
| `claude_code_cli` | `claude -p --output-format json`, tools stripped | your Claude Code subscription | JSON + validation | no | no (its list-price equivalent counts against the cap) |

Each provider has two tiers: a **worker** model for bulk calls (extraction,
tags, summaries) and a **judge** model for hard calls (duplicate adjudication,
the ICP builder). Defaults (`ai/defaults.yaml`, verified 2026-10-07):

| Provider | Worker | Judge |
|---|---|---|
| anthropic, claude_code_cli | `claude-haiku-4-5` | `claude-opus-5-5` |
| openai | `gpt-6-luna` | `gpt-6-astra` |
| gemini | `gemini-3.5-flash-lite` | `gemini-3.1-pro-preview` |
| openai_compatible | (you name them) | (you name them) |

Claude Opus 5.5 / Opus 5 / Sonnet 5.5 calls opt into Anthropic's **server-side
refusal fallback** (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`):
a request a safety classifier declines is re-run on a fallback model inside the
same call. A refusal that survives is a `Refused` error: that item is skipped,
the provider stays in the chain. Fable and Mythos models (billed as extra
credits) are never selected, whatever a config says.

### The fallback chain

A run uses the first of:

1. the run config's `ai.providers` (names or entries, first preferred);
2. the legacy single `ai.provider` (`claude_cli`, `anthropic_api`) -- the presets;
3. the workspace's `ai.providers` (`PATCH /settings`);
4. the Claude Code subscription (`claude_code_cli`); with `allow_billed_api: true`,
   the Anthropic API first (the old `auto`).

```yaml
ai:
  enabled: true
  budget_usd: 2.00
  features: [extraction_fallback, normalisation, lead_summary]
  providers:
    - anthropic                                   # defaults from ai/defaults.yaml
    - {name: groq, kind: openai_compatible, base_url: https://api.groq.com/openai/v1,
       worker_model: llama-4-scout, key_secret_name: ai-groq-key}
    - claude_code_cli
```

What the chain does with each error:

| Error | Example | Effect |
|---|---|---|
| not configured | no key, no SDK, no `claude` on PATH | provider left out (shown in `GET /ai/providers`) |
| credentials refused | 401 / 403 | dropped for the run (`ai_provider_dropped`) |
| out of credit / rate-limited past retries | 402, `insufficient_quota`, 429 after 2 retries | dropped for the run |
| refused | safety decline | item skipped, provider kept (`ai_refused`) |
| other error | 5xx, timeout, bad model id | next provider for this call (`ai_call_failed`) |

When no provider is left -- or after `failure_limit` (4) consecutive calls no
provider answered -- AI stops for the rest of the run (`ai_disabled`, with the
reason) and the run continues without it.

**Spending money is an explicit choice.** A provider that bills separately is
used only when a config or the workspace names it, and never while
`ai.allow_billed_api` is explicitly `false` (the Mumbai and Nashik presets set
that). An `ANTHROPIC_API_KEY` sitting in the environment changes nothing.

## Keys

Keys are secrets (`core/secrets.py`): `LEADSCRAPER_SECRET_<NAME>` → a file in
`LEADSCRAPER_SECRETS_DIR` or `/run/secrets` → the OS keychain. Each provider
names its secret (`key_secret_name`; defaults `anthropic-api-key`,
`openai-api-key`, `gemini-api-key`) and also accepts its standard environment
variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`).

```bash
python -m leadscraper secrets set anthropic-api-key      # prompts; stored in the keychain
python -m leadscraper ai providers                       # key: yes / NO, usable or why not
python -m leadscraper ai test anthropic                  # one tiny call
```

- Keys are never written to the database, logs, failure records, bundles or
  exports. Configs and settings hold only the secret's *name*; a settings
  entry that contains a key is refused.
- The API reports only whether a key is configured and where (env / file /
  keyring), plus `POST /ai/providers/{name}/test`.
- **A key goes only to the endpoint it belongs to.** The API has no login yet
  (A.2.2), so a provider entry cannot borrow another endpoint's key: a built-in
  provider's secret is refused with a custom `base_url` or another kind,
  `key_env` comes from defaults only (and is dropped for a custom `base_url`),
  notification secrets (`notify-*`) are refused, and any other secret is bound
  to its endpoint by name: `ai-<site>-...`, where `<site>` is the base URL's
  registrable name (`ai-groq-key` for `https://api.groq.com/openai/v1`). So an
  entry can never send one endpoint's key to another.

## Budget, prices, cache, ledger

- **Hard cap per run** (`ai.budget_usd`, default $2; `null` = no cap). Before
  every call the client holds the call's worst case against the cap, under a
  lock (so calls on parallel worker threads can't all pass against the same
  spend): the prompt and schema (estimated at ~4 characters a token, plus a
  30 % margin), the answer at its full `max_tokens`, and its web searches. An
  answer the remaining budget can't cover in full gets a smaller `max_tokens`;
  below 256 tokens the call isn't made and AI stops for the rest of the run,
  which continues. The answer side is exact; the prompt side is an estimate,
  so a provider tokenising far above 4 characters a token could overshoot by a
  fraction of one call. One run, one cap, shared by every lead type.
- **Prices are data.** `ai/defaults.yaml` <- workspace `ai.prices` <- the run's
  `ai.prices`, USD per million tokens, plus per 1,000 web searches. A model the
  table doesn't list costs its provider's `"*"` rate, a deliberately high guess
  (`priced: false` in the estimate). A local endpoint is free.
- **Pre-run estimate.** `POST /estimate` (and `--dry-run`) returns `ai`: calls
  and cost per feature, capped by the budget.
- **Cache.** `<data_dir>/cache/ai_cache.sqlite3`, keyed by provider kind +
  model + schema + prompt hash; only validated answers are stored; hits cost
  nothing. The pre-Phase-5 cache (model + prompt) is still read for the Claude
  providers, so the presets' cached answers stay valid.
- **Ledger.** `ai_usage` rows per run, provider, model and feature: calls,
  cache hits, failures, tokens, searches, cost. `GET /ai/usage[?run_id=]`;
  `GET /runs/{id}` has the run's rows; the workbook's Overview names the
  providers a run used and its spend. Config-assist calls are recorded with no run.

## Features

Each is switched on by name in `ai.features`. An empty list means the legacy
set -- `extract`, `names`, `venues` -- so the presets behave exactly as before;
every new feature is opt-in.

| Feature | When | What |
|---|---|---|
| `icp_builder` | config time (`POST /ai/icp`, `leadscraper ai icp`) | A brief → a draft run config (schema-validated; sent back once with the errors if invalid), starting from a Skillminds offering (`GET /ai/offerings`: custom software, web/app dev, ERP, staff augmentation, training). **Returned for confirmation, never saved or run.** Without AI, the offering alone makes the draft. |
| `planner` | config time (`POST /ai/plan`) | Sub-areas for a place, search synonyms per category, title variants per role, lead types and packs that fit (checked against what exists and applies to the country). Suggestions plus a revised valid config; nothing applied. |
| `source_discovery` | config time (`POST /ai/source-discovery`) + runs | Proposes directories and feeds as **candidate sources** (`proposed`). Never Upwork, Indeed, Glassdoor or LinkedIn, nor sites with a dedicated source; with web search, never a site the search didn't return. A user approves (`POST /ai/candidate-sources/{id}/decision`); approved ones are read by the `ai_directory` source in matching runs. |
| `web_search_discovery` | runs (`ai_web_search` source) | The provider's web search finds businesses; each is kept only if its source URL came from the search, we fetched that page ourselves (paced, robots.txt), its name is on it, and each contact detail is on it. |
| `extraction_fallback` (`extract`) | after enrichment | Reads contacts off pages whose selectors found nothing, from token-lean Markdown, grounded. |
| `normalisation` (`names`, `industry`, `size`, `dedup`) | after enrichment | Clean names; industry tags; a company-size **estimate**; duplicate adjudication by the judge tier (look-alike pairs the rules kept apart; merged only at ≥ 0.85 confidence). |
| `venue_classification` (`venues`) | venue runs | Types venues the rules could not. |
| `signal_interpretation` | for signal lead types (Phase 6, 8) | `ai.features.signals.interpret(client, items, specs)`: a signal type from the lead type's own specs, a strength, and a verbatim evidence quote (or it is dropped). `with_company=True` (funding_news) also asks which organisation the item is about, kept only when the item contains that name verbatim; without it the prompt is unchanged (hiring's cache stays valid). Phase 8: `ai.features.review_themes.classify` (app_store) places low-star app reviews the rules couldn't into the fixed complaint themes, each kept only with a verbatim quote from the review; same task (`signals`), cap and ledger. |
| `lead_summary` | after enrichment | One factual line: what the organisation does and why it matched. No outreach copy: a line that addresses anyone, pitches or calls to action is rejected. |

Config-time features run under the workspace's `ai.assist_budget_usd` (default
$0.50 per request) with the workspace's providers. The `quick` depth drops
`names`, `normalisation` and `lead_summary`.

## Grounding guardrails

- **Contacts.** An AI-supplied phone, email, address, handle or website is kept
  only when it appears in the text we fetched (`ai/grounding.py`): phones by the
  scraper's own phone rules (so formatting may differ, the number may not),
  emails plain or lightly obfuscated, addresses and names contiguously (case,
  punctuation and spacing aside). Everything dropped is recorded as
  `ai_ungrounded_dropped`. Tested with hallucinated phones and emails.
- **Estimates are labelled.** AI attributes live in `org_attributes`
  (`origin: ai`, `estimated: true` for the size band), never among published
  facts. The workbook shows them as **Summary (AI)**, **Industry (AI)** and
  **Company Size (AI estimate)**; the API's organisation detail lists them
  under `attributes` with their producer (`provider/model`).
- **Schema-validated.** Every call names a pydantic model; native structured
  output is used where the provider declares it, JSON + validation elsewhere.
  Invalid output is retried once, with the reason, then skipped (`ai_invalid_output`).

## Extraction techniques (native, not ScrapeGraphAI)

- **Page preparation** (`ai/pageprep.py`): scripts, styles, SVG, iframes,
  navigation and boilerplate (cookie banners, modals, newsletters, breadcrumbs,
  share bars) go -- unless a block links the business's phone, email or
  profiles; what is left becomes compact Markdown (headings, paragraphs, list
  items, table rows; links only where the target is informative); the page's
  business JSON-LD is kept on one line; repeated lines are dropped; the result
  is chunked to the model's context. On the fixture pages it saves 97-99 % of
  the raw HTML's tokens with every target field kept (`tests/test_ai_pageprep.py`).
- **Schema-driven extraction** (`ai/schema.py`): the pydantic model as a strict
  JSON Schema (references inlined, every property required, optional ones
  nullable, no extra properties), Gemini's subset where needed.
- **Self-healing selectors** (`ai/selectors.py`, table `selector_sets`): per
  (host, page type), CSS with `@attr`. First contact: the model reads the page,
  proposes selectors from a trimmed HTML sample, and they are accepted only if
  they reproduce its records on that page and on one more page of the same
  layout. Then they run by rule at no AI cost. When they match nothing
  (`selectors_stale` in the source's health), they are regenerated once per
  run; failing that, per-page AI within budget (`selectors_failed`).

## Where AI usage shows

| | |
|---|---|
| Run log | `AI layer: active via anthropic -> claude_code_cli (...)`, or exactly why it is off |
| Events | `ai_spend` (cost, cap, calls) after each lead type's AI passes |
| Failures | `ai_*` reasons under source `ai`; `selectors_stale` / `selectors_failed` under `ai_directory` |
| API | `GET /ai/usage`, `GET /runs/{id}` (`ai_usage`), `GET /ai/providers`, `GET /health` (key booleans) |
| Workbook | Overview: AI on/off, providers, spend of cap; Records: the three AI columns when present |
