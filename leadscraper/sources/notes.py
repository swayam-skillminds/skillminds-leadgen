"""What the catalog says about each source: endpoint, method, terms note, last check.

The machine-readable twin of ``docs/sources.md`` (keep them in step;
``tests/test_catalog.py`` fails when a registered source has no entry here).
``review`` marks the sources A.2.4 asks the project owner to decide on.
"""

from __future__ import annotations

from typing import Any

NOTES: dict[str, dict[str, Any]] = {
    "ai_web_search": {
        "endpoint": "the configured AI provider's web search (Anthropic web_search tool, "
                    "OpenAI web_search tool, Gemini googleSearch), then each result page",
        "method": "provider API, then requests (robots.txt honoured)",
        "terms": "Pages the provider's search returned, fetched once each at the site's "
                 "own pacing; a lead needs its name and contacts on the fetched page. Only "
                 "with the web_search_discovery AI feature.",
        "review": False, "last_verified": "2026-10-07 (provider API docs; no live call)"},
    "ai_directory": {
        "endpoint": "approved candidate sources (directories a user approved after "
                    "source_discovery proposed them)",
        "method": "requests; self-healing CSS selectors, AI only on a new or changed layout",
        "terms": "Each candidate's terms are the approver's call (its terms_note); robots.txt "
                 "honoured; never login walls. Only with the source_discovery AI feature.",
        "review": True, "last_verified": None},
    "google_maps": {
        "endpoint": "https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z",
        "method": "Playwright (headless)",
        "terms": "Google's terms restrict automated access; a paid Places API exists.",
        "review": True, "last_verified": "2026-10-07 (live check 1: one quick run)"},
    "google_search": {
        "endpoint": "https://www.google.com/search, falling back to "
                    "https://html.duckduckgo.com/html/",
        "method": "requests, then rendered, then DuckDuckGo",
        "terms": "Google's terms restrict automated queries; DuckDuckGo throttles with 202.",
        "review": True, "last_verified": None},
    "website": {
        "endpoint": "the business's own site: home, then up to 3 contact/about pages",
        "method": "requests",
        "terms": "The business's public pages; robots.txt honoured when the workspace "
                 "setting is on.",
        "review": False, "last_verified": None},
    "maps_lookup": {
        "endpoint": "Google Maps search by name", "method": "Playwright",
        "terms": "As google_maps.", "review": True, "last_verified": None},
    "justdial": {
        "endpoint": "https://www.justdial.com/{City}/{query}-in-{locality}",
        "method": "Playwright",
        "terms": "Blocks automated clients; recorded as a failure, never evaded.",
        "review": False, "last_verified": "2026-10-06 (blocked)"},
    "sulekha": {
        "endpoint": "https://www.sulekha.com/{query}/{city}", "method": "Playwright",
        "terms": "", "review": False, "last_verified": "2026-10-06"},
    "indiamart": {
        "endpoint": "https://dir.indiamart.com/search.mp?ss={query}&cq={city}",
        "method": "Playwright", "terms": "IndiaMART runs a seller API, not a search API.",
        "review": False, "last_verified": None},
    "wedmegood": {
        "endpoint": "https://www.wedmegood.com/vendors/{city}/{slug}/?page=N",
        "method": "plain GET, then Playwright", "terms": "", "review": False,
        "last_verified": "2026-10-06"},
    "weddingwire": {
        "endpoint": "https://www.weddingwire.in/{slug}/{city}?page=N", "method": "Playwright",
        "terms": "", "review": False, "last_verified": "2026-10-06 (403 to a plain client)"},
    "stayvista": {
        "endpoint": "https://www.stayvista.com/villas-sitemap.xml, then property pages",
        "method": "requests (sitemap)", "terms": "", "review": False, "last_verified": None},
    "saffronstays": {
        "endpoint": "https://www.saffronstays.com/sitemap-properties.xml",
        "method": "requests (sitemap)", "terms": "", "review": False, "last_verified": None},
    "elivaas": {
        "endpoint": "https://www.elivaas.com/sitemap.xml", "method": "requests (sitemap)",
        "terms": "", "review": False, "last_verified": None},
    "airbnb": {
        "endpoint": "https://www.airbnb.co.in search pages", "method": "Playwright",
        "terms": "Airbnb's terms prohibit scraping.", "review": True, "last_verified": None},
    "booking": {
        "endpoint": "https://www.booking.com search results", "method": "Playwright",
        "terms": "Booking.com's terms prohibit scraping; an affiliate API exists.",
        "review": True, "last_verified": None},
    "facebook": {
        "endpoint": "SERP site:facebook.com, then https://mbasic.facebook.com/{page}",
        "method": "requests",
        "terms": "Public pages only; a login wall is recorded, never logged into.",
        "review": True, "last_verified": None},
    "instagram": {
        "endpoint": "https://www.instagram.com/{handle}/", "method": "requests",
        "terms": "Public profiles only, never logged into.", "review": True,
        "last_verified": None},
    "linkedin_serp": {
        "endpoint": "SERP site:linkedin.com/in", "method": "via google_search",
        "terms": "LinkedIn is never fetched: only search-indexed profile URLs are recorded.",
        "review": False, "last_verified": None},
}

#: Sources a lead type runs itself (Phase 6), served beside the registry's by the catalog.
NOTES.update({
    "greenhouse": {
        "endpoint": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs (+ /{token})",
        "method": "requests (JSON)",
        "terms": "Greenhouse Job Board API: board data is public and GET needs no auth (its "
                 "docs). One company's published postings. Lead type hiring.",
        "review": False, "last_verified": "2026-10-07 (200, jobs[] with company_name)"},
    "lever": {
        "endpoint": "https://api.lever.co/v0/postings/{token}?mode=json",
        "method": "requests (JSON)",
        "terms": "Lever Postings API: published postings, public without a key (its docs). "
                 "Lead type hiring.",
        "review": False, "last_verified": "2026-10-07 (200, postings with createdAt)"},
    "ashby": {
        "endpoint": "https://api.ashbyhq.com/posting-api/job-board/{token}",
        "method": "requests (JSON)",
        "terms": "Ashby public job posting API, meant for career sites (its docs). Lead type "
                 "hiring.",
        "review": False, "last_verified": "2026-10-07 (200, jobs[] with publishedAt)"},
    "workable": {
        "endpoint": "https://apply.workable.com/api/v1/widget/accounts/{token}",
        "method": "requests (JSON)",
        "terms": "The JSON behind Workable's documented embeddable jobs widget; public, no key, "
                 "but not a documented API (the documented one needs the employer's token). "
                 "Lead type hiring.",
        "review": True, "last_verified": "2026-10-07 (200 for a real account; 404 otherwise)"},
    "smartrecruiters": {
        "endpoint": "https://api.smartrecruiters.com/v1/companies/{token}/postings",
        "method": "requests (JSON), 100 per page",
        "terms": "SmartRecruiters Posting API: answers without a key for companies with a "
                 "public feed (verified), though its docs now describe API-key/OAuth access. "
                 "Lead type hiring.",
        "review": True, "last_verified": "2026-10-07 (200 without auth, content[] postings)"},
    "hn_whos_hiring": {
        "endpoint": "https://hn.algolia.com/api/v1/search_by_date?tags=story,"
                    "author_whoishiring, then /api/v1/items/{thread id}",
        "method": "requests (JSON), two requests per monthly thread",
        "terms": "The public HN Search API by Algolia, no key; a commonly cited limit is "
                 "10,000 requests per hour per IP. Ads are posted by the hiring companies. "
                 "Lead type hiring.",
        "review": False,
        "last_verified": "2026-10-07 (200; 'Who is hiring? (October 2026)', 236 ads)"},
    "careers_pages": {
        "endpoint": "the organisation's own site: home, then its careers page",
        "method": "requests (robots.txt honoured)",
        "terms": "The business's public pages, one request at a time per site. ATS boards they "
                 "link are read through the ATS's own API. Lead type hiring.",
        "review": False, "last_verified": None},
    "board_search": {
        "endpoint": "Google search (then DuckDuckGo) for site:boards.greenhouse.io … pages",
        "method": "via google_search's SERP client (no browser)",
        "terms": "As google_search: Google's terms restrict automated queries. Off by default. "
                 "Lead type hiring.",
        "review": True, "last_verified": None},
    "upload": {
        "endpoint": "your own list (targets, domains, targets_file, POST /audit/targets)",
        "method": "none", "terms": "Your data. Lead types digital_audit and tech_stack.",
        "review": False, "last_verified": None},
    "site_audit": {
        "endpoint": "the organisation's own site: TLS handshake, home (HTTPS then HTTP), one "
                    "contact page, a HEAD per checked link",
        "method": "requests + ssl (robots.txt honoured for every page and link)",
        "terms": "The business's public pages, paced per site. Lead type digital_audit.",
        "review": False, "last_verified": None},
    "pagespeed": {
        "endpoint": "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=…"
                    "&strategy=mobile&category=performance",
        "method": "requests (JSON); key from the secret pagespeed-api-key",
        "terms": "Google's official free API; a key is recommended by its docs and needed in "
                 "practice. Lead type digital_audit.",
        "review": False,
        "last_verified": "2026-10-07 (keyless: 429 'Quota exceeded … Queries per day')"},
    # funding_news (Phase 6.3)
    "gdelt": {
        "endpoint": "https://api.gdeltproject.org/api/v2/doc/doc?query=…&mode=artlist"
                    "&format=json&sort=datedesc&timespan={days}d",
        "method": "requests (JSON), one query per event kind and place",
        "terms": "GDELT's data is free for any use, commercial included, with a citation of "
                 "the GDELT Project; it asks for at most one request every 5 seconds (paced "
                 "6-9 s). Headlines and links only; articles are never fetched. Lead type "
                 "funding_news.",
        "review": False,
        "last_verified": "2026-10-08 (200; articles[] with url, title, seendate, domain; "
                         "429 text when faster than 1 per 5 s)"},
    "google_news": {
        "endpoint": "https://news.google.com/rss/search?q=…&hl={locale}&gl={CC}&ceid={CC}:{lang}",
        "method": "requests (RSS)",
        "terms": "The feed's own terms: \"solely for the purpose of rendering Google News "
                 "results within a personal feed reader for personal, non-commercial use. Any "
                 "other use of the feed is expressly prohibited.\" Off by default; enabling it "
                 "needs Google's permission. Lead type funding_news.",
        "review": True, "last_verified": "2026-10-08 (200; RSS with <source>, the terms above)"},
    "press_releases": {
        "endpoint": "https://www.globenewswire.com/RssFeed/country/{Country}/feedTitle/…",
        "method": "requests (RSS); the issuer from dc:contributor",
        "terms": "robots.txt allows /RssFeed/ and the feed carries no restriction, but "
                 "GlobeNewswire's terms page could not be found: off by default, to review. "
                 "PR Newswire's terms (personal, non-commercial use; no robots) exclude it. "
                 "Lead type funding_news.",
        "review": True,
        "last_verified": "2026-10-08 (200 for India, 20 releases with dc:contributor; a "
                         "browser User-Agent was refused once)"},
    "feeds": {
        "endpoint": "RSS / Atom URLs the config lists",
        "method": "requests (RSS / Atom)",
        "terms": "Each feed's terms are the config author's call. Inc42 and YourStory forbid "
                 "automated access and Entrackr's feed is gone (2026-10-08), so no regional "
                 "feed ships with a country pack. Lead type funding_news.",
        "review": True, "last_verified": None},
    # decision_makers (Phase 6.4)
    "team_pages": {
        "endpoint": "the organisation's own site: home, then up to 3 team / leadership / "
                    "about pages it links",
        "method": "requests (robots.txt honoured); JSON-LD, microdata, name-and-title cards",
        "terms": "The business's public pages, one request at a time per site. Only people the "
                 "page names with a title. Lead type decision_makers.",
        "review": False, "last_verified": None},
    "companies_house": {
        "endpoint": "https://api.company-information.service.gov.uk: /search/companies?q=… "
                    "then /company/{number}/officers (decision_makers); "
                    "/advanced-search/companies?incorporated_from=…&location=… "
                    "(registrations)",
        "method": "requests (JSON); HTTP Basic with the secret companies-house-api-key",
        "terms": "The UK registry's official public API: a free key (register at "
                 "developer.company-information.service.gov.uk), 600 requests per 5 minutes, "
                 "for real-time lookups (not bulk). Without the key it is skipped: 'key not "
                 "configured'. Current directors and LLP members only. Pack uk (GB). Lead "
                 "types decision_makers and registrations.",
        "review": False,
        "last_verified": "2026-10-13 (401 'Empty Authorization header' without a key, for "
                         "both endpoints; no key here for a 200)"},
    # tenders (Phase 7)
    "ted": {
        "endpoint": "POST https://api.ted.europa.eu/v3/notices/search (expert query: "
                    "buyer-country, notice-type, classification-cpv or FT~(words), "
                    "publication-date)",
        "method": "requests (JSON); anonymous",
        "terms": "The EU Publications Office's Search API, 'primarily targeted at data reusers' "
                 "and open without authentication; TED content may be reused, commercially "
                 "too, with the source acknowledged. Pack eu (EU + EEA). Lead type tenders.",
        "review": False,
        "last_verified": "2026-10-13 (200; Irish IT notices with buyer, contacts, deadline, "
                         "value, documents)"},
    "find_a_tender": {
        "endpoint": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
                    "?stages=tender&updatedFrom=…&limit=100 (cursor pages)",
        "method": "requests (OCDS JSON); no key",
        "terms": "UK Cabinet Office, published under the Open Government Licence v3.0 (the "
                 "package's own licence). No CPV filter: pages are read and sorted here. Pack "
                 "uk. Lead type tenders.",
        "review": False, "last_verified": "2026-10-13 (200, OGL v3.0)"},
    "contracts_finder": {
        "endpoint": "POST https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices"
                    "/json (types, statuses, cpvCodes, publishedFrom)",
        "method": "requests (JSON); no key",
        "terms": "UK Cabinet Office, Open Government Licence v3.0. Lower-value contracts "
                 "(England). Pack uk. Lead type tenders.",
        "review": False, "last_verified": "2026-10-13 (200; open IT notices by CPV)"},
    "sam_gov": {
        "endpoint": "https://api.sam.gov/opportunities/v2/search?api_key=…&postedFrom=…"
                    "&postedTo=…&ptype=o&ptype=k&ptype=p&ptype=r&limit=1000",
        "method": "requests (JSON); the secret sam-gov-api-key, sent as the API requires and "
                  "never logged",
        "terms": "GSA's official public API: a free personal key (SAM.gov → Account Details); "
                 "10 requests a day for a non-federal user without a SAM.gov role, so one "
                 "request a run by default. Without the key it is skipped: 'key not "
                 "configured'. Pack us. Lead type tenders.",
        "review": False,
        "last_verified": "2026-10-13 (docs read; without a valid key the API answers an empty "
                         "404, so no 200 was seen)"},
    "cppp": {
        "endpoint": "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata (the public "
                    "latest-active-tenders list; never the CAPTCHA search)",
        "method": "requests (HTML table, ten a page)",
        "terms": "No API; no robots.txt (404). Its copyright policy: material 'maybe reproduced "
                 "free of charge after taking proper permission from the respective "
                 "Organisation / Department'. Off by default: the owner's call. Pack in. Lead "
                 "type tenders.",
        "review": True, "last_verified": "2026-10-13 (200; ten tenders a page, Next link)"},
    "gem": {
        "endpoint": "https://bidplus.gem.gov.in/all-bids, then POST /all-bids-data (the page's "
                    "own form token, a full-text search, ongoing bids)",
        "method": "requests (JSON behind the public bid listing)",
        "terms": "robots.txt allows the listing; GeM's copyright policy: contents 'may not be "
                 "reproduced partially or fully without due permission in writing in "
                 "advance'. Off by default: the owner's call. Pack in. Lead type tenders.",
        "review": True, "last_verified": "2026-10-13 (200; 'software': 573 ongoing bids)"},
    # registrations (Phase 7)
    "opencorporates": {
        "endpoint": "https://api.opencorporates.com/v0.4/companies/search?country_code=…"
                    "&incorporation_date=from:to&current_status=Active&api_token=…",
        "method": "requests (JSON); the secret opencorporates-api-token, never logged",
        "terms": "Every request needs a token. Free tokens only for 'Permitted Users' "
                 "(journalists, NGOs, academics; 'corporations … are not'); commercial use needs "
                 "a paid non-share-alike key; ODbL. 200 requests a month, 50 a day by default. "
                 "Off by default. Any country. Lead type registrations.",
        "review": True,
        "last_verified": "2026-10-13 (401 'Invalid Api Token' without one; terms read)"},
    "linkedin_search": {
        "endpoint": "Google search (then DuckDuckGo) for site:linkedin.com/in \"<company>\" "
                    "(founder OR CTO …)",
        "method": "via google_search's SERP client; profile URLs and result titles only",
        "terms": "LinkedIn is never fetched or logged into (A.2.4): only URLs a search engine "
                 "indexed. Google's terms restrict automated queries: off by default. Lead "
                 "type decision_makers.",
        "review": True, "last_verified": None},
    "github": {
        "endpoint": "https://api.github.com/orgs/{org}, /orgs/{org}/public_members, "
                    "/users/{login}",
        "method": "requests (JSON); optional token from the secret github-token",
        "terms": "GitHub's official REST API (60 requests/hour without a token). Its Acceptable "
                 "Use Policies limit personal information to the purpose its users published "
                 "it for and forbid unsolicited email: off by default, never used for the "
                 "email pattern. Lead type decision_makers.",
        "review": True,
        "last_verified": "2026-10-08 (200 for /orgs/github, /orgs/github/public_members, "
                         "/users/defunkt)"},
    "email_pattern": {
        "endpoint": "DNS MX records of the organisation's own domain (the system resolver)",
        "method": "dnspython; no SMTP, ever",
        "terms": "An inference (A.2.5), off by default: only from published personal addresses "
                 "at the same domain, always labelled Inferred Email (unverified), never "
                 "published, never used for matching. Lead type decision_makers.",
        "review": False, "last_verified": "2026-10-08 (example.com: a null MX, so no mail)"},
    # tech_stack (Phase 8.1)
    "site_fingerprint": {
        "endpoint": "the organisation's own site: robots.txt, then its homepage (HTTPS, then "
                    "HTTP)",
        "method": "requests; the shared fingerprint rules (sources/web/fingerprints.yaml: meta "
                  "generator, script URLs, headers, HTML)",
        "terms": "The business's public homepage, one request at a time per site, robots.txt "
                 "honoured. Outdated thresholds dated against endoflife.date (2026-10-13). Lead "
                 "type tech_stack.",
        "review": False, "last_verified": "2026-10-13 (endoflife.date API for the thresholds)"},
    "footprints": {
        "endpoint": "Google search (then DuckDuckGo) for a technology's footprint phrases in a "
                    "place: \"powered by Shopify\" \"Pune\"; each result's homepage is then "
                    "fingerprinted",
        "method": "via google_search's SERP client, then site_fingerprint",
        "terms": "Google's terms restrict automated queries: off by default (the owner's "
                 "call, as board_search). A site is kept only when its own homepage shows the "
                 "technology. Lead type tech_stack.",
        "review": True, "last_verified": None},
    # app_store (Phase 8.2)
    "itunes": {
        "endpoint": "https://itunes.apple.com/search?term=…&country=…&entity=software&limit=…; "
                    "https://itunes.apple.com/lookup?id=…&country=…",
        "method": "requests (JSON); keyless; an identifying User-Agent",
        "terms": "Apple's documented Search API: 'approximately 20 calls per minute', paced "
                 "3.5–5 s apart. Its terms govern promotional content (artwork, previews), none "
                 "of which is kept. robots.txt disallows /search* for crawlers, and Apple's site "
                 "terms forbid page-scraping 'through any means not purposely made available': "
                 "the API is that means, so App Store web pages and the customer-review RSS "
                 "(robots.txt: /*/rss/*) are never read. Lead type app_store.",
        "review": True,
        "last_verified": "2026-10-13 (200; trackId, sellerUrl, averageUserRating, "
                         "userRatingCount, currentVersionReleaseDate, bundleId)"},
    "google_play": {
        "endpoint": "https://play.google.com/store/apps/details?id={package}&hl=en&gl={country}",
        "method": "requests (HTML: JSON-LD, 'Updated on', downloads, App support, the reviews "
                  "the page shows; never reviewers' names)",
        "terms": "robots.txt allows /store/apps/details and disallows /store/search; Google's "
                 "terms forbid automated access 'in violation of the machine-readable "
                 "instructions on our web pages (for example, robots.txt …)'. So listings are "
                 "read one at a time (packages from organisations' sites, the config, or an iOS "
                 "bundle id confirmed by the same developer website), never searched. Lead type "
                 "app_store.",
        "review": True,
        "last_verified": "2026-10-13 (200; JSON-LD rating 4.49 / 278,696, 'Updated on Oct 10, "
                         "2026', '10M+ Downloads', developer contacts, 3 reviews)"},
    "listed": {
        "endpoint": "the apps a config names (store links, Apple ids, Play packages), read "
                    "through itunes lookup and google_play",
        "method": "as itunes and google_play", "terms": "Your list. Lead type app_store.",
        "review": False, "last_verified": None},
    "site_apps": {
        "endpoint": "the organisation's own homepage, read for App Store and Google Play links",
        "method": "requests (robots.txt honoured), then itunes lookup / google_play",
        "terms": "The business's public homepage, paced per site. Lead type app_store.",
        "review": False, "last_verified": None},
})



def note(name: str) -> dict[str, Any]:
    return dict(NOTES.get(name) or {"endpoint": "", "method": "", "terms": "",
                                    "review": False, "last_verified": None})
