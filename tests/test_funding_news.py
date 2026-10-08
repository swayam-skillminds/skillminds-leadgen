"""funding_news (Phase 6.3): headline rules, amounts, feeds, GDELT, runs, isolation.

All offline: every news source is a fictional fixture in the shape it answered
live on 2026-10-08 (``tests/fixtures/news``), served by ``FakeWeb``. The
``live`` tests (``-m live``) make one GDELT query and read GlobeNewswire's
India feed.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Iterator

import pytest
from openpyxl import load_workbook

from fake_web import (FakeWeb, Page, fixture, kit_for, local_business_view, patch_kit,
                      probe_enricher, project_rows, run_database)
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Evidence, FailureRow, Organization, Signal
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes import registry
from leadscraper.leadtypes.funding_news.amounts import parse_amount, parse_round
from leadscraper.leadtypes.funding_news.events import classify, subject_company
from leadscraper.leadtypes.funding_news.plugin import FUNDING_NEWS, news_signals
from leadscraper.leadtypes.funding_news.config import FundingNewsConfig
from leadscraper.leadtypes.funding_news.session import FundingNewsSession, plan_units, strength
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.net.ratelimit import BlockedError
from leadscraper.services import exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.base import Source
from leadscraper.sources.news import feeds, gdelt, google_news
from leadscraper.sources.news.base import NewsItem, parse_feed, unspace

GDELT_API = gdelt.API
GOOGLE = "https://news.google.com/rss/search"
GNW_INDIA = feeds.globenewswire_url("IN", "India")
PIA_FEED = "https://pia.example/feed.atom"


def dated(text: str, now: datetime | None = None) -> str:
    """"@gdelt:N" / "@rfc:N" / "@iso:N" -> N days before now, in that source's format."""
    now = now or datetime.now(timezone.utc)

    def sub(match: re.Match) -> str:
        when = now - timedelta(days=float(match.group(2)))
        kind = match.group(1)
        if kind == "gdelt":
            return when.strftime("%Y%m%dT%H%M%SZ")
        if kind == "rfc":
            return format_datetime(when, usegmt=True)
        return when.isoformat(timespec="seconds").replace("+00:00", "Z")
    return re.sub(r"@(gdelt|rfc|iso):(\d+(?:\.\d+)?)", sub, text)


def news_web() -> FakeWeb:
    web = FakeWeb()
    web.add(GDELT_API, Page(dated(fixture("news/gdelt_artlist.json")),
                            headers={"Content-Type": "application/json"}))
    web.add(GOOGLE, Page(dated(fixture("news/google_news.xml"))))
    web.add(GNW_INDIA, Page(dated(fixture("news/globenewswire_india.xml"))))
    web.add(PIA_FEED, Page(dated(fixture("news/atom_feed.xml"))))
    return web


# --- amounts, rounds, headlines -----------------------------------------------------------

@pytest.mark.parametrize("text, amount, value, currency, stage", [
    ("Ledgerly raises Rs 22 crore in funding", "Rs 22 crore", 2.2e8, "INR", None),
    ("raises ₹12.5 Cr pre-series A funding", "₹12.5 Cr", 1.25e8, "INR", "Pre-Series A"),
    ("raises $1.1 Mn in pre Seed funding", "$1.1 Mn", 1.1e6, "USD", "Pre-Seed"),
    ("raises $10 million from Uber at Rs 1,505 crore valuation", "$10 million", 1e7, "USD",
     None),
    ("raises ₹8-10 cr in growth funding", "₹8-10 cr", 8e7, "INR", "Growth"),
    ("raises ₹1,000 crores in bank financing", "₹1,000 crores", 1e10, "INR", None),
    ("raises EUR 2.5m Series B", "EUR 2.5m", 2.5e6, "EUR", "Series B"),
    ("gets 22 crore rupees", "22 crore rupees", 2.2e8, "INR", None),
    ("passes 12 million users", None, None, None, None),
    ("$5 off this week", None, None, None, None),
])
def test_amounts_and_rounds_as_headlines_state_them(text, amount, value, currency, stage):
    found = parse_amount(text)
    if amount:
        assert (found.text, found.value, found.currency) == (amount, value, currency)
    else:
        assert found is None
    assert parse_round(text) == stage


@pytest.mark.parametrize("headline, kind, company", [
    # Fictional rewrites of live headline shapes (2026-10-08).
    ("Pune-based fintech and brokerage startup Ledgerly raises Rs 22 crore in funding",
     "funding", "Ledgerly"),
    ("Ledgerly Raises Rs 22 Cr In Pre-Series A Round", "funding", "Ledgerly"),
    ("Medical tourism startup HealWay raises $1.1 Mn in pre Seed funding", "funding", "HealWay"),
    ("Travel and lifestyle brand Monsoon Bags raises Rs 170 crore in funding from Sauce Capital",
     "funding", "Monsoon Bags"),
    ("Exclusive: Lotus Logistics raises $5 million Series A", "funding", "Lotus Logistics"),
    ("Bengaluru's Zentro Labs raises seed funding", "funding", "Zentro Labs"),
    ("Kestrel Robotics opens new R&D centre in Pune", "expansion", "Kestrel Robotics"),
    ("Zentro Labs expands to Pune with new office", "expansion", "Zentro Labs"),
    ("Kestrel Logistics acquires Beta Labs for $20M", "acquisition", "Kestrel Logistics"),
    ("Beta Labs acquired by Kestrel Logistics", "acquisition", "Beta Labs"),
    ("Marigold Textiles partners with Infosys for digital transformation",
     "digital_transformation", "Marigold Textiles"),
    ("Marigold Textiles selects SAP S/4HANA to modernise operations",
     "digital_transformation", "Marigold Textiles"),
    ("Brightline Foods appoints Priya Raman as CTO", "leadership_change", "Brightline Foods"),
    ("Brightline Foods Appoints Priya Raman As Chief Technology Officer", "leadership_change",
     "Brightline Foods"),
    ("Priya Raman joins Brightline Foods as Chief Digital Officer", "leadership_change",
     "Brightline Foods"),
    ("Kalpa Health Names Arjun Mehta Chief Information Officer", "leadership_change",
     "Kalpa Health"),
    ("Acme launches AI-powered inventory platform for retailers", "launch", "Acme"),
    # Not events, or not about a company.
    ("Pune mosque offers prayers and raises funds for flood relief", None, None),
    ("Sensex raises concerns as markets fall", None, None),
    ("RBI raises repo rate by 25 bps", None, None),
    ("Govt launches scheme for startups", None, None),
    ("AAP red-flags absence of staff; launches campaign for accountability", None, None),
    ("Elevate Campuses debuts at discount, valued at $625 mn", None, None),
    ("Tech M&A deals: HumanTronik, CloudMoyo, Shipyard", None, None),
    ("Acme Foods partners with local farmers for fresh produce", None, None),
    # Live 2026-10-08: a transport office is a public body, not a company.
    ("Pune RTO launches WhatsApp number for passengers", None, None),
    # The Phase 6b review: business won, loans, people, questions, commas, parentheses.
    ("Kestrel Logistics bags $1 billion deal from a state utility", None, None),
    ("Marigold Textiles bags 'significant' order worth Rs 1,000 crore", None, None),
    ("Lotus Logistics gets $750 million loan from banks", None, None),
    ("Ledgerly CEO Anika Desai raises $10 million for new venture", None, None),
    ("Byju's founder Byju Raveendran raises $1 billion", None, None),
    ("Board names Rahul Mehta as CEO", None, None),
    ("Man raises Rs 5 lakh for daughter's surgery through crowdfunding", None, None),
    ("Why Zentro Labs raises $500 million now", "funding", "Zentro Labs"),
    ("IPO-bound Zentro Labs raises Rs 1,000 crore from anchor investors", "funding",
     "Zentro Labs"),
    ("Nifty, Sensex Gain As Reliance Raises Rs 10,000 Crore", "funding", "Reliance"),
    ("Acme Robotics Inc (NASDAQ: ACME) Raises $5M", "funding", "Acme Robotics Inc"),
    ("Startup Acme closes $10M Series A", "funding", "Acme"),
    ("Acme Expands Its Product Line", None, None),
    ("Acme Enters Into Agreement With Beta", None, None),
    ("Acme sets up Rs 100 crore fund for startups", None, None),
    ("Acme Expands Its Footprint To Pune", "expansion", "Acme"),
])
def test_headlines_name_an_event_and_its_company(headline, kind, company):
    event = classify(headline, places=["Pune", "India"])
    if kind:
        assert (event.kind, event.company) == (kind, company)
    else:
        assert event is None


def test_event_details():
    funding = classify("Pune's Ledgerly raises ₹22 Cr in pre-Series A led by Monsoon Ventures",
                       places=["Pune"])
    assert (funding.amount.text, funding.round, funding.investors) == \
        ("₹22 Cr", "Pre-Series A", "Monsoon Ventures")
    cto = classify("Brightline Foods appoints Priya Raman as CTO")
    assert (cto.person, cto.role, cto.tech_role) == ("Priya Raman", "CTO", True)
    ceo = classify("Brightline Foods names Arjun Mehta CEO")
    assert ceo.tech_role is False
    assert strength("leadership_change", 1, cto) == 0.75
    assert strength("leadership_change", 1, ceo) == 0.6
    assert strength("funding", 3) == 0.9 and strength("funding", 9) == 0.95
    deal = classify("Kestrel Logistics acquires Beta Labs for $20M")
    assert (deal.other, deal.amount.text) == ("Beta Labs", "$20M")
    # A press release names its issuer: the company whatever the grammar.
    release = classify("Announces record quarter and opens new office in Pune",
                       issuer="Saffron Analytics")
    assert (release.kind, release.company) == ("expansion", "Saffron Analytics")
    titled = classify("Microsoft Appoints Satya Nadella As Chairman")
    assert (titled.person, titled.role) == ("Satya Nadella", "Chairman")   # not "Nadella As"
    assert subject_company("Pune Acme", ["Pune"]) == "Acme"
    assert subject_company("lab-grown diamond startup ONYA") == "ONYA"


# --- feeds and sources ----------------------------------------------------------------------

def test_rss_and_atom_feeds_parse_and_hostile_xml_does_not():
    rss = parse_feed(dated(fixture("news/globenewswire_india.xml")), source="press_releases")
    assert [i.company for i in rss] == ["Saffron Analytics", "Kalpa Health", "Insight Reports"]
    assert "saffron-analytics.example" in rss[0].links[0]
    assert rss[0].published_at < datetime.now(timezone.utc)
    atom = parse_feed(dated(fixture("news/atom_feed.xml")), source="feeds")
    assert [i.url for i in atom] == ["https://pia.example/news/lotus-fleet-app",
                                     "https://pia.example/news/agm"]
    assert atom[0].summary.startswith("Lotus Logistics, a Pune member")
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">'
            '<!ENTITY ext SYSTEM "file:///etc/passwd">]>'
            '<rss><channel><item><title>&c; &ext;</title><link>https://x.example/1</link>'
            '</item></channel></rss>')
    items = parse_feed(bomb, source="feeds")
    assert all("aaaaaaaaaa" not in i.title and "root:" not in i.title for i in items)
    assert parse_feed("not a feed at all", source="feeds") == []
    assert parse_feed(b"", source="feeds") == []


def test_gdelt_queries_and_titles():
    query = gdelt.build_query(["raises", "seed round", "series a"], place="Pune",
                              country="India", language="english")
    assert query == '(raises OR "seed round" OR "series a") Pune sourcecountry:india ' \
                    'sourcelang:english'
    assert gdelt.build_query(["new office"], place="New Delhi") == '"new office" "New Delhi"'
    assert unspace("Pune - based Ledgerly raises ₹1 , 000 crores , led by Acme ' s fund") == \
        "Pune-based Ledgerly raises ₹1,000 crores, led by Acme's fund"
    items = gdelt.search(news_web(), query, days=30)
    assert items[0].title.startswith("Pune-based fintech startup Ledgerly raises Rs 22 crore")
    assert items[0].publisher == "startupwire.example" and items[0].published_at is not None


def test_gdelt_answering_with_text_is_a_failure_not_a_crash(tmp_path):
    web = FakeWeb({GDELT_API: Page("Please limit requests to one every 5 seconds.")})
    with pytest.raises(gdelt.GdeltError, match="one every 5 seconds"):
        gdelt.search(web, "raises", days=30)
    assert session(tmp_path, web=web, events=["funding"]).read_gdelt("Pune").status == "failed"
    web = FakeWeb({GDELT_API: BlockedError("HTTP 429 from api.gdeltproject.org")})
    assert session(tmp_path, web=web, events=["funding"]).read_gdelt("Pune").status == "failed"


def test_one_gdelt_query_per_place_and_none_after_a_throttle(tmp_path):
    asked: list[str] = []

    def throttled(url):
        asked.append(url)
        return Page("Please limit requests to one every 5 seconds or contact us.")

    news = session(tmp_path, web=FakeWeb({GDELT_API: throttled}), places=("Pune", "Nashik"))
    assert news.read_gdelt("Pune").status == "failed"
    assert news.read_gdelt("Nashik").error.startswith("GDELT throttled")
    assert len(asked) == 1                          # one request, then the breaker
    reasons = [f["reason"] for f in _failures(tmp_path)]
    assert reasons == ["rate_limited"]
    plain = session(tmp_path)
    queries = plain.gdelt_queries("Pune")
    assert len(queries) == 3 and all(q.count(" OR ") <= 5 for q in queries)
    assert plain.read_gdelt("Pune").found == 27      # three queries, the same 9 fixture items


def _failures(tmp_path) -> list[dict]:
    import json

    path = tmp_path / "failures.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("company, links, site", [
    ("Kalpa Health", ["https://www.globenewswire.com/x", "https://kalpa-health.example/news"],
     "https://kalpa-health.example/news"),
    ("The Sleep Company", ["https://www.thehindubusinessline.com/companies/x",
                           "https://thesleepcompany.in"], "https://thesleepcompany.in/"),
    ("Tata Motors", ["https://www.tatacapital.com/", "https://www.tatamotors.com/"],
     "https://tatamotors.com/"),
    ("Acme Robotics", ["https://www.acmeinvestors.vc/portfolio"], None),
    ("Ledgerly", ["https://getledgerly.example/"], "https://getledgerly.example/"),
    ("Ledgerly", ["https://ledgerlyventurepartners.example/"], None),
    ("Indian Hotels Company", ["https://www.indianexpress.com/a"], None),
])
def test_a_news_companys_site_must_spell_its_name(company, links, site):
    assert FundingNewsSession._site(company, links) == site


def test_the_outlet_is_never_the_companys_site():
    assert FundingNewsSession._site("Startup Wire", ["https://startupwire.example/2026/x"],
                                    {"startupwire.example"}) is None


def test_google_news_titles_lose_their_outlet():
    items = google_news.search(news_web(), google_news.build_query(["raises"], place="Pune"),
                               locale="en-IN", country="IN")
    assert items[0].title == "Pune-based fintech startup Ledgerly raises Rs 22 crore in " \
                             "pre-Series A round"
    assert items[0].publisher == "Startup Wire" and items[0].summary == ""
    assert google_news.build_query(["raises", "new office"], place="Pune", days=14) == \
        '(raises OR "new office") "Pune" when:14d'


# --- the session ------------------------------------------------------------------------------

def session(tmp_path, *, web=None, ai=None, places=("Pune",), **section) -> FundingNewsSession:
    config = compile_run_config(RunConfig.model_validate({
        "name": "News", "project": "news", "geography": {"country": "IN", "places": list(places)},
        "lead_types": {"funding_news": section}}))
    context = SimpleNamespace(config=config, store=None, ai=ai, run_id=None)
    return FundingNewsSession(context, FUNDING_NEWS.effective(config),
                              kit=kit_for(web or news_web(), tmp_path))


def test_the_place_filter(tmp_path):
    items = gdelt.search(news_web(), "x", days=90)
    tidewater = next(i for i in items if "Tidewater" in i.title)
    ledgerly = items[0]
    headline = session(tmp_path)
    assert headline._keep(ledgerly, "Pune") and not headline._keep(tidewater, "Pune")
    assert session(tmp_path, place_filter="article")._keep(tidewater, "Pune")
    assert session(tmp_path, place_filter="none")._keep(tidewater, None)
    old = next(i for i in items if "Oldco" in i.title)
    assert not session(tmp_path, place_filter="none")._keep(old, None)       # 40 days old


class FakeAI:
    """Answers signal_interpretation with fixed verdicts (no provider is ever called)."""

    def __init__(self, items):
        self.items = items
        self.systems: list[str] = []

    def uses(self, task: str) -> bool:
        return task == "signals"

    def call(self, task, *, system, user, output):
        self.systems.append(system)
        return SimpleNamespace(data=output.model_validate({"items": self.items}),
                               provider="fake", model="judge")


def test_ai_reads_headlines_the_rules_cannot_with_a_grounded_company(tmp_path):
    items = [NewsItem(title="Monsoon Bank's new app puts Pune farmers online",
                      url="https://citybusiness.example/monsoon-bank-app", source="gdelt"),
             NewsItem(title="A quiet week for Pune's makers",
                      url="https://citybusiness.example/quiet", source="gdelt")]
    ai = FakeAI([{"id": "0", "signal_type": "digital_transformation", "strength": 0.7,
                  "summary": "A bank digitises farm lending", "evidence": "puts Pune farmers online",
                  "company": "Monsoon Bank"},
                 {"id": "1", "signal_type": "funding_round", "strength": 0.9,
                  "summary": "Raised money", "evidence": "A quiet week for Pune",
                  "company": "Acme Invented Ltd"}])
    read = session(tmp_path, ai=ai).interpret(items)
    assert [(e.kind, e.company, e.interpreted_by) for _, e in read] == \
        [("digital_transformation", "Monsoon Bank", "fake/judge")]   # the invented company fails
    assert "`company`" in ai.systems[0]
    assert session(tmp_path).interpret(items) == []                  # AI off: no verdicts


# --- runs --------------------------------------------------------------------------------------

def run(tmp_path, monkeypatch, name: str, lead_types: dict, places=("Pune",)):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    config = compile_run_config(RunConfig.model_validate({
        "name": "News test", "project": f"news-test-{tmp_path.name}-{name}",
        "geography": {"country": "IN", "places": list(places)},
        "lead_types": lead_types,
        "runtime": {"db_url": run_database(tmp_path, name)},
    }))
    return config, execute(RunRequest(config=config, install_signals=False))


def signals_by_org(config) -> dict[str, list[dict]]:
    names = {o["id"]: o["name"] for o in project_rows(config, Organization)}
    out: dict[str, list[dict]] = {}
    for s in project_rows(config, Signal):
        if s["lead_type"] == "funding_news":
            out.setdefault(names[s["organization_id"]], []).append(s)
    return out


class NewsSource(Source):
    """local_business's discovery, offline: two firms the news will mention, one it won't."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Kestrel Robotics", website="https://kestrel.example/",
                             locality=locality.name, category=category.name,
                             phones=["+912000000011"])
        yield self.make_lead(business_name="Lotus Logistics", website="https://lotus.example/",
                             locality=locality.name, category=category.name,
                             phones=["+912000000012"])
        yield self.make_lead(business_name="Quiet Traders", locality=locality.name,
                             category=category.name, phones=["+912000000013"])


LOCAL = {"local_business": {"categories": [{"name": "Companies"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0},
                                        "probe": {"enabled": True, "min_delay": 0,
                                                  "max_delay": 0}}}}
GDELT_ONLY = {"sources": {"gdelt": {"min_delay": 5, "max_delay": 5}}}


@pytest.fixture
def news(monkeypatch):
    web = news_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": NewsSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"probe": probe_enricher()})
    return web


def test_a_run_turns_news_into_signals(tmp_path, monkeypatch, news):
    config, outcome = run(tmp_path, monkeypatch, "gdelt", {**LOCAL, "funding_news": GDELT_ONLY})
    assert outcome.ok, outcome.results
    by_org = signals_by_org(config)
    assert {name: [(s["type"], s["title"]) for s in sigs] for name, sigs in by_org.items()} == {
        "Ledgerly": [("funding_round", "Raised ₹22 Cr (Pre-Series A) led by Monsoon Ventures")],
        "Kestrel Robotics": [("expansion", "Kestrel Robotics opens new R & D centre in Pune")],
        "Marigold Textiles": [("digital_transformation",
                               "Marigold Textiles selects SAP S/4HANA to modernise its Pune "
                               "plant")],
        "Brightline Foods": [("leadership_change", "Appointed Priya Raman as Chief Technology "
                                                   "Officer")],
    }
    ledgerly = by_org["Ledgerly"][0]
    payload = ledgerly["payload"]
    assert payload["amount"] == {"text": "₹22 Cr", "value": 2.2e8, "currency": "INR"}
    assert payload["round"] == "Pre-Series A" and payload["investors"] == "Monsoon Ventures"
    assert [c["publisher"] for c in payload["coverage"]] == ["startupwire.example",
                                                             "citybusiness.example"]
    assert ledgerly["strength"] == 0.85 and ledgerly["published_at"] is not None  # 2 outlets
    assert by_org["Brightline Foods"][0]["strength"] == 0.75                      # a new CTO
    stats = outcome.results["funding_news"].stats
    assert stats["skipped_place"] >= 1 and stats["skipped_old"] >= 1             # Mumbai, 40 days
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    assert "Tidewater Labs" not in orgs and "Oldco Systems" not in orgs
    # A company the finders hold gets the signal and keeps its record (rule 6).
    assert orgs["Kestrel Robotics"]["lead_types"] == ["local_business", "funding_news"]
    assert orgs["Kestrel Robotics"]["record"]["website"] == "https://kestrel.example/"
    assert orgs["Ledgerly"]["lead_types"] == ["funding_news"]
    assert orgs["Ledgerly"]["locality"] == "Pune"

    # The workbook shows its columns.
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                             run_id=outcome.run_id)[1]
    store.close()
    sheet = load_workbook(io.BytesIO(content))["Records"]
    header = [c.value for c in sheet[1]]
    for column in ("News Event", "Latest News", "News Date", "Amount Raised", "News Link",
                   "News Events", "News Sources", "Top Signal", "Intent"):
        assert column in header, column
    names = [r[header.index("Name")] for r in sheet.iter_rows(min_row=2, values_only=True)]
    row = names.index("Ledgerly") + 2
    cell = lambda column: sheet.cell(row, header.index(column) + 1)  # noqa: E731
    assert cell("News Event").value == "Funding round"
    assert cell("Amount Raised").value == "₹22 Cr"
    assert cell("News Link").hyperlink.target.startswith("https://startupwire.example/")
    assert cell("News Sources").value == "startupwire.example, citybusiness.example"

    # Seen again: refreshed in place, not duplicated.
    _, again = run(tmp_path, monkeypatch, "gdelt", {**LOCAL, "funding_news": GDELT_ONLY})
    assert again.ok
    assert sum(len(v) for v in signals_by_org(config).values()) == 4


def test_press_releases_google_news_and_your_own_feeds(tmp_path, monkeypatch, news):
    section = {"sources": {"gdelt": {"enabled": False},
                           "google_news": {"enabled": True, "min_delay": 0, "max_delay": 0},
                           "press_releases": {"enabled": True, "min_delay": 0, "max_delay": 0},
                           "feeds": {"urls": [PIA_FEED], "min_delay": 0, "max_delay": 0}}}
    config, outcome = run(tmp_path, monkeypatch, "feeds", {**LOCAL, "funding_news": section})
    assert outcome.ok, outcome.results
    by_org = {name: sorted(s["type"] for s in sigs)
              for name, sigs in signals_by_org(config).items()}
    assert by_org == {"Saffron Analytics": ["expansion"],
                      "Kalpa Health": ["leadership_change"],
                      "Ledgerly": ["funding_round"],
                      "Lotus Logistics": ["expansion", "product_launch"]}
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    # The issuer's site from the release's links (never the wire's own).
    assert orgs["Saffron Analytics"]["record"]["website"] == "https://saffron-analytics.example/"
    assert orgs["Kalpa Health"]["record"]["website"] == "https://kalpa-health.example/news"
    assert "Insight Reports" not in orgs                     # a report from Delhi: no event
    assert orgs["Lotus Logistics"]["lead_types"] == ["local_business", "funding_news"]


def test_a_follow_up_story_never_makes_an_event_look_fresh(tmp_path, monkeypatch, news):
    config, _ = run(tmp_path, monkeypatch, "fresh", {**LOCAL, "funding_news": GDELT_ONLY})
    first = signals_by_org(config)["Ledgerly"][0]
    # A week later, a follow-up about the same round (amount only, no stage).
    news.add(GDELT_API, Page(json_dump({"articles": [{
        "url": "https://citybusiness.example/news/ledgerly-follow-up", "domain": "citybusiness.example",
        "title": "Pune-based Ledgerly raises Rs 22 crore to hire engineers",
        "seendate": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}]}),
        headers={"Content-Type": "application/json"}))
    run(tmp_path, monkeypatch, "fresh", {**LOCAL, "funding_news": GDELT_ONLY})
    after = signals_by_org(config)["Ledgerly"]
    assert len(after) == 1                                       # the same round, refreshed
    assert after[0]["published_at"] == first["published_at"]     # still as old as its first story
    assert after[0]["payload"]["headline"] == first["payload"]["headline"]
    assert len(after[0]["payload"]["coverage"]) == 3


def json_dump(data) -> str:
    import json

    return json.dumps(data)


def test_no_new_companies_when_create_new_is_off(tmp_path, monkeypatch, news):
    config, outcome = run(tmp_path, monkeypatch, "attach", {**LOCAL, "funding_news": {
        **GDELT_ONLY, "create_new": False}})
    assert outcome.ok
    assert set(signals_by_org(config)) == {"Kestrel Robotics"}
    assert outcome.results["funding_news"].stats["skipped_unknown"] == 3


def local_business_output(config) -> tuple[list, list]:
    """local_business's own output: its rows, score columns and its own evidence."""
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


def test_enabling_or_disabling_funding_news_leaves_local_business_unchanged(tmp_path,
                                                                           monkeypatch, news):
    alone, first = run(tmp_path, monkeypatch, "alone", LOCAL)
    view = local_business_view(first)
    together, outcome = run(tmp_path, monkeypatch, "together",
                            {**LOCAL, "funding_news": GDELT_ONLY})
    assert local_business_view(outcome) == view      # Ledgerly & co. are not its leads
    off, last = run(tmp_path, monkeypatch, "off", {**LOCAL, "funding_news": {"enabled": False}})
    assert local_business_view(last) == view
    assert outcome.results["funding_news"].new == 3
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline


def test_a_crashing_source_is_contained(tmp_path, monkeypatch, news):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self, place):
        raise RuntimeError("gdelt exploded")

    monkeypatch.setattr(FundingNewsSession, "read_gdelt", boom)
    section = {"sources": {"feeds": {"urls": [PIA_FEED], "min_delay": 0, "max_delay": 0}}}
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "funding_news": section})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert any(f["reason"] == "unit_failed" for f in project_rows(crashed, FailureRow))
    assert "Lotus Logistics" in signals_by_org(crashed)                # the feed unit ran


# --- scoring, columns, plan --------------------------------------------------------------------

def news_signal(type_: str, days: float, strength_: float, title: str = "", **payload) -> dict:
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return {"type": type_, "lead_type": "funding_news", "title": title, "payload": payload,
            "observed_at": when, "published_at": when, "strength": strength_,
            "source_url": f"https://news.example/{type_}/{days}"}


def test_score_fades_with_age_and_adds_for_more_kinds_of_news():
    now = datetime.now(timezone.utc)
    fresh = FUNDING_NEWS.score(None, [news_signal("funding_round", 0, 0.8, "Raised $5M")], None,
                               now=now)
    assert fresh.intent == 0.8 and fresh.reasons[0].startswith("funding round: Raised $5M (")
    aged = FUNDING_NEWS.score(None, [news_signal("funding_round", 90, 0.8)], None, now=now)
    assert aged.intent == pytest.approx(0.4, abs=0.01)                # one half-life
    stale = FUNDING_NEWS.score(None, [news_signal("funding_round", 400, 0.8)], None, now=now)
    assert stale.intent == 0.0
    both = FUNDING_NEWS.score(None, [news_signal("funding_round", 0, 0.8),
                                     news_signal("expansion", 0, 0.6)], None, now=now)
    assert both.intent == pytest.approx(0.9)
    assert FUNDING_NEWS.score(None, [], None, now=now).intent == 0.0


def test_column_values():
    org = SimpleNamespace(signals=[
        news_signal("expansion", 2, 0.6, "Opened a Pune office",
                    coverage=[{"publisher": "City Business"}]),
        news_signal("funding_round", 5, 0.8, "Raised ₹22 Cr (Pre-Series A)",
                    amount={"text": "₹22 Cr", "value": 2.2e8, "currency": "INR"},
                    coverage=[{"publisher": "Startup Wire"}, {"publisher": "City Business"}])],
        as_of=datetime.now(timezone.utc))
    values = FUNDING_NEWS.column_values(org, None)
    assert values["event"] == "Funding round" and values["amount"] == "₹22 Cr"
    assert values["headline"] == "Raised ₹22 Cr (Pre-Series A)"
    assert values["events"] == "Funding round, Expansion"
    assert values["outlets"] == "Startup Wire, City Business"
    assert FUNDING_NEWS.column_values(SimpleNamespace(signals=[], as_of=None), None) == {}
    assert news_signals([news_signal("job_posting", 1, 0.5)]) == []


def test_plan_sources_and_depth():
    config = compile_run_config(RunConfig.model_validate({
        "name": "n", "project": "n", "geography": {"country": "IN", "places": ["Pune", "Nashik"]},
        "lead_types": {"funding_news": {"sources": {"feeds": {"urls": [PIA_FEED]}}}}}))
    units = FUNDING_NEWS.plan(config)
    assert [(u.source, u.area, u.category) for u in units] == [
        ("gdelt", "Pune", None), ("gdelt", "Nashik", None), ("feeds", None, PIA_FEED)]
    assert FUNDING_NEWS.sources(config) == {"discovery": ["feeds", "gdelt"], "enrichment": []}
    assert registry.get("funding_news") is FUNDING_NEWS
    assert FUNDING_NEWS.apply_depth(config, "balanced") == []
    assert FUNDING_NEWS.apply_depth(config, "deep") == ["funding_news: 250 GDELT articles per "
                                                        "query"]
    # A legacy config without places: one query for the country.
    legacy = SimpleNamespace(get=lambda key, default=None: None)
    assert [(u.source, u.scope) for u in plan_units(legacy, FundingNewsConfig())] == \
        [("gdelt", "run")]
    with pytest.raises(ValueError):
        FundingNewsConfig.model_validate({"sources": {"gdelt": {"min_delay": 1}}})  # 5 s
    names = {s["name"] for s in FUNDING_NEWS.catalog_sources()}
    assert names == {"gdelt", "google_news", "press_releases", "feeds"}


def test_google_news_and_press_releases_are_opt_in():
    from leadscraper.sources.notes import NOTES

    section = FundingNewsConfig()
    assert section.sources.gdelt.enabled and section.sources.feeds.enabled
    assert not section.sources.google_news.enabled
    assert not section.sources.press_releases.enabled
    assert "expressly prohibited" in NOTES["google_news"]["terms"]
    assert NOTES["google_news"]["review"] and NOTES["press_releases"]["review"]


# --- live (opt-in) -------------------------------------------------------------------------

@pytest.mark.live
def test_live_gdelt():
    import time

    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    limiter = RateLimiter()
    limiter.configure(gdelt.HOST, 6.0, 7.0)
    http = HttpClient(limiter, public_only=True)
    query = gdelt.build_query(["raises", "funding round"], place="Pune", country="India",
                              language="english")
    for _ in range(3):
        try:
            items = gdelt.search(http, query, days=30, max_records=25)
            break
        except (gdelt.GdeltError, BlockedError):
            time.sleep(10)
    else:
        pytest.fail("GDELT kept refusing")
    assert all(i.url.startswith("http") and i.title for i in items)
    events = [classify(i.title, places=["Pune", "India"]) for i in items]
    print([(e.kind, e.company) for e in events if e])


@pytest.mark.live
def test_live_globenewswire_country_feed():
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    http = HttpClient(RateLimiter(), public_only=True)
    items = feeds.read(http, GNW_INDIA, source="press_releases",
                       rate_key=feeds.GLOBENEWSWIRE_HOST)
    assert items and all(i.company for i in items)
