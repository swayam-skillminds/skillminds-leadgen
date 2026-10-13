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

## `funding_news` lead type (Phase 6.3; every country)

Re-verified live on **2026-10-08** (A.9.2). Only headlines, links, dates and
feed summaries are used: no article is ever fetched. News APIs and feeds get
an identifying User-Agent (`core/geo.USER_AGENT`). `pytest -m live
tests/test_funding_news.py` makes one GDELT query and reads GlobeNewswire's
India feed.

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `gdelt` | `https://api.gdeltproject.org/api/v2/doc/doc?query=…&mode=artlist&format=json&sort=datedesc&maxrecords=…&timespan={days}d` | requests, JSON; a few queries of at most six OR terms per place; one attempt each | none | 6–9 s (it asks for ≤ 1 request per 5 s; the config can't go below 5) | [GDELT](https://www.gdeltproject.org/about.html): "unlimited and unrestricted use for any academic, commercial, or governmental use … without fee", with a citation of the GDELT Project. Matches the whole article text, so results are candidates; headline rules decide. A 429 or its "Please limit requests" text stops GDELT for the run | **2026-10-08**: 200, `articles[]` {url, title, seendate `20261002T083000Z`, domain, language, sourcecountry}; titles carry spaces around punctuation; "Your query was too short or too long" for a 17-term OR group; repeated 429s for this IP after a burst of probes |
| `google_news` (opt-in) | `https://news.google.com/rss/search?q=…&hl={locale}&gl={CC}&ceid={CC}:{lang}` | requests, RSS | none | 2–4 s | The feed's own `<copyright>`: "made available solely for the purpose of rendering Google News results within a personal feed reader for personal, non-commercial use. Any other use of the feed is expressly prohibited." **Off by default; enabling it needs Google's permission (A.2.4)** | **2026-10-08**: 200, RSS `<item>` {title "Headline - Outlet", news.google.com link, pubDate, `<source url>`} |
| `press_releases` (opt-in) | `https://www.globenewswire.com/RssFeed/country/{Country}/feedTitle/GlobeNewswire%20-%20News%20from%20{Country}` | requests, RSS; the issuer from `dc:contributor` | none | 2–4 s | robots.txt allows `/RssFeed/`; the feed carries no restriction; its terms page could not be found (`/Home/TermsOfUse` is a 404 page). **Off by default; to review**. A browser User-Agent is refused (HTTP/2 reset); an identifying one is answered | **2026-10-08**: 200 for India, United Kingdom, United States, United Arab Emirates; 20 releases each, every one with `dc:contributor` |
| `feeds` | RSS / Atom URLs in `lead_types.funding_news.sources.feeds.urls` | requests, RSS/Atom (lxml, entities unresolved, no network) | none | 2–4 s per feed host | Each feed's terms are the config author's call | n/a (any feed) |

**Not used** (checked 2026-10-08):

| Source | Why |
|---|---|
| PR Newswire RSS | Its site terms: "solely for your personal, noncommercial use"; no "robot, spider, site search/retrieval application" |
| Business Wire RSS | The home feed answers "The RSS channel you requested was deactivated" |
| Inc42 (IN) | Terms §8.1: no "screen scraping … data mining, … spidering", no automated tools without prior written consent |
| YourStory (IN) | Terms 2.5 / 2.6.17: no "robot, spider or other automatic device" |
| Entrackr (IN) | `/feed/` answers 404 |

So no regional startup feed qualified for a country pack; a licensed feed goes
in `feeds.urls`.

## `decision_makers` lead type (Phase 6.4)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `team_pages` | the organisation's own site: home, then up to 3 team / leadership / about pages it links | requests | none | 1.5–4 s per site; 4 sites at once | The business's public pages; robots.txt honoured; internal addresses refused. Only people the page names with a title | **2026-10-08**: a live run over three Pune IT companies' sites found 15 leaders from their leadership / company-overview pages (see `docs/phase-6b-report.md`) |
| `companies_house` (pack `uk`, GB) | `https://api.company-information.service.gov.uk/search/companies?q=…`, then `/company/{number}/officers` | requests, JSON; HTTP Basic, the key as user name | a free API key, secret `companies-house-api-key` (must start `companies-house`) | 0.6–1.2 s; the API allows 600 requests per 5 minutes | The UK registry's official public API, for real-time lookups (not bulk). Current directors and LLP members only | **2026-10-08**: 401 `{"error":"Empty Authorization header"}` without a key; no key here for a 200 (fixtures follow the documented shape) |
| `linkedin_search` (opt-in) | Google search (then DuckDuckGo) for `site:linkedin.com/in "<company>" (founder OR CEO OR …)` | via `google_search`'s SERP client | none | 12–28 s | **LinkedIn is never fetched or logged into**: only profile URLs and result titles a search engine indexed (A.2.4). Google's terms restrict automated queries: **off by default** | not re-verified (same client as `google_search`) |
| `github` (opt-in) | `https://api.github.com/orgs/{org}`, `/orgs/{org}/public_members`, `/users/{login}` | requests, JSON | none (60 requests/hour); optional token, secret `github-token` | 1–2 s | GitHub's official REST API. Its [Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) limit personal information to "the purpose for which that User has authorized it" and forbid using it for unsolicited email: **off by default**, and the email pattern is never applied to people found here | **2026-10-08**: 200 for `/orgs/github`, `/orgs/github/public_members`, `/users/defunkt` |
| `email_pattern` (opt-in) | DNS MX records of the organisation's own domain | dnspython, the system resolver; **no SMTP, ever** | none | — | The only inference A.2.5 allows: labelled `Inferred Email (unverified)`, never published, never matched on | **2026-10-08**: example.com has a null MX (`.`, RFC 7505: no mail); github.com has MX hosts |

**Company registries elsewhere:** India's MCA has no free public API (its
master-data search sits behind a captcha), so no Indian registry yet.

## `tenders` lead type (Phase 7.1; per country pack)

Public procurement notices. Each portal sits in the pack of the countries it
covers (`Pack.tenders`); elsewhere it is not built and the lead type plans
nothing for it. Every request carries the identifying `core.geo.USER_AGENT`.

| Source | Pack | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|---|
| `ted` | `eu` (EU 27 + IS, LI, NO) | `POST https://api.ted.europa.eu/v3/notices/search` with an expert query (`buyer-country=IRL AND notice-type IN (cn-standard …) AND publication-date>=… AND classification-cpv IN (72000000 …)`), `fields`, `limit` ≤ 250, `page` | requests, JSON | none (anonymous) | 1.5–3 s | TED's developer docs: the Search API is "primarily targeted at data reusers and does not require authentication". TED content may be reused, commercially too, with the source acknowledged. A CPV code finds its whole hierarchy | **2026-10-13**: 200, `notices[]` {publication-number, notice-title (every EU language), buyer-name, buyer-city, buyer-email, buyer-internet-address, deadline-receipt-tender-date-lot `2026-11-17+01:00`, estimated-value-*, classification-cpv, document-url-lot, links}; an unknown field is a 400 listing all 1,830 supported ones |
| `find_a_tender` | `uk` (GB) | `https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?stages=tender&updatedFrom=…&limit=100`, then `links.next` (a cursor) | requests, OCDS JSON | none | 1.5–3 s; 20 pages a run | Cabinet Office; the package's own `license`: Open Government Licence v3.0. No classification filter, so pages are read and sorted here. The buyer's site is its party's `details.url` only: a contact point's link is usually the e-tendering portal hundreds of buyers share | **2026-10-13**: 200, `releases[]` {id, date, tender{title, status, classification, items[].additionalClassifications, tenderPeriod.endDate, value, lots[].value, documents}, parties[] with role buyer, address, contactPoint}; notice pages `/Notice/{id}` answer 403 to curl (they open in a browser) |
| `contracts_finder` | `uk` (GB) | `POST https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json` `{"searchCriteria": {"types": ["Contract","Pipeline"], "statuses": ["Open"], "cpvCodes": […], "publishedFrom": "…"}, "size": 1000}` | requests, JSON | none | 1.5–3 s | Cabinet Office, Open Government Licence v3.0. England's lower-value contracts. A result has the buyer's name and postcode, not its contacts | **2026-10-13**: 200, `noticeList[].item` {id, title, organisationName, postcode, deadlineDate, valueLow, valueHigh, cpvCodes "72262000 72230000", noticeType, noticeStatus} |
| `sam_gov` | `us` (US) | `GET https://api.sam.gov/opportunities/v2/search?api_key=…&postedFrom=MM/dd/yyyy&postedTo=…&ptype=o&ptype=k&ptype=p&ptype=r&limit=1000[&state=TX]` | requests, JSON; **one attempt per request** | **a free personal API key**, secret `sam-gov-api-key` (must start `sam-gov`). Without it the unit is skipped: "key not configured" | 1.5–3 s; **one request a run** (`max_requests`) | GSA's official API. A non-federal user without a SAM.gov role gets **10 requests a day** (the rate-limit table in GSA's Entity API docs). The key must travel in the query string; requests' errors quote the URL, so the key is redacted from every error, and a request is never retried (no retry log line) | **2026-10-13**: docs read (open.gsa.gov); an empty 404 without a valid key (the docs list 404 as "No data found"), so no 200 was seen. Fixtures follow the documented response |
| `cppp` (opt-in) | `in` (IN) | `https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata`, then its "Next" links | requests, HTML table (ten tenders a page) | none | 3–6 s; 10 pages a run | No API; no robots.txt (404). The portal's copyright policy: material "maybe reproduced free of charge after taking proper permission from the respective Organisation / Department". The keyword search sits behind a CAPTCHA and is never used. **Off by default: the owner's call** | **2026-10-13**: 200; columns e-Published Date, Bid Submission Closing Date, Opening Date, Title/Ref.No./Tender Id, Organisation; times in IST without a zone |
| `gem` (opt-in) | `in` (IN) | `GET https://bidplus.gem.gov.in/all-bids` (the page's form token `csrf_bd_gem_nk`), then `POST /all-bids-data` with `payload={"page": n, "param": {"searchBid": "<keyword>", "searchType": "fullText"}, "filter": {"bidStatusType": "ongoing_bids", …}}`; documents `/showbidDocument/{id}` | requests, JSON behind the public listing; one search per keyword | none | 3–6 s; 2 pages × 8 keywords | robots.txt disallows only `/resources/` and some bank-guarantee paths. GeM's copyright policy: contents "may not be reproduced partially or fully without due permission in writing in advance". **Off by default: the owner's call** | **2026-10-13**: 200; "software" found 573 ongoing bids, ten a page {b_bid_number, b_category_name, final_start/end_date_sort "…T13:00:00Z" (1 pm IST), ba_official_details_minName/deptName} |

Not used: data.gov.in has no current tender dataset; state e-procurement portals
(India) and other national portals are not built.

## `registrations` lead type (Phase 7.2)

| Source | Pack | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|---|
| `companies_house` | `uk` (GB) | `GET https://api.company-information.service.gov.uk/advanced-search/companies?incorporated_from=…&incorporated_to=…&company_status=active&location=<place>&size=…[&sic_codes=…]`; directors from `/company/{number}/officers` | requests, JSON; HTTP Basic, the key as user name | **a free API key**, secret `companies-house-api-key` (shared with decision_makers). Without it the units are skipped: "key not configured" | 0.6–1.2 s (600 requests per 5 minutes allowed); 100 director lookups a run | The UK registry's official public API, for real-time lookups. `location` matches the registered office, so one unit per place. 404 means "No companies found" | **2026-10-13**: 401 `{"error":"Empty Authorization header"}` without a key; the API reference lists the parameters above and `size` up to 5,000. No key here for a 200: fixtures follow the documented shape |
| `opencorporates` (opt-in) | none (any country) | `GET https://api.opencorporates.com/v0.4/companies/search?country_code=…&incorporation_date=from:to&current_status=Active&per_page=100&api_token=…[&registered_address=<place>]` | requests, JSON; one attempt per request | a token, secret `opencorporates-api-token` (must start `opencorporates`) | 2–4 s; 2 pages a run | Every request needs a token. Its terms: free tokens only for "Permitted Users" (journalists, NGOs, academics; "corporations … are not Permitted Users"); commercial use needs a **paid** non-share-alike key; the database is ODbL. 200 requests a month, 50 a day by default. The docs don't say what a search without a name term returns (not verifiable without a token). **Off by default** | **2026-10-13**: 401 "Invalid Api Token. Please check your OpenCorporates account" without one; terms and API reference read |

**India:** no source. The MCA has no free API; data.gov.in's "Registrars of
Companies (RoC)-wise Company Master Data" (the newest company master data
there, refreshed 2026-07-22) says "The data upto 3rd November 2023": no new
incorporations to find (checked 2026-10-13 through the OGD platform).

## `tech_stack` lead type (Phase 8.1; every country)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `upload` | your list (`targets`, `domains`, `targets_file` from the CLI) | none | none | n/a | Your data | n/a |
| `site_fingerprint` | the organisation's own site: robots.txt, then its homepage (HTTPS, then HTTP) | requests (public-only client); the shared rules in `sources/web/fingerprints.yaml` over the HTML, meta generator, script / stylesheet URLs and response headers | none | 1–3 s per site; 6 sites at once | The business's public homepage, robots.txt honoured, one request at a time per site | n/a (any site). The rules' outdated thresholds were checked against **endoflife.date**'s API on **2026-10-13** (Angular 19 ended May 2026, 20 until Nov 2026; Vue 2 Dec 2023; TYPO3 12 Apr 2026; PHP 8.1 Dec 2025; SharePoint 2013 Apr 2023). Live smoke test: blog.mozilla.org → WordPress |
| `footprints` (opt-in) | Google search (then DuckDuckGo) for a technology's footprint phrases in a place: `"powered by Shopify" "Pune"`; each result's homepage is then fingerprinted | via `google_search`'s SERP client, then `site_fingerprint` | none | 12–28 s (shared SERP pacing, set only when enabled) | Google's terms restrict automated queries: **off by default**, the owner's call (as `board_search`). The technology's own sites, social profiles and directories are never candidates; a site is kept only when its homepage shows the technology | Not run live: Google and DuckDuckGo SERP are blocked from this network (see live check 2) |

**Other "who uses X" sources considered:** BuiltWith, Wappalyzer and PublicWWW
sell this data (paid APIs); HTTP Archive needs BigQuery. None is used.

## `app_store` lead type (Phase 8.2; every country)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `itunes` | `GET https://itunes.apple.com/search?term=…&country=…&entity=software&limit=…`; `GET https://itunes.apple.com/lookup?id=…&country=…` | requests, JSON; the identifying User-Agent | none | 3.5–5 s (Apple: "approximately 20 calls per minute") | Apple's documented Search API. Its terms govern promotional content (artwork, previews), none of which is kept. robots.txt disallows `/search*` and `/*/lookup?` for crawlers, and Apple's site terms forbid page-scraping "through any means not purposely made available": the API is that means, so **App Store web pages and the customer-review RSS (robots.txt: `/*/rss/*`) are never read**. **To review against A.2.4** | **2026-10-13**: 200; `trackId`, `trackName`, `sellerName`, `sellerUrl`, `averageUserRating` (0 when unrated), `userRatingCount`, `currentVersionReleaseDate`, `bundleId`, `primaryGenreName`, `description`; a `genreId` filter is ignored, so genres are filtered here. Live smoke test passes |
| `google_play` | `GET https://play.google.com/store/apps/details?id={package}&hl=en&gl={country}` | requests, HTML: JSON-LD `SoftwareApplication`, "Updated on", "<band> Downloads", the App support section, the reviews the page shows (never reviewers' names) | none | 2–4 s; 60 listings a run | robots.txt **allows** `/store/apps/details` and **disallows** `/store/search`; Google's terms forbid automated access "in violation of the machine-readable instructions on our web pages (for example, robots.txt files …)". So listings are read one at a time -- packages from organisations' sites, the config, or an App Store app's bundle id (kept only with the same developer website) -- and **Play is never searched**. **To review against A.2.4** | **2026-10-13**: 200 for a live listing (rating 4.49 from 278,696, "Updated on Oct 10, 2026", "10M+ Downloads", the developer's website, email, phone and address, 3 reviews); a missing package is a 404. Live smoke test passes |
| `listed` | the apps a config names | via `itunes` lookup and `google_play` | none | as those | Your list | n/a |
| `site_apps` | the organisation's own homepage, read for App Store / Play links | requests (robots.txt honoured), then `itunes` / `google_play` | none | 1–3 s per site; 4 at once | The business's public homepage | n/a (any site) |

**Not used:** Apple's App Store web pages and review RSS (site terms, robots.txt),
Google Play search (robots.txt), and third-party app-data APIs (paid).

## Keys: where to register

A source that needs a key and has none is **skipped, never tried**: its work
units are recorded `skipped` with "key not configured: … needs the secret …
(leadscraper secrets set …); get one at …", the estimate lists them under
"Not run", the run's failures hold one `key_not_configured` row per source,
`GET /catalog/sources` shows `key.configured: false` and the register link,
and `GET /health` lists the key as not configured.

| Secret | Used by | Where to get it | Cost / limits |
|---|---|---|---|
| `sam-gov-api-key` | tenders: `sam_gov` | https://sam.gov: create a login.gov account, sign in, open **Account Details**, enter your password, **Request Public API Key** | Free. 10 requests/day without a SAM.gov role; 1,000/day with a role (an entity registration) |
| `companies-house-api-key` | registrations: `companies_house`; decision_makers: UK registry | https://developer.company-information.service.gov.uk: register, **create an application**, add a **REST** API key | Free. 600 requests per 5 minutes |
| `opencorporates-api-token` | registrations: `opencorporates` (opt-in) | https://opencorporates.com/api_accounts/new | Free only for public-benefit users; commercial use needs a paid plan |
| `pagespeed-api-key` | digital_audit: `pagespeed` | Google Cloud console: enable the PageSpeed Insights API, create an API key | Free quota |

Set one with `leadscraper secrets set <name>` (local keychain), or as
`LEADSCRAPER_SECRET_<NAME>` / a file in `LEADSCRAPER_SECRETS_DIR` in server
mode. Keys are never stored in the database, configs, logs or exports.

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

Phase 6b adds four more, all **off by default** (2026-10-08): `google_news`
(its feed terms forbid other than personal use), `press_releases`
(GlobeNewswire's terms page could not be found), `linkedin_search` (Google
SERP) and `github` (GitHub's Acceptable Use Policies on personal information).

Phase 8 adds `footprints` (Google SERP, **off by default**) and two **on by
default** that are marked for review: `itunes` (Apple's Search API, whose terms
are written for partners promoting store content) and `google_play` (listing
pages, read only where robots.txt allows). Turning either off is
`lead_types.app_store.sources.<name>.enabled: false`.

Phase 1 moved these sources into packs without changing their behaviour.
Whether each one stays, moves to an official API, or is retired is a
decision for the project owner. It's listed as an open issue in
`docs/phase-1-report.md`.
