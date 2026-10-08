"""decision_makers (Phase 6.4): names and titles, team pages, registries, the email pattern.

All offline: team pages are fictional (``.example`` domains, invented people);
Companies House and GitHub answers keep the shape the APIs gave live on
2026-10-08; MX lookups go to a stub resolver, never to DNS. The ``live`` tests
(``-m live``) read GitHub's public API and two domains' MX records.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Iterator

import pytest
from openpyxl import load_workbook

from fake_web import (FakeWeb, Page, local_business_view, patch_kit, probe_enricher,
                      project_rows, run_database)
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import (Contact, Evidence, FailureRow, Organization, OrgAttribute,
                                   Person)
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes import registry
from leadscraper.leadtypes.decision_makers import email_pattern as ep
from leadscraper.leadtypes.decision_makers import session as dm_session
from leadscraper.leadtypes.decision_makers.config import DecisionMakersConfig
from leadscraper.leadtypes.decision_makers.plugin import DECISION_MAKERS
from leadscraper.leadtypes.decision_makers.session import DecisionMakersSession
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.packs.uk.companies_house import CompaniesHouseDirectors, display_name
from leadscraper.services import exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.base import Source
from leadscraper.sources.google_search import SerpClient, SerpResult
from leadscraper.sources.people import github, linkedin
from leadscraper.sources.people.roles import family_of, name_parts, person_name
from leadscraper.sources.people.team_pages import emails_on, people_on, team_links

# --- fixtures: fictional pages and API answers -------------------------------------------------

KESTREL_HOME = """<!doctype html><html><head><title>Kestrel Robotics</title></head><body>
<nav><a href="/">Home</a> <a href="/team">Meet the Team</a> <a href="/about">About us</a>
<a href="/careers">Careers</a></nav>
<h1>Kestrel Robotics</h1><p>Warehouse automation, made in Pune (fictional).</p>
<footer><a href="mailto:info@kestrel.example">info@kestrel.example</a>
<a href="https://github.com/kestrel-robotics">GitHub</a> <p>© 2026 Kestrel Robotics</p></footer>
</body></html>"""
KESTREL_TEAM = """<!doctype html><html><body>
<nav><a href="/team">Meet the Team</a><span>Director of Sales Enablement</span></nav>
<main><h1>Our Leadership</h1>
<div class="person"><h3>Anika Desai</h3><p class="role">Co-founder &amp; CEO</p>
  <a href="mailto:anika.desai@kestrel.example">Email Anika</a>
  <a href="https://www.linkedin.com/in/anika-desai-example/">LinkedIn</a></div>
<div class="person"><h3>Rohan Iyer</h3><p class="role">Chief Technology Officer</p></div>
<div class="person"><h3>Meera Pillai</h3><p class="role">Head of Marketing</p></div>
<div class="person"><h3>Vikram Sethi</h3><p class="role">Advisor</p></div>
<ul><li>Sanjay Kulkarni, IT Manager</li></ul>
</main>
<footer><h4>Ravi Footer</h4><p>Director</p></footer>
</body></html>"""
KESTREL_ABOUT = """<!doctype html><html><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization",
 "name": "Kestrel Robotics", "founder": [{"@type": "Person", "name": "Anika Desai",
 "jobTitle": "CEO"}]}</script></head><body><main>
<p>Kestrel Robotics was founded in 2019 by Anika Desai and Rohan Iyer.</p></main></body></html>"""
LOTUS_HOME = """<!doctype html><html><body><main>
<div itemscope itemtype="https://schema.org/Person"><span itemprop="name">Sunil Kapoor</span>
<span itemprop="jobTitle">Managing Director</span></div>
<p>Write to <a href="mailto:sales@lotus.example">sales@lotus.example</a>.</p>
</main></body></html>"""
CH_SEARCH = {"items": [
    {"title": "THAMES ANALYTICS LIMITED", "company_number": "09999991",
     "company_status": "active"},
    {"title": "THAMES ANALYTICS (HOLDINGS) LIMITED", "company_number": "09999992",
     "company_status": "dissolved"}]}
CH_OFFICERS = {"items": [
    {"name": "OKAFOR, Grace Adaeze", "officer_role": "director", "occupation": "Director"},
    {"name": "BENNETT, Tom", "officer_role": "director", "resigned_on": "2024-03-31"},
    {"name": "THAMES NOMINEES LIMITED", "officer_role": "corporate-secretary"},
    {"name": "HUGHES, Sarah", "officer_role": "secretary"}]}
CH_API = "https://api.company-information.service.gov.uk"
GH = "https://api.github.com"


def people_web() -> FakeWeb:
    web = FakeWeb()
    web.add("https://kestrel.example/", Page(KESTREL_HOME))
    web.add("https://kestrel.example/team", Page(KESTREL_TEAM))
    web.add("https://kestrel.example/about", Page(KESTREL_ABOUT))
    web.add("https://lotus.example/", Page(LOTUS_HOME))
    web.add(f"{CH_API}/search/companies", Page.of_json(CH_SEARCH))
    web.add(f"{CH_API}/company/09999991/officers", Page.of_json(CH_OFFICERS))
    web.add(f"{GH}/orgs/kestrel-robotics", Page.of_json(
        {"login": "kestrel-robotics", "blog": "https://kestrel.example", "type": "Organization"}))
    web.add(f"{GH}/orgs/kestrel-robotics/public_members",
            Page.of_json([{"login": "rohan-iyer"}, {"login": "kestrel-dev"}]))
    web.add(f"{GH}/users/rohan-iyer", Page.of_json(
        {"login": "rohan-iyer", "name": "Rohan Iyer", "bio": "CTO at Kestrel Robotics",
         "html_url": "https://github.com/rohan-iyer"}))
    web.add(f"{GH}/users/kestrel-dev", Page.of_json(
        {"login": "kestrel-dev", "name": "Dev Bot", "bio": "Robotics engineer",
         "html_url": "https://github.com/kestrel-dev"}))
    return web


def soup(html: str):
    from leadscraper.net.http import HttpClient

    return HttpClient.soup(html)


# --- names and titles ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, name", [
    ("Priya Shah", "Priya Shah"), ("PRIYA SHAH", "Priya Shah"),
    ("Dr. R. K. Menon", "Dr. R. K. Menon"), ("Anand de Souza", "Anand de Souza"),
    ("A. Kumar", "A. Kumar"),
    ("Meet the Team", None), ("Our Leadership", None), ("Read More", None), ("Priya", None),
    ("R. K.", None), ("Ravi Kumar Pvt Ltd", None), ("Sales Head", None),
    ("Call 98200 12345", None), ("priya shah", None), ("info@kestrel.example", None),
    ("AI Assistant", None), ("Lena Okoro CIB", None), ("Kiran Patil Co-founder", None),
    ("Deep Application Development Expertise", None),
])
def test_what_a_persons_name_looks_like(text, name):
    assert person_name(text) == name


@pytest.mark.parametrize("title, family", [
    ("Founder & CEO", "owner"), ("Co-Founder", "owner"), ("Partner", "owner"),
    ("LLP designated member", "owner"), ("Chief Technology Officer", "technology"),
    ("VP, Engineering", "technology"), ("Head of IT", "technology"), ("Head - IT", "technology"),
    ("IT Manager", "it"), ("Managing Director", "executive"), ("Director", "executive"),
    ("CFO", "executive"),
    ("Independent Director", None), ("Advisor", None), ("Marketing Manager", None),
    ("Senior Developer", None), ("Intern", None), ("Head of Marketing", None),
    ("Principal", "owner"), ("Principal Software Architect", None),
    ("Extensive Partner Ecosystem", None),
    ("Explore our services & industries Our AI offerings Our partner ecosystem", None),
])
def test_which_titles_are_decision_makers(title, family):
    assert family_of(title) == family


def test_name_parts():
    assert name_parts("Dr. Priya Shah") == ("priya", "shah")
    assert name_parts("Anand de Souza") == ("anand", "souza")
    assert name_parts("José Álvarez") == ("jose", "alvarez")
    assert name_parts("Priya") is None


# --- team pages ----------------------------------------------------------------------------------

def test_team_links_put_team_pages_before_about_pages():
    assert team_links(soup(KESTREL_HOME), "https://kestrel.example/") == [
        "https://kestrel.example/team", "https://kestrel.example/about"]


def test_people_on_team_pages():
    found = {p.name: p for p in people_on(soup(KESTREL_TEAM), "https://kestrel.example/team")}
    assert set(found) == {"Anika Desai", "Rohan Iyer", "Sanjay Kulkarni"}  # no marketing, advisor
    anika = found["Anika Desai"]
    assert (anika.title, anika.how) == ("Co-founder & CEO", "card")
    assert anika.email == "anika.desai@kestrel.example"                     # her own card's
    assert anika.profile_url == "https://www.linkedin.com/in/anika-desai-example"
    assert found["Rohan Iyer"].email is None                                # not someone else's
    assert found["Sanjay Kulkarni"].title == "IT Manager"
    about = people_on(soup(KESTREL_ABOUT), "https://kestrel.example/about")
    assert [(p.name, p.title, p.how) for p in about] == [
        ("Anika Desai", "CEO", "jsonld"), ("Rohan Iyer", "Founder", "prose")]
    lotus = people_on(soup(LOTUS_HOME), "https://lotus.example/")
    assert [(p.name, p.title, p.how) for p in lotus] == [
        ("Sunil Kapoor", "Managing Director", "microdata")]
    assert emails_on(soup(KESTREL_HOME)) == ["info@kestrel.example"]
    assert emails_on(soup(KESTREL_TEAM)) == ["anika.desai@kestrel.example"]


# Live 2026-10-08, rewritten as fiction: a bigger company's site whose homepage carries
# customer testimonials, a blog author and marketing copy, and a services page whose URL
# merely contains "management".
BIG_HOME = """<!doctype html><html><body>
<nav><a href="/company-overview/">Company</a>
<a href="/services/application-development-and-management/">App development</a></nav>
<main>
<section><h3>AI Assistant</h3><p>Explore our services &amp; industries Our AI offerings Our partner
ecosystem</p></section>
<div class="quote"><h4>Owen Marsh</h4><p>CIO, Fernwood Retail</p></div>
<div class="quote"><h4>Lena Okoro CIB</h4><p>Head of Technology Payments, Coastline Bank</p></div>
<div class="blog"><h4>Ravi Gokhale</h4><p>Principal Software Architect, Sahyadri Systems</p></div>
</main></body></html>"""
BIG_OVERVIEW = """<!doctype html><html><body><main>
<div><h3>Dr. Anil Joshi</h3><p>Founder, Chairman and Managing Director</p></div>
<div><h3>Seema Kale</h3><p>Executive Director &amp; Chief Executive Officer</p></div>
<div><h3>Kiran Patil Co-founder</h3><p>CEO and Managing Director</p></div>
<div><h3>Tom Becker</h3><p>MD &amp; Chief Technology Officer, Rheinwerk Engineering</p></div>
</main></body></html>"""


def test_a_bigger_sites_testimonials_and_copy_are_nobody(tmp_path):
    from fake_web import kit_for

    web = FakeWeb({"https://sahyadri.example/": Page(BIG_HOME),
                   "https://sahyadri.example/company-overview/": Page(BIG_OVERVIEW)})
    assert team_links(soup(BIG_HOME), "https://sahyadri.example/") == [
        "https://sahyadri.example/company-overview/"]        # not the services page
    config = compile_run_config(RunConfig.model_validate({
        "name": "n", "project": "n", "geography": {"country": "IN", "places": ["Pune"]},
        "lead_types": {"decision_makers": FAST}}))
    dm = DecisionMakersSession(SimpleNamespace(config=config, store=None, run_id=None),
                               DECISION_MAKERS.effective(config), kit=kit_for(web, tmp_path))
    lead = Lead(business_name="Sahyadri Systems", website="https://sahyadri.example/")
    status, found, _, _ = dm.read_site("https://sahyadri.example/", lead)
    kept = dm.keep(found, lead)
    assert status == "complete"
    assert [(p.name, p.title) for p in kept] == [
        ("Dr. Anil Joshi", "Founder, Chairman and Managing Director"),
        ("Seema Kale", "Executive Director & Chief Executive Officer")]
    # The homepage counts only for a site with no team or about page.
    small = FakeWeb({"https://kestrel.example/": Page(
        "<main><div><h3>Anika Desai</h3><p>Founder</p></div></main>")})
    dm.kit = kit_for(small, tmp_path)
    _, found, _, _ = dm.read_site("https://kestrel.example/", Lead(business_name="Kestrel"))
    assert [p.name for p in found] == ["Anika Desai"]


# --- the email pattern ------------------------------------------------------------------------

def test_the_pattern_needs_a_published_personal_address_for_a_named_person():
    people = ["Anika Desai", "Rohan Iyer"]
    learned = ep.learn(["info@kestrel.example", "anika.desai@kestrel.example"], people,
                       "kestrel.example")
    assert learned.pattern.pattern == "first.last"
    assert learned.pattern.apply("Rohan Iyer") == "rohan.iyer@kestrel.example"
    assert learned.pattern.confidence == 0.6
    two = ep.learn(["anika.desai@kestrel.example", "rohan.iyer@kestrel.example"], people,
                   "kestrel.example")
    assert two.pattern.confidence == 0.75
    assert ep.learn(["info@kestrel.example", "sales@kestrel.example"], people,
                    "kestrel.example").reason == "no_personal_address"
    assert ep.learn(["anika.desai@kestrel.example", "riyer@kestrel.example"], people,
                    "kestrel.example").reason == "conflicting_patterns"
    assert ep.learn(["anika.desai@gmail.com"], people, "gmail.com").reason == "shared_domain"
    assert ep.learn(["anika.desai@other.example"], people, "kestrel.example").pattern is None
    # An address naming nobody we know reveals nothing.
    assert ep.learn(["jdoe@kestrel.example"], people, "kestrel.example").pattern is None


def test_mx_is_the_only_check():
    import inspect

    import dns.resolver

    def resolver(answers):
        def resolve(name):
            value = answers[name]
            if isinstance(value, Exception):
                raise value
            return [SimpleNamespace(exchange=host) for host in value]
        return resolve

    resolve = resolver({"kestrel.example": ["mx1.kestrel.example.", "mx2.kestrel.example."],
                        "nullmx.example": ["."], "gone.example": dns.resolver.NXDOMAIN(),
                        "slow.example": dns.resolver.LifetimeTimeout()})
    assert ep.mx_hosts("kestrel.example", resolve=resolve) == ["mx1.kestrel.example",
                                                               "mx2.kestrel.example"]
    assert ep.mx_hosts("nullmx.example", resolve=resolve) == []        # RFC 7505: no mail
    assert ep.mx_hosts("gone.example", resolve=resolve) == []
    assert ep.mx_hosts("slow.example", resolve=resolve) is None         # try another day
    source = inspect.getsource(ep) + inspect.getsource(dm_session)
    assert "smtplib" not in source and "RCPT" not in source              # no SMTP probing


# --- registries, GitHub, LinkedIn search -----------------------------------------------------

def test_companies_house_directors():
    assert display_name("OKAFOR, Grace Adaeze") == "Grace Adaeze Okafor"
    web = people_web()
    found, why = CompaniesHouseDirectors().find(web, "Thames Analytics Ltd", key="k-123")
    assert why is None
    assert [(p.name, p.title, p.how) for p in found] == [("Grace Adaeze Okafor", "Director",
                                                          "registry")]
    assert found[0].page.endswith("/company/09999991/officers")
    web.add(f"{CH_API}/search/companies", Page.of_json({"items": []}))
    assert CompaniesHouseDirectors().find(web, "Nobody Ltd", key="k")[1] == "registry_no_match"
    headers = CompaniesHouseDirectors._headers("k-123")
    assert headers["Authorization"] == "Basic ay0xMjM6"                  # "k-123:", the key only


def test_github_members_who_say_they_lead_the_company():
    web = people_web()
    found, why = github.find(web, "kestrel-robotics", domain="kestrel.example")
    assert why is None and [(p.name, p.title) for p in found] == [("Rohan Iyer", "CTO")]
    assert found[0].profile_url == "https://github.com/rohan-iyer"
    assert github.find(web, "kestrel-robotics", domain="other.example")[1] == "github_not_theirs"
    assert github.org_logins(["https://github.com/kestrel-robotics/robot-arm",
                              "https://github.com/features", "https://gitlab.com/x"]) == \
        ["kestrel-robotics"]


def test_linkedin_search_keeps_only_this_companys_decision_makers(monkeypatch):
    results = [SerpResult("Anika Desai - CEO - Kestrel Robotics | LinkedIn",
                          "https://in.linkedin.com/in/anika-desai-example"),
               SerpResult("Rohan Iyer - CTO at Kestrel Robotics | LinkedIn",
                          "https://www.linkedin.com/in/rohan-iyer-example"),
               SerpResult("Neha Joshi - CEO - Kestrel Foods | LinkedIn",
                          "https://www.linkedin.com/in/neha-joshi-example"),
               SerpResult("Amit Rao - Sales Executive - Kestrel Robotics | LinkedIn",
                          "https://www.linkedin.com/in/amit-rao-example"),
               SerpResult("Kestrel Robotics | LinkedIn",
                          "https://www.linkedin.com/company/kestrel-robotics")]
    queries = []
    monkeypatch.setattr(SerpClient, "search",
                        lambda self, query, **kw: queries.append(query) or results)
    ctx = SimpleNamespace(config=None, http=None, limiter=None, failures=None)
    found = linkedin.find(ctx, "Kestrel Robotics", place="Pune")
    assert [(p.name, p.title, p.profile_url) for p in found] == [
        ("Anika Desai", "CEO", "https://www.linkedin.com/in/anika-desai-example"),
        ("Rohan Iyer", "CTO", "https://www.linkedin.com/in/rohan-iyer-example")]
    assert queries[0].startswith('site:linkedin.com/in "Kestrel Robotics" (founder OR')


# --- runs ------------------------------------------------------------------------------------

class PeopleSource(Source):
    """local_business's discovery, offline: two firms with sites, one without."""

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


class UKSource(Source):
    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Thames Analytics Ltd", locality=locality.name,
                             category=category.name, phones=["+442079460000"])


LOCAL = {"local_business": {"categories": [{"name": "Companies"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0},
                                        "probe": {"enabled": True, "min_delay": 0,
                                                  "max_delay": 0}}}}
FAST = {"sources": {"team_pages": {"min_delay": 0, "max_delay": 0}}}
MX = {"kestrel.example": ["mx1.kestrel.example."], "lotus.example": ["mx.lotus.example."]}


def stub_mx(name: str):
    return [SimpleNamespace(exchange=host) for host in MX.get(name, ["."])]


@pytest.fixture
def people(monkeypatch):
    web = people_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": PeopleSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"probe": probe_enricher()})
    original = DecisionMakersSession.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("resolve_mx", stub_mx)
        original(self, *args, **kwargs)
    monkeypatch.setattr(DecisionMakersSession, "__init__", init)
    return web


def run(tmp_path, monkeypatch, name: str, lead_types: dict, *, country="IN", places=("Pune",),
        fields=None):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    document = {"name": "People test", "project": f"people-test-{tmp_path.name}-{name}",
                "geography": {"country": country, "places": list(places)},
                "lead_types": lead_types, "runtime": {"db_url": run_database(tmp_path, name)}}
    if fields is not None:
        document["fields"] = {"include": fields}
    config = compile_run_config(RunConfig.model_validate(document))
    return config, execute(RunRequest(config=config, install_signals=False))


def people_by_org(config) -> dict[str, list[dict]]:
    names = {o["id"]: o["name"] for o in project_rows(config, Organization)}
    out: dict[str, list[dict]] = {}
    for p in sorted(project_rows(config, Person), key=lambda p: p["id"]):
        if p["lead_type"] == "decision_makers":
            out.setdefault(names[p["organization_id"]], []).append(p)
    return out


def workbook(config, run_id: int):
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                                 run_id=run_id)[1]
    finally:
        store.close()
    sheet = load_workbook(io.BytesIO(content))["Records"]
    header = [c.value for c in sheet[1]]
    names = [r[header.index("Name")] for r in sheet.iter_rows(min_row=2, values_only=True)]

    def cell(org: str, column: str):
        return sheet.cell(names.index(org) + 2, header.index(column) + 1)
    return header, cell


def test_a_run_finds_decision_makers_on_team_pages(tmp_path, monkeypatch, people):
    config, outcome = run(tmp_path, monkeypatch, "team", {**LOCAL, "decision_makers": FAST})
    assert outcome.ok, outcome.results
    by_org = {name: [(p["name"], p["title"], p["source"]) for p in rows]
              for name, rows in people_by_org(config).items()}
    assert by_org == {
        "Kestrel Robotics": [("Anika Desai", "Co-founder & CEO", "team_pages"),
                             ("Rohan Iyer", "Chief Technology Officer", "team_pages"),
                             ("Sanjay Kulkarni", "IT Manager", "team_pages")],
        "Lotus Logistics": [("Sunil Kapoor", "Managing Director", "team_pages")],
    }
    anika = people_by_org(config)["Kestrel Robotics"][0]
    assert anika["email"] == "anika.desai@kestrel.example"
    assert anika["source_url"] == "https://kestrel.example/team"
    assert anika["profile_url"] == "https://www.linkedin.com/in/anika-desai-example"
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    # The record is the finder's: tagged, never rewritten (rule 6).
    assert orgs["Kestrel Robotics"]["lead_types"] == ["local_business", "decision_makers"]
    assert not orgs["Kestrel Robotics"]["record"].get("contact_name")
    assert orgs["Quiet Traders"]["lead_types"] == ["local_business"]       # no site, no registry
    # Off by default: no email pattern.
    assert not [c for c in project_rows(config, Contact) if c["kind"] == "inferred"]

    header, cell = workbook(config, outcome.run_id)
    for column in ("Decision Makers", "Decision Maker Profile", "Decision Maker Email",
                   "DM Roles", "People Found", "People Sources"):
        assert column in header, column
    assert "Inferred Email (unverified)" not in header and "Email Pattern" not in header
    assert cell("Kestrel Robotics", "Decision Makers").value == (
        "Anika Desai (Co-founder & CEO); Rohan Iyer (Chief Technology Officer); "
        "Sanjay Kulkarni (IT Manager)")
    assert cell("Kestrel Robotics", "Decision Maker Email").value == \
        "anika.desai@kestrel.example"
    assert cell("Kestrel Robotics", "People Found").value == 3
    assert cell("Kestrel Robotics", "People Sources").value == "Team page"

    # Looked into recently: skipped next run, unless forced.
    people.calls.clear()
    _, again = run(tmp_path, monkeypatch, "team", {**LOCAL, "decision_makers": FAST})
    assert again.ok and not any("kestrel.example/team" in url for url in people.calls)
    assert again.results["decision_makers"].stats["recent_skipped"] == 3


def test_a_team_page_is_a_snapshot(tmp_path, monkeypatch, people):
    section = {**FAST, "refresh_after_days": 0}
    config, _ = run(tmp_path, monkeypatch, "snap", {**LOCAL, "decision_makers": section})
    assert len(people_by_org(config)["Kestrel Robotics"]) == 3
    # Sanjay leaves; the next full read removes him -- and nobody else.
    people.add("https://kestrel.example/team",
               Page(KESTREL_TEAM.replace("<ul><li>Sanjay Kulkarni, IT Manager</li></ul>", "")))
    run(tmp_path, monkeypatch, "snap", {**LOCAL, "decision_makers": section})
    assert [p["name"] for p in people_by_org(config)["Kestrel Robotics"]] == ["Anika Desai",
                                                                              "Rohan Iyer"]
    # A page that doesn't answer is not a reason to forget anyone.
    people.add("https://kestrel.example/team", Page(status=503))
    run(tmp_path, monkeypatch, "snap", {**LOCAL, "decision_makers": section})
    assert [p["name"] for p in people_by_org(config)["Kestrel Robotics"]] == ["Anika Desai",
                                                                              "Rohan Iyer"]


def test_the_email_pattern_is_labelled_and_never_published(tmp_path, monkeypatch, people):
    section = {**FAST, "email_pattern": {"enabled": True}}
    config, outcome = run(tmp_path, monkeypatch, "pattern", {**LOCAL, "decision_makers": section})
    assert outcome.ok
    inferred = sorted((c["value"], c["person"], c["confidence"], c["source"])
                      for c in project_rows(config, Contact) if c["kind"] == "inferred")
    assert inferred == [("rohan.iyer@kestrel.example", "Rohan Iyer", 0.6, "email_pattern"),
                        ("sanjay.kulkarni@kestrel.example", "Sanjay Kulkarni", 0.6,
                         "email_pattern")]                         # Anika's own is published
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    assert not orgs["Kestrel Robotics"]["record"].get("emails")       # never on the record
    attrs = {(a["organization_id"], a["key"]): a for a in project_rows(config, OrgAttribute)}
    kestrel = attrs[(orgs["Kestrel Robotics"]["id"], "email_pattern")]["value"]
    assert (kestrel["pattern"], kestrel["mx"]) == ("first.last", ["mx1.kestrel.example"])
    lotus = attrs[(orgs["Lotus Logistics"]["id"], "email_pattern")]["value"]
    assert (lotus["pattern"], lotus["reason"]) == (None, "no_personal_address")   # sales@ only
    # Never an identity key: a lead with the inferred address matches nothing.
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        assert store.find_match(Lead(business_name="Somebody Else",
                                     emails=["rohan.iyer@kestrel.example"]).normalized()) is None
    finally:
        store.close()
    header, cell = workbook(config, outcome.run_id)
    assert cell("Kestrel Robotics", "Inferred Email (unverified)").value == (
        "rohan.iyer@kestrel.example | sanjay.kulkarni@kestrel.example")
    assert cell("Kestrel Robotics", "Primary Email").value in (None, "")
    assert cell("Kestrel Robotics", "Email Pattern").value == \
        "first.last@kestrel.example (from 1 published address)"


def test_a_null_mx_means_no_inferred_addresses(tmp_path, monkeypatch, people):
    monkeypatch.setitem(MX, "kestrel.example", ["."])
    config, _ = run(tmp_path, monkeypatch, "nullmx", {**LOCAL, "decision_makers": {
        **FAST, "email_pattern": {"enabled": True}}})
    assert not [c for c in project_rows(config, Contact) if c["kind"] == "inferred"]


def test_fields_include_without_decision_makers_does_nothing(tmp_path, monkeypatch, people):
    config, outcome = run(tmp_path, monkeypatch, "fields", {**LOCAL, "decision_makers": FAST},
                          fields=["phones", "website"])
    assert outcome.ok and people_by_org(config) == {}
    assert not any("kestrel.example/team" in url for url in people.calls)


def test_registry_directors_in_the_uk_pack(tmp_path, monkeypatch, people):
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": UKSource})
    section = {"sources": {"team_pages": {"enabled": False}}}
    config, outcome = run(tmp_path, monkeypatch, "nokey", {**LOCAL, "decision_makers": section},
                          country="GB", places=("London",))
    assert outcome.ok and people_by_org(config) == {}
    assert any(f["reason"] == "key_missing" for f in project_rows(config, FailureRow))
    monkeypatch.setenv("LEADSCRAPER_SECRET_COMPANIES_HOUSE_API_KEY", "k-123")
    config, outcome = run(tmp_path, monkeypatch, "key", {**LOCAL, "decision_makers": section},
                          country="GB", places=("London",))
    assert outcome.ok
    assert [(p["name"], p["title"], p["source"]) for p in
            people_by_org(config)["Thames Analytics Ltd"]] == [
        ("Grace Adaeze Okafor", "Director", "registry")]
    # India has no registry pack: nothing asked of Companies House.
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": PeopleSource})
    people.calls.clear()
    run(tmp_path, monkeypatch, "india", {**LOCAL, "decision_makers": section})
    assert not any("company-information" in url for url in people.calls)


def test_github_is_opt_in_and_never_feeds_the_pattern(tmp_path, monkeypatch, people):
    # No team cards; the about page names Anika (with her address) and Sanjay; GitHub, Rohan.
    people.add("https://kestrel.example/team", Page("<html><body>No team page yet</body></html>"))
    people.add("https://kestrel.example/about", Page(
        '<html><head><script type="application/ld+json">{"@type": "Organization", '
        '"employee": [{"@type": "Person", "name": "Anika Desai", "jobTitle": "CEO"}, '
        '{"@type": "Person", "name": "Sanjay Kulkarni", "jobTitle": "IT Manager"}]}</script>'
        '</head><body><a href="mailto:anika.desai@kestrel.example">Write to Anika</a>'
        '</body></html>'))
    section = {"sources": {"team_pages": {"min_delay": 0, "max_delay": 0},
                           "github": {"enabled": True, "min_delay": 0, "max_delay": 0}},
               "email_pattern": {"enabled": True}}
    config, _ = run(tmp_path, monkeypatch, "github", {**LOCAL, "decision_makers": section})
    kestrel = people_by_org(config)["Kestrel Robotics"]
    assert [(p["name"], p["source"]) for p in kestrel] == [
        ("Anika Desai", "team_pages"), ("Rohan Iyer", "github"),
        ("Sanjay Kulkarni", "team_pages")]
    inferred = [c["value"] for c in project_rows(config, Contact) if c["kind"] == "inferred"]
    assert inferred == ["sanjay.kulkarni@kestrel.example"]          # never Rohan (GitHub's AUP)
    off, _ = run(tmp_path, monkeypatch, "nogithub", {**LOCAL, "decision_makers": FAST})
    assert "Rohan Iyer" not in [p["name"] for p in people_by_org(off)["Kestrel Robotics"]]


def test_published_later_an_inferred_address_becomes_published(tmp_path):
    store = LeadStore(f"sqlite:///{tmp_path / 'store.sqlite3'}", project="p")
    contacts = Contact.__table__
    try:
        org, _, _ = store.upsert(Lead(business_name="Kestrel Robotics",
                                      website="https://kestrel.example/", phones=["+912000000011"]))
        store.lead_type = "decision_makers"
        person = store.add_person(org, "Rohan Iyer", title="CTO", source="team_pages")
        assert store.add_person(org, "rohan iyer", title="", profile_url="https://x.example/r") \
            == person                                                   # the same person
        assert store.people(org, "decision_makers")[0]["title"] == "CTO"  # a blank never wins
        assert store.set_inferred_contacts(org, "email_pattern",
                                           [("email", "rohan.iyer@kestrel.example", "Rohan Iyer",
                                             0.6)]) == 1
        # local_business rewrites its record: the inferred contact survives...
        store.lead_type = "local_business"
        store.merge_into(org, Lead(business_name="Kestrel Robotics", category="Robotics"))
        kinds = {(c.value, c.kind) for c in store._conn.execute(
            contacts.select().where(contacts.c.organization_id == org))}
        assert ("rohan.iyer@kestrel.example", "inferred") in kinds
        # ...until the address is published, which makes it a published contact.
        store.merge_into(org, Lead(business_name="Kestrel Robotics",
                                   emails=["rohan.iyer@kestrel.example"]))
        rows = list(store._conn.execute(contacts.select().where(
            contacts.c.value == "rohan.iyer@kestrel.example")))
        assert [(r.kind, r.person) for r in rows] == [("published", "")]
        store.lead_type = "decision_makers"
        assert store.set_inferred_contacts(org, "email_pattern",
                                           [("email", "rohan.iyer@kestrel.example", "Rohan Iyer",
                                             0.6)]) == 0                 # never inferred again
        assert store.prune_people(org, "team_pages", []) == 1
        assert store.people(org, "decision_makers") == []
    finally:
        store.close()


def local_business_output(config) -> tuple[list, list]:
    """local_business's own output: its rows, score columns, evidence and published contacts."""
    orgs = sorted((o for o in project_rows(config, Organization)
                   if "local_business" in o["lead_types"]), key=lambda o: o["name"])
    out = []
    for org in orgs:
        row = Lead.from_dict(org["record"]).to_row()
        for column in ("First Seen", "Last Updated"):
            row.pop(column)
        out.append((row, org["score"], org["fit"], org["reach"], org["score_reasons"]))
    ids = {o["id"]: o["name"] for o in orgs}
    evidence = sorted((e["source"], tuple(e["fields"])) for e in project_rows(config, Evidence)
                      if e["organization_id"] in ids and e["lead_type"] == "local_business")
    published = sorted((ids[c["organization_id"]], c["channel"], c["value"])
                       for c in project_rows(config, Contact)
                       if c["kind"] == "published" and c["organization_id"] in ids)
    return out, evidence, published


def test_enabling_or_disabling_decision_makers_leaves_local_business_unchanged(tmp_path,
                                                                              monkeypatch,
                                                                              people):
    alone, first = run(tmp_path, monkeypatch, "alone", LOCAL)
    view = local_business_view(first)
    section = {**FAST, "email_pattern": {"enabled": True}}
    together, outcome = run(tmp_path, monkeypatch, "together",
                            {**LOCAL, "decision_makers": section})
    assert local_business_view(outcome) == view
    off, last = run(tmp_path, monkeypatch, "off", {**LOCAL, "decision_makers": {"enabled": False}})
    assert local_business_view(last) == view
    assert outcome.results["decision_makers"].stats["people"] == 4
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline


def test_a_crash_on_one_organisation_is_contained(tmp_path, monkeypatch, people):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)
    original = DecisionMakersSession.read_site

    def flaky(self, site, lead):
        if "kestrel" in site:
            raise RuntimeError("team page exploded")
        return original(self, site, lead)

    monkeypatch.setattr(DecisionMakersSession, "read_site", flaky)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "decision_makers": FAST})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert set(people_by_org(crashed)) == {"Lotus Logistics"}             # the other one ran
    assert any(f["reason"] == "enrich_failed" for f in project_rows(crashed, FailureRow))


# --- scoring, columns, plan -------------------------------------------------------------------

def test_score_adds_reach_for_a_named_decision_maker():
    from leadscraper.core.scoring import contact_reach

    lead = Lead(business_name="Kestrel Robotics", phones=["+912000000011"])
    named = SimpleNamespace(lead=lead, people=[
        {"name": "Anika Desai", "title": "Co-founder & CEO", "lead_type": "decision_makers",
         "profile_url": "https://www.linkedin.com/in/anika-desai-example"},
        {"name": "Rohan Iyer", "title": "CTO", "lead_type": "decision_makers"}])
    parts = DECISION_MAKERS.score(lead, [], None, record=named)
    assert parts.reach == pytest.approx(contact_reach(lead) + 0.2)
    assert parts.reasons == ["decision-maker named: Anika Desai (Co-founder & CEO) and 1 more"]
    nobody = DECISION_MAKERS.score(lead, [], None, record=SimpleNamespace(lead=lead, people=[]))
    assert nobody.reach is None and nobody.intent is None and nobody.fit is None


def test_an_export_scores_reach_with_the_record():
    from leadscraper.export.records import ExportRecord, score_records

    lead = Lead(business_name="Kestrel Robotics", phones=["+912000000011"])
    record = ExportRecord(id=1, lead=lead, lead_types=["local_business", "decision_makers"],
                          people=[{"name": "Anika Desai", "title": "CEO",
                                   "lead_type": "decision_makers"}])
    plain = ExportRecord(id=2, lead=Lead(business_name="Lotus", phones=["+912000000012"]),
                         lead_types=["local_business"])
    score_records([record, plain], None, {"fit": 0.0, "intent": 0.0, "reach": 1.0})
    assert record.overall.reach > plain.overall.reach


def test_column_values_and_plan():
    org = SimpleNamespace(people=[
        {"name": "Anika Desai", "title": "Co-founder & CEO", "lead_type": "decision_makers",
         "source": "team_pages"},
        {"name": "Grace Okafor", "title": "Director", "lead_type": "decision_makers",
         "source": "registry"},
        {"name": "Old Contact", "title": "", "lead_type": "local_business"}],
        attributes={"email_pattern": {"value": {"pattern": "first.last", "domain": "k.example",
                                                "examples": ["a@k.example", "b@k.example"],
                                                "mx": ["mx.k.example"]}}})
    assert DECISION_MAKERS.column_values(org, None) == {
        "roles": "Co-founder & CEO, Director", "count": 2,
        "sources": "Team page, Company registry",
        "email_pattern": "first.last@k.example (from 2 published addresses)"}
    assert DECISION_MAKERS.column_values(SimpleNamespace(people=[], attributes={}), None) == {}
    config = compile_run_config(RunConfig.model_validate({
        "name": "n", "project": "n", "geography": {"country": "IN", "places": ["Pune"]},
        "lead_types": {"decision_makers": {}}}))
    assert DECISION_MAKERS.plan(config) == []
    assert DECISION_MAKERS.sources(config) == {"discovery": [],
                                               "enrichment": ["team_pages", "registry"]}
    assert DECISION_MAKERS.apply_depth(config, "quick") == [
        "decision_makers: one team page each, at most 50 organisations"]
    assert registry.get("decision_makers") is DECISION_MAKERS
    order = [lt.key for lt in sorted(registry.all_lead_types(), key=lambda lt: lt.post_order)]
    assert order.index("hiring") < order.index("decision_makers") < order.index("digital_audit")
    section = DecisionMakersConfig()
    assert not section.email_pattern.enabled and not section.sources.linkedin_search.enabled
    assert not section.sources.github.enabled
    with pytest.raises(ValueError):
        DecisionMakersConfig.model_validate({"sources": {"registry": {
            "key_secret_name": "anthropic-api-key"}}})               # a key goes only home
    with pytest.raises(ValueError):
        DecisionMakersConfig.model_validate({"sources": {"github": {
            "token_secret_name": "pagespeed-api-key"}}})


# --- live (opt-in) -------------------------------------------------------------------------

@pytest.mark.live
def test_live_github_public_members():
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    http = HttpClient(RateLimiter(), public_only=True)
    found, why = github.find(http, "github", domain="github.com", max_members=3)
    assert why is None and isinstance(found, list)


@pytest.mark.live
def test_live_mx_records():
    assert ep.mx_hosts("example.com") == []                  # a null MX (RFC 7505)
    assert ep.mx_hosts("github.com")                         # it takes mail
