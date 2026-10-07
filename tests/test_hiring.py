"""hiring (Phase 6.2): role rules, ATS boards, HN, careers pages, runs, isolation.

All offline: every job source is a fictional fixture in the shape its API
answered live on 2026-10-07 (``tests/fixtures/jobs``), served by ``FakeWeb``.
The ``live`` tests (``-m live``) read one real public board per ATS and the
current HN thread.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from fake_web import project_rows, run_database, FakeWeb, Page, fixture, json_fixture, kit_for, patch_kit
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Evidence, FailureRow, Organization, Signal
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes import registry
from leadscraper.leadtypes.hiring import roles
from leadscraper.leadtypes.hiring.config import HiringConfig
from leadscraper.leadtypes.hiring.plugin import HIRING, open_postings
from leadscraper.leadtypes.hiring.session import HiringSession, strength
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.services import exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.base import Source
from leadscraper.sources.jobs import hn
from leadscraper.sources.jobs.ats import fetch_board
from leadscraper.sources.jobs.base import BoardRef, Posting, find_boards

BOARDS = ["greenhouse:kestrelrobotics", "lever:marigold-labs", "ashby:tamarind",
          "workable:saffron-analytics", "smartrecruiters:NilgiriTextiles1"]
COUNTRY_IN = ["india"]


def job_web() -> FakeWeb:
    web = FakeWeb()
    web.add("https://boards-api.greenhouse.io/v1/boards/kestrelrobotics/jobs",
            Page.of_json(json_fixture("jobs/greenhouse_jobs.json")))
    web.add("https://api.lever.co/v0/postings/marigold-labs",
            Page.of_json(json_fixture("jobs/lever_postings.json")))
    web.add("https://api.ashbyhq.com/posting-api/job-board/tamarind",
            Page.of_json(json_fixture("jobs/ashby_board.json")))
    web.add("https://apply.workable.com/api/v1/widget/accounts/saffron-analytics",
            Page.of_json(json_fixture("jobs/workable_account.json")))
    web.add("https://api.smartrecruiters.com/v1/companies/NilgiriTextiles1/postings",
            Page.of_json(json_fixture("jobs/smartrecruiters_postings.json")))
    web.add("https://hn.algolia.com/api/v1/search_by_date",
            Page.of_json(json_fixture("jobs/hn_threads.json")))
    web.add("https://hn.algolia.com/api/v1/items/41000001",
            Page.of_json(json_fixture("jobs/hn_item.json")))
    return web


# --- role rules --------------------------------------------------------------------------

@pytest.mark.parametrize("title, family", [
    ("Senior PHP Developer", "developer"), ("Backend Engineer (Python)", "developer"),
    ("SDE II", "developer"), ("QA Automation Engineer", "qa"),
    ("Site Reliability Engineer", "devops"), ("Data Analyst", "data"),
    ("IT Support Executive", "it_support"), ("SEO Specialist", "digital_marketing"),
    ("Sales Engineer", None), ("Account Executive", None),
    ("Mechanical Design Engineer", None), ("Delivery Driver", None),
])
def test_job_titles_map_to_families(title, family):
    assert roles.classify(title, roles.FAMILY_PATTERNS) == family


def test_keywords_seniority_and_free_text():
    assert roles.classify("Odoo Functional Consultant", ["developer"], ["odoo"]) == "custom"
    assert roles.classify("Odoo Functional Consultant", ["developer"]) is None
    assert [roles.seniority_of(t) for t in ("Software Engineering Intern", "Junior Developer",
                                            "Senior Backend Engineer", "Lead QA Engineer",
                                            "Engineering Manager", "Developer")] == \
        ["intern", "junior", "senior", "lead", "manager", "mid"]
    found = roles.families_in("Acme | Senior Backend Engineer, DevOps Engineer | ONSITE",
                              ["developer", "devops"])
    assert ("developer", "Backend Engineer") in found and ("devops", "DevOps") in found


@pytest.mark.parametrize("location, remote, policy, expected", [
    ("Pune, India", None, "include", "place"),
    ("Remote - India", None, "include", "remote"),
    ("Remote", None, "include", "remote"),
    ("Remote (US)", None, "include", None),
    ("London, UK or Remote", None, "include", None),
    ("Remote (Worldwide)", None, "include", "remote"),
    ("Berlin, Germany", None, "include", None),
    ("Mumbai", True, "include", "remote"),
    ("Pune, India", None, "only", None),
    ("Remote - India", None, "exclude", None),
    ("Pune or Remote", None, "exclude", "place"),
])
def test_location_matching(location, remote, policy, expected):
    assert roles.location_match(location, remote=remote, places=["Pune"],
                                country_terms=COUNTRY_IN, policy=policy) == expected


def test_boards_are_found_in_links_and_embeds():
    refs = find_boards([
        "https://boards.greenhouse.io/embed/job_board/js?for=kestrelrobotics",
        "https://job-boards.greenhouse.io/acme/jobs/123", "https://jobs.lever.co/marigold-labs/aa",
        "https://jobs.ashbyhq.com/tamarind", "https://apply.workable.com/saffron-analytics/j/X1",
        "https://apply.workable.com/j/ABC123", "https://lotus.workable.com/",
        "https://careers.smartrecruiters.com/NilgiriTextiles1", "https://jobs.lever.co/",
        "https://example.com/careers"])
    assert [r.key for r in refs] == [
        "greenhouse:kestrelrobotics", "greenhouse:acme", "lever:marigold-labs", "ashby:tamarind",
        "workable:saffron-analytics", "workable:lotus", "smartrecruiters:NilgiriTextiles1"]


def test_board_config_is_validated():
    assert HiringConfig(boards=["Greenhouse: acme ", "greenhouse:acme"]).boards == \
        ["greenhouse:acme"]
    with pytest.raises(ValidationError, match="kind:token"):
        HiringConfig(boards=["indeed:acme"])


# --- the boards ----------------------------------------------------------------------------

def test_each_ats_board_is_read_into_postings():
    web = job_web()
    results = {key: fetch_board(web, BoardRef.parse(key)) for key in BOARDS}
    gh = results["greenhouse:kestrelrobotics"]
    assert gh.company == "Kestrel Robotics" and len(gh.postings) == 6 and not gh.error
    assert gh.postings[0].location == "Pune, India" and gh.postings[0].posted_at is not None
    assert gh.page == "https://boards.greenhouse.io/kestrelrobotics"
    lever = results["lever:marigold-labs"]
    assert lever.company == "Marigold Labs" and lever.postings[1].remote is True
    assert lever.postings[0].posted_at > datetime.now(timezone.utc) - timedelta(days=5)
    ashby = results["ashby:tamarind"]
    assert [p.title for p in ashby.postings] == ["Site Reliability Engineer"]   # unlisted dropped
    workable = results["workable:saffron-analytics"]
    assert workable.company == "Saffron Analytics"
    assert workable.postings[0].location == "Pune, Maharashtra, India"
    sr = results["smartrecruiters:NilgiriTextiles1"]
    assert sr.company == "Nilgiri Textiles"
    assert sr.postings[0].url == "https://jobs.smartrecruiters.com/NilgiriTextiles1/744000000000001"


def test_board_errors_come_back_as_reasons():
    web = FakeWeb({"https://boards-api.greenhouse.io/v1/boards/nobody/jobs": Page(status=404),
                   "https://api.lever.co/v0/postings/busy": Page(status=429),
                   "https://api.ashbyhq.com/posting-api/job-board/garbled": Page("<html>")})
    assert fetch_board(web, BoardRef("greenhouse", "nobody")).error == "board_not_found"
    assert fetch_board(web, BoardRef("lever", "busy")).error == "blocked"
    assert fetch_board(web, BoardRef("ashby", "garbled")).error == "bad_response"
    assert fetch_board(web, BoardRef("workable", "offline")).error == "fetch_failed"


def test_hn_threads_and_ads_are_parsed():
    web = job_web()
    [thread] = hn.threads(web, 1)                                      # not "wants to be hired"
    assert thread.id == "41000001"
    ads = [hn.parse_comment(c, thread) for c in hn.comments(web, thread)]
    assert len(ads) == 3                                               # the deleted one is gone
    pinecrest, glacier, monsoon = ads
    assert pinecrest.company == "Pinecrest Health"
    assert pinecrest.company_url == "https://pinecrest-health.example/"
    assert pinecrest.url == "https://news.ycombinator.com/item?id=41000101"
    assert glacier.remote and "REMOTE (US)" in glacier.location
    assert monsoon.company == "Monsoon Studio" and "web storefront" in monsoon.text


# --- the session -----------------------------------------------------------------------------

def session(tmp_path, *, web=None, ai=None, **section) -> HiringSession:
    config = compile_run_config(RunConfig.model_validate({
        "name": "Hiring", "project": "hiring", "geography": {"country": "IN", "places": ["Pune"]},
        "lead_types": {"hiring": section}}))
    context = SimpleNamespace(config=config, store=None, ai=ai, run_id=None)
    return HiringSession(context, HIRING.effective(config),
                         kit=kit_for(web or job_web(), tmp_path))


def test_postings_are_matched_by_role_level_age_and_place(tmp_path):
    hiring = session(tmp_path)
    board = fetch_board(job_web(), BoardRef.parse("greenhouse:kestrelrobotics"))
    matched = [m for m in (hiring.accept(p) for p in board.postings) if m]
    assert [(m.posting.title, m.where) for m in matched] == [
        ("Senior Backend Engineer (Python)", "place"), ("QA Automation Engineer", "remote")]
    seniors = session(tmp_path, seniority=["senior"])
    assert [m.posting.title for m in (seniors.accept(p) for p in board.postings) if m] == \
        ["Senior Backend Engineer (Python)"]
    longer = session(tmp_path, posted_within_days=120)
    assert "Frontend Developer" in [m.posting.title for m in
                                    (longer.accept(p) for p in board.postings) if m]


def test_more_open_roles_make_a_stronger_signal():
    assert [strength(n) for n in (1, 2, 4, 9)] == [0.45, 0.55, 0.75, 0.95]


class FakeAI:
    """Answers signal_interpretation with fixed verdicts (no provider is ever called)."""

    def __init__(self, items):
        self.items = items
        self.calls = 0

    def uses(self, task: str) -> bool:
        return task == "signals"

    def call(self, task, *, system, user, output):
        self.calls += 1
        return SimpleNamespace(data=output.model_validate({"items": self.items}),
                               provider="fake", model="judge")


def test_ai_reads_ads_the_rules_cannot_place_only_with_a_grounded_quote(tmp_path):
    ads = [Posting(title="Monsoon Studio | REMOTE", url="https://news.ycombinator.com/item?id=1",
                   company="Monsoon Studio", text="We are looking for someone to own our web "
                                                  "storefront and the systems behind it."),
           Posting(title="Quiet Co | REMOTE", url="https://news.ycombinator.com/item?id=2",
                   company="Quiet Co", text="We make tea.")]
    ai = FakeAI([{"id": "0", "signal_type": "job_posting", "strength": 0.6,
                  "summary": "Hiring an engineer for its web storefront",
                  "evidence": "own our web storefront"},
                 {"id": "1", "signal_type": "job_posting", "strength": 0.9,
                  "summary": "Hiring developers", "evidence": "we are hiring ten developers"}])
    matches = session(tmp_path, ai=ai).interpret(ads)
    assert [m.posting.company for m in matches] == ["Monsoon Studio"]   # the invented quote fails
    assert matches[0].where == "ai" and matches[0].interpreted_by == "fake/judge"
    assert session(tmp_path).interpret(ads) == []                        # AI off: no verdicts


# --- runs --------------------------------------------------------------------------------------

def run(tmp_path, monkeypatch, name: str, lead_types: dict):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    config = compile_run_config(RunConfig.model_validate({
        "name": "Hiring test", "project": f"hiring-test-{tmp_path.name}-{name}",
        "geography": {"country": "IN", "places": ["Pune"]},
        "lead_types": lead_types,
        "runtime": {"db_url": run_database(tmp_path, name)},
    }))
    return config, execute(RunRequest(config=config, install_signals=False))


def rows(config, model) -> list[dict]:
    return project_rows(config, model)


def signals_by_org(config) -> dict[str, list[dict]]:
    names = {o["id"]: o["name"] for o in rows(config, Organization)}
    out: dict[str, list[dict]] = {}
    for s in rows(config, Signal):
        out.setdefault(names[s["organization_id"]], []).append(s)
    return out


NO_CAREERS = {"careers_pages": {"enabled": False}}
FAST = {name: {"min_delay": 0, "max_delay": 0} for name in ("ats", "hn_whos_hiring")}


def test_a_run_turns_open_tech_roles_into_signals(tmp_path, monkeypatch):
    web = job_web()
    patch_kit(monkeypatch, web)
    section = {"boards": BOARDS, "sources": {**FAST, **NO_CAREERS}}
    config, outcome = run(tmp_path, monkeypatch, "jobs", {"hiring": section})
    assert outcome.ok, outcome.results
    by_org = signals_by_org(config)
    assert {name: sorted(s["title"] for s in sigs) for name, sigs in by_org.items()} == {
        "Kestrel Robotics": ["QA Automation Engineer", "Senior Backend Engineer (Python)"],
        "Marigold Labs": ["Full Stack Developer", "SEO Specialist"],
        "Tamarind": ["Site Reliability Engineer"],
        "Saffron Analytics": ["Data Analyst"],
        "Nilgiri Textiles": ["IT Support Engineer"],
        "Pinecrest Health": ["Backend Engineer, DevOps"],
    }
    kestrel = by_org["Kestrel Robotics"]
    assert {s["strength"] for s in kestrel} == {0.55}                  # two roles
    assert all(s["key"] == s["source_url"] and s["lead_type"] == "hiring" for s in kestrel)
    assert {s["payload"]["family"] for s in kestrel} == {"developer", "qa"}
    pinecrest = by_org["Pinecrest Health"][0]
    assert pinecrest["payload"]["open_roles"] == 2 and pinecrest["strength"] == 0.55
    orgs = {o["name"]: o for o in rows(config, Organization)}
    assert orgs["Pinecrest Health"]["record"]["website"] == "https://pinecrest-health.example/"
    assert orgs["Kestrel Robotics"]["locality"] == "Pune"
    assert "Glacier Payments" not in orgs                              # Remote (US) only
    assert all(o["lead_types"] == ["hiring"] for o in orgs.values())

    # The workbook shows its columns.
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                             run_id=outcome.run_id)[1]
    store.close()
    sheet = load_workbook(io.BytesIO(content))["Records"]
    header = [c.value for c in sheet[1]]
    for column in ("Open Tech Roles", "Role Families", "Latest Posting", "Jobs Page",
                   "Open Roles", "Hiring Sources", "Top Signal", "Intent"):
        assert column in header, column
    names = [r[header.index("Name")] for r in sheet.iter_rows(min_row=2, values_only=True)]
    kestrel_row = names.index("Kestrel Robotics") + 2
    cell = lambda column: sheet.cell(kestrel_row, header.index(column) + 1)  # noqa: E731
    assert cell("Open Tech Roles").value == 2
    assert cell("Role Families").value in ("Developer, QA / testing", "QA / testing, Developer")
    assert cell("Jobs Page").hyperlink.target == "https://boards.greenhouse.io/kestrelrobotics"

    # Seen again next month: refreshed in place, not duplicated.
    _, again = run(tmp_path, monkeypatch, "jobs", {"hiring": section})
    assert again.ok
    assert sum(len(v) for v in signals_by_org(config).values()) == \
        sum(len(v) for v in by_org.values())


def test_a_missing_board_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    web = job_web()
    web.add("https://boards-api.greenhouse.io/v1/boards/ghost/jobs", Page(status=404))
    patch_kit(monkeypatch, web)
    config, outcome = run(tmp_path, monkeypatch, "ghost", {"hiring": {
        "boards": ["greenhouse:ghost", "lever:marigold-labs"],
        "sources": {**FAST, **NO_CAREERS, "hn_whos_hiring": {"enabled": False}}}})
    assert outcome.ok
    assert any(f["source"] == "greenhouse" and f["reason"] == "board_not_found"
               for f in rows(config, FailureRow))
    assert set(signals_by_org(config)) == {"Marigold Labs"}


class CareersSource(Source):
    """local_business's discovery, offline: two firms with careers pages, one without a site."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Kestrel Robotics Pvt Ltd",
                             website="https://kestrel.example/", locality=locality.name,
                             category=category.name, phones=["+912000000011"])
        yield self.make_lead(business_name="Lotus Logistics", website="https://lotus.example/",
                             locality=locality.name, category=category.name,
                             phones=["+912000000012"])
        yield self.make_lead(business_name="Quiet Traders", locality=locality.name,
                             category=category.name, phones=["+912000000013"])


def careers_web() -> FakeWeb:
    web = job_web()
    web.add("https://kestrel.example/", Page(fixture("sites/careers_with_board.html")))
    web.add("https://kestrel.example/careers", Page(fixture("sites/careers_board_embed.html")))
    web.add("https://lotus.example/", Page(fixture("sites/careers_titles_home.html")))
    web.add("https://lotus.example/join-us", Page(fixture("sites/careers_titles.html")))
    return web


LOCAL = {"local_business": {"categories": [{"name": "Companies"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0}}}}
CAREERS_ONLY = {"sources": {**FAST, "hn_whos_hiring": {"enabled": False},
                            "careers_pages": {"min_delay": 0, "max_delay": 0}}}


@pytest.fixture
def careers(monkeypatch):
    web = careers_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": CareersSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    return web


def test_careers_pages_of_other_lead_types_organisations(tmp_path, monkeypatch, careers):
    config, outcome = run(tmp_path, monkeypatch, "careers", {**LOCAL, "hiring": CAREERS_ONLY})
    assert outcome.ok, outcome.results
    orgs = {o["name"]: o for o in rows(config, Organization)}
    # Signals go on the organisations local_business found; no new organisation.
    assert set(orgs) == {"Kestrel Robotics Pvt Ltd", "Lotus Logistics", "Quiet Traders"}
    by_org = signals_by_org(config)
    assert sorted(s["title"] for s in by_org["Kestrel Robotics Pvt Ltd"]) == \
        ["QA Automation Engineer", "Senior Backend Engineer (Python)"]  # via its Greenhouse embed
    lotus = by_org["Lotus Logistics"]
    assert sorted(s["title"] for s in lotus) == ["IT Support Executive", "Senior PHP Developer"]
    assert {s["source_url"] for s in lotus} == {"https://lotus.example/join-us/php-developer",
                                                "https://lotus.example/join-us/it-support"}
    assert all(s["published_at"] is None and s["payload"]["match"] == "site" for s in lotus)
    assert "Quiet Traders" not in by_org                                   # no website
    assert orgs["Lotus Logistics"]["lead_types"] == ["local_business", "hiring"]
    assert orgs["Quiet Traders"]["lead_types"] == ["local_business"]
    # Scanned once per window: a second run fetches no careers page.
    careers.calls.clear()
    run(tmp_path, monkeypatch, "careers", {**LOCAL, "hiring": CAREERS_ONLY})
    assert not any("lotus.example" in url for url in careers.calls)


def local_business_output(config) -> tuple[list, list]:
    """local_business's own output: its rows, score columns and its own evidence."""
    orgs = sorted((o for o in rows(config, Organization) if "local_business" in o["lead_types"]),
                  key=lambda o: o["name"])
    out = []
    for org in orgs:
        row = Lead.from_dict(org["record"]).to_row()
        for column in ("First Seen", "Last Updated"):
            row.pop(column)
        out.append((row, org["score"], org["fit"], org["reach"], org["score_reasons"]))
    ids = {o["id"] for o in orgs}
    evidence = sorted((e["source"], tuple(e["fields"])) for e in rows(config, Evidence)
                      if e["organization_id"] in ids and e["lead_type"] == "local_business")
    return out, evidence


def test_enabling_or_disabling_hiring_leaves_local_business_unchanged(tmp_path, monkeypatch,
                                                                     careers):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)
    together, outcome = run(tmp_path, monkeypatch, "together",
                            {**LOCAL, "hiring": {**CAREERS_ONLY, "boards": BOARDS}})
    off, _ = run(tmp_path, monkeypatch, "off", {**LOCAL, "hiring": {"enabled": False}})
    assert outcome.results["hiring"].stats["signals"] > 0
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline


def test_a_crashing_source_is_contained(tmp_path, monkeypatch, careers):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self):
        raise RuntimeError("hn exploded")

    monkeypatch.setattr(HiringSession, "read_hn", boom)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "hiring": {
        "boards": ["lever:marigold-labs"], "sources": {**FAST, **NO_CAREERS}}})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert any(f["reason"] == "unit_failed" for f in rows(crashed, FailureRow))
    assert "Marigold Labs" in signals_by_org(crashed)                    # the other unit ran


# --- scoring, columns, plan ----------------------------------------------------------------

def posting(days: float, roles_: list[str] | None = None, family: str = "developer") -> dict:
    now = datetime.now(timezone.utc)
    return {"type": "job_posting", "lead_type": "hiring", "title": "Backend Engineer",
            "published_at": now - timedelta(days=days), "observed_at": now,
            "source_url": f"https://jobs.example/{days}",
            "payload": {"family": family, "family_title": roles.FAMILY_TITLES[family],
                        "roles": roles_ or ["Backend Engineer"], "source": "greenhouse",
                        "board_page": "https://boards.greenhouse.io/x"}}


def test_score_grows_with_open_roles_and_fades_with_age():
    one = HIRING.score(None, [posting(0)], None)
    three = HIRING.score(None, [posting(0), posting(1, family="qa"), posting(2)], None)
    assert one.intent == pytest.approx(0.4, abs=0.01)
    assert three.intent == pytest.approx(0.7, abs=0.02) and three.fit is None
    assert three.reasons[0].startswith("hiring 3 tech roles (Developer ×2, QA / testing), "
                                       "latest posted ")
    old = HIRING.score(None, [posting(30)], None)
    assert old.intent == pytest.approx(0.2, abs=0.01)                    # one half-life
    stale = dict(posting(0), observed_at=datetime.now(timezone.utc) - timedelta(days=60))
    assert open_postings([stale]) == [] and HIRING.score(None, [stale], None).intent == 0.0


def test_column_values():
    org = SimpleNamespace(signals=[posting(3), posting(1, ["Backend Engineer", "DevOps"],
                                                      family="devops")])
    values = HIRING.column_values(org, None)
    assert values["open_roles"] == 3 and values["jobs_page"] == "https://boards.greenhouse.io/x"
    assert values["families"] in ("DevOps / cloud, Developer", "Developer, DevOps / cloud")
    assert HIRING.column_values(SimpleNamespace(signals=[]), None) == {}


def test_plan_sources_and_depth():
    config = compile_run_config(RunConfig.model_validate({
        "name": "H", "project": "h", "geography": {"country": "GB", "places": ["Leeds", "York"]},
        "lead_types": {"hiring": {"boards": ["lever:a", "greenhouse:b"],
                                  "sources": {"board_search": {"enabled": True}}}}}))
    units = [(u.source, u.area, u.category) for u in HIRING.plan(config)]
    assert units == [("ats", None, "lever:a"), ("ats", None, "greenhouse:b"),
                     ("hn_whos_hiring", None, None), ("board_search", "Leeds", None),
                     ("board_search", "York", None)]
    assert HIRING.sources(config) == {"discovery": ["board_search", "greenhouse",
                                                    "hn_whos_hiring", "lever"],
                                      "enrichment": ["careers_pages"]}
    assert HIRING.effective(config).sources.hn_whos_hiring.months == 1
    config.set("depth", "deep")
    assert HIRING.effective(config).sources.hn_whos_hiring.months == 3
    assert registry.get("hiring") is HIRING


def test_the_icp_builder_switches_built_lead_types_on_and_notes_later_ones():
    from leadscraper.ai.features.icp_builder import ICPDraft, to_document

    draft = ICPDraft(name="Staff aug", country="IN", places=["Pune"],
                     categories=[{"name": "Software companies"}],
                     lead_types=["local_business", "hiring", "digital_audit", "funding_news"])
    document, notes = to_document(draft)
    config = RunConfig.model_validate(document)
    assert config.enabled_lead_types() == ["local_business", "hiring", "digital_audit"]
    assert any("funding_news" in note for note in notes)


# --- live (opt-in) -------------------------------------------------------------------------

LIVE_BOARDS = ["greenhouse:stripe", "lever:leverdemo", "ashby:ashby", "workable:huggingface",
               "smartrecruiters:BoschGroup"]


@pytest.mark.live
@pytest.mark.parametrize("key", LIVE_BOARDS)
def test_live_public_ats_board(key):
    """One real public board per ATS (verified 2026-10-07): postings come back, shaped."""
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    result = fetch_board(HttpClient(RateLimiter((1.0, 2.0)), timeout=30, max_retries=1),
                         BoardRef.parse(key), limit=20)
    assert not result.error, (result.error, result.detail)
    assert result.company and result.postings
    assert all(p.title and p.url.startswith("https://") for p in result.postings)


@pytest.mark.live
def test_live_hn_whos_hiring():
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    http = HttpClient(RateLimiter((1.0, 2.0)), timeout=30, max_retries=1)
    [thread] = hn.threads(http, 1)
    ads = [a for a in (hn.parse_comment(c, thread) for c in hn.comments(http, thread)) if a]
    assert len(ads) > 50 and sum(bool(a.company_url) for a in ads) > len(ads) / 2


# --- stages: hiring runs after the finders ---------------------------------------------------

def test_a_later_stage_waits_for_every_earlier_unit():
    from leadscraper.engine.control import RunControl
    from leadscraper.engine.costs import CostModel, prior_cost
    from leadscraper.engine.parallel import EXHAUSTED
    from leadscraper.engine.scheduler import Planned, Scheduler
    from leadscraper.leadtypes.base import WorkUnit

    finder = Planned(WorkUnit("local_business", "maps", "A", "C"), None, 0, frozenset({"maps"}))
    other = Planned(WorkUnit("local_business", "serp", "A", "C"), None, 1, frozenset({"serp"}))
    board = Planned(WorkUnit("hiring", "ats", None, "lever:a", "run"), None, 2,
                    frozenset({"api.lever.co"}), stage=1)
    costs = CostModel(None, lambda unit: prior_cost(10, 1), lambda unit: ("s", "*", "*"))
    scheduler = Scheduler([board, finder, other], costs, RunControl())
    first = scheduler.next_ready(set())
    second = scheduler.next_ready({"maps"})
    assert {first, second} == {finder, other}          # a "run" unit of stage 1 still waits
    assert scheduler.next_ready({"maps", "serp"}) is None
    scheduler.finished(first)
    assert scheduler.next_ready({"serp"}) is None       # one finder is still running
    scheduler.finished(second)
    assert scheduler.next_ready(set()) is board
    assert scheduler.next_ready(set()) is EXHAUSTED


def test_a_company_the_finders_hold_gets_the_signals_and_keeps_its_record(tmp_path, monkeypatch,
                                                                         careers):
    config, outcome = run(tmp_path, monkeypatch, "shared", {**LOCAL, "hiring": {
        "boards": ["greenhouse:kestrelrobotics"],
        "sources": {**FAST, **NO_CAREERS, "hn_whos_hiring": {"enabled": False}}}})
    assert outcome.ok
    orgs = {o["name"]: o for o in rows(config, Organization)}
    kestrel = orgs["Kestrel Robotics Pvt Ltd"]                  # one organisation, its name kept
    assert "Kestrel Robotics" not in orgs
    assert kestrel["record"]["sources"] == ["stub"]              # the record was not rewritten
    assert kestrel["lead_types"] == ["local_business", "hiring"]
    assert len(signals_by_org(config)["Kestrel Robotics Pvt Ltd"]) == 2
    assert outcome.results["hiring"].new == 0


# Shapes seen in the live October 2026 thread, rewritten as fictional ads.
@pytest.mark.parametrize("location, expected", [
    ("Acme | Founding Engineer | Remote but Boston based only", None),
    ("Acme | Senior Software Engineer | REMOTE in select countries", None),
    ("Acme | Backend Engineers | REMOTE (US/Canada/Brazil/India)", "remote"),
    ("Acme | Software Engineer | Full Time, Remote India", "remote"),
    ("Acme | Android Engineer | Remote | Full-time", "remote"),
])
def test_remote_ads_must_be_open_to_the_country(location, expected):
    assert roles.location_match(location, remote=None, places=["Pune"],
                                country_terms=COUNTRY_IN) == expected


def test_roles_come_from_the_headline_and_role_lists_not_prose():
    headline = "Acme | Go-To-Market Specialist | REMOTE"
    prose = f"{headline}\nWe help QA teams and developers ship faster with SEO-friendly docs."
    assert roles.families_in(roles.role_text(headline, prose), roles.FAMILY_PATTERNS) == []
    listed = "Acme | Several roles | REMOTE\n- Senior Backend Engineer\n- QA Engineer\nAbout us"
    found = roles.families_in(roles.role_text("Acme | Several roles | REMOTE", listed),
                              roles.FAMILY_PATTERNS)
    assert {f for f, _ in found} == {"developer", "qa"}
