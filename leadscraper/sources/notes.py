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
        "method": "none", "terms": "Your data. Lead type digital_audit.",
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
        "endpoint": "https://api.company-information.service.gov.uk/search/companies?q=…, "
                    "then /company/{number}/officers",
        "method": "requests (JSON); HTTP Basic with the secret companies-house-api-key",
        "terms": "The UK registry's official public API: a free key, 600 requests per 5 "
                 "minutes, for real-time lookups (not bulk). Current directors and LLP members "
                 "only. Pack uk (GB). Lead type decision_makers.",
        "review": False,
        "last_verified": "2026-10-08 (401 'Empty Authorization header' without a key; no key "
                         "here for a 200)"},
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
})



def note(name: str) -> dict[str, Any]:
    return dict(NOTES.get(name) or {"endpoint": "", "method": "", "terms": "",
                                    "review": False, "last_verified": None})
