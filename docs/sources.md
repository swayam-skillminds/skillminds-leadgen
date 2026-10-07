# Sources

Every external endpoint the tool talks to: where it is, how it's used, what
it needs, how hard it's paced, terms notes, and when it was last checked
live. A.9.2: re-verify a source live before coding against it, and update
its row here.

**Pack** decides where a source applies; see `leadscraper/packs/`. A source
whose pack doesn't apply to a run's country or verticals is reported as not
built, never as a failure. **Delays** are the defaults (seconds between
requests to that host, random in the range); a config can change them.

## Core (every country)

| Source | Endpoint | Method | Auth | Default delays | Terms note | Last verified |
|---|---|---|---|---|---|---|
| `google_maps` | `https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z?hl=en&gl={country}`, then place pages | Playwright, headless | none | 2.5–6 s, shared `google_maps` budget | Google's terms restrict automated access, and a paid Places API exists. **To review against A.2.4** | Code moved, not changed, in Phase 1; not re-verified |
| `google_search` | `https://www.google.com/search?q=…&gl={country}`, falling back to `https://html.duckduckgo.com/html/?kl={region}` | requests, then rendered, then DuckDuckGo | none | 12–28 s (Google), shared by every SERP user; circuit breaker after 3 total failures | Google's terms restrict automated queries; DuckDuckGo throttles with HTTP 202. **To review against A.2.4** | Not re-verified in Phase 1 |
| `website` (enricher) | the business's own site: home, then up to 3 contact/about pages | requests | none | 1.5–4 s per host | The business's public pages. robots.txt support is a Phase 2 item | n/a (any site) |
| `maps_lookup` (enricher) | Google Maps search by venue name | Playwright | none | 2.5–6 s | As `google_maps` | Not re-verified in Phase 1 |
| Geocoder (`core/geo.py`) | `https://nominatim.openstreetmap.org/search?q=…&format=jsonv2&limit=1&addressdetails=1&countrycodes={cc}` | requests | none; identifying User-Agent required | at most 1 req/s; every answer, misses included, cached in `<data_dir>/cache/geocode.json` | [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/): ≤ 1 req/s, identify the app, cache results, no bulk geocoding. Used only for the few place names in a config | **2026-10-06**: 200 OK, `[{lat, lon, display_name, boundingbox, address{state, country_code}}]` for "Austin", US |

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

## Open questions for the project owner

A.2.4 says: never scrape sites whose terms prohibit it when a public API
exists. Several sources inherited from the original scraper sit close to
that line:

- Google Maps and Search: the Places and Custom Search APIs are paid.
- Airbnb and Booking.com.
- Instagram and Facebook: the Graph APIs are restricted.

Phase 1 moved these sources into packs without changing their behaviour.
Whether each one stays, moves to an official API, or is retired is a
decision for the project owner. It's listed as an open issue in
`docs/phase-1-report.md`.
