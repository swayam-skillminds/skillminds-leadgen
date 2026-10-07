"""digital_audit (Phase 6.1): checks, fingerprints, PageSpeed, uploads, runs, isolation.

All offline: sites are fictional fixture pages served by ``FakeWeb``; the TLS
handshake is a stub. ``test_live_audit_of_example_com`` (``-m live``) audits one
real, reserved site.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
import requests
from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from fake_web import FakeWeb, Page, dead, fixture, kit_for, patch_kit
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Evidence, FailureRow, Organization, OrgAttribute, Signal
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes import registry
from leadscraper.leadtypes.digital_audit import checks as C
from leadscraper.leadtypes.digital_audit.auditor import Auditor, own_website
from leadscraper.leadtypes.digital_audit.config import DigitalAuditConfig
from leadscraper.leadtypes.digital_audit.fingerprint import detect, rules, version_tuple
from leadscraper.leadtypes.digital_audit.pagespeed import PageSpeed, parse
from leadscraper.leadtypes.digital_audit.plugin import DIGITAL_AUDIT
from leadscraper.leadtypes.digital_audit.targets import load_targets, parse_targets
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.net.tls import Certificate
from leadscraper.services import exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.base import Source

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DENTAL = "https://brightwater-dental.example/"
BISTRO = "oldmill-bistro.example"


def cert(days: float = 200, valid: bool = True, error: str = ""):
    def handshake(host: str) -> Certificate:
        if not valid:
            return Certificate(valid=False, error=error)
        return Certificate(valid=True, expires_at=datetime.now(timezone.utc)
                           + timedelta(days=days), issuer="Fictional CA")
    return handshake


def sites() -> FakeWeb:
    web = FakeWeb()
    web.add(DENTAL, Page(fixture("sites/modern_dental_home.html")))
    for path in ("about", "treatments", "contact"):
        web.add(f"{DENTAL}{path}", Page("<html><body>ok</body></html>"))
    web.add(f"https://{BISTRO}/", requests.exceptions.SSLError(
        "certificate verify failed: certificate has expired"))
    web.add(f"http://{BISTRO}/", Page(fixture("sites/old_bistro_home.html"),
                                      headers={"Server": "Apache/2.2.15 (CentOS)",
                                               "X-Powered-By": "PHP/7.2.34"}))
    for path in ("menu/", "gallery/"):
        web.add(f"http://{BISTRO}/{path}", Page("<html>ok</html>"))
    web.add(f"http://{BISTRO}/specials-2016/", Page(status=404))
    web.add(f"http://{BISTRO}/contact-us/", Page(fixture("sites/old_bistro_contact.html")))
    web.add("https://gone-bakery.example/", dead("gone-bakery.example"))
    web.add("https://walled.example/", Page("<html>denied</html>", status=403))
    web.add("https://private.example/robots.txt", Page("User-agent: *\nDisallow: /\n"))
    return web


def auditor(web: FakeWeb, tmp_path, *, tls=None, pagespeed=None, **settings) -> Auditor:
    section = DigitalAuditConfig(min_delay=0, max_delay=0, **settings)
    kit = kit_for(web, tmp_path)
    return Auditor(section, kit.http, kit.robots, kit.failures, pagespeed=pagespeed,
                   tls=tls or cert(), clock=lambda: NOW)


def dental() -> Lead:
    return Lead(business_name="Brightwater Dental Studio", website=DENTAL, category="Dentists",
                phones=["+912000000001"], address="1 Lake Road, Pune",
                gmaps_url="https://maps.google.com/?cid=1", gmaps_rating=4.8, gmaps_reviews=120)


def bistro() -> Lead:
    return Lead(business_name="Old Mill Bistro", website=f"http://{BISTRO}/",
                category="Restaurants")


# --- fingerprints ------------------------------------------------------------------------

def test_fingerprint_rules_are_dated_and_detect_versions():
    verified, table = rules()
    assert verified == "2026-10-07" and len(table) >= 20
    found = {d.name: d for d in detect(
        "<link href='/wp-content/themes/x.css'>", {"Server": "nginx/1.25.3",
                                                   "X-Powered-By": "PHP/8.3.1"},
        generators=["WordPress 6.6.2"],
        script_urls=["https://cdn.example/jquery-3.7.1.min.js",
                     "https://cdn.example/bootstrap@5.3.2/dist/js/bootstrap.min.js"])}
    assert found["WordPress"].version == "6.6.2" and not found["WordPress"].outdated
    assert found["jQuery"].version == "3.7.1" and not found["jQuery"].outdated
    assert found["Bootstrap"].version == "5.3.2" and not found["Bootstrap"].outdated
    assert found["PHP"].version == "8.3.1" and not found["PHP"].outdated
    assert found["nginx"].evidence == "header server"


def test_outdated_versions_are_flagged_with_their_reason():
    found = {d.name: d for d in detect("<div ng-app='shop'>", {"X-Powered-By": "PHP/7.4.33"},
                                       generators=["Joomla! - Open Source Content Management"],
                                       script_urls=["/js/jquery-1.11.0.min.js"])}
    assert found["PHP"].outdated and "8.1" in found["PHP"].why
    assert found["jQuery"].outdated and "CVE-2020-11022" in found["jQuery"].why
    assert found["AngularJS"].outdated                  # end of life, whatever the version
    assert found["Joomla"].version == "" and not found["Joomla"].outdated   # no version: no claim
    # A config can move a threshold.
    [php] = detect("", {"X-Powered-By": "PHP/8.2.10"}, thresholds={"php": "8.3"})
    assert php.outdated and php.why == "older than 8.3 (this run's threshold)"
    assert version_tuple("4.9.8") < version_tuple("6.4")


# --- page facts and checks ---------------------------------------------------------------

def test_page_facts_of_an_old_site():
    facts = C.page_facts(fixture("sites/old_bistro_home.html"), f"http://{BISTRO}/")
    assert facts.viewport is None and facts.generators == ["WordPress 4.9.8"]
    assert facts.copyright_year == 2017
    assert facts.internal_links == [f"http://{BISTRO}/menu/", f"http://{BISTRO}/gallery/",
                                    f"http://{BISTRO}/specials-2016/",
                                    f"http://{BISTRO}/contact-us/"]       # no PDF, no Facebook
    assert facts.contact_links == [f"http://{BISTRO}/contact-us/"]
    assert not facts.has_contact_form                                    # a search box isn't one
    assert not facts.booking and not facts.ordering


def test_contact_forms_booking_and_expectations():
    facts = C.page_facts(fixture("sites/modern_dental_home.html"), DENTAL)
    assert facts.has_contact_form and facts.booking and facts.copyright_year == 2026
    newsletter = C.page_facts(fixture("sites/old_bistro_contact.html"), f"http://{BISTRO}/c")
    assert not newsletter.has_contact_form                               # email-only signup
    embedded = C.page_facts("<iframe src='https://docs.google.com/forms/d/e/x/viewform'>",
                            "https://a.example/")
    assert embedded.has_contact_form
    assert C.expected_online("Dentists") == ("booking",)
    assert C.expected_online("Event caterers") == ("ordering", "booking")
    assert C.expected_online("Software company") == ()
    assert C.expected_online("Tattoo parlour", {"tattoo": ["booking"]}) == ("booking",)


def test_each_check_says_what_it_found_in_plain_words():
    facts = C.page_facts("<meta name='viewport' content='width=1024'>", "https://a.example/")
    mobile = C.check_mobile(facts)
    assert mobile.type == "not_mobile_friendly" and "width=1024" in mobile.text
    assert C.check_copyright(C.PageFacts(url="x", copyright_year=2021), NOW, 2).text == \
        "Stale copyright: the footer still says 2021, 5 years ago."
    assert C.check_copyright(C.PageFacts(url="x", copyright_year=2025), NOW, 2) is None
    links = C.check_broken_links({"https://a.example/x": "HTTP 404", "https://a.example/y": "ok"})
    assert links.text.startswith("1 broken link on the homepage (of 2 checked)")
    maps = C.check_maps_profile(Lead(business_name="A", gmaps_url="https://maps.google.com/?cid=9",
                                     gmaps_rating=4.1, gmaps_reviews=3), 10)
    assert maps.text == "Incomplete Maps profile: no website, no phone, no address, " \
                        "no reviews (only 3)."
    assert C.check_maps_profile(Lead(business_name="A"), 10) is None   # no listing: no claim
    assert C.audit_score(["no_https", "not_mobile_friendly", "no_https"]) == 65


# --- one site at a time ------------------------------------------------------------------

def test_a_modern_site_has_nothing_to_fix(tmp_path):
    result = auditor(sites(), tmp_path).audit(dental())
    assert result.status == "audited" and result.findings == [] and result.score == 100
    assert [d.name for d in result.stack] == ["Next.js"]
    assert result.summary(NOW)["status_text"] == "OK"


def test_an_old_site_gets_plain_findings_and_a_low_score(tmp_path):
    web = sites()
    result = auditor(web, tmp_path).audit(bistro())
    types = [f.type for f in result.findings]
    assert types == ["no_https", "not_mobile_friendly", "outdated_tech", "stale_copyright",
                     "no_contact_form", "no_online_booking", "broken_links"]
    text = {f.type: f.text for f in result.findings}
    assert "certificate is invalid (certificate has expired)" in text["no_https"]
    assert text["outdated_tech"] == "Outdated technology: WordPress 4.9.8, jQuery 1.12.4, " \
                                    "PHP 7.2.34, Apache 2.2.15."
    whys = {t["name"]: t["why"] for t in
            next(f for f in result.findings if f.type == "outdated_tech").data["technologies"]}
    assert "Dec 2025" in whys["PHP"] and "2017" in whys["Apache"]
    assert "2017" in text["stale_copyright"]
    assert text["no_online_booking"] == "No online booking or ordering: customers of " \
        "restaurants expect to book or order online, and the site offers no way to."
    assert f"http://{BISTRO}/specials-2016/ (HTTP 404)" in text["broken_links"]
    assert result.score == 100 - 20 - 15 - 15 - 5 - 5 - 10 - 8
    # The contact page was read (no form on the homepage) and the PDF never probed.
    assert f"http://{BISTRO}/contact-us/" in web.calls
    assert not any(url.endswith(".pdf") for url in web.calls)


def test_no_website_unreachable_blocked_and_robots(tmp_path):
    web = sites()
    audit = auditor(web, tmp_path)
    none = audit.audit(Lead(business_name="Corner Tailor", gmaps_url="https://maps.google.com/?c=2"))
    assert none.status == "no_website"
    assert [f.type for f in none.findings] == ["incomplete_maps_profile", "no_website"]
    social = audit.audit(Lead(business_name="Insta Cakes", website="https://instagram.com/x"))
    assert social.findings[0].text == "No website of its own (only instagram.com)."
    gone = audit.audit(Lead(business_name="Gone Bakery", website="gone-bakery.example"))
    assert gone.status == "unreachable" and gone.findings[0].type == "site_unreachable"
    assert "doesn't resolve" in gone.findings[0].text
    walled = audit.audit(Lead(business_name="Walled", website="walled.example"))
    assert walled.status == "blocked" and walled.findings == []          # refused: no claims
    private = audit.audit(Lead(business_name="Private", website="private.example"))
    assert private.status == "robots" and private.findings == []
    assert "https://private.example/" not in web.calls                   # never fetched
    reasons = [r["reason"] for r in audit.failures.records]
    assert {"site_unreachable", "blocked", "robots_disallowed"} <= set(reasons)


def test_unchecked_checks_say_nothing(tmp_path):
    result = auditor(sites(), tmp_path, checks=["https", "mobile"]).audit(bistro())
    assert [f.type for f in result.findings] == ["no_https", "not_mobile_friendly"]


def test_certificates_expiring_soon_and_invalid(tmp_path):
    soon = auditor(sites(), tmp_path, tls=cert(days=9.5)).audit(dental())
    assert [f.type for f in soon.findings] == ["cert_expiring"]
    assert "(9 days)" in soon.findings[0].text
    bad = auditor(sites(), tmp_path, tls=cert(valid=False, error="Hostname mismatch")) \
        .audit(dental())
    assert bad.findings[0].text == "No valid HTTPS: Hostname mismatch."


def test_pagespeed_scores_and_switches_off_on_quota(tmp_path):
    settings = DigitalAuditConfig().pagespeed.model_copy(update={"min_delay": 0, "max_delay": 0,
                                                                "max_per_run": 3})
    web = sites()
    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    web.add(endpoint, Page.of_json({"lighthouseResult": {
        "categories": {"performance": {"score": 0.31}},
        "audits": {"largest-contentful-paint": {"numericValue": 6400.0}}}}))
    kit = kit_for(web, tmp_path)
    speed = PageSpeed(kit.http, settings, kit.failures, key="test-key")
    result = auditor(web, tmp_path, pagespeed=speed).audit(dental())
    assert result.pagespeed == 31
    assert result.findings[0].text == "Slow site: PageSpeed 31/100 on mobile, largest paint " \
                                      "6.4 s (below 50)."
    assert any(c.startswith(endpoint) and "key=test-key" in c for c in web.calls)
    # Quota: off for the rest of the run, recorded once, and the key never in a record.
    web.add(endpoint, Page.of_json({"error": {"code": 429}}, status=429))
    assert speed.score(DENTAL) is None and speed.off_reason == "pagespeed_quota"
    calls = len(web.calls)
    assert speed.score(DENTAL) is None and len(web.calls) == calls        # no further call
    records = kit.failures.records
    assert [r["reason"] for r in records] == ["pagespeed_quota"]
    assert "test-key" not in repr(records)
    assert parse({"lighthouseResult": {}}) is None


def test_own_website_excludes_profiles_but_keeps_builder_sites():
    assert own_website(Lead(business_name="A", website="https://facebook.com/a")) is None
    assert own_website(Lead(business_name="A", website="https://a.wixsite.com/shop"))
    assert own_website(Lead(business_name="A", website="a.example")) == "https://a.example/"


# --- uploads -------------------------------------------------------------------------------

def test_targets_from_csv_xlsx_and_bare_lists(tmp_path):
    csv = b"Business Name,Website,Phone,Category,City\n" \
          b"Brightwater Dental,brightwater-dental.example,020 0000 0001,Dentists,Pune\n" \
          b",,,,\nNo Site Cafe,,,Cafe,Pune\nnot a business,not a url,,,\n"
    targets, skipped = parse_targets(csv, "list.csv")
    assert [t.name for t in targets] == ["Brightwater Dental", "No Site Cafe", "not a business"]
    assert targets[0].website == "brightwater-dental.example" and targets[0].place == "Pune"
    assert targets[2].website is None and skipped == 0
    bare, _ = parse_targets(b"oldmill-bistro.example\nhttps://a.example/x\nCorner Tailor\n",
                            "list.txt")
    assert [(t.name, t.website) for t in bare] == [("", "oldmill-bistro.example"),
                                                   ("", "https://a.example/x"),
                                                   ("Corner Tailor", None)]
    book = Workbook()
    book.active.append(["Domain"])
    book.active.append(["walled.example"])
    path = tmp_path / "list.xlsx"
    book.save(path)
    section = DigitalAuditConfig(domains=["walled.example", "b.example"],
                                 targets=[{"name": "Corner Tailor"}],
                                 targets_file=str(path))
    assert [(t.name, t.website) for t in load_targets(section)] == [
        ("Corner Tailor", None), ("", "walled.example"), ("", "b.example")]  # repeats dropped


def test_the_api_reads_an_upload_into_targets(tmp_path):
    from fastapi.testclient import TestClient

    from leadscraper.api.app import create_app
    from leadscraper.db.engine import open_database

    engine = open_database(f"sqlite:///{tmp_path / 'api.sqlite3'}")
    with TestClient(create_app(engine)) as client:
        response = client.post("/api/v1/audit/targets", files={"file": (
            "list.csv", b"name,website\nBrightwater Dental,brightwater-dental.example\n")})
        assert response.status_code == 200, response.text
        assert response.json() == {"targets": [{"name": "Brightwater Dental",
                                                "website": "brightwater-dental.example"}],
                                   "read": 1, "skipped": 0}
        bad = client.post("/api/v1/audit/targets", files={"file": ("list.pdf", b"%PDF")})
        assert bad.status_code == 422


# --- scoring and columns -----------------------------------------------------------------

def test_score_is_intent_from_the_findings_fading_with_age():
    fresh = [{"type": "no_https", "observed_at": datetime.now(timezone.utc)},
             {"type": "not_mobile_friendly", "observed_at": datetime.now(timezone.utc)}]
    parts = DIGITAL_AUDIT.score(None, fresh, None)
    assert parts.intent == pytest.approx(0.35, abs=1e-3) and parts.fit is None
    assert parts.reasons == ["digital audit 65/100: no https, not mobile-friendly"]
    old = [{**s, "observed_at": datetime.now(timezone.utc) - timedelta(days=180)} for s in fresh]
    assert DIGITAL_AUDIT.score(None, old, None).intent == pytest.approx(0.175, abs=1e-3)
    assert DIGITAL_AUDIT.score(None, [], None).intent == 0.0
    assert {s.type for s in DIGITAL_AUDIT.signal_specs()} == set(C.FINDINGS)


def test_depth_presets_scale_a_copy():
    section = DigitalAuditConfig()
    update, notes = DIGITAL_AUDIT.depth_changes(section, "quick")
    assert update["max_links"] == 3 and "pagespeed" not in update["checks"] and len(notes) == 2
    assert section.max_links == 8 and "pagespeed" in section.checks     # untouched
    assert DIGITAL_AUDIT.depth_changes(section, "balanced") == ({}, [])


# --- runs ------------------------------------------------------------------------------------

class SiteSource(Source):
    """local_business's discovery, offline: three businesses with websites."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Brightwater Dental Studio", website=DENTAL,
                             locality=locality.name, category="Dentists",
                             phones=["+912000000001"])
        yield self.make_lead(business_name="Old Mill Bistro", website=f"http://{BISTRO}/",
                             locality=locality.name, category="Restaurants",
                             phones=["+912000000002"])
        yield self.make_lead(business_name="Gone Bakery", website="gone-bakery.example",
                             locality=locality.name, category="Bakeries",
                             phones=["+912000000003"])


LOCAL = {"local_business": {"categories": [{"name": "Shops"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0}}}}
AUDIT = {"min_delay": 0, "max_delay": 0, "checks": [c for c in DigitalAuditConfig().checks
                                                    if c != "pagespeed"]}


@pytest.fixture
def offline(monkeypatch):
    web = sites()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr("leadscraper.net.tls.certificate", cert())
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": SiteSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
    return web


def run(tmp_path, monkeypatch, name: str, lead_types: dict):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    config = compile_run_config(RunConfig.model_validate({
        "name": "Audit test", "project": "audit-test",
        "geography": {"country": "IN", "places": ["Pune"]},
        "lead_types": lead_types,
        "runtime": {"db_url": f"sqlite:///{tmp_path / name / 'app.sqlite3'}"},
    }))
    return config, execute(RunRequest(config=config, install_signals=False))


def rows(config, model) -> list[dict]:
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        return [dict(r) for r in store._conn.execute(select(model.__table__)).mappings()]
    finally:
        store.close()


def workbook_records(config, run_id) -> tuple[list[str], dict[str, tuple]]:
    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    try:
        content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                                 run_id=run_id)[1]
    finally:
        store.close()
    sheet = load_workbook(io.BytesIO(content))["Records"]
    header = [c.value for c in sheet[1]]
    records = {r[header.index("Name")]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    return header, records


def test_a_run_audits_the_organisations_other_lead_types_found(tmp_path, monkeypatch, offline):
    config, outcome = run(tmp_path, monkeypatch, "both", {**LOCAL, "digital_audit": AUDIT})
    assert outcome.ok, outcome.results
    assert outcome.results["digital_audit"].stats["audited"] == 3
    orgs = {o["name"]: o for o in rows(config, Organization)}
    assert all(o["lead_types"] == ["local_business", "digital_audit"] for o in orgs.values())
    by_org: dict[str, set] = {}
    for s in rows(config, Signal):
        name = next(n for n, o in orgs.items() if o["id"] == s["organization_id"])
        by_org.setdefault(name, set()).add(s["type"])
        assert s["lead_type"] == "digital_audit" and s["key"] == s["type"]
    assert "Brightwater Dental Studio" not in by_org                   # nothing to fix
    assert by_org["Gone Bakery"] == {"site_unreachable"}
    assert {"no_https", "outdated_tech", "no_online_booking"} <= by_org["Old Mill Bistro"]
    audits = {a["organization_id"]: a["value"] for a in rows(config, OrgAttribute)
              if a["key"] == "audit"}
    assert audits[orgs["Old Mill Bistro"]["id"]]["score"] == 22
    assert audits[orgs["Brightwater Dental Studio"]["id"]]["findings"] == []
    # Evidence names the audit; the audit never touched a record.
    assert {e["source"] for e in rows(config, Evidence) if e["lead_type"] == "digital_audit"} \
        >= {"digital_audit", "rule:digital_audit"}

    # The workbook shows its columns, and intent ranks the audited sites.
    header, records = workbook_records(config, outcome.run_id)
    for column in ("Site", "Audit Score", "Audit Findings", "Site Stack", "Top Signal",
                   "Audited"):
        assert column in header, column
    assert records["Old Mill Bistro"][header.index("Audit Score")] == 22
    assert records["Brightwater Dental Studio"][header.index("Audit Findings")] == \
        "Nothing to fix"
    assert records["Gone Bakery"][header.index("Site")] == "Unreachable"
    assert "Next.js" in records["Brightwater Dental Studio"][header.index("Site Stack")]
    assert records["Brightwater Dental Studio"][header.index("Intent")] == 0
    assert records["Old Mill Bistro"][header.index("Intent")] > 70


def test_a_reaudit_replaces_findings_and_recent_audits_are_skipped(tmp_path, monkeypatch,
                                                                   offline):
    config, _ = run(tmp_path, monkeypatch, "again", {**LOCAL, "digital_audit": AUDIT})
    # Within reaudit_after_days nothing is fetched again.
    offline.calls.clear()
    _, second = run(tmp_path, monkeypatch, "again", {**LOCAL, "digital_audit": AUDIT})
    assert second.results["digital_audit"].stats["recent_skipped"] == 3
    assert not any("oldmill" in url for url in offline.calls)
    # The bistro fixes HTTPS; a re-audit drops that finding and keeps the rest.
    offline.add(f"https://{BISTRO}/", Page(fixture("sites/old_bistro_home.html")))
    offline.add(f"https://{BISTRO}/specials-2016/", Page(status=404))
    for path in ("menu/", "gallery/"):
        offline.add(f"https://{BISTRO}/{path}", Page("ok"))
    offline.add(f"https://{BISTRO}/contact-us/", Page(fixture("sites/old_bistro_contact.html")))
    _, third = run(tmp_path, monkeypatch, "again",
                   {**LOCAL, "digital_audit": {**AUDIT, "reaudit_after_days": 0}})
    assert third.ok
    bistro_id = next(o["id"] for o in rows(config, Organization) if o["name"] == "Old Mill Bistro")
    types = sorted(s["type"] for s in rows(config, Signal) if s["organization_id"] == bistro_id)
    assert "no_https" not in types and "outdated_tech" in types
    assert len(types) == len(set(types))                                  # refreshed, not added


def test_an_uploaded_list_alone_is_audited(tmp_path, monkeypatch, offline):
    config, outcome = run(tmp_path, monkeypatch, "uploads", {"digital_audit": {
        **AUDIT, "audit": "targets", "domains": ["brightwater-dental.example"],
        "targets": [{"name": "Old Mill Bistro", "website": BISTRO, "category": "Restaurant"}]}})
    assert outcome.ok, outcome.results
    result = outcome.results["digital_audit"]
    assert result.new == 2 and result.stats["audited"] == 2
    orgs = {o["name"]: o for o in rows(config, Organization)}
    assert set(orgs) == {"brightwater-dental.example", "Old Mill Bistro"}
    assert all(o["lead_types"] == ["digital_audit"] for o in orgs.values())


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


def test_enabling_or_disabling_digital_audit_leaves_local_business_unchanged(
        tmp_path, monkeypatch, offline):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)
    together, outcome = run(tmp_path, monkeypatch, "together", {**LOCAL, "digital_audit": AUDIT})
    off, _ = run(tmp_path, monkeypatch, "off", {**LOCAL, "digital_audit": {"enabled": False}})
    assert outcome.results["digital_audit"].stats["findings"] > 0
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline


def test_a_crashing_audit_is_contained(tmp_path, monkeypatch, offline):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self, org_id, result):
        raise RuntimeError("audit store exploded")

    monkeypatch.setattr("leadscraper.leadtypes.digital_audit.session.DigitalAuditSession.write",
                        boom)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "digital_audit": AUDIT})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert any(f["reason"] == "audit_not_stored" for f in rows(crashed, FailureRow))


def test_the_catalog_and_registry_know_it():
    assert registry.get("digital_audit") is DIGITAL_AUDIT
    assert DIGITAL_AUDIT.post_order > registry.get("hiring").post_order > 0
    names = {s["name"] for s in DIGITAL_AUDIT.catalog_sources()}
    assert names == {"upload", "site_audit", "pagespeed"}


# --- live (opt-in) -----------------------------------------------------------------------------

@pytest.mark.live
def test_live_audit_of_example_com(tmp_path):
    """One real, reserved site (IANA's example.com): reachable over HTTPS, a valid certificate."""
    from leadscraper.engine.failures import FailureLog
    from leadscraper.leadtypes.toolkit import Kit
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter
    from leadscraper.net.robots import RobotsCache

    limiter = RateLimiter()
    http = HttpClient(limiter, timeout=20, max_retries=1)
    kit = Kit(FailureLog(tmp_path / "f.jsonl"), limiter, http, RobotsCache(http))
    section = DigitalAuditConfig(checks=["unreachable", "https", "mobile", "cms_age"],
                                 min_delay=1, max_delay=2)
    result = Auditor(section, kit.http, kit.robots, kit.failures).audit(
        Lead(business_name="Example Domain", website="https://example.com/"))
    assert result.status == "audited", result.notes
    assert "no_https" not in {f.type for f in result.findings}


def test_an_upload_the_finders_hold_is_audited_with_its_details_but_not_rewritten(
        tmp_path, monkeypatch, offline):
    class NoSite(Source):
        name = "stub"

        def discover(self, locality, category) -> Iterator[Lead]:
            yield self.make_lead(business_name="Old Mill Bistro", locality=locality.name,
                                 category="Restaurants", phones=["+912000000002"])

    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": NoSite})
    config, outcome = run(tmp_path, monkeypatch, "matched", {**LOCAL, "digital_audit": {
        **AUDIT, "audit": "targets",
        "targets": [{"name": "Old Mill Bistro", "website": BISTRO, "phone": "+912000000002"}]}})
    assert outcome.ok
    [org] = rows(config, Organization)
    assert org["record"]["website"] is None and org["record"]["sources"] == ["stub"]
    assert org["lead_types"] == ["local_business", "digital_audit"]
    [audit] = [a["value"] for a in rows(config, OrgAttribute) if a["key"] == "audit"]
    assert audit["status"] == "audited" and "outdated_tech" in audit["types"]   # its upload's site
    assert outcome.results["digital_audit"].new == 0
