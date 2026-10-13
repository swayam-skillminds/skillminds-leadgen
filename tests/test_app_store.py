"""app_store (Phase 8.2): Apple's Search API, Play listings, complaint themes, the
assessment, runs, own-site store links, isolation, scoring and columns.

All offline: fictional apps and developers (``tests/fixtures/apps``, reserved
``.example`` domains) in the shapes the iTunes Search API and a Play listing page had
when checked live on 2026-10-13, served by ``FakeWeb``. Dates are relative to now.
The ``live`` tests make one Search API call and read one Play listing.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from string import Template
from types import SimpleNamespace
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from openpyxl import load_workbook

from fake_web import (FakeWeb, Page, fixture, json_fixture, kit_for, local_business_view,
                      patch_kit, probe_enricher, project_rows, run_database)
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Evidence, FailureRow, Organization, Signal
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes.app_store.config import AppStoreConfig
from leadscraper.leadtypes.app_store.plugin import APP_STORE, app_signals, weight
from leadscraper.leadtypes.app_store.session import AppStoreSession, plan_units
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.services import exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.apps import google_play, itunes
from leadscraper.sources.apps.base import (AppListing, Review, apple_id, parse_app_ref,
                                           play_package, store_links)
from leadscraper.sources.apps.themes import complaints, themes, unplaced
from leadscraper.sources.base import Source

UTC = timezone.utc
TODAY = datetime.now(UTC).date()
SEARCH = "https://itunes.apple.com/search"
LOOKUP = "https://itunes.apple.com/lookup"
DETAILS = "https://play.google.com/store/apps/details"
REVIEW = Template('<div class="EGFGHd"><div class="YNR7H"><div class="gSGphe"><div class="X5PpBb">'
                  '$author</div></div><div><i class="google-material-icons">more_vert</i>'
                  '<span>Flag inappropriate</span></div></div><div class="Jx4nYe"><div '
                  'class="iXRFPc" aria-label="Rated $stars stars out of five stars" role="img">'
                  '</div><span class="bp9Aid">$date</span></div><div class="h3YV2d">$text</div>'
                  '</div>')


def day(days_ago: int) -> str:
    return f"{TODAY - timedelta(days=days_ago):%b %-d, %Y}"


def play_page(package: str, name: str, *, website: str, rating: str, count: int,
              installs: str, updated_days: int, reviews=(), email="", phone="",
              legal="", address="", category="MEDICAL", author=None) -> Page:
    html = Template(fixture("apps/play_listing.html")).substitute(
        name=name, package=package, description=f"{name}: a fictional app.",
        category=category, author=author or legal or name, website=website,
        rating=rating, count=count, count_text=f"{count:,}", installs=installs,
        updated=day(updated_days), dev_id="5550001", email=email,
        phone_compact=phone.replace(" ", ""), phone=phone, legal_name=legal,
        address=address.replace("\n", "<br>"),
        reviews="".join(REVIEW.substitute(author=a, stars=s, date=day(d), text=t)
                        for a, s, d, t in reviews))
    return Page(html)


SAHYADRI_REVIEWS = [
    ("Reviewer One", 1, 20, "The app crashes every time I try to book an appointment. Useless."),
    ("Reviewer Two", 2, 45, "OTP never arrives so I cannot log in. Please fix this soon."),
    ("Reviewer Three", 1, 60, "Terrible experience, nothing happens when I tap anything at all."),
    ("Reviewer Four", 5, 10, "Great doctors and friendly staff, highly recommended clinic."),
]
PLAY = {
    "com.sahyadricare.patient": play_page(
        "com.sahyadricare.patient", "Sahyadri Care", website="https://www.sahyadri-care.example",
        rating="2.1", count=1240, installs="10K+", updated_days=760, reviews=SAHYADRI_REVIEWS,
        email="care@sahyadri-care.example", phone="+91 20 4000 1234",
        legal="Sahyadri Care Pvt Ltd",
        address="12 FC Road,\nShivajinagar,\nPune, Maharashtra 411005\nIndia"),
    "com.healthtrack.pro": play_page(
        "com.healthtrack.pro", "HealthTrack", website="https://other-agency.example/",
        rating="4.6", count=9000, installs="500K+", updated_days=5),
    "com.banerbakes.order": play_page(
        "com.banerbakes.order", "Baner Bakes", website="https://appcrafters.example/",
        rating="4.0", count=210, installs="1K+", updated_days=1100, category="FOOD_AND_DRINK",
        legal="AppCrafters Studio"),
}


def play_route(url: str) -> Page:
    package = (parse_qs(urlsplit(url).query).get("id") or [""])[0]
    return PLAY.get(package) or Page("Not Found", status=404)


def lookup_route(url: str) -> Page:
    ids = (parse_qs(urlsplit(url).query).get("id") or [""])[0].split(",")
    data = json_fixture("apps/itunes_lookup.json")
    data["results"] = [r for r in data["results"] if str(r["trackId"]) in ids]
    data["resultCount"] = len(data["results"])
    return Page.of_json(data)


def app_web() -> FakeWeb:
    return FakeWeb({SEARCH: lambda url: Page.of_json(json_fixture("apps/itunes_search_clinic"
                                                                  ".json")),
                    LOOKUP: lookup_route, DETAILS: play_route,
                    "https://baner-bakes.example/": Page(fixture(
                        "sites/bakery_with_apps_home.html")),
                    "https://kothrud-dental.example/": Page("<html><title>Kothrud Dental"
                                                            "</title></html>")})


# --- the sources --------------------------------------------------------------------------------

def test_itunes_search_and_lookup_read_apps(tmp_path):
    web = app_web()
    found = itunes.search(web, "clinic", "IN", 50)
    assert [a.app_id for a in found] == ["1700000001", "1700000002", "1700000003",
                                         "1700000004", "1700000005", "1700000006"]  # no artist
    first = found[0]
    assert (first.name, first.developer, first.developer_website, first.bundle_id) == (
        "Sahyadri Clinic Appointments", "Sahyadri Care Pvt Ltd",
        "https://sahyadri-care.example/", "com.sahyadricare.patient")
    assert first.rating == 2.4 and first.ratings == 58 and first.genre == "Medical"
    assert first.url == "https://apps.apple.com/in/app/sahyadri-clinic-appointments/id1700000001"
    assert (TODAY - first.updated).days == 800
    assert web.calls[0] == ("https://itunes.apple.com/search?term=clinic&country=in"
                            "&entity=software&limit=50")
    unrated = itunes.parse({"wrapperType": "software", "trackId": 1, "trackName": "New app",
                            "averageUserRating": 0, "userRatingCount": 0})
    assert unrated.rating is None and unrated.ratings == 0     # live: Apple says 0, not "none"
    [baner] = itunes.lookup(web, ["1700000009"], "IN")
    assert baner.name == "Baner Bakes Order" and baner.developer == "AppCrafters Studio"


def test_a_play_listing_is_read_without_its_class_names():
    listing = google_play.parse(PLAY["com.sahyadricare.patient"].text,
                                "com.sahyadricare.patient")
    assert (listing.name, listing.rating, listing.ratings) == ("Sahyadri Care", 2.1, 1240)
    assert (listing.installs, listing.installs_min) == ("10K+", 10_000)
    assert (TODAY - listing.updated).days == 760
    assert listing.developer == "Sahyadri Care Pvt Ltd"
    assert listing.developer_website == "https://www.sahyadri-care.example"
    assert listing.developer_email == "care@sahyadri-care.example"
    assert listing.developer_phone == "+912040001234"
    assert listing.developer_address == "12 FC Road, Shivajinagar, Pune, Maharashtra 411005, " \
                                        "India"
    assert listing.genre == "Medical"
    assert [(r.rating, r.text[:20]) for r in listing.reviews] == [
        (1, "The app crashes ever"), (2, "OTP never arrives so"), (1, "Terrible experience,"),
        (5, "Great doctors and fr")]
    assert "Reviewer" not in json.dumps([r.text for r in listing.reviews])   # never names
    assert listing.url == "https://play.google.com/store/apps/details?id=com.sahyadricare.patient"
    assert google_play.parse("<html><body>Not an app</body></html>", "x.y") is None
    assert google_play.fetch(app_web(), "com.unknown.app", "in") is None     # 404
    assert google_play.installs_min("1,000+") == 1000 and google_play.installs_min("5M+") == \
        5_000_000


def test_store_links_and_app_refs():
    links = store_links(["https://play.google.com/store/apps/details?id=com.a.b&hl=en",
                         "https://apps.apple.com/in/app/name/id1234567890",
                         "https://play.google.com/store/apps/details?id=com.a.b",
                         "https://play.google.com/store/search?q=x",
                         "https://example.com/id1234567"])
    assert links == [("google_play", "com.a.b"), ("app_store", "1234567890")]
    assert apple_id("https://itunes.apple.com/us/app/x/id55555555?mt=8") == "55555555"
    assert play_package("https://play.google.com/store/apps/details?id=not a package") is None
    assert parse_app_ref("id1700000001") == ("app_store", "1700000001")
    assert parse_app_ref("com.example.app") == ("google_play", "com.example.app")
    assert parse_app_ref("hello") is None
    with pytest.raises(ValueError, match="a store link"):
        AppStoreConfig(apps=["hello"])
    with pytest.raises(ValueError, match="installs.min"):
        AppStoreConfig(installs={"min": 10, "max": 5})


def test_complaint_themes_by_rule():
    reviews = [Review(r[1], r[3]) for r in SAHYADRI_REVIEWS]
    found = themes(reviews)
    assert [(t.key, t.count) for t in found] == [("crashes", 1), ("login", 1)]
    assert found[0].quote.startswith("The app crashes every time")
    assert len(complaints(reviews)) == 3                       # the 5-star one isn't read
    assert [r.text[:8] for r in unplaced(reviews)] == ["Terrible"]
    assert themes([Review(4, "It crashes sometimes but fine")]) == []       # not a complaint


# --- the assessment -------------------------------------------------------------------------------

def _config(lead_types: dict, places=("Pune",), country="IN"):
    return compile_run_config(RunConfig.model_validate({
        "name": "t", "project": "t", "geography": {"country": country, "places": list(places)},
        "lead_types": lead_types}))


def session(tmp_path, *, web=None, ai=None, **section) -> AppStoreSession:
    config = _config({"app_store": section})
    context = SimpleNamespace(config=config, store=None, ai=ai)
    return AppStoreSession(context, APP_STORE.effective(config),
                           kit=kit_for(web or app_web(), tmp_path))


def listing(**kw) -> AppListing:
    base = dict(store="app_store", app_id="1", name="Acme", url="https://apps.apple.com/x/id1",
                rating=4.5, ratings=100, updated=TODAY - timedelta(days=30), genre="Business")
    return AppListing(**{**base, **kw})


def test_assessment(tmp_path):
    s = session(tmp_path)
    assert s.assess(listing()) == []
    [low] = s.assess(listing(rating=2.0, ratings=50))
    assert low["type"] == "low_rating" and low["strength"] == pytest.approx(0.671, abs=0.001)
    assert low["title"] == "Acme (App Store): rated 2.0 from 50 ratings"
    assert s.assess(listing(rating=2.0, ratings=4)) == []                 # too few ratings
    [stale] = s.assess(listing(updated=TODAY - timedelta(days=37 * 30.44)))
    assert stale["type"] == "stale_app" and stale["payload"]["months_since_update"] == 36  # floored
    assert stale["strength"] == pytest.approx(0.65, abs=0.01)
    play = google_play.parse(PLAY["com.sahyadricare.patient"].text, "com.sahyadricare.patient")
    types = {f["type"]: f for f in s.assess(play)}
    assert set(types) == {"low_rating", "stale_app", "review_complaints"}
    payload = types["review_complaints"]["payload"]
    assert [t["label"] for t in payload["themes"]] == ["crashes / won't open", "login / OTP"]
    assert payload["low_star_reviews"] == 3
    assert session(tmp_path, min_complaints=3).complaint_themes(play) == []
    # The same complaints on an app rated well overall count for less.
    play.rating = 4.5
    rated = {f["type"]: f for f in s.assess(play)}
    assert rated["review_complaints"]["strength"] == pytest.approx(0.65 / 3, abs=0.01)


def test_which_apps_count(tmp_path):
    s = session(tmp_path, genres=["Medical"], installs={"max": 100_000}, scope="place")
    assert s.unwanted(listing(genre="Games")) == "skipped_genre"           # excluded by default
    assert s.unwanted(listing(genre="Business")) == "skipped_genre"        # not a wanted genre
    assert s.unwanted(listing(genre="Medical", installs_min=1_000_000)) == "skipped_installs"
    assert s.unwanted(listing(genre="Medical")) == ""
    assert s.out_of_scope([listing(description="Clinics across India")]) == "skipped_place"
    assert s.out_of_scope([listing(description="Clinics across Pune")]) == ""
    # country (the default): evidence the developer is in the run's country.
    s = session(tmp_path)
    assert s.out_of_scope([listing()]) == "skipped_country"              # a storefront lists all
    assert s.out_of_scope([listing(description="Made in India")]) == ""
    assert s.out_of_scope([listing(developer_website="https://acme.co.in/")]) == ""
    assert s.out_of_scope([listing(), listing(store="google_play",
                                              developer_address="Baner, Pune 411045")]) == ""
    assert session(tmp_path, scope="storefront").out_of_scope([listing()]) == ""
    # National brands are left out (live: Subway, Rappi); a run can lift the cap.
    assert s.too_big([listing(ratings=2_372_879)]) == "skipped_size"
    assert session(tmp_path, max_ratings=None).too_big([listing(ratings=2_372_879)]) == ""


def test_ai_places_reviews_the_rules_cannot_and_must_quote_them(tmp_path):
    class FakeAI:
        def uses(self, task):
            return task == "signals"

        def call(self, task, *, system, user, output):
            items = json.loads(user.split("Reviews:\n", 1)[1])
            data = output.model_validate({"items": [
                {"id": items[0]["id"], "themes": ["usability"],
                 "evidence": "nothing happens when I tap"},
                {"id": "999", "themes": ["crashes"], "evidence": "invented"}]})
            return SimpleNamespace(data=data, provider="fake", model="m")

    play = google_play.parse(PLAY["com.sahyadricare.patient"].text, "com.sahyadricare.patient")
    s = session(tmp_path, ai=FakeAI(), min_complaints=3)
    found = s.complaint_themes(play)
    assert [t.key for t in found] == ["crashes", "login", "usability"]
    assert found[-1].quote == "nothing happens when I tap" and s.stats["ai_themes"] == 1

    class Inventing(FakeAI):
        def call(self, task, *, system, user, output):
            items = json.loads(user.split("Reviews:\n", 1)[1])
            return SimpleNamespace(data=output.model_validate({"items": [
                {"id": items[0]["id"], "themes": ["ads"], "evidence": "too many ads here"}]}),
                provider="fake", model="m")

    assert session(tmp_path, ai=Inventing(), min_complaints=3).complaint_themes(play) == []


def test_plan_units():
    section = AppStoreConfig(keywords=["clinic", "dentist"], apps=["com.example.app"])
    assert [u.key for u in plan_units(section)] == ["app_store|itunes|*|clinic",
                                                    "app_store|itunes|*|dentist",
                                                    "app_store|listed|*|*"]
    assert plan_units(AppStoreConfig()) == []                  # site links only (post)
    config = _config({"app_store": {"keywords": ["clinic"]}})
    assert APP_STORE.sources(config) == {"discovery": ["itunes"],
                                         "enrichment": ["site_apps", "google_play"]}


# --- runs ----------------------------------------------------------------------------------------

class FirmSource(Source):
    """local_business's discovery, offline: a dentist whose app is on the App Store, a
    bakery whose site links its apps, and a café without a site."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        for name, site, phone in (("Kothrud Dental Care", "https://kothrud-dental.example/",
                                   "+912025550201"),
                                  ("Baner Bakes", "https://baner-bakes.example/",
                                   "+912025550202"),
                                  ("Corner Café", None, "+912025550203")):
            yield self.make_lead(business_name=name, website=site, locality=locality.name,
                                 category=category.name, phones=[phone])


LOCAL = {"local_business": {"categories": [{"name": "Shops"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0},
                                        "probe": {"enabled": True, "min_delay": 0,
                                                  "max_delay": 0}}}}
FAST = {"keywords": ["clinic"], "min_delay": 0, "max_delay": 0,
        "sources": {"itunes": {"min_delay": 3.0, "max_delay": 3.0},
                    "google_play": {"min_delay": 1.0, "max_delay": 1.0}}}


@pytest.fixture
def stores(monkeypatch):
    web = app_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": FirmSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"probe": probe_enricher()})
    return web


def run(tmp_path, monkeypatch, name: str, lead_types: dict, *, places=("Pune",)):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    document = {"name": "App store test", "project": f"appstore-{tmp_path.name}-{name}",
                "geography": {"country": "IN", "places": list(places)},
                "lead_types": lead_types, "runtime": {"db_url": run_database(tmp_path, name)}}
    config = compile_run_config(RunConfig.model_validate(document))
    return config, execute(RunRequest(config=config, install_signals=False))


def by_org(config) -> dict[str, list[tuple[str, str]]]:
    names = {o["id"]: o["name"] for o in project_rows(config, Organization)}
    out: dict[str, list[tuple[str, str]]] = {}
    for row in project_rows(config, Signal):
        if row["lead_type"] == "app_store":
            out.setdefault(names[row["organization_id"]], []).append((row["type"], row["key"]))
    return {k: sorted(v) for k, v in out.items()}


def test_a_run_finds_neglected_apps_and_their_developers(tmp_path, monkeypatch, stores):
    config, outcome = run(tmp_path, monkeypatch, "run", {**LOCAL, "app_store": FAST})
    assert outcome.ok, outcome.results
    assert by_org(config) == {
        # Its iOS app, and the Play twin its bundle id found (same developer website).
        "Sahyadri Care Pvt Ltd": [
            ("low_rating", "app_store:1700000001:low_rating"),
            ("low_rating", "google_play:com.sahyadricare.patient:low_rating"),
            ("review_complaints", "google_play:com.sahyadricare.patient:review_complaints"),
            ("stale_app", "app_store:1700000001:stale_app"),
            ("stale_app", "google_play:com.sahyadricare.patient:stale_app")],
        # local_business holds it (same website): the signal attaches, the record stays.
        "Kothrud Dental Care": [("low_rating", "app_store:1700000002:low_rating")],
        # Its site links its apps: they are its apps, whoever built them.
        "Baner Bakes": [("low_rating", "app_store:1700000009:low_rating"),
                        ("stale_app", "google_play:com.banerbakes.order:stale_app")],
        # Too few ratings to judge, but two years without an update; no website.
        "Pune Physio LLP": [("stale_app", "app_store:1700000005:stale_app")]}
    stats = outcome.results["app_store"].stats
    assert stats["skipped_genre"] == 1 and stats["healthy"] == 1 and stats["play_matched"] == 1
    assert stats["skipped_country"] == 1                 # Lakeside: nothing says India
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    sahyadri = orgs["Sahyadri Care Pvt Ltd"]["record"]
    # The developer's own site, and the contacts the Play listing publishes for it.
    assert sahyadri["website"] == "https://sahyadri-care.example/"
    assert sahyadri["emails"] == ["care@sahyadri-care.example"]
    assert sahyadri["phones"] == ["+912040001234"]
    assert sahyadri["sources"] == ["app_store", "google_play"]
    assert orgs["Kothrud Dental Care"]["record"]["sources"] == ["stub"]       # rule 6
    assert orgs["Kothrud Dental Care"]["lead_types"] == ["local_business", "app_store"]
    assert "HealthTrack Pro" not in json.dumps(list(orgs))                    # healthy
    assert not any("search" in c and "play.google.com" in c for c in stores.calls)

    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                             run_id=outcome.run_id)[1]
    store.close()
    sheet = load_workbook(io.BytesIO(content))["Records"]
    header = [c.value for c in sheet[1]]
    for column in ("App Issue", "App", "App Store", "App Rating", "App Ratings", "Installs",
                   "App Updated", "App Link", "App Complaints", "Flagged Apps",
                   "App Developer", "Intent"):
        assert column in header, column
    rows = {r[header.index("Name")]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    row = rows["Sahyadri Care Pvt Ltd"]
    assert row[header.index("App Issue")] in ("Low rating", "Not updated")
    assert row[header.index("App Store")] in ("App Store, Google Play", "Google Play, App Store")
    assert "crashes / won't open (1)" in row[header.index("App Complaints")]
    assert "(+1 more)" in row[header.index("App")]
    dental = rows["Kothrud Dental Care"]
    assert dental[header.index("App Rating")] == 3.1 and dental[header.index("App Ratings")] == 12

    # Next run: the dentist's app was fixed. Its signal goes; nothing is duplicated.
    healthy = json_fixture("apps/itunes_search_clinic.json")
    healthy["results"][1].update(averageUserRating=4.8, userRatingCount=40)
    stores.add(SEARCH, Page.of_json(healthy))
    _, again = run(tmp_path, monkeypatch, "run", {**LOCAL, "app_store": FAST})
    assert again.ok
    after = by_org(config)
    assert "Kothrud Dental Care" not in after
    assert len(after["Sahyadri Care Pvt Ltd"]) == 5
    assert sum(len(v) for v in after.values()) == 8


def test_listed_apps_and_no_sites(tmp_path, monkeypatch, stores):
    config, outcome = run(tmp_path, monkeypatch, "listed", {"app_store": {
        "apps": ["https://apps.apple.com/in/app/baner-bakes-order/id1700000009",
                 "com.sahyadricare.patient", "com.not.listed"],
        "check_sites": "none", "min_delay": 0, "max_delay": 0}})
    assert outcome.ok
    assert sorted(by_org(config)) == ["AppCrafters Studio", "Sahyadri Care Pvt Ltd"]


def test_a_failing_store_is_recorded_and_the_run_completes(tmp_path, monkeypatch, stores):
    stores.add(SEARCH, Page("Too many requests", status=429))
    config, outcome = run(tmp_path, monkeypatch, "blocked", {**LOCAL, "app_store": FAST})
    assert outcome.status == "completed"
    assert any(f["source"] == "itunes" and f["reason"] == "blocked"
               for f in project_rows(config, FailureRow))
    assert "Baner Bakes" in by_org(config)                    # the site links still work


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


def test_enabling_or_disabling_app_store_leaves_local_business_unchanged(
        tmp_path, monkeypatch, stores):
    alone, first = run(tmp_path, monkeypatch, "alone", LOCAL)
    view = local_business_view(first)
    together, outcome = run(tmp_path, monkeypatch, "together", {**LOCAL, "app_store": FAST})
    assert local_business_view(outcome) == view        # Sahyadri & co. are not its leads
    off, last = run(tmp_path, monkeypatch, "off", {**LOCAL, "app_store": {"enabled": False}})
    assert local_business_view(last) == view
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline
    assert outcome.results["app_store"].new == 2          # Sahyadri, Pune Physio


def test_a_crashing_assessment_is_contained(tmp_path, monkeypatch, stores):
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self, listing):
        raise RuntimeError("assessment exploded")

    monkeypatch.setattr(AppStoreSession, "assess", boom)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "app_store": FAST})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert any(f["reason"] == "unit_failed" and "exploded" in (f["detail"] or "")
               for f in project_rows(crashed, FailureRow))


# --- scoring, columns, depth ---------------------------------------------------------------------

def app_signal(type_: str, days_ago: float, strength: float, **payload) -> dict:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    base = {"store": "google_play", "app_id": "com.a.b", "app": "Acme", "rating": 2.0,
            "ratings": 300, "installs": "10K+", "updated": (date.today() - timedelta(days=700))
            .isoformat(), "url": "https://play.google.com/store/apps/details?id=com.a.b",
            "developer": "Acme Ltd"}
    return {"type": type_, "lead_type": "app_store", "title": f"Acme: {type_}",
            "strength": strength, "observed_at": when, "source_url": base["url"],
            "payload": {**base, **payload}}


def test_score_takes_the_worst_finding_and_fades():
    now = datetime.now(UTC)
    low, stale = app_signal("low_rating", 0, 0.67), app_signal("stale_app", 0, 0.6)
    parts = APP_STORE.score(None, [stale, low], None, now=now)
    assert parts.intent == pytest.approx(0.72, abs=0.01)                 # 0.67 + 0.05
    assert parts.reasons == ["Acme: low_rating", "Acme: stale_app"]
    assert weight(app_signal("low_rating", 120, 0.6), now) == pytest.approx(0.3, abs=0.01)
    assert APP_STORE.score(None, [], None, now=now).intent == 0.0
    assert app_signals([{"type": "other"}], now) == []


def test_column_values():
    now = datetime.now(UTC)
    org = SimpleNamespace(as_of=now, signals=[
        app_signal("stale_app", 1, 0.6),
        app_signal("review_complaints", 1, 0.55, themes=[
            {"theme": "crashes", "label": "crashes / won't open", "reviews": 2,
             "quote": "it crashes"}]),
        app_signal("low_rating", 1, 0.5, store="app_store", app_id="9", app="Acme iOS",
                   url="https://apps.apple.com/in/app/acme/id9", rating=2.9, ratings=40,
                   installs=None)])
    values = APP_STORE.column_values(org, None)
    assert values["app_issue"] == "Not updated" and values["app"] == "Acme (+1 more)"
    assert values["app_store"] == "Google Play, App Store"
    assert values["app_rating"] == 2.0 and values["installs"] == "10K+"
    assert values["app_complaints"] == 'crashes / won\'t open (2): "it crashes"'
    assert values["flagged_apps"] == "Acme (Google Play); Acme iOS (App Store)"
    assert values["app_updated"].date() == date.today() - timedelta(days=700)
    assert values["app_updated"].hour == 12            # a calendar date: noon UTC keeps the day
    assert APP_STORE.column_values(SimpleNamespace(as_of=now, signals=[]), None) == {}


def test_depth():
    update, notes = APP_STORE.depth_changes(AppStoreConfig(), "quick")
    assert update["sources"].itunes.max_results == 25 and update["max_sites"] == 50
    assert APP_STORE.depth_changes(AppStoreConfig(), "deep")[0]["sources"].itunes.max_results \
        == 200
    assert APP_STORE.depth_changes(AppStoreConfig(), "balanced") == ({}, [])


# --- live (opt-in) -------------------------------------------------------------------------------

def _live_http():
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    return HttpClient(RateLimiter((3.5, 5.0)), max_retries=1, public_only=True)


@pytest.mark.live
def test_live_itunes_search():
    found = itunes.search(_live_http(), "clinic", "IN", 10)
    assert found and all(a.app_id and a.name and a.url for a in found)
    assert any(a.updated for a in found) and any(a.rating for a in found)


@pytest.mark.live
def test_live_google_play_listing():
    listing = google_play.fetch(_live_http(), "com.google.android.apps.maps", "in")
    assert listing is not None and listing.name
    assert listing.rating and listing.ratings and listing.installs_min
    assert listing.updated and listing.developer


def test_run_py_exits_0_for_a_completed_run_without_local_business(tmp_path, monkeypatch,
                                                                    stores):
    """Before Phase 8, run.py exited 1 whenever local_business wasn't in the run."""
    import yaml

    from leadscraper.cli import run_cmd

    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / "cli"))
    path = tmp_path / "apps.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": 1, "name": "Apps only", "project": f"apps-only-{tmp_path.name}",
        "geography": {"country": "IN", "places": ["Pune"]},
        "lead_types": {"local_business": {"enabled": False}, "app_store": FAST},
        "runtime": {"db_url": run_database(tmp_path, "cli")}}), encoding="utf-8")
    assert run_cmd.main(["--config", str(path), "--no-sheets"]) == 0
