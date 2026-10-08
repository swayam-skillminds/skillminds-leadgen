# Sources

Every external endpoint the tool talks to: where it is, how it's used, what
it needs, how hard it's paced, terms notes, and when it was last checked
live. A.9.2: re-verify a source live before coding against it, and update
its row here.

The machine-readable twin of these tables is `leadscraper/sources/notes.py`
(served by `GET /api/v1/catalog/sources`, with each source's health from
recent runs); `tests/test_services.py` fails if a registered source has no
entry there. Keep both in step.

**Pack** decides where a source applies; see `leadscraper/packs/`. A source
whose pack doesn't apply to a run's country or verticals is reported as not
built, never as a failure. **Delays** are the defaults (seconds between
requests to that host, random in the range); a config can change them.

## Core (every country)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `google_maps` | `https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z?hl=en&gl={country}`, then place pages | Playwright, headless | none | 2.5–6 s, shared `google_maps` budget | Google's terms restrict automated access, and a paid Places API exists. **To review against A.2.4** | Code moved, not changed, in Phase 1; not re-verified |
| `google_search` | `https://www.google.com/search?q=…&gl={country}`, falling back to `https://html.duckduckgo.com/html/?kl={region}` | requests, then rendered, then DuckDuckGo | none | 12–28 s (Google), shared by every SERP user; circuit breaker after 3 total failures | Google's terms restrict automated queries; DuckDuckGo throttles with HTTP 202. **To review against A.2.4** | Not re-verified in Phase 1 |
| `website` (enricher) | the business's own site: home, then up to 3 contact/about pages | requests | none | 1.5–4 s per host | The business's public pages. **robots.txt honoured** (Phase 3, `net/robots.py`; RFC 9309 statuses; one probe per host per run; workspace setting `robots_txt`, on by default). A disallowed page is recorded as `robots_disallowed` | n/a (any site) |
| `maps_lookup` (enricher) | Google Maps search by venue name | Playwright | none | 2.5–6 s | As `google_maps` | Not re-verified in Phase 1 |
| Geocoder (`core/geo.py`) | `https://nominatim.openstreetmap.org/search?q=…&format=jsonv2&limit=1&addressdetails=1&countrycodes={cc}` | requests | none; identifying User-Agent required | at most 1 req/s; every answer, misses included, cached in `<data_dir>/cache/geocode.json` | [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/): ≤ 1 req/s, identify the app, cache results, no bulk geocoding. Used only for the few place names in a config | **2026-10-06**: 200 OK, `[{lat, lon, display_name, boundingbox, address{state, country_code}}]` for "Austin", US |

### AI sources (Phase 5; built only with their AI feature)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `ai_web_search` | the configured AI provider's web search, then each result page | provider API, then requests (robots.txt honoured) | the provider's key | provider calls unpaced; pages at the `website` pacing per host | A lead needs a source URL the search returned, a page we fetched, and its name and contacts on that page. Feature `web_search_discovery` | 2026-10-07: provider API docs (below); no live call |
| `ai_directory` | approved candidate sources (`/ai/candidate-sources`) | requests; self-healing CSS selectors, AI only for a new or changed layout | none | at least 3–8 s per directory host | Each candidate's terms are the approver's call (its `terms_note`); robots.txt honoured; never login walls; Upwork, Indeed, Glassdoor and LinkedIn are never proposed. Feature `source_discovery`. **Each approval is an A.2.4 decision** | n/a (per candidate) |

## AI providers (Phase 5; `docs/ai.md`)

Re-verified against the providers' own documentation on **2026-10-07** (A.9.2). No live call was made: no key was configured on this machine. `tests/test_ai_live.py` (`pytest -m live`) makes one tiny call per configured provider, under $0.10 in total.

| Provider | Endpoint | Auth | Structured output / web search | Rate limits | Terms note |
|---|---|---|---|---|---|
| Anthropic | `POST https://api.anthropic.com/v1/messages` via the official `anthropic` SDK 1.x (`anthropic-version: 2023-06-01`) | `x-api-key` | `output_config.format` (json_schema); server tool `web_search_20260209` / `web_search_20250305`; refusal fallback beta `server-side-fallback-2026-07-01` | per account tier; 429 `rate_limit_error`, 529 `overloaded_error` | Pay-as-you-go API billing, separate from a Claude subscription |
| OpenAI | `POST https://api.openai.com/v1/responses` | `Authorization: Bearer` | `text.format` json_schema (strict); tool `web_search`, sources via `include: ["web_search_call.action.sources"]` | per tier; 429 `rate_limit_exceeded` vs `insufficient_quota` | Pay-as-you-go; web search $10 / 1k calls |
| Google Gemini | `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | `x-goog-api-key` | `generationConfig.responseMimeType` + `responseSchema`; tool `googleSearch` (grounding chunks are redirect links; their titles are the source domains) | per project; 429 `RESOURCE_EXHAUSTED` | Grounding with Google Search $14 / 1k requests (Gemini 3) after a free allowance |
| OpenAI-compatible | `POST {base_url}/chat/completions` (OpenRouter, Groq, Ollama, LM Studio) | `Authorization: Bearer`, or none locally | `response_format` json_schema when the entry declares it | the endpoint's | The endpoint's own terms |
| Claude Code CLI | `claude -p --output-format json` (CLI 2.1.x; flags checked with `claude --help`) | your Claude Code login | JSON + validation (`--json-schema` exists; off by default) | your plan's usage limits | Draws on the subscription allowance; no separate bill |

## `in` pack (India)

| Source | Endpoint | Method | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|
| `justdial` | `https://www.justdial.com/{City}/{query}-in-{locality}` | Playwright | 10–22 s | Blocks automated clients. `site:` SERP fallback is opt-in only | 2026-10-06 (Phase 0): 200 with a 14-byte body, i.e. blocked; recorded as a failure, not evaded |
| `sulekha` | `https://www.sulekha.com/{query}/{city}` | Playwright | 8–18 s | — | 2026-10-06 (Phase 0): 200, 551 KB |
| `indiamart` | `https://dir.indiamart.com/search.mp?ss={query}&cq={city}` | Playwright | 5–11 s | IndiaMART runs a seller API, not a search API | Not re-verified in Phase 1 |

City segments come from the pack (`packs/india/__init__.py`, e.g. Bengaluru
becomes "Bangalore" on Justdial).

## `events` pack (India's events trade)

| Source | Endpoint | Method | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|
| `wedmegood` | `https://www.wedmegood.com/vendors/{city}/{slug}/?page=N` and profile pages | plain GET first, then Playwright | 4–9 s | Category slugs are read from the live listing, not hard-coded | 2026-10-06 (Phase 0): `/vendors/mumbai/` 200 |
| `weddingwire` | `https://www.weddingwire.in/{slug}/{city}?page=N` | Playwright | 4–9 s | — | 2026-10-06 (Phase 0): 403 to a plain client; the browser path is used |

## `hospitality` pack

| Source | Endpoint | Countries | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|
| `stayvista` | `https://www.stayvista.com/villas-sitemap.xml`, then property pages | IN | 2–5 s | Sitemap-driven | Not re-verified in Phase 1 |
| `saffronstays` | `https://www.saffronstays.com/sitemap-properties.xml` | IN | 2–5 s | Sitemap-driven | Not re-verified in Phase 1 |
| `elivaas` | `https://www.elivaas.com/sitemap.xml` | IN | 2–5 s | Sitemap-driven | Not re-verified in Phase 1 |
| `airbnb` | `https://www.airbnb.co.in` search pages per `platform_searches` place | any | 8–16 s | Airbnb's terms prohibit scraping. **To review against A.2.4** | Not re-verified in Phase 1 |
| `booking` | `https://www.booking.com` search results per place and property type | any | 8–16 s | Booking.com's terms prohibit scraping, and an affiliate API exists. **To review against A.2.4** | Not re-verified in Phase 1 |

## `social` pack (every country)

| Source | Endpoint | Method | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|
| `facebook` | SERP `site:facebook.com …`, then `https://mbasic.facebook.com/{page}` | requests | 6–14 s | Public pages only. A login wall is recorded as `login_wall`, never logged into | Not re-verified in Phase 1 |
| `instagram` (enricher) | `https://www.instagram.com/{handle}/` | requests | 8–20 s, at most 150 profiles a run | Public profiles only, never logged into. Rate-limits fast | Not re-verified in Phase 1 |
| `linkedin_serp` | SERP `site:linkedin.com/in "…"` | via `google_search` | 15–30 s | **LinkedIn is never fetched**: only profile URLs a search engine already indexed are recorded (A.2.4) | n/a |

## `hiring` lead type (Phase 6.2; every country)

Re-verified live on **2026-10-07** (A.9.2). Each ATS board is one company's
published postings, read through the JSON the ATS serves for career sites.
The tests use fictional fixtures in exactly these shapes; `pytest -m live
tests/test_hiring.py` reads one real public board per ATS and the current HN
thread (all passed on 2026-10-07).

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `greenhouse` | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs` (+ `/{token}` for the name) | requests, JSON | none | 1–2.5 s per API host | Greenhouse's Job Board API docs: board data is public, GET needs no auth. Each job carries `company_name` | **2026-10-07**: 200, `jobs[]` {title, absolute_url, location.name, first_published, updated_at, company_name}, `meta.total` |
| `lever` | `https://api.lever.co/v0/postings/{token}?mode=json` | requests, JSON | none | 1–2.5 s | Lever Postings API (github.com/lever/postings-api): published postings, public. No company name: the board token stands in | **2026-10-07**: 200, `[]` {text, hostedUrl, categories.location, workplaceType, createdAt (epoch ms)} |
| `ashby` | `https://api.ashbyhq.com/posting-api/job-board/{token}` | requests, JSON | none | 1–2.5 s | Ashby's public job posting API, meant for career sites (its docs). Unlisted jobs (`isListed: false`) are skipped | **2026-10-07**: 200, `jobs[]` {title, jobUrl, location, isRemote, workplaceType, publishedAt, isListed} |
| `workable` | `https://apply.workable.com/api/v1/widget/accounts/{token}` | requests, JSON | none | 1–2.5 s | The JSON behind Workable's documented embeddable jobs widget; not itself a documented API (the documented API needs the employer's token). **Off by default** (`lead_types.hiring.sources.ats.kinds`); opt in per config | **2026-10-07**: 200 for a real account (`{name, jobs[]}` with title, url, city, country, telecommuting, published_on); 404 otherwise |
| `smartrecruiters` | `https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=…` | requests, JSON | none observed | 1–2.5 s | Answers without a key for companies with a public feed (verified), but SmartRecruiters' docs now describe API-key/OAuth access for the Posting API. **Off by default** (`sources.ats.kinds`); opt in per config | **2026-10-07**: 200 without auth, `content[]` {id, name, releasedDate, location{fullLocation, remote}, company{name}} |
| `hn_whos_hiring` | `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring`, then `/api/v1/items/{thread id}` | requests, JSON; two requests per monthly thread | none | 1–2.5 s | The public HN Search API by Algolia; 10,000 requests/hour/IP is the commonly cited limit. Ads are posted by the hiring companies themselves | **2026-10-07**: 200; "Ask HN: Who is hiring? (October 2026)", 236 top-level ads |
| `careers_pages` | the organisation's own site: home, then one careers page | requests | none | 1.5–4 s per site | The business's public pages; robots.txt honoured; internal addresses refused. ATS boards found there are read through the ATS's API above | n/a (any site) |
| `board_search` (opt-in) | Google search (then DuckDuckGo) for `site:boards.greenhouse.io …` pages naming the place | via `google_search`'s SERP client, no browser | none | 12–28 s | As `google_search`: Google's terms restrict automated queries. **Off by default; to review against A.2.4** | not re-verified (same client as `google_search`) |

**Excluded** (A.2.4): Upwork, Indeed, Glassdoor, LinkedIn Jobs.

**Country job boards** (Phase 6.2 "in packs only where terms allow"): none
qualifies yet, so no pack carries one; the first that does would add a
hiring unit applicable to its countries.

| Board | Checked 2026-10-07 | Verdict |
|---|---|---|
| Naukri (IN) | Terms: no "automated program … spiders, robots, crawlers to access, … scrape, crawl or otherwise extract data" | Excluded |
| Arbeitnow (EU/UK; free keyless API at `/api/job-board-api`) | API works (200, 325 jobs/page); its terms' use licence forbids use "for any commercial purpose", and API use requires a link back | Excluded: we are a commercial lead generator |

## `digital_audit` lead type (Phase 6.1; every country)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `upload` | your own list: `targets`, `domains`, `targets_file`, `POST /audit/targets` | none | — | — | Your data | n/a |
| `site_audit` | the organisation's own site: one TLS handshake, the homepage (HTTPS, then HTTP), at most one contact page, a HEAD (or streamed GET) per checked homepage link (8 by default) | requests + `ssl` | none | 1–3 s per site; 6 sites at once | The business's public pages. **robots.txt honoured for every page and every link** (`runtime.robots_txt`, on by default); a site that refuses us is recorded and left unjudged. Hosts resolving to internal addresses (loopback, private, link-local, reserved) are refused, redirects included (`LEADSCRAPER_ALLOW_PRIVATE_TARGETS=1` opts in). Never logs in, never submits a form | **2026-10-07**: `pytest -m live tests/test_digital_audit.py` audits example.com (IANA's reserved site): reachable over HTTPS, valid certificate |
| `pagespeed` | `https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=…&strategy=mobile&category=performance&key=…` | requests, JSON | an API key (secret `pagespeed-api-key`): optional on paper, needed in practice | 1–2 s; at most 25 calls a run | Google's official, free API; its docs recommend a key for automated use. The key goes only to `www.googleapis.com` and is never logged or recorded | **2026-10-07**: keyless request answered 429 "Quota exceeded for quota metric 'Queries' and limit 'Queries per day'"; no key was available to test a 200 |

## Open questions for the project owner

A.2.4 says: never scrape sites whose terms prohibit it when a public API
exists. Several sources inherited from the original scraper sit close to
that line:

- Google Maps and Search: the Places and Custom Search APIs are paid.
- Airbnb and Booking.com.
- Instagram and Facebook: the Graph APIs are restricted.

Phase 6's three borderline sources are **off by default** (decided 2026-10-08):
`workable` (an undocumented widget endpoint) and `smartrecruiters` (its docs
now describe key-based access) run only when named in
`lead_types.hiring.sources.ats.kinds`; `board_search` (Google SERP) only with
`board_search.enabled`. A config that opts in takes that call for its run.

Phase 1 moved these sources into packs without changing their behaviour.
Whether each one stays, moves to an official API, or is retired is a
decision for the project owner. It's listed as an open issue in
`docs/phase-1-report.md`.
