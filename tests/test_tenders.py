"""tenders (Phase 7.1): portals, matching, runs, isolation, key-less skipping, scoring.

All offline: every portal answers from fictional fixtures in the shape it answered
live on 2026-10-13 (TED, Find a Tender, Contracts Finder, CPPP, GeM; SAM.gov from its
documentation), served by ``FakeWeb``. Dates are relative to now, so no fixture goes
stale. The ``live`` tests (``-m live``) ask each keyless portal one small question
and SAM.gov only when a key is configured.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from string import Template
from types import SimpleNamespace
from typing import Iterator

import pytest
import requests
from openpyxl import load_workbook

from fake_web import (FakeWeb, Page, kit_for, local_business_view, patch_kit, probe_enricher,
                      project_rows, relative, run_database)
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import (Evidence, FailureRow, OrgAttribute, Organization, Signal,
                                   WorkUnitRow)
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.estimator import describe_estimate
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.leadtypes.tenders.config import TendersConfig
from leadscraper.leadtypes.tenders.plugin import TENDERS, tender_signals, weight
from leadscraper.leadtypes.tenders.session import TendersSession, plan_units
from leadscraper.net.ratelimit import BlockedError
from leadscraper.packs import tender_sources_for
from leadscraper.packs.eu import ted
from leadscraper.packs.india import cppp, gem
from leadscraper.packs.uk import contracts_finder, find_a_tender
from leadscraper.packs.us import sam_gov
from leadscraper.services import catalog, exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.base import Source
from leadscraper.sources.tenders.base import (Money, TenderQuery, code_matches, code_prefix,
                                              keyword_hits, parse_time)

UTC = timezone.utc


def days(n: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=n)


# --- fixtures: fictional notices, live shapes ----------------------------------------------------

TED = {
    "notices": [
        {"publication-number": "900101-2026", "publication-date": "@date:4",
         "notice-type": "cn-standard",
         "notice-title": {"eng": "Ireland – IT services – Case management system",
                          "gle": "Éire – Seirbhísí TF – Córas bainistíochta"},
         "title-proc": {"eng": "Case management system for planning applications"},
         "description-lot": {"eng": ["Design, build and support of a web-based system."]},
         "classification-cpv": ["72000000", "72268000", "72268000"],
         "deadline-receipt-tender-date-lot": ["@date:-20", "@date:-20"],
         "estimated-value-proc": "400000", "estimated-value-cur-proc": "EUR",
         "estimated-value-lot": ["400000", "400000"], "estimated-value-cur-lot": ["EUR"],
         "document-url-lot": ["https://etenders.example/docs/900101"],
         "place-of-performance-city-lot": ["Fernvale"],
         "buyer-name": {"eng": ["Fernvale County Council"]},
         "buyer-city": {"mul": ["Fernvale"]}, "buyer-country": ["IRL"],
         "buyer-email": ["procurement@fernvale-council.example"],
         "buyer-internet-address": ["https://www.fernvale-council.example/"],
         "organisation-post-code-buyer": ["F01 X2Y3"],
         "organisation-street-buyer": ["Council Buildings, Main Street"],
         "organisation-identifier-buyer": ["20110042"],
         "links": {"html": {"ENG": "https://ted.europa.eu/en/notice/-/detail/900101-2026"}}},
        {"publication-number": "900102-2026", "publication-date": "@date:10",
         "notice-type": "pin-only",
         "title-proc": {"eng": "Future cloud hosting and software licences"},
         "classification-cpv": ["48000000"],
         "buyer-name": {"eng": ["Harbourline Port Authority"]},
         "buyer-city": {"mul": ["Kilbarra"]}, "buyer-country": ["IRL"],
         "buyer-email": ["tenders@harbourline-port.example"],
         "organisation-identifier-buyer": ["NA"]},
        {"publication-number": "900103-2026", "publication-date": "@date:6",
         "notice-type": "cn-standard",
         "title-proc": {"eng": "Medical devices with software maintenance"},
         "classification-cpv": ["33100000"], "deadline-receipt-tender-date-lot": ["@date:-30"],
         "buyer-name": {"eng": ["Brookmere Health Board"]},
         "buyer-city": {"mul": ["Brookmere"]}, "buyer-country": ["IRL"]},
        {"publication-number": "900104-2026", "publication-date": "@date:25",
         "notice-type": "cn-standard", "title-proc": {"eng": "Student records software"},
         "classification-cpv": ["48000000"], "deadline-receipt-tender-date-lot": ["@date:2"],
         "buyer-name": {"eng": ["Glenmore University"]},
         "buyer-city": {"mul": ["Glenmore"]}, "buyer-country": ["IRL"]},
    ],
    "totalNoticeCount": 4, "iterationNextToken": None, "timedOut": False}


def _release(id_, title, status, cpv, end_days, buyer, *, extra_cpv=(), value=None):
    tender = {"id": f"T-{id_}", "title": title, "status": status,
              "description": f"{title}: fictional fixture.",
              "classification": {"scheme": "CPV", "id": cpv},
              "items": [{"id": "1", "additionalClassifications": [
                  {"scheme": "CPV", "id": c} for c in extra_cpv],
                  "deliveryLocation": {"description": "Northwick"}}],
              "tenderPeriod": {"endDate": f"@iso:{end_days}"} if end_days is not None else {}}
    if value:
        tender["value"] = {"amount": value, "currency": "GBP"}
    town = buyer[0].split()[0].lower()
    return {"ocid": f"ocds-test-{id_}", "id": f"0{id_}-2026", "date": "@iso:3",
            "tag": ["tender"], "buyer": {"id": f"GB-FTS-{buyer[1]}", "name": buyer[0]},
            "parties": [{"name": buyer[0], "id": f"GB-FTS-{buyer[1]}", "roles": ["buyer"],
                         "address": {"streetAddress": "Town Hall, High Street",
                                     "locality": buyer[0].split()[0], "postalCode": "NW1 0AA",
                                     "countryName": "United Kingdom"},
                         "contactPoint": {
                             "name": "Procurement Team", "email": f"procurement@{town}.example",
                             "telephone": "+44 20 7946 0000",
                             # The e-tendering portal every buyer shares: never a website.
                             "url": "https://www.publictendersscotland.publiccontractsscotland"
                                    ".gov.uk/esop/login.html"},
                         "details": {"url": f"http://https://www.{town}.example"}},
                        {"name": "Northwick County Court", "id": "GB-FTS-9",
                         "roles": ["reviewBody"]}],
            "tender": tender}


FTS_PAGE1 = {"uri": "x", "license": "http://www.nationalarchives.gov.uk/doc/"
             "open-government-licence/version/3/",
             "releases": [
                 _release(1001, "Revenues and benefits software replacement", "active",
                          "48000000", -25, ("Northwick Borough Council", 11),
                          extra_cpv=["72260000"], value=850000),
                 _release(1002, "Highways resurfacing framework", "active", "45233142", -25,
                          ("Northwick Borough Council", 11)),
                 _release(1003, "Cancelled ICT support", "cancelled", "72000000", -10,
                          ("Eastvale District Council", 12))],
             "links": {"next": f"{find_a_tender.API}?cursor=page2"}}
FTS_PAGE2 = {"releases": [
    _release(1004, "Intention to procure a data platform", "planned", "72300000", None,
             ("Eastvale District Council", 12))], "links": {}}

CF = {"hitCount": 2, "maxHits": 1000, "noticeList": [
    {"score": 1, "item": {
        "id": "7f000000-0000-4000-8000-000000000001", "title": "Website redesign",
        "description": "A new accessible website for the parish council.",
        "publishedDate": "@iso:5", "deadlineDate": "@iso:-14", "valueLow": 5000,
        "valueHigh": 15000, "postcode": "ab1 2cd", "organisationName": "Ashby Parish Council",
        "cpvCodes": "72413000 72212224", "region": "East Midlands",
        "regionText": "East Midlands", "noticeType": "Contract", "noticeStatus": "Open"}},
    {"score": 1, "item": {
        "id": "7f000000-0000-4000-8000-000000000002", "title": "Command and control software",
        "description": "Market engagement for a future system.", "publishedDate": "@iso:2",
        "deadlineDate": None, "valueLow": 0, "valueHigh": 0, "postcode": "",
        "organisationName": "Kettering Fire Service", "cpvCodes": "48000000",
        "region": "Any region", "noticeType": "Pipeline", "noticeStatus": "Open"}}]}

SAM = {"totalRecords": 3, "limit": 1000, "offset": 0, "opportunitiesData": [
    {"noticeId": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
     "title": "Custom software development for case management",
     "solicitationNumber": "EX-26-0001",
     "fullParentPathName": "DEPARTMENT OF EXAMPLES.EXAMPLE AGENCY.OFFICE OF ACQUISITIONS",
     "fullParentPathCode": "999.9999.99XX", "postedDate": "@date:5", "type": "Solicitation",
     "baseType": "Solicitation", "responseDeadLine": "@iso:-15", "naicsCode": "541511",
     "classificationCode": "D", "active": "Yes",
     "pointOfContact": [{"type": "primary", "email": "contracting@agency.example",
                         "phone": "5125550123", "fullName": "Jordan Example",
                         "title": "Contracting Officer"}],
     "officeAddress": {"zipcode": "78701", "city": "AUSTIN", "countryCode": "USA",
                       "state": "TX"},
     "placeOfPerformance": {"city": {"code": "05000", "name": "Austin"},
                            "state": {"code": "TX"}, "zip": "78701"},
     "uiLink": "https://sam.gov/opp/a1b2c3d4e5f60718293a4b5c6d7e8f90/view",
     "resourceLinks": ["https://sam.gov/files/example/download"]},
    {"noticeId": "b1b2c3d4e5f60718293a4b5c6d7e8f90", "title": "Cloud hosting market research",
     "fullParentPathName": "DEPARTMENT OF EXAMPLES.EXAMPLE AGENCY.OFFICE OF ACQUISITIONS",
     "fullParentPathCode": "999.9999.99XX", "postedDate": "@date:8", "type": "Sources Sought",
     "responseDeadLine": None, "naicsCode": "518210",
     "officeAddress": {"city": "AUSTIN", "state": "TX"},
     "uiLink": "https://sam.gov/opp/b1b2c3d4e5f60718293a4b5c6d7e8f90/view"},
    {"noticeId": "c1b2c3d4e5f60718293a4b5c6d7e8f90", "title": "Parking garage repairs",
     "fullParentPathName": "DEPARTMENT OF EXAMPLES.FACILITIES", "postedDate": "@date:3",
     "type": "Solicitation", "responseDeadLine": "@iso:-9", "naicsCode": "236220"}]}

CPPP_ROW = """<tbody><tr><td>$n.</td><td>$pub</td><td>$close</td><td>$close</td>
<td><a href="https://eprocure.gov.in/cppp/tendersfullview/$id" title="External Url">$title</a>
/REF/$n/2026/$id</td><td>$org</td><td>--</td></tr></tbody>"""
CPPP_PAGE = """<html><body><div><a href="$next" class="paginate_button">Next »</a></div>
<table id="table" class="list_table"><thead><tr><th>Sl.No</th><th>e-Published Date</th>
<th>Bid Submission Closing Date</th><th>Tender Opening Date</th>
<th>Title/Ref.No./Tender Id</th><th>Organisation Name</th><th>Corrigendum</th></tr></thead>
$rows</table></body></html>"""
CPPP_LAST = CPPP_PAGE.replace('<div><a href="$next" class="paginate_button">Next »</a></div>',
                              "")
CPPP_NEXT = f"{cppp.LIST}?url=cGFnZTI%3D"

GEM_PAGE = """<html><script>$.ajax({type: 'POST', url: 'https://bidplus.gem.gov.in/all-bids-data',
data: {'payload': JSON.stringify(postdata), 'csrf_bd_gem_nk': '0123456789abcdef0123456789abcdef'}
});</script></html>"""
GEM_DATA = {"status": 1, "code": 200, "message": "", "response": {"response": {
    "numFound": 2, "start": 0, "docs": [
        {"id": "9900001", "b_id": [9900001], "b_bid_number": ["GEM/2026/B/9900001"],
         "b_category_name": ["Custom Bid for Services - Development of Web Portal"],
         "b_total_quantity": [1], "final_start_date_sort": ["@iso:3"],
         "final_end_date_sort": ["@iso:-12"],
         "ba_official_details_minName": ["Ministry of Examples"],
         "ba_official_details_deptName": ["Department of Sample Affairs"], "b_bid_to_ra": [0]},
        {"id": "9900002", "b_id": [9900002], "b_bid_number": ["GEM/2026/B/9900002"],
         "b_category_name": ["Desktop Computers"], "b_total_quantity": [40],
         "final_start_date_sort": ["@iso:4"], "final_end_date_sort": ["@iso:1"],
         "ba_official_details_minName": ["Ministry of Examples"], "b_bid_to_ra": [1]}]}}}


def dumps(data) -> str:
    return relative(json.dumps(data))


def cppp_html(rows: list[tuple], next_url: str | None = None) -> str:
    def when(n: float) -> str:
        return (datetime.now(UTC) + timedelta(hours=5, minutes=30) - timedelta(days=n)) \
            .strftime("%d-%b-%Y %I:%M %p")
    body = "".join(Template(CPPP_ROW).substitute(n=i + 1, pub=when(pub), close=when(close),
                                                 id=tid, title=title, org=org)
                   for i, (pub, close, tid, title, org) in enumerate(rows))
    page = CPPP_PAGE if next_url else CPPP_LAST
    return Template(page).substitute(rows=body, next=next_url or "")


def tender_web() -> FakeWeb:
    web = FakeWeb()
    web.add(ted.API, Page(dumps(TED), headers={"Content-Type": "application/json"}))
    web.add(find_a_tender.API, Page(dumps(FTS_PAGE1)))
    web.add(f"{find_a_tender.API}?cursor=page2", Page(dumps(FTS_PAGE2)))
    web.add(contracts_finder.API, Page(dumps(CF)))
    web.add(sam_gov.API, Page(dumps(SAM)))
    web.add(cppp.LIST, Page(cppp_html([
        (1, -10, "2026_EX_1001_1", "Development of web portal for district services",
         "Example Smart City Limited"),
        (1, -12, "2026_EX_1002_1", "Annual maintenance of garden at the museum",
         "Archaeological Survey of Examples"),
        (2, -8, "2026_EX_1003_1", "Supply of 50 laptops", "Example Institute of Technology")],
        next_url=CPPP_NEXT)))
    web.add(CPPP_NEXT, Page(cppp_html([
        (2, -6, "2026_EX_1004_1", "AMC of computers and peripherals", "Example Port Trust")])))
    web.add(gem.PAGE, Page(GEM_PAGE))
    web.add(gem.DATA, Page(dumps(GEM_DATA)))
    return web


def query(country: str, **kw) -> TenderQuery:
    section = TendersConfig()
    q = TenderQuery(country=country, cpv=section.cpv, naics=section.naics,
                    keywords=section.keywords, posted_from=days(30),
                    closing_after=datetime.now(UTC), max_results=250, max_pages=5)
    for k, v in kw.items():
        setattr(q, k, v)
    return q


# --- matching and values ------------------------------------------------------------------------

def test_codes_are_prefixes_and_keywords_whole_words():
    assert code_prefix("72000000") == "72" and code_prefix("72268000-5") == "72268"
    assert code_prefix("5415") == "5415" and code_prefix("30200000") == "302"
    assert code_matches(["72268000", "33100000", "48781000-1"], ["72000000", "48000000"]) == \
        ["72268000", "48781000"]
    assert code_matches(["541511"], ["5415"]) == ["541511"]
    assert code_matches(["236220"], ["5415", "518210"]) == []
    assert keyword_hits("Development of Web Portal and ERP", ["web portal", "erp", "port"]) \
        == ["web portal", "erp"]
    assert keyword_hits("Serverless", ["server"]) == []          # whole words only


def test_a_procurement_portal_is_never_a_buyers_website():
    from leadscraper.sources.tenders.base import own_site

    assert own_site("http://https://www.northwick.example") == "https://www.northwick.example"
    assert own_site("www.fernvale-council.example") == "https://www.fernvale-council.example"
    for portal in ("https://www.publictendersscotland.publiccontractsscotland.gov.uk/x",
                   "https://www.etenders.gov.ie/epps/home.do", "https://in-tendhost.co.uk/a",
                   "https://ted.europa.eu/en/notice/1", "not a link", None):
        assert own_site(portal) is None, portal


def test_portal_dates_and_values():
    assert parse_time("2026-11-04+01:00", end_of_day=True) == \
        datetime(2026, 11, 4, 23, 59, 59, tzinfo=timezone(timedelta(hours=1)))
    assert parse_time("2026-11-04Z") == datetime(2026, 11, 4, tzinfo=UTC)
    ist = timezone(timedelta(hours=5, minutes=30))
    assert parse_time("23-Oct-2026 01:15 PM", zone=ist) == \
        datetime(2026, 10, 23, 13, 15, tzinfo=ist)
    assert parse_time("10/31/2026", end_of_day=True).hour == 23
    assert parse_time("not a date") is None
    assert Money(400000, "EUR").text == "€400,000"
    assert Money(15000, "GBP", 5000).text == "£5,000–£15,000"
    assert Money(2_000_000, "SEK").text == "SEK 2,000,000"


# --- the portals --------------------------------------------------------------------------------

def test_ted_query_and_notices():
    q = query("IE", posted_from=datetime(2026, 9, 1, tzinfo=UTC))
    expert = ted.build_query(q)
    assert expert.startswith("buyer-country=IRL AND notice-type IN (cn-standard")
    assert "publication-date>=20260901" in expert
    assert "classification-cpv IN (72000000 48000000 30200000 80533000)" in expert
    assert expert.endswith("SORT BY publication-date DESC")
    q.match = "keywords"
    assert 'FT~(software OR website OR "web portal"' in ted.build_query(q)
    q.match = "any"
    assert "(classification-cpv IN (" in ted.build_query(q) and " OR FT~(" in ted.build_query(q)

    web = tender_web()
    notices = ted.TedTenders().search(web, query("IE"))
    assert [n.notice_id for n in notices] == ["900101-2026", "900102-2026", "900103-2026",
                                              "900104-2026"]
    first = notices[0]
    assert first.title == "Case management system for planning applications"
    assert first.buyer.name == "Fernvale County Council" and first.buyer.locality == "Fernvale"
    assert first.buyer.identifier == "IE:20110042" and first.buyer.scheme == "ted_buyer"
    assert first.value.text == "€400,000" and first.deadline > datetime.now(UTC)
    assert first.documents_url == "https://etenders.example/docs/900101"
    assert first.url == "https://ted.europa.eu/en/notice/-/detail/900101-2026"
    assert notices[1].stage == "planned" and notices[1].buyer.identifier == ""   # "NA"
    body = web.posts[0][1]
    assert body["limit"] == 250 and body["page"] == 1 and "buyer-name" in body["fields"]


def test_find_a_tender_reads_pages_and_keeps_live_tenders():
    web = tender_web()
    notices = find_a_tender.FindATenderTenders().search(web, query("GB"))
    assert [n.title for n in notices] == ["Revenues and benefits software replacement",
                                         "Highways resurfacing framework",
                                         "Intention to procure a data platform"]  # cancelled out
    first = notices[0]
    assert first.codes == ["48000000", "72260000"] and first.value.text == "£850,000"
    assert first.buyer.website == "https://www.northwick.example"     # "http://https://" fixed
    assert first.buyer.email == "procurement@northwick.example"
    assert first.buyer.identifier == "GB-FTS-11" and first.buyer.scheme == "gb_fts"
    assert first.url == "https://www.find-tender.service.gov.uk/Notice/01001-2026"
    assert notices[2].stage == "planned" and notices[2].deadline is None
    assert web.calls == [find_a_tender.API, f"{find_a_tender.API}?cursor=page2"]


def test_contracts_finder_by_cpv_unless_keywords_only():
    web = tender_web()
    portal = contracts_finder.ContractsFinderTenders()
    notices = portal.search(web, query("GB"))
    criteria = web.posts[0][1]["searchCriteria"]
    assert criteria["cpvCodes"] == TendersConfig().cpv and criteria["statuses"] == ["Open"]
    website, pipeline = notices
    assert website.value.text == "£5,000–£15,000" and website.buyer.postcode == "AB1 2CD"
    assert website.places == ["East Midlands"] and website.stage == "tender"
    assert pipeline.stage == "planned" and pipeline.buyer.region == ""      # "Any region"
    portal.search(web, query("GB", match="keywords"))
    assert "cpvCodes" not in web.posts[1][1]["searchCriteria"]


def test_sam_gov_needs_a_key_and_never_leaks_it():
    portal = sam_gov.SamGovTenders()
    web = tender_web()
    with pytest.raises(sam_gov.SamKeyRejected):
        portal.search(web, query("US"), key=None)
    notices = portal.search(web, query("US", region="Texas"), key="k-secret")
    assert [n.title for n in notices][:2] == ["Custom software development for case management",
                                             "Cloud hosting market research"]
    first = notices[0]
    assert first.buyer.name == "OFFICE OF ACQUISITIONS" and first.buyer.locality == "Austin"
    assert first.buyer.email == "contracting@agency.example"
    assert first.buyer.contact == "Jordan Example" and first.codes == ["541511"]
    assert first.scheme == "naics" and first.description.startswith("DEPARTMENT OF EXAMPLES ›")
    assert notices[1].stage == "planned"
    assert sam_gov.state_code("Texas") == "TX" and sam_gov.state_code("tx") == "TX"
    assert sam_gov.state_code("TX") == "TX" and sam_gov.state_code("Ontario") is None

    def unauthorised(url):
        raise requests.HTTPError(f"401 Client Error for url: {url}?api_key=k-secret")
    web.add(sam_gov.API, unauthorised)
    with pytest.raises(sam_gov.SamKeyRejected) as error:
        portal.search(web, query("US"), key="k-secret")
    assert "k-secret" not in str(error.value) and "***" in str(error.value)
    web.add(sam_gov.API, BlockedError("HTTP 403 from api.sam.gov"))
    with pytest.raises(BlockedError) as blocked:
        portal.search(web, query("US"), key="k-secret")
    assert "daily quota" in str(blocked.value)


def test_cppp_reads_the_active_list_in_ist():
    web = tender_web()
    notices = cppp.CpppTenders().search(web, query("IN"))
    assert [n.notice_id for n in notices] == ["2026_EX_1001_1", "2026_EX_1002_1",
                                              "2026_EX_1003_1", "2026_EX_1004_1"]
    first = notices[0]
    assert first.title == "Development of web portal for district services"
    assert first.buyer.name == "Example Smart City Limited"
    assert first.description == "Ref. REF/1/2026"
    assert first.deadline.utcoffset() == timedelta(hours=5, minutes=30)
    assert first.url.startswith("https://eprocure.gov.in/cppp/tendersfullview/")
    assert web.calls == [cppp.LIST, CPPP_NEXT]


def test_gem_posts_its_own_form_token_per_keyword():
    web = tender_web()
    notices = gem.GemTenders().search(web, query("IN", keywords=["web portal", "computers"],
                                                 max_pages=1))
    assert [n.notice_id for n in notices] == ["GEM/2026/B/9900001", "GEM/2026/B/9900002"]
    first = notices[0]
    assert first.buyer.name == "Department of Sample Affairs"
    assert first.description == "Ministry of Examples, quantity 1"
    assert first.url == "https://bidplus.gem.gov.in/showbidDocument/9900001"
    assert first.deadline.utcoffset() == timedelta(hours=5, minutes=30)
    assert notices[1].buyer.name == "Ministry of Examples"           # no department named
    url, body = web.posts[0]
    assert url == gem.DATA and body["csrf_bd_gem_nk"] == "0123456789abcdef0123456789abcdef"
    assert json.loads(body["payload"])["param"]["searchBid"] == "web portal"
    assert len(web.posts) == 2                                       # one search per keyword
    web.add(gem.PAGE, Page("<html>no token</html>"))
    with pytest.raises(gem.GemFormChanged):
        gem.GemTenders().search(web, query("IN"))


# --- packs: where each portal applies ----------------------------------------------------------

def _config(country: str, places=("Fernvale",), depth: str | None = None, **section):
    document = {"name": "Tenders", "project": "tenders",
                "geography": {"country": country, "places": list(places)},
                "lead_types": {"tenders": section}}
    if depth:
        document["depth"] = depth
    return compile_run_config(RunConfig.model_validate(document))


@pytest.mark.parametrize("country, portals", [
    ("IE", ["ted"]), ("DE", ["ted"]), ("NO", ["ted"]),
    ("GB", ["find_a_tender", "contracts_finder"]), ("US", ["sam_gov"]), ("IN", ["cppp", "gem"]),
    ("AU", []), ("AE", []), ("CA", [])])
def test_tender_packs_stay_inactive_where_they_dont_apply(country, portals):
    assert [cls.name for cls in tender_sources_for(country)] == portals
    config = _config(country)
    planned = [u.source for u in plan_units(config, TENDERS.effective(config))]
    # India's portals are opt-in (their terms ask for permission); the rest default on.
    assert planned == ([] if country == "IN" else portals)
    assert TENDERS.sources(config)["discovery"] == planned
    by_name = {s["name"]: s for s in catalog.sources(country=country, with_health=False)
               if s["lead_type"] == "tenders"}
    assert {n for n, s in by_name.items() if s["applicable"]} == set(portals)
    for name, entry in by_name.items():
        if name not in portals:
            assert "covers" in entry["reason"]


def test_indian_portals_are_opt_in():
    config = _config("IN", places=("Pune",), sources={"cppp": {"enabled": True},
                                                     "gem": {"enabled": True}})
    assert [u.source for u in plan_units(config, TENDERS.effective(config))] == ["cppp", "gem"]


# --- the session: which notices count -----------------------------------------------------------

def session(tmp_path, country="IE", *, web=None, places=("Fernvale",), store=None,
            **section) -> TendersSession:
    config = _config(country, places=places, **section)
    context = SimpleNamespace(config=config, store=store, run_id=None, dry_run=False)
    return TendersSession(context, TENDERS.effective(config),
                          kit=kit_for(web or tender_web(), tmp_path))


def test_which_notices_count(tmp_path):
    s = session(tmp_path)
    notices = {n.notice_id: n for n in ted.TedTenders().search(tender_web(), query("IE"))}
    keep, _, matched = s.wanted(notices["900101-2026"])
    assert keep and matched["codes"] == ["72000000", "72268000"]
    assert s.wanted(notices["900102-2026"])[0]                         # planned: kept
    assert s.wanted(notices["900103-2026"])[:2] == (False, "skipped_unmatched")  # medical CPV
    assert s.wanted(notices["900104-2026"])[:2] == (False, "skipped_closed")
    # "any": a word in the title counts even under another code.
    assert session(tmp_path, match="any").wanted(notices["900103-2026"])[0]
    assert session(tmp_path, planned=False).wanted(notices["900102-2026"])[1] == \
        "skipped_planned"
    assert session(tmp_path, min_value=500000).wanted(notices["900101-2026"])[1] == \
        "skipped_value"
    assert session(tmp_path, max_value=100000).wanted(notices["900101-2026"])[1] == \
        "skipped_value"
    assert session(tmp_path, max_value=100000).wanted(notices["900102-2026"])[0]  # no value
    later = (datetime.now(UTC) + timedelta(days=40)).date().isoformat()
    assert session(tmp_path, closing_after=later).wanted(notices["900101-2026"])[1] == \
        "skipped_closed"
    assert session(tmp_path, scope="place").wanted(notices["900101-2026"])[0]
    assert session(tmp_path, scope="place").wanted(notices["900102-2026"])[1] == \
        "skipped_place"                                                 # Kilbarra
    # Uncoded portals (India) match by words.
    india = session(tmp_path, "IN", places=("Pune",))
    rows = {n.notice_id: n for n in cppp.CpppTenders().search(tender_web(), query("IN"))}
    assert india.wanted(rows["2026_EX_1001_1"])[2]["keywords"] == ["web portal"]
    assert india.wanted(rows["2026_EX_1002_1"])[1] == "skipped_unmatched"
    assert india.wanted(rows["2026_EX_1004_1"])[0]                      # "AMC of computers"


def test_a_missing_key_skips_the_portal_and_says_where_to_get_one(tmp_path, monkeypatch):
    monkeypatch.delenv("LEADSCRAPER_SECRET_SAM_GOV_API_KEY", raising=False)
    s = session(tmp_path, "US", places=("Austin",))
    unit = s.units()[0]
    reason = s.unit_skip_reason(unit)
    assert reason.startswith("key not configured: SAM.gov")
    assert "leadscraper secrets set sam-gov-api-key" in reason and "https://sam.gov" in reason
    monkeypatch.setenv("LEADSCRAPER_SECRET_SAM_GOV_API_KEY", "k-123")
    assert session(tmp_path, "US", places=("Austin",)).unit_skip_reason(unit) is None


def test_the_portal_key_can_only_be_a_sam_gov_secret():
    with pytest.raises(ValueError):
        TendersConfig.model_validate({"sources": {"sam_gov": {"key_secret_name":
                                                               "anthropic-api-key"}}})


# --- runs ---------------------------------------------------------------------------------------

def run(tmp_path, monkeypatch, name: str, lead_types: dict, *, country="IE",
        places=("Fernvale",)):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    config = compile_run_config(RunConfig.model_validate({
        "name": "Tenders test", "project": f"tenders-test-{tmp_path.name}-{name}",
        "geography": {"country": country, "places": list(places)},
        "lead_types": lead_types,
        "runtime": {"db_url": run_database(tmp_path, name)}}))
    return config, execute(RunRequest(config=config, install_signals=False))


class CouncilSource(Source):
    """local_business's discovery, offline: a council a tender will name, and a shop."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Fernvale County Council",
                             website="https://www.fernvale-council.example/",
                             locality=locality.name, category=category.name,
                             phones=["+35310000999"])
        yield self.make_lead(business_name="Fernvale Florist", locality=locality.name,
                             category=category.name, phones=["+35310000998"])


LOCAL = {"local_business": {"categories": [{"name": "Organisations"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0},
                                        "probe": {"enabled": True, "min_delay": 0,
                                                  "max_delay": 0}}}}
FAST = {"sources": {"ted": {"min_delay": 0, "max_delay": 0}}}


@pytest.fixture
def portals(monkeypatch):
    web = tender_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": CouncilSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"probe": probe_enricher()})
    return web


def signals_by_org(config, lead_type="tenders") -> dict[str, list[dict]]:
    names = {o["id"]: o["name"] for o in project_rows(config, Organization)}
    out: dict[str, list[dict]] = {}
    for s in project_rows(config, Signal):
        if s["lead_type"] == lead_type:
            out.setdefault(names[s["organization_id"]], []).append(s)
    return out


def workbook(config, run_id):
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                             run_id=run_id)[1]
    store.close()
    return load_workbook(io.BytesIO(content))["Records"]


def test_a_run_turns_notices_into_signals_on_public_buyers(tmp_path, monkeypatch, portals):
    config, outcome = run(tmp_path, monkeypatch, "ted", {**LOCAL, "tenders": FAST})
    assert outcome.ok, outcome.results
    by_org = signals_by_org(config)
    assert {name: sorted(s["type"] for s in sigs) for name, sigs in by_org.items()} == {
        "Fernvale County Council": ["tender"], "Harbourline Port Authority": ["tender_planned"]}
    council = by_org["Fernvale County Council"][0]
    assert council["key"] == "ted:900101-2026" and council["strength"] == 0.75
    assert council["expires_at"] is not None and council["published_at"] is not None
    payload = council["payload"]
    assert payload["value"]["text"] == "€400,000" and payload["portal"] == "ted"
    assert payload["matched_codes"] == ["72000000", "72268000"]
    assert payload["documents_url"] == "https://etenders.example/docs/900101"
    stats = outcome.results["tenders"].stats
    assert stats["skipped_unmatched"] == 1 and stats["skipped_closed"] == 1
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    # A buyer local_business already holds gets the signal and keeps its record (rule 6).
    assert orgs["Fernvale County Council"]["lead_types"] == ["local_business", "tenders"]
    assert orgs["Fernvale County Council"]["record"]["phones"] == ["+35310000999"]
    port = orgs["Harbourline Port Authority"]
    assert port["lead_types"] == ["tenders"]
    assert port["record"]["emails"] == ["tenders@harbourline-port.example"]
    assert port["record"]["locality"] == "Kilbarra"
    public = {a["organization_id"] for a in project_rows(config, OrgAttribute)
              if a["key"] == "org_type" and a["value"] == "public_buyer"}
    assert public == {orgs["Fernvale County Council"]["id"],
                      orgs["Harbourline Port Authority"]["id"]}

    sheet = workbook(config, outcome.run_id)
    header = [c.value for c in sheet[1]]
    for column in ("Org Type", "Open Tender", "Tender Closes", "Tender Value", "Tender Link",
                   "Tender Documents", "Open Tenders", "Tender Portals", "Tender Codes",
                   "Intent", "Top Signal"):
        assert column in header, column
    names = [r[header.index("Name")] for r in sheet.iter_rows(min_row=2, values_only=True)]
    row = names.index("Fernvale County Council") + 2
    cell = lambda column: sheet.cell(row, header.index(column) + 1)  # noqa: E731
    assert cell("Org Type").value == "Public buyer"
    assert cell("Open Tender").value == "Case management system for planning applications"
    assert cell("Tender Value").value == "€400,000" and cell("Tender Portals").value == "TED"
    assert cell("Tender Link").hyperlink.target.startswith("https://ted.europa.eu/")

    # Seen again: refreshed in place, not duplicated.
    _, again = run(tmp_path, monkeypatch, "ted", {**LOCAL, "tenders": FAST})
    assert again.ok
    assert sum(len(v) for v in signals_by_org(config).values()) == 2


def test_uk_portals_together(tmp_path, monkeypatch, portals):
    section = {"sources": {"find_a_tender": {"min_delay": 0, "max_delay": 0},
                           "contracts_finder": {"min_delay": 0, "max_delay": 0}}}
    config, outcome = run(tmp_path, monkeypatch, "uk", {"tenders": section}, country="GB",
                          places=("Northwick",))
    assert outcome.ok, outcome.results
    by_org = {name: sorted(s["type"] for s in sigs)
              for name, sigs in signals_by_org(config).items()}
    assert by_org == {"Northwick Borough Council": ["tender"],
                      "Eastvale District Council": ["tender_planned"],
                      "Ashby Parish Council": ["tender"],
                      "Kettering Fire Service": ["tender_planned"]}
    council = {o["name"]: o for o in project_rows(config, Organization)}[
        "Northwick Borough Council"]["record"]
    assert council["registry_ids"] == {"gb_fts": "GB-FTS-11"}
    assert council["website"] == "https://northwick.example/"
    assert council["emails"] == ["procurement@northwick.example"]
    assert council["locality"] == "Northwick"
    # The two councils share a procurement helpdesk number, but not a register number.
    assert orgs_named(config, "Eastvale District Council")


def orgs_named(config, name: str) -> list[dict]:
    return [o for o in project_rows(config, Organization) if o["name"] == name]


def test_one_shared_phone_never_merges_two_registered_buyers(tmp_path):
    store = LeadStore(f"sqlite:///{tmp_path / 'ids.sqlite3'}", project="ids")
    first, _, _ = store.upsert(Lead(business_name="Northwick Borough Council",
                                    phones=["+442079460000"],
                                    registry_ids={"gb_fts": "GB-FTS-11"}))
    second, action, _ = store.upsert(Lead(business_name="Eastvale District Council",
                                          phones=["+442079460000"],
                                          registry_ids={"gb_fts": "GB-FTS-12"}))
    again, merged, _ = store.upsert(Lead(business_name="Northwick Borough Council",
                                         registry_ids={"gb_fts": "GB-FTS-11"}))
    plain, joined, _ = store.upsert(Lead(business_name="Eastvale Florist",
                                         phones=["+442079460000"]))       # no register number
    store.close()
    assert action == "new" and second != first
    assert again == first and merged in ("unchanged", "merged")
    assert joined in ("merged", "unchanged") and plain == first          # the phone rule as ever


def _units(config, run_id) -> list[dict]:
    return [r for r in project_rows(config, WorkUnitRow) if r["run_id"] == run_id]


def test_a_missing_key_is_a_skipped_unit_and_the_run_completes(tmp_path, monkeypatch, portals):
    monkeypatch.delenv("LEADSCRAPER_SECRET_SAM_GOV_API_KEY", raising=False)
    section = {"sources": {"sam_gov": {"min_delay": 0, "max_delay": 0}}}
    config, outcome = run(tmp_path, monkeypatch, "nokey", {"tenders": section}, country="US",
                          places=("Austin",))
    assert outcome.status == "completed"
    [(key, reason)] = outcome.skipped
    assert key == "tenders|sam_gov|*|*"
    assert reason.startswith("key not configured: SAM.gov (US federal opportunities) needs "
                             "the secret sam-gov-api-key")
    assert outcome.estimate["unavailable"][0]["source"] == "sam_gov"
    assert [(r["status"], r["skip_reason"][:18]) for r in _units(config, outcome.run_id)] == \
        [("skipped", "key not configured")]
    failures = [f for f in project_rows(config, FailureRow) if f["source"] == "sam_gov"]
    assert [f["reason"] for f in failures] == ["key_not_configured"]
    assert not [c for c in portals.calls if "sam.gov" in c]              # never asked
    assert signals_by_org(config) == {}
    # With the key, the same run asks SAM.gov.
    monkeypatch.setenv("LEADSCRAPER_SECRET_SAM_GOV_API_KEY", "k-123")
    config, outcome = run(tmp_path, monkeypatch, "key", {"tenders": section}, country="US",
                          places=("Austin",))
    assert outcome.ok and not outcome.skipped
    sam_signals = signals_by_org(config)
    assert list(sam_signals) == ["OFFICE OF ACQUISITIONS"]
    assert sorted(s["type"] for s in sam_signals["OFFICE OF ACQUISITIONS"]) == \
        ["tender", "tender_planned"]
    assert all("k-123" not in json.dumps(f, default=str)
               for f in project_rows(config, FailureRow))


def test_dry_run_estimate_lists_units_that_will_not_run(tmp_path, monkeypatch, portals):
    monkeypatch.delenv("LEADSCRAPER_SECRET_SAM_GOV_API_KEY", raising=False)
    (tmp_path / "dry").mkdir()
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / "dry"))
    config = compile_run_config(RunConfig.model_validate({
        "name": "Dry", "project": f"dry-{tmp_path.name}",
        "geography": {"country": "US", "places": ["Austin"]},
        "lead_types": {"tenders": {}}, "runtime": {"db_url": run_database(tmp_path, "dry")}}))
    outcome = execute(RunRequest(config=config, dry_run=True, install_signals=False))
    text = describe_estimate(outcome.estimate)
    assert "Not run      : 1 unit(s)" in text
    assert "tenders/sam_gov: key not configured: SAM.gov" in text
    assert outcome.estimate["units_queued"] == 0
    assert not project_rows(config, FailureRow)                       # a dry run records none


def local_business_output(config) -> tuple[list, list]:
    orgs = sorted((o for o in project_rows(config, Organization)
                   if "local_business" in o["lead_types"]), key=lambda o: o["name"])
    out = []
    for org in orgs:
        row = Lead.from_dict(org["record"]).to_row()
        for column in ("First Seen", "Last Updated"):
            row.pop(column)
        out.append((row, org["score"], org["fit"], org["reach"], org["score_reasons"]))
    ids = {o["id"] for o in orgs}
    evidence = sorted((e["source"], tuple(e["fields"])) for e in project_rows(config, Evidence)
                      if e["organization_id"] in ids and e["lead_type"] == "local_business")
    return out, evidence


def test_enabling_or_disabling_tenders_leaves_local_business_unchanged(tmp_path, monkeypatch,
                                                                       portals):
    alone, first = run(tmp_path, monkeypatch, "alone", LOCAL)
    view = local_business_view(first)
    together, outcome = run(tmp_path, monkeypatch, "together", {**LOCAL, "tenders": FAST})
    assert local_business_view(outcome) == view         # the port authority is not its lead
    off, last = run(tmp_path, monkeypatch, "off", {**LOCAL, "tenders": {"enabled": False}})
    assert local_business_view(last) == view
    assert outcome.results["tenders"].new == 1
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline


def test_a_crashing_portal_is_contained(tmp_path, monkeypatch, portals):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self, http, query, *, key=None):
        raise RuntimeError("ted exploded")

    monkeypatch.setattr(ted.TedTenders, "search", boom)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "tenders": FAST})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert any(f["reason"] == "fetch_failed" and "exploded" in (f["detail"] or "")
               for f in project_rows(crashed, FailureRow))


# --- scoring, columns, depth ---------------------------------------------------------------------

def tender_signal(type_: str, published: float, closes: float | None, strength: float = 0.75,
                  title: str = "Case management system", **payload) -> dict:
    return {"type": type_, "lead_type": "tenders", "title": title, "strength": strength,
            "published_at": days(published), "observed_at": days(0),
            "expires_at": days(closes) if closes is not None else None,
            "payload": {"portal": "ted", **payload}, "source_url": "https://ted.example/n"}


def test_closed_tenders_drop_out_of_intent_and_fresh_ones_count_most():
    now = datetime.now(UTC)
    open_ = tender_signal("tender", 3, -20, value={"text": "€400,000"})
    closed = tender_signal("tender", 40, 1, title="Old one")
    planned = tender_signal("tender_planned", 10, None, strength=0.5, title="Future cloud")
    assert weight(closed, now) == 0 and tender_signals([closed], now) == []
    assert TENDERS.score(None, [closed], None, now=now).intent == 0.0
    parts = TENDERS.score(None, [open_, closed, planned], None, now=now)
    assert 0.7 < parts.intent <= 1.0
    assert parts.reasons[0].startswith("open tender: Case management system (closes ")
    assert "€400,000, TED" in parts.reasons[0]
    assert "planned tender: Future cloud" in parts.reasons[1]
    # A tender ages: the same notice published 60 days ago (one half-life) counts half.
    assert abs(weight(tender_signal("tender", 60, -5), now) - 0.375) < 0.01
    # An undated planned notice goes stale after four half-lives.
    assert weight(tender_signal("tender_planned", 400, None, strength=0.5), now) == 0.0


def test_an_export_after_the_deadline_drops_the_tender(tmp_path, monkeypatch, portals):
    config, outcome = run(tmp_path, monkeypatch, "late", {"tenders": FAST})
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    ws = default_workspace(store.engine)
    now_table = exports.build_table(store.engine, ws, run_id=outcome.run_id)
    later = exports.build_table(store.engine, ws, run_id=outcome.run_id,
                                generated_at=datetime.now(UTC) + timedelta(days=60))
    store.close()

    def intent(table, name):
        record = next(r for r in table.records if r.lead.business_name == name)
        return record.overall.intent
    assert intent(now_table, "Fernvale County Council") > 0.5
    assert intent(later, "Fernvale County Council") == 0.0          # closed by then


def test_column_values():
    now = datetime.now(UTC)
    org = SimpleNamespace(as_of=now, attributes={"org_type": {"value": "public_buyer"}},
                          signals=[tender_signal("tender", 3, -20, value={"text": "€400,000"},
                                                 documents_url="https://docs.example/1",
                                                 matched_codes=["72000000"],
                                                 contact="Procurement Team"),
                                   tender_signal("tender", 50, 2, title="Closed")])
    values = TENDERS.column_values(org, None)
    assert values["org_type"] == "Public buyer" and values["open_tenders"] == 1
    assert values["value"] == "€400,000" and values["documents"] == "https://docs.example/1"
    assert values["codes"] == "72000000" and values["contact"] == "Procurement Team"
    assert values["tenders"].startswith("Case management system (closes ")
    bare = SimpleNamespace(as_of=now, attributes={}, signals=[])
    assert TENDERS.column_values(bare, None) == {}


def test_depth_scales_what_is_read():
    assert TENDERS.apply_depth(_config("GB"), "deep") == ["tenders: deep reads 2× the notices"]
    deep = TENDERS.depth_changes(TendersConfig(), "deep")[0]["sources"]
    assert deep.find_a_tender.max_pages == 40 and deep.ted.max_results == 500
    quick = TENDERS.depth_changes(TendersConfig(), "quick")[0]["sources"]
    assert quick.find_a_tender.max_pages == 10
    assert TENDERS.depth_changes(TendersConfig(), "balanced") == ({}, [])


def test_no_key_value_is_ever_in_the_catalog(monkeypatch):
    monkeypatch.setenv("LEADSCRAPER_SECRET_SAM_GOV_API_KEY", "k-very-secret")
    rows = [s for s in catalog.sources(country="US", with_health=False)
            if s["name"] == "sam_gov"]
    assert rows[0]["key"]["configured"] is True and rows[0]["health"]["status"] == "unknown"
    assert "k-very-secret" not in json.dumps(rows, default=str)


# --- live (opt-in) -------------------------------------------------------------------------------

def _live_http():
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    return HttpClient(RateLimiter((1.0, 2.0)), max_retries=1)


@pytest.mark.live
def test_live_ted():
    notices = ted.TedTenders().search(_live_http(), query("IE", max_results=5, max_pages=1))
    assert notices and all(n.buyer.name and n.notice_id for n in notices)


@pytest.mark.live
def test_live_find_a_tender_and_contracts_finder():
    http = _live_http()
    assert find_a_tender.FindATenderTenders().search(http, query("GB", max_pages=1))
    assert contracts_finder.ContractsFinderTenders().search(http, query("GB", max_results=20))


@pytest.mark.live
def test_live_sam_gov():
    from leadscraper.leadtypes.toolkit import secret

    key = secret("sam-gov-api-key")
    if not key:
        pytest.skip("no SAM.gov key: leadscraper secrets set sam-gov-api-key")
    notices = sam_gov.SamGovTenders().search(_live_http(), query("US", max_results=10,
                                                                 max_pages=1), key=key)
    assert all(n.notice_id for n in notices)


@pytest.mark.live
def test_live_indian_portals():
    http = _live_http()
    assert cppp.CpppTenders().search(http, query("IN", max_pages=1))
    assert gem.GemTenders().search(http, query("IN", keywords=["software"], max_pages=1))
