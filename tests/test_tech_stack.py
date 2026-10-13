"""tech_stack (Phase 8.1): fingerprint rules, the site check, footprints, uploads, runs,
isolation, scoring and columns.

All offline: fictional sites (``tests/fixtures/sites``, reserved ``.example`` domains)
served by ``FakeWeb``; the web search behind footprints is stubbed. The ``live`` test
fingerprints one real public site.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from openpyxl import load_workbook

from fake_web import (FakeWeb, Page, dead, fixture, kit_for, local_business_view, patch_kit,
                      probe_enricher, project_rows, run_database)
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Evidence, FailureRow, Organization, Signal
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.leadtypes.tech_stack.config import TechStackConfig
from leadscraper.leadtypes.tech_stack.plugin import TECH_STACK
from leadscraper.leadtypes.tech_stack.session import (TechStackSession, matches, phrases,
                                                      plan_units)
from leadscraper.leadtypes.tech_stack.sitecheck import SiteChecker, own_site, site_name
from leadscraper.net.http import PrivateAddressError
from leadscraper.services import exports
from leadscraper.services.configs import validate_document
from leadscraper.services.errors import Invalid
from leadscraper.services.workspace import default_workspace
from leadscraper.sources import google_search
from leadscraper.sources.base import Source
from leadscraper.sources.google_search import SerpResult
from leadscraper.sources.web.fingerprint import canonical, detect, names, rule, rules

UTC = timezone.utc
BISTRO = "https://oldmill-bistro.example/"
DENTAL = "https://brightwater-dental.example/"
SPICE = "https://deccan-spice.example/"
OLD_HEADERS = {"Server": "Apache/2.2.15 (CentOS)", "X-Powered-By": "PHP/7.2.34"}


def site_web(**overrides) -> FakeWeb:
    routes = {
        BISTRO: Page(fixture("sites/old_bistro_home.html"), headers=OLD_HEADERS),
        DENTAL: Page(fixture("sites/modern_dental_home.html"), headers={"Server": "nginx"}),
        SPICE: Page(fixture("sites/shopify_store_home.html")),
    }
    routes.update(overrides)
    return FakeWeb(routes)


# --- the shared fingerprint rules (Phase 8 additions) ------------------------------------------

def test_phase_8_rules_detect_versions_and_are_dated():
    file_date, table = rules()
    assert file_date == "2026-10-07"                       # digital_audit's rules, unchanged
    assert len(table) >= 44 and len(names()) == len(table)
    assert rule("angular").verified == "2026-10-13" and rule("WordPress").verified == "2026-10-07"
    found = {d.name: d for d in detect(
        '<app-root ng-version="17.3.12"></app-root><div data-v-app></div>',
        {"Set-Cookie": "XSRF-TOKEN=x; path=/, laravel_session=y; path=/; httponly",
         "MicrosoftSharePointTeamServices": "15.0.0.4569"},
        generators=["TYPO3 CMS", "Odoo"],
        script_urls=["https://cdn.example/npm/vue@2.6.14/dist/vue.min.js"])}
    assert found["Angular"].version == "17.3.12" and found["Angular"].outdated
    assert "May 2026" in found["Angular"].why
    assert found["Vue.js"].version == "2.6.14" and found["Vue.js"].outdated
    assert found["Laravel"].evidence == "header set-cookie" and not found["Laravel"].outdated
    assert found["Microsoft SharePoint"].outdated
    assert found["TYPO3"].version == "" and not found["TYPO3"].outdated   # no version: no claim
    assert "Odoo" in found
    assert not detect('<app-root ng-version="20.1.0">')[0].outdated        # supported
    # Names are compared without case or punctuation.
    assert rule("next.js").name == "Next.js" and canonical("Magento 1") == "magento1"
    assert rule("shopify").footprints == ('"powered by Shopify"',)
    assert "shopify.com" in rule("Shopify").vendor


def test_config_validates_technologies_and_thresholds():
    section = TechStackConfig(technologies=["wordpress", "SHOPIFY", "WordPress"],
                              thresholds={"php": "8.3"},
                              sources={"footprints": {"phrases": {"odoo": ['"Odoo ERP"']}}})
    assert section.technologies == ["WordPress", "Shopify"]
    assert section.thresholds == {"PHP": "8.3"}
    assert section.sources.footprints.phrases == {"Odoo": ['"Odoo ERP"']}
    assert section.sources.footprints.enabled is False          # web search: opt-in
    with pytest.raises(ValueError, match="not a technology"):
        TechStackConfig(technologies=["COBOL"])
    with pytest.raises(ValueError, match="a version"):
        TechStackConfig(thresholds={"PHP": "latest"})


def test_which_technologies_count():
    stack = detect("<link href='/wp-content/x.css'>", OLD_HEADERS, generators=["WordPress 6.6.2"])
    named = TechStackConfig(technologies=["WordPress", "PHP"])
    assert [(d.label, n) for d, n in matches(stack, named)] == [("WordPress 6.6.2", True),
                                                                ("PHP 7.2.34", True)]
    only_old = TechStackConfig(technologies=["WordPress", "PHP"], outdated_only=True)
    assert [d.label for d, _ in matches(stack, only_old)] == ["PHP 7.2.34"]
    anything_old = TechStackConfig()
    assert [(d.label, n) for d, n in matches(stack, anything_old)] == [("PHP 7.2.34", False),
                                                                       ("Apache 2.2.15", False)]
    # A run's threshold moves the line.
    newer = TechStackConfig(technologies=["WordPress"], thresholds={"WordPress": "7.0"})
    stack = detect("", {}, generators=["WordPress 6.6.2"], thresholds=newer.thresholds)
    assert stack[0].outdated and stack[0].why == "older than 7.0 (this run's threshold)"


# --- one site ----------------------------------------------------------------------------------

def checker(tmp_path, web: FakeWeb, **kw) -> SiteChecker:
    kit = kit_for(web, tmp_path)
    return SiteChecker(kit.http, kit.robots, kit.failures, **kw)


def test_the_site_check(tmp_path):
    web = site_web()
    check = checker(tmp_path, web).check("Old Mill Bistro", "oldmill-bistro.example")
    assert check.checked and check.url == BISTRO and check.title == "Old Mill Bistro"
    assert [d.label for d in check.stack] == ["WordPress 4.9.8", "jQuery 1.12.4", "PHP 7.2.34",
                                              "Apache 2.2.15"]
    assert site_name("<title>Acme Tools | Hand tools in Pune</title>") == "Acme Tools"
    # HTTPS that doesn't answer: HTTP.
    web = FakeWeb({"http://plain.example/": Page("<meta name='generator' content='Joomla! 3.9'>")})
    check = checker(tmp_path, web).check("Plain", "http://plain.example/")
    assert check.checked and check.url == "http://plain.example/"
    assert check.stack[0].name == "Joomla"
    # Statuses that say nothing about the site.
    assert checker(tmp_path, web).check("Insta", "https://instagram.com/acme").status == \
        "no_website"
    assert own_site("https://acme.myshopify.com/") == "https://acme.myshopify.com/"
    robots = FakeWeb({"https://shy.example/robots.txt": Page("User-agent: *\nDisallow: /")})
    assert checker(tmp_path, robots).check("Shy", "shy.example").status == "robots"
    walled = FakeWeb({"https://walled.example/": Page("Access denied", status=403)})
    assert checker(tmp_path, walled).check("Walled", "walled.example").status == "blocked"
    gone = FakeWeb({"https://gone.example/": dead("gone.example")})
    assert checker(tmp_path, gone).check("Gone", "gone.example").status == "unreachable"
    inner = FakeWeb({"https://inner.example/": PrivateAddressError("10.0.0.5 is internal")})
    assert checker(tmp_path, inner).check("Inner", "inner.example").status == "refused"
    stop = checker(tmp_path, site_web(), should_stop=lambda: True)
    assert stop.check("Bistro", BISTRO).status == "stopped"


# --- planning ----------------------------------------------------------------------------------

def _config(lead_types: dict, places=("Pune",), country="IN"):
    return compile_run_config(RunConfig.model_validate({
        "name": "t", "project": "t", "geography": {"country": country, "places": list(places)},
        "lead_types": lead_types}))


def test_plan_units_uploads_and_opt_in_footprints():
    config = _config({"tech_stack": {"technologies": ["Shopify", "Magento 2"]}})
    section = TECH_STACK._section(config)
    assert plan_units(config, section, uploads=False) == []       # post-discovery only
    assert [u.key for u in plan_units(config, section, uploads=True)] == [
        "tech_stack|upload|*|*"]
    config = _config({"tech_stack": {"technologies": ["Shopify", "Magento 2"],
                                     "sources": {"footprints": {"enabled": True}}}},
                     places=("Pune", "Nashik"))
    units = TECH_STACK.plan(config)
    # Magento 2 has no footprint phrase in the table: no unit for it.
    assert [u.key for u in units] == ["tech_stack|footprints|Pune|Shopify",
                                      "tech_stack|footprints|Nashik|Shopify"]
    assert phrases(TECH_STACK._section(config), "Magento 2") == []
    assert TECH_STACK.sources(config) == {"discovery": ["footprints"],
                                          "enrichment": ["site_fingerprint"]}


# --- runs ----------------------------------------------------------------------------------------

class FirmSource(Source):
    """local_business's discovery, offline: three firms with sites, one without."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        for name, site, phone in (("Old Mill Bistro", BISTRO, "+912025550101"),
                                  ("Brightwater Dental Studio", DENTAL, "+912025550102"),
                                  ("Deccan Spice Traders", SPICE, "+912025550103"),
                                  ("Corner Café", None, "+912025550104")):
            yield self.make_lead(business_name=name, website=site, locality=locality.name,
                                 category=category.name, phones=[phone])


LOCAL = {"local_business": {"categories": [{"name": "Shops"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0},
                                        "probe": {"enabled": True, "min_delay": 0,
                                                  "max_delay": 0}}}}
TECH = {"technologies": ["WordPress", "Shopify"], "min_delay": 0, "max_delay": 0}


@pytest.fixture
def sites(monkeypatch):
    web = site_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": FirmSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"probe": probe_enricher()})
    return web


def run(tmp_path, monkeypatch, name: str, lead_types: dict, *, force=False, places=("Pune",)):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    document = {"name": "Tech stack test", "project": f"techstack-{tmp_path.name}-{name}",
                "geography": {"country": "IN", "places": list(places)},
                "lead_types": lead_types, "runtime": {"db_url": run_database(tmp_path, name)}}
    config = compile_run_config(RunConfig.model_validate(document))
    return config, execute(RunRequest(config=config, install_signals=False, force_enrich=force))


def by_org(config, model=Signal) -> dict[str, list[dict]]:
    names = {o["id"]: o["name"] for o in project_rows(config, Organization)}
    out: dict[str, list[dict]] = {}
    for row in project_rows(config, model):
        if row.get("lead_type") == "tech_stack":
            out.setdefault(names[row["organization_id"]], []).append(row)
    return out


def workbook(config, run_id):
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                                 run_id=run_id)[1]
    finally:
        store.close()
    return load_workbook(io.BytesIO(content))["Records"]


def test_a_run_fingerprints_other_lead_types_sites(tmp_path, monkeypatch, sites):
    config, outcome = run(tmp_path, monkeypatch, "run", {**LOCAL, "tech_stack": TECH})
    assert outcome.ok, outcome.results
    signals = {name: [(s["type"], s["key"], s["title"], s["payload"]["targeted"])
                      for s in rows] for name, rows in by_org(config).items()}
    assert signals == {
        "Old Mill Bistro": [("outdated_technology", "tech:wordpress", "WordPress 4.9.8", True)],
        "Deccan Spice Traders": [("technology", "tech:shopify", "Shopify", True)]}
    stats = outcome.results["tech_stack"].stats
    assert stats["checked"] == 3 and stats["matched"] == 2 and stats["no_website"] == 1
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    # Rule 6: the records stay local_business's; the organisations are only stamped.
    assert orgs["Old Mill Bistro"]["lead_types"] == ["local_business", "tech_stack"]
    assert orgs["Old Mill Bistro"]["record"]["sources"] == ["stub"]
    assert "tech_stack" not in orgs["Corner Café"]["lead_types"]
    evidence = [e for e in project_rows(config, Evidence) if e["lead_type"] == "tech_stack"]
    assert {e["source"] for e in evidence} == {"site_fingerprint", "rule:tech_stack"}

    sheet = workbook(config, outcome.run_id)
    header = [c.value for c in sheet[1]]
    for column in ("Technology", "Tech Status", "Tech Found", "Outdated Tech", "Technologies",
                   "Tech Evidence", "Tech Checked", "Fit", "Intent"):
        assert column in header, column
    rows = {r[header.index("Name")]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    bistro = rows["Old Mill Bistro"]
    assert bistro[header.index("Technology")] == "WordPress"
    assert bistro[header.index("Tech Status")] == "Outdated"
    assert "three years behind" in bistro[header.index("Outdated Tech")]
    assert bistro[header.index("Technologies")] == \
        "WordPress 4.9.8, jQuery 1.12.4, PHP 7.2.34, Apache 2.2.15"
    assert rows["Deccan Spice Traders"][header.index("Tech Status")] == "Current"
    # Checked, nothing wanted: not a tech_stack lead (its check is kept as an attribute).
    assert rows["Brightwater Dental Studio"][header.index("Technologies")] is None
    assert orgs["Brightwater Dental Studio"]["lead_types"] == ["local_business"]

    # Again within a month: the sites checked are left alone.
    _, again = run(tmp_path, monkeypatch, "run", {**LOCAL, "tech_stack": TECH})
    assert again.results["tech_stack"].stats["recent_skipped"] == 3
    # Forced, after the bistro upgraded WordPress: a snapshot, so the finding goes.
    sites.add(BISTRO, Page(fixture("sites/old_bistro_home.html").replace(
        "WordPress 4.9.8", "WordPress 6.6.2"), headers=OLD_HEADERS))
    _, forced = run(tmp_path, monkeypatch, "run", {**LOCAL, "tech_stack": TECH}, force=True)
    assert forced.ok
    assert [(s["type"], s["title"]) for s in by_org(config)["Old Mill Bistro"]] == \
        [("technology", "WordPress 6.6.2")]


def test_any_outdated_technology_when_none_is_named(tmp_path, monkeypatch, sites):
    config, outcome = run(tmp_path, monkeypatch, "any",
                          {**LOCAL, "tech_stack": {"min_delay": 0, "max_delay": 0}})
    assert outcome.ok
    assert sorted((n, s["title"]) for n, rows in by_org(config).items() for s in rows) == [
        ("Old Mill Bistro", "Apache 2.2.15"), ("Old Mill Bistro", "PHP 7.2.34"),
        ("Old Mill Bistro", "WordPress 4.9.8"), ("Old Mill Bistro", "jQuery 1.12.4")]


def test_uploads_are_written_then_checked(tmp_path, monkeypatch, sites):
    sheet = tmp_path / "list.csv"
    sheet.write_text("Company,Website\nDeccan Spice Traders,deccan-spice.example\n"
                     "Nowhere Ltd,\n", encoding="utf-8")
    config, outcome = run(tmp_path, monkeypatch, "uploads", {"tech_stack": {
        **TECH, "check": "targets", "domains": ["oldmill-bistro.example"],
        "targets_file": str(sheet)}})
    assert outcome.ok
    assert sorted(by_org(config)) == ["Deccan Spice Traders", "oldmill-bistro.example"]
    assert outcome.results["tech_stack"].stats["uploaded"] == 3       # Nowhere Ltd: no site


def test_footprints_search_then_confirm_by_fingerprint(tmp_path, monkeypatch, sites):
    sites.add("https://not-shopify.example/", Page("<html><title>Plain Shop</title></html>"))
    queries = []

    def search(self, query, *, pages=1, per_page=20):
        queries.append(query)
        return [SerpResult("Shopify – start a store", "https://www.shopify.com/in"),
                SerpResult("Deccan Spice Traders – Fresh masalas", SPICE + "collections/all"),
                SerpResult("Deccan again", SPICE),
                SerpResult("Plain Shop", "https://not-shopify.example/about"),
                SerpResult("Facebook page", "https://www.facebook.com/deccanspice")]

    monkeypatch.setattr(google_search.SerpClient, "search", search)
    footprints = {"technologies": ["Shopify"], "check": "targets", "min_delay": 0,
                  "max_delay": 0, "sources": {"footprints": {"enabled": True, "min_delay": 0.5,
                                                             "max_delay": 0.5}}}
    config, outcome = run(tmp_path, monkeypatch, "footprints", {"tech_stack": footprints})
    assert outcome.ok
    assert queries == ['"powered by Shopify" "Pune"']
    assert list(by_org(config)) == ["Deccan Spice Traders"]          # confirmed on its site
    org = next(o for o in project_rows(config, Organization) if o["name"] == "Deccan Spice "
                                                                         "Traders")
    assert org["record"]["website"] == SPICE and org["record"]["locality"] == "Pune"
    stats = outcome.results["tech_stack"].stats
    assert stats["searches"] == 1 and stats["search_sites"] == 2      # vendor + social dropped
    # create_new off: a site nobody holds is not added.
    footprints["create_new"] = False
    config, outcome = run(tmp_path, monkeypatch, "footprints-off", {"tech_stack": footprints})
    assert by_org(config) == {} and outcome.results["tech_stack"].stats["skipped_unknown"] == 1


# --- isolation ---------------------------------------------------------------------------------

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


def test_enabling_or_disabling_tech_stack_leaves_local_business_unchanged(
        tmp_path, monkeypatch, sites):
    alone, first = run(tmp_path, monkeypatch, "alone", LOCAL)
    view = local_business_view(first)
    together, outcome = run(tmp_path, monkeypatch, "together", {**LOCAL, "tech_stack": TECH})
    assert local_business_view(outcome) == view
    off, last = run(tmp_path, monkeypatch, "off", {**LOCAL, "tech_stack": {"enabled": False}})
    assert local_business_view(last) == view
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline
    assert by_org(together)                                      # it did write its own


def test_a_crashing_check_is_contained(tmp_path, monkeypatch, sites):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self, name, website):
        raise RuntimeError("fingerprinter exploded")

    monkeypatch.setattr(SiteChecker, "_check", boom)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "tech_stack": TECH})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert by_org(crashed) == {}
    assert any(f["reason"] == "check_failed" and "exploded" in (f["detail"] or "")
               for f in project_rows(crashed, FailureRow))


def test_targets_file_is_refused_through_the_api():
    with pytest.raises(Invalid, match="tech_stack.targets_file"):
        validate_document({"schema_version": 1, "name": "x", "project": "x",
                           "geography": {"country": "IN", "places": ["Pune"]},
                           "lead_types": {"tech_stack": {"targets_file": "/etc/hosts"}}})


# --- scoring, columns, depth ---------------------------------------------------------------------

def tech(type_: str, name: str, version: str, days_ago: float, targeted=True, why="") -> dict:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return {"type": type_, "lead_type": "tech_stack", "title": f"{name} {version}".strip(),
            "strength": 0.7 if type_ == "outdated_technology" else 0.5, "observed_at": when,
            "payload": {"technology": name, "version": version, "targeted": targeted,
                        "outdated": type_ == "outdated_technology", "why": why,
                        "evidence": "meta generator"}}


def test_score_fit_for_named_technologies_and_intent_for_outdated_ones():
    now = datetime.now(UTC)
    cfg = _config({"tech_stack": {"technologies": ["WordPress", "Shopify"]}})
    old = tech("outdated_technology", "WordPress", "4.9.8", 0)
    parts = TECH_STACK.score(None, [old], cfg, now=now)
    assert parts.fit == pytest.approx(0.6, abs=0.01)
    assert parts.intent == pytest.approx(0.7, abs=0.01)
    assert parts.reasons == ["runs WordPress", "outdated: WordPress 4.9.8"]
    both = TECH_STACK.score(None, [old, tech("technology", "Shopify", "", 0)], cfg, now=now)
    assert both.fit == pytest.approx(0.7, abs=0.01)
    faded = TECH_STACK.score(None, [tech("outdated_technology", "WordPress", "4.9", 180)], cfg,
                             now=now)
    assert faded.intent == pytest.approx(0.35, abs=0.01)                # one half-life
    assert TECH_STACK.score(None, [], cfg, now=now).fit == 0.0          # named, not running it
    # With no technology named, fit isn't this lead type's to give.
    anything = TECH_STACK.score(None, [tech("outdated_technology", "PHP", "7.2", 0, False)],
                                _config({"tech_stack": {}}), now=now)
    assert anything.fit is None and anything.intent == pytest.approx(0.7, abs=0.01)


def test_column_values():
    now = datetime.now(UTC)
    org = SimpleNamespace(as_of=now, signals=[
        tech("technology", "Shopify", "", 1),
        tech("outdated_technology", "jQuery", "1.12.4", 1, why="old jQuery")],
        attributes={"tech_stack": {"value": {"stack": ["Shopify", "jQuery 1.12.4"],
                                             "checked_at": now.isoformat()}}})
    values = TECH_STACK.column_values(org, None)
    assert values["technology"] == "jQuery" and values["tech_status"] == "Outdated"
    assert values["tech_found"] == "jQuery 1.12.4, Shopify"
    assert values["outdated_tech"] == "jQuery 1.12.4 (old jQuery)"
    assert values["technologies"] == "Shopify, jQuery 1.12.4"
    checked = SimpleNamespace(as_of=now, signals=[], attributes={
        "tech_stack": {"value": {"stack": ["Next.js"], "checked_at": now.isoformat()}}})
    assert TECH_STACK.column_values(checked, None)["technologies"] == "Next.js"
    assert TECH_STACK.column_values(SimpleNamespace(signals=[], attributes={}), None) == {}


def test_depth():
    on = TechStackConfig(sources={"footprints": {"enabled": True}})
    update, notes = TECH_STACK.depth_changes(on, "quick")
    assert update["sources"].footprints.max_sites == 8 and update["max_orgs"] == 100
    assert TECH_STACK.depth_changes(on, "deep")[0]["sources"].footprints.max_sites == 30
    assert TECH_STACK.depth_changes(on, "balanced") == ({}, [])


def test_session_prior_and_hosts(tmp_path):
    config = _config({"tech_stack": {"technologies": ["Shopify"],
                                     "sources": {"footprints": {"enabled": True}}}})
    web = site_web()
    context = SimpleNamespace(config=config, store=None)
    session = TechStackSession(context, TECH_STACK.effective(config), kit=kit_for(web, tmp_path))
    unit = session.units()[0]
    assert session.hosts(unit) == frozenset({"www.google.com", "duckduckgo.com"})
    assert session.prior(unit).seconds > 15 * session.site_seconds()
    assert session.run_unit(unit).status == "done"                # no store: nothing written


# --- live (opt-in) -------------------------------------------------------------------------------

@pytest.mark.live
def test_live_fingerprint_of_a_wordpress_site(tmp_path):
    # A public WordPress site (wordpress.org itself is a platform host, never a business).
    from leadscraper.engine.failures import FailureLog
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter
    from leadscraper.net.robots import RobotsCache

    http = HttpClient(RateLimiter((1.0, 2.0)), max_retries=1, public_only=True)
    check = SiteChecker(http, RobotsCache(http), FailureLog(tmp_path / "f.jsonl")).check(
        "The Mozilla Blog", "https://blog.mozilla.org/")
    assert check.checked, check.notes
    assert "WordPress" in [d.name for d in check.stack]
