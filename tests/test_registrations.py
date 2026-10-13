"""registrations (Phase 7.2): registers, filters, directors, runs, isolation, missing keys.

All offline: Companies House and OpenCorporates answer from fictional fixtures in the
shape their documentation gives (no key was available here for a live 200: both
answered 401 without one on 2026-10-13), served by ``FakeWeb``. Dates are relative to
now. The ``live`` tests run only when a key is configured.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from openpyxl import load_workbook

from fake_web import (FakeWeb, Page, kit_for, local_business_view, patch_kit, probe_enricher,
                      project_rows, relative, run_database)
from leadscraper.config.compile import compile_run_config
from leadscraper.config.schema import RunConfig
from leadscraper.core.lead import Lead
from leadscraper.db.models import Evidence, FailureRow, Organization, Person, Signal, WorkUnitRow
from leadscraper.db.store import LeadStore, store_target
from leadscraper.engine.runner import RunRequest, execute
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod
from leadscraper.leadtypes.registrations.config import RegistrationsConfig
from leadscraper.leadtypes.registrations.plugin import (REGISTRATIONS, company_type,
                                                        incorporation_signals, weight)
from leadscraper.leadtypes.registrations.session import RegistrationsSession, plan_units
from leadscraper.packs import incorporation_sources_for
from leadscraper.packs.uk import companies_house as ch
from leadscraper.services import catalog, exports
from leadscraper.services.workspace import default_workspace
from leadscraper.sources.base import Source
from leadscraper.sources.registers import opencorporates as oc
from leadscraper.sources.registers.base import IncorporationQuery
from leadscraper.sources.registers.sic import describe, division

UTC = timezone.utc
SEARCH = f"{ch.API}/advanced-search/companies"
KEY_ENV = "LEADSCRAPER_SECRET_COMPANIES_HOUSE_API_KEY"


def officers_url(number: str) -> str:
    return f"{ch.API}/company/{number}/officers?items_per_page=50"


def _company(number, name, created, sic, locality="London", type_="ltd", line="1 Example Way"):
    return {"company_number": number, "company_name": name, "company_status": "active",
            "company_type": type_, "date_of_creation": f"@date:{created}",
            "kind": "search-results#company", "links": {"company_profile": f"/company/{number}"},
            "registered_office_address": {"address_line_1": line, "locality": locality,
                                          "postal_code": "EC1A 1AA", "country": "England"},
            "sic_codes": sic}


CH_SEARCH = {"hits": 5, "kind": "search#advanced-search", "items": [
    _company("15550001", "BRIGHTPATH SOFTWARE LTD", 5, ["62012"]),
    _company("15550002", "QUIET HOLDINGS LTD", 4, ["99999"]),
    _company("15550003", "ELMWOOD BAKERY LIMITED", 6, ["10710", "47240"]),
    _company("15550004", "OLDCO TRADING LTD", 60, ["62020"]),
    _company("OC555005", "HARBOUR & FINCH LLP", 9, ["69201"], type_="llp",
             locality="Croydon", line="2 Example Road, London")]}
CH_OFFICERS = {
    "15550001": {"items": [
        {"name": "OKAFOR, Ada", "officer_role": "director", "appointed_on": "@date:5"},
        {"name": "EXAMPLE SECRETARIES LIMITED", "officer_role": "corporate-secretary"},
        {"name": "MORGAN, Lewis", "officer_role": "director", "appointed_on": "@date:5",
         "resigned_on": "@date:1"}]},
    "15550003": {"items": [{"name": "WREN, Tobias James", "officer_role": "director"}]},
    "OC555005": {"items": [{"name": "FINCH, Harriet",
                            "officer_role": "llp-designated-member"}]}}

OC_SEARCH = {"api_version": "0.4", "results": {
    "page": 1, "per_page": 100, "total_pages": 1, "total_count": 1, "companies": [
        {"company": {"name": "NORDLICHT DIGITAL GMBH", "company_number": "HRB 999001",
                     "jurisdiction_code": "de", "incorporation_date": "@date:7",
                     "company_type": "GmbH", "current_status": "Active",
                     "registered_address_in_full": "Beispielstraße 1, 10115 Berlin",
                     "registered_address": {"locality": "Berlin", "postal_code": "10115",
                                            "country": "Germany"},
                     "industry_codes": [{"industry_code": {"code": "62.01",
                                                           "code_scheme_id": "eu_nace_2"}}],
                     "opencorporates_url":
                         "https://opencorporates.com/companies/de/HRB_999001"}}]}}


def register_web() -> FakeWeb:
    web = FakeWeb()
    web.add(SEARCH, Page(relative(json.dumps(CH_SEARCH)),
                         headers={"Content-Type": "application/json"}))
    for number, data in CH_OFFICERS.items():
        web.add(officers_url(number), Page(relative(json.dumps(data))))
    web.add(oc.API, Page(relative(json.dumps(OC_SEARCH))))
    return web


def query(**kw) -> IncorporationQuery:
    q = IncorporationQuery(country="GB", incorporated_from=date.today() - timedelta(days=30),
                           incorporated_to=date.today(), max_results=500, max_pages=2)
    for k, v in kw.items():
        setattr(q, k, v)
    return q


class Recorder(FakeWeb):
    """FakeWeb that remembers each GET's params and headers."""

    def __init__(self):
        super().__init__(register_web().routes)
        self.requests: list[tuple[str, dict, dict]] = []

    def get(self, url, *, rate_key=None, headers=None, params=None, **kwargs):
        self.requests.append((url, dict(params or {}), dict(headers or {})))
        return super().get(url, rate_key=rate_key, headers=headers, params=params, **kwargs)


# --- the registers ------------------------------------------------------------------------------

def test_companies_house_new_incorporations():
    web = Recorder()
    found = ch.CompaniesHouseIncorporations().search(web, query(), key="k-123", place="London")
    assert [c.number for c in found] == ["15550001", "15550002", "15550003", "15550004",
                                         "OC555005"]
    first = found[0]
    assert first.name == "BRIGHTPATH SOFTWARE LTD" and first.scheme == "uk_company"
    assert first.incorporated_on == date.today() - timedelta(days=5)
    assert first.address == "1 Example Way, London, EC1A 1AA" and first.locality == "London"
    assert first.url == ("https://find-and-update.company-information.service.gov.uk/company/"
                         "15550001")
    url, params, headers = web.requests[0]
    assert url == SEARCH and params["location"] == "London"
    assert params["company_status"] == "active" and "sic_codes" not in params
    assert params["incorporated_from"] == (date.today() - timedelta(days=30)).isoformat()
    assert headers["Authorization"].startswith("Basic ")                 # the key, as user
    # Five-digit codes go to the register; a shorter prefix is matched afterwards.
    ch.CompaniesHouseIncorporations().search(web, query(industry_codes=["62012", "62020"]),
                                             key="k-123")
    assert web.requests[-1][1]["sic_codes"] == "62012,62020"
    ch.CompaniesHouseIncorporations().search(web, query(industry_codes=["62"]), key="k-123")
    assert "sic_codes" not in web.requests[-1][1]
    officers = ch.CompaniesHouseIncorporations().officers(web, "15550001", key="k-123")
    assert [(o.name, o.role) for o in officers] == [("Ada Okafor", "Director")]
    with pytest.raises(PermissionError):
        ch.CompaniesHouseIncorporations().search(web, query(), key=None)


def test_companies_house_no_companies_is_404():
    web = register_web()
    web.add(SEARCH, Page(status=404))
    assert ch.CompaniesHouseIncorporations().search(web, query(), key="k") == []


def test_opencorporates_needs_a_token_and_never_leaks_it():
    import requests

    web = register_web()
    with pytest.raises(PermissionError):
        oc.OpenCorporatesIncorporations().search(web, query(country="DE"), key=None)
    [company] = oc.OpenCorporatesIncorporations().search(web, query(country="DE"), key="t-1",
                                                         place="Berlin")
    assert company.name == "NORDLICHT DIGITAL GMBH" and company.scheme == "oc_de"
    assert company.number == "HRB 999001" and company.industry_codes == ["62.01"]
    assert company.country == "DE" and company.locality == "Berlin"

    def refused(url):
        raise requests.HTTPError(f"401 for url: {url}?api_token=t-secret")
    web.add(oc.API, refused)
    with pytest.raises(RuntimeError) as error:
        oc.OpenCorporatesIncorporations().search(web, query(country="DE"), key="t-secret")
    assert "t-secret" not in str(error.value)


def test_industry_codes_read_as_words():
    assert describe("62012") == "62012 (computer programming and consultancy)"
    assert describe("99999") == "99999 (dormant company)"
    assert division("10710") == "food manufacturing"
    assert describe("ABC") == "ABC" and division("") == ""
    assert company_type("ltd") == "Private limited" and company_type("llp") == "LLP"
    assert company_type("community-interest-company") == "Community interest company"


# --- packs: where each register applies ---------------------------------------------------------

def _config(country: str, places=("London",), **section):
    return compile_run_config(RunConfig.model_validate({
        "name": "Registrations", "project": "registrations",
        "geography": {"country": country, "places": list(places)},
        "lead_types": {"registrations": section}}))


@pytest.mark.parametrize("country, registers, planned", [
    ("GB", ["companies_house", "opencorporates"], ["companies_house"]),
    ("IN", ["opencorporates"], []), ("US", ["opencorporates"], []),
    ("IE", ["opencorporates"], []), ("AU", ["opencorporates"], [])])
def test_registration_packs_stay_inactive_where_they_dont_apply(country, registers, planned):
    assert [cls.name for cls in incorporation_sources_for(country)] == registers
    config = _config(country)
    assert [u.source for u in plan_units(config, REGISTRATIONS.effective(config))] == planned
    by_name = {s["name"]: s for s in catalog.sources(country=country, with_health=False)
               if s["lead_type"] == "registrations"}
    assert by_name["companies_house"]["applicable"] is (country == "GB")
    assert by_name["opencorporates"]["applicable"] is True               # anywhere, opt-in


def test_units_per_place_and_opencorporates_opt_in():
    config = _config("GB", places=("London", "Leeds"),
                     sources={"opencorporates": {"enabled": True}})
    assert [(u.source, u.area, u.scope) for u in
            plan_units(config, REGISTRATIONS.effective(config))] == [
        ("companies_house", "London", "locality"), ("companies_house", "Leeds", "locality"),
        ("opencorporates", "London", "locality"), ("opencorporates", "Leeds", "locality")]
    country = _config("GB", places=("London",), scope="country")
    assert [(u.source, u.area) for u in plan_units(country, REGISTRATIONS.effective(country))] \
        == [("companies_house", None)]


def test_secrets_are_bound_to_their_register():
    with pytest.raises(ValueError):
        RegistrationsConfig.model_validate({"sources": {"companies_house": {
            "key_secret_name": "sam-gov-api-key"}}})
    with pytest.raises(ValueError):
        RegistrationsConfig.model_validate({"sources": {"opencorporates": {
            "token_secret_name": "companies-house-api-key"}}})


# --- the session: which companies count ---------------------------------------------------------

def session(tmp_path, *, web=None, places=("London",), **section) -> RegistrationsSession:
    config = _config("GB", places=places, **section)
    context = SimpleNamespace(config=config, store=None, run_id=None, dry_run=False)
    return RegistrationsSession(context, REGISTRATIONS.effective(config),
                                kit=kit_for(web or register_web(), tmp_path))


def test_which_companies_count(tmp_path):
    companies = {c.number: c for c in ch.CompaniesHouseIncorporations().search(
        register_web(), query(), key="k")}
    s = session(tmp_path)
    assert s.unwanted(companies["15550001"], "London") == ""
    assert s.unwanted(companies["15550002"], "London") == "skipped_dormant"
    assert s.unwanted(companies["15550003"], "London") == ""
    assert s.unwanted(companies["15550004"], "London") == "skipped_old"
    assert s.unwanted(companies["OC555005"], "London") == ""             # "…, London" address
    tech = session(tmp_path, industry_codes=["62"])
    assert tech.unwanted(companies["15550003"], "London") == "skipped_industry"
    assert tech.unwanted(companies["15550001"], "London") == ""
    llp = session(tmp_path, company_types=["llp"])
    assert llp.unwanted(companies["15550001"], "London") == "skipped_type"
    assert s.unwanted(companies["15550001"], "Leeds") == "skipped_place"


def test_a_missing_key_skips_the_register(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    s = session(tmp_path)
    reason = s.unit_skip_reason(s.units()[0])
    assert reason.startswith("key not configured: Companies House (UK) needs the secret "
                             "companies-house-api-key (leadscraper secrets set "
                             "companies-house-api-key); get one at "
                             "https://developer.company-information.service.gov.uk")
    monkeypatch.setenv(KEY_ENV, "k-123")
    assert session(tmp_path).unit_skip_reason(s.units()[0]) is None


# --- runs ---------------------------------------------------------------------------------------

def run(tmp_path, monkeypatch, name: str, lead_types: dict, *, country="GB",
        places=("London",), fields=None):
    (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path / name))
    document = {"name": "Registrations test",
                "project": f"registrations-test-{tmp_path.name}-{name}",
                "geography": {"country": country, "places": list(places)},
                "lead_types": lead_types, "runtime": {"db_url": run_database(tmp_path, name)}}
    if fields is not None:
        document["fields"] = {"include": fields}
    config = compile_run_config(RunConfig.model_validate(document))
    return config, execute(RunRequest(config=config, install_signals=False))


class FirmSource(Source):
    """local_business's discovery, offline: a bakery the register will name, and a café."""

    name = "stub"

    def discover(self, locality, category) -> Iterator[Lead]:
        yield self.make_lead(business_name="Elmwood Bakery", website="https://elmwood.example/",
                             locality=locality.name, category=category.name,
                             phones=["+442079460001"])
        yield self.make_lead(business_name="Corner Café", locality=locality.name,
                             category=category.name, phones=["+442079460002"])


LOCAL = {"local_business": {"categories": [{"name": "Shops"}],
                            "sources": {"stub": {"enabled": True, "min_delay": 0,
                                                 "max_delay": 0},
                                        "probe": {"enabled": True, "min_delay": 0,
                                                  "max_delay": 0}}}}
FAST = {"sources": {"companies_house": {"min_delay": 0.5, "max_delay": 0.5}}}


@pytest.fixture
def registers(monkeypatch):
    web = register_web()
    patch_kit(monkeypatch, web)
    monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", {"stub": FirmSource})
    monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {"probe": probe_enricher()})
    return web


def by_org(config, model, lead_type="registrations") -> dict[str, list[dict]]:
    names = {o["id"]: o["name"] for o in project_rows(config, Organization)}
    out: dict[str, list[dict]] = {}
    for row in project_rows(config, model):
        if row["lead_type"] == lead_type:
            out.setdefault(names[row["organization_id"]], []).append(row)
    return out


def test_a_run_turns_incorporations_into_signals_and_directors_into_people(
        tmp_path, monkeypatch, registers):
    monkeypatch.setenv(KEY_ENV, "k-123")
    config, outcome = run(tmp_path, monkeypatch, "ch", {**LOCAL, "registrations": FAST})
    assert outcome.ok, outcome.results
    signals = by_org(config, Signal)
    assert sorted(signals) == ["BRIGHTPATH SOFTWARE LTD", "Elmwood Bakery",
                               "HARBOUR & FINCH LLP"]
    bright = signals["BRIGHTPATH SOFTWARE LTD"][0]
    assert bright["type"] == "incorporation" and bright["key"] == "uk_company:15550001"
    assert bright["published_at"] is not None and bright["strength"] == 0.7
    assert bright["payload"]["industries"] == ["62012 (computer programming and consultancy)"]
    stats = outcome.results["registrations"].stats
    assert stats["skipped_dormant"] == 1 and stats["skipped_old"] == 1
    orgs = {o["name"]: o for o in project_rows(config, Organization)}
    assert orgs["BRIGHTPATH SOFTWARE LTD"]["record"]["registry_ids"] == \
        {"uk_company": "15550001"}
    assert orgs["BRIGHTPATH SOFTWARE LTD"]["record"]["locality"] == "London"
    # A company local_business already holds gets the signal and keeps its record (rule 6).
    assert "ELMWOOD BAKERY LIMITED" not in orgs
    assert orgs["Elmwood Bakery"]["lead_types"] == ["local_business", "registrations"]
    assert orgs["Elmwood Bakery"]["record"]["registry_ids"] == {}
    people = {name: [(p["name"], p["title"], p["source"]) for p in rows]
              for name, rows in by_org(config, Person).items()}
    assert people == {"BRIGHTPATH SOFTWARE LTD": [("Ada Okafor", "Director", "registry")],
                      "Elmwood Bakery": [("Tobias James Wren", "Director", "registry")],
                      "HARBOUR & FINCH LLP": [("Harriet Finch", "LLP designated member",
                                               "registry")]}
    assert "Lewis Morgan" not in json.dumps(people)                     # resigned

    target, kwargs = store_target(config)
    store = LeadStore(target, **kwargs)
    content = exports.export(store.engine, default_workspace(store.engine), "xlsx",
                             run_id=outcome.run_id)[1]
    store.close()
    sheet = load_workbook(io.BytesIO(content))["Records"]
    header = [c.value for c in sheet[1]]
    for column in ("Industry", "Incorporated", "Company No.", "Company Type", "Register Link",
                   "Industry Codes", "Register", "Decision Makers", "Intent"):
        assert column in header, column
    names = [r[header.index("Name")] for r in sheet.iter_rows(min_row=2, values_only=True)]
    row = names.index("BRIGHTPATH SOFTWARE LTD") + 2
    cell = lambda column: sheet.cell(row, header.index(column) + 1)  # noqa: E731
    assert cell("Industry").value == "Computer programming and consultancy"
    assert cell("Company No.").value == "15550001"
    assert cell("Company Type").value == "Private limited"
    assert cell("Decision Makers").value == "Ada Okafor (Director)"
    assert cell("Register Link").hyperlink.target.endswith("/company/15550001")

    # Next month: a director resigned. The register is a snapshot, so they go.
    resigned = json.loads(json.dumps(CH_OFFICERS["15550001"]))
    resigned["items"][0]["resigned_on"] = "@date:0"
    registers.add(officers_url("15550001"), Page(relative(json.dumps(resigned))))
    _, again = run(tmp_path, monkeypatch, "ch", {**LOCAL, "registrations": FAST})
    assert again.ok
    assert "BRIGHTPATH SOFTWARE LTD" not in by_org(config, Person)
    assert sum(len(v) for v in by_org(config, Signal).values()) == 3     # refreshed, not added


def test_without_decision_makers_in_fields_no_director_is_read(tmp_path, monkeypatch,
                                                               registers):
    monkeypatch.setenv(KEY_ENV, "k-123")
    config, outcome = run(tmp_path, monkeypatch, "fields", {"registrations": FAST},
                          fields=["phones", "website"])
    assert outcome.ok and by_org(config, Signal) and not by_org(config, Person)
    assert not [c for c in registers.calls if "/officers" in c]


def test_a_missing_key_is_a_skipped_unit_and_the_run_completes(tmp_path, monkeypatch,
                                                               registers):
    monkeypatch.delenv(KEY_ENV, raising=False)
    config, outcome = run(tmp_path, monkeypatch, "nokey", {**LOCAL, "registrations": FAST},
                          places=("London", "Leeds"))
    assert outcome.status == "completed"
    assert [k for k, _ in outcome.skipped] == ["registrations|companies_house|London|*",
                                               "registrations|companies_house|Leeds|*"]
    assert all(r.startswith("key not configured: Companies House") for _, r in outcome.skipped)
    units = [r for r in project_rows(config, WorkUnitRow) if r["run_id"] == outcome.run_id
             and r["lead_type"] == "registrations"]
    assert {r["status"] for r in units} == {"skipped"}
    failures = [f for f in project_rows(config, FailureRow) if f["source"] == "companies_house"]
    assert [f["reason"] for f in failures] == ["key_not_configured"]        # said once
    assert not [c for c in registers.calls if "company-information" in c]   # never asked
    assert by_org(config, Signal) == {}


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


def test_enabling_or_disabling_registrations_leaves_local_business_unchanged(
        tmp_path, monkeypatch, registers):
    monkeypatch.setenv(KEY_ENV, "k-123")
    alone, first = run(tmp_path, monkeypatch, "alone", LOCAL)
    view = local_business_view(first)
    together, outcome = run(tmp_path, monkeypatch, "together",
                            {**LOCAL, "registrations": FAST})
    assert local_business_view(outcome) == view       # Brightpath & co. are not its leads
    off, last = run(tmp_path, monkeypatch, "off",
                    {**LOCAL, "registrations": {"enabled": False}})
    assert local_business_view(last) == view
    assert outcome.results["registrations"].new == 2
    baseline = local_business_output(alone)
    assert baseline[0]
    assert local_business_output(together) == baseline
    assert local_business_output(off) == baseline


def test_a_crashing_register_is_contained(tmp_path, monkeypatch, registers):
    monkeypatch.setenv(KEY_ENV, "k-123")
    alone, _ = run(tmp_path, monkeypatch, "alone", LOCAL)

    def boom(self, http, query, *, key=None, place=None):
        raise RuntimeError("register exploded")

    monkeypatch.setattr(ch.CompaniesHouseIncorporations, "search", boom)
    crashed, outcome = run(tmp_path, monkeypatch, "crash", {**LOCAL, "registrations": FAST})
    assert outcome.status == "completed"
    assert local_business_output(crashed) == local_business_output(alone)
    assert any(f["reason"] == "fetch_failed" and "exploded" in (f["detail"] or "")
               for f in project_rows(crashed, FailureRow))


# --- scoring, columns, depth ---------------------------------------------------------------------

def incorporation(days_ago: float, **payload) -> dict:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    return {"type": "incorporation", "lead_type": "registrations", "title": "Incorporated",
            "strength": 0.7, "published_at": when, "observed_at": when,
            "source_url": "https://find-and-update.example/company/1",
            "payload": {"register": "companies_house", "number": "15550001",
                        "industry_codes": ["62012"], "company_type": "ltd", **payload}}


def test_score_fades_with_the_company_s_age():
    now = datetime.now(UTC)
    fresh, quarter, year = incorporation(1), incorporation(90), incorporation(400)
    assert abs(weight(fresh, now) - 0.7) < 0.01
    assert abs(weight(quarter, now) - 0.35) < 0.01                    # one half-life
    assert weight(year, now) == 0.0 and incorporation_signals([year], now) == []
    parts = REGISTRATIONS.score(None, [fresh], None, now=now)
    assert parts.intent == pytest.approx(0.6946, abs=0.01)
    assert parts.reasons == [
        f"newly incorporated ({(now - timedelta(days=1)):%Y-%m-%d}; 62012 (computer "
        "programming and consultancy); Companies House)"]
    assert REGISTRATIONS.score(None, [], None, now=now).intent == 0.0


def test_column_values_show_the_incorporation_even_once_it_has_faded():
    now = datetime.now(UTC)
    values = REGISTRATIONS.column_values(SimpleNamespace(as_of=now,
                                                         signals=[incorporation(400)]), None)
    assert values["industry"] == "Computer programming and consultancy"
    assert values["company_number"] == "15550001" and values["company_type"] == "Private limited"
    assert values["register"] == "Companies House"
    assert REGISTRATIONS.column_values(SimpleNamespace(as_of=now, signals=[]), None) == {}


def test_depth():
    assert REGISTRATIONS.depth_changes(RegistrationsConfig(), "quick") == \
        ({"directors": False}, ["registrations: no director lookups (quick)"])
    assert REGISTRATIONS.depth_changes(RegistrationsConfig(), "deep")[0] == \
        {"max_director_lookups": 500}
    assert REGISTRATIONS.depth_changes(RegistrationsConfig(), "balanced") == ({}, [])


# --- live (opt-in) -------------------------------------------------------------------------------

def _live_http():
    from leadscraper.net.http import HttpClient
    from leadscraper.net.ratelimit import RateLimiter

    return HttpClient(RateLimiter((0.6, 1.2)), max_retries=1)


@pytest.mark.live
def test_live_companies_house_incorporations():
    from leadscraper.leadtypes.toolkit import secret

    key = secret("companies-house-api-key")
    if not key:
        pytest.skip("no Companies House key: leadscraper secrets set companies-house-api-key")
    found = ch.CompaniesHouseIncorporations().search(
        _live_http(), query(max_results=20, max_pages=1), key=key, place="London")
    assert found and all(c.number and c.incorporated_on for c in found)


@pytest.mark.live
def test_live_opencorporates():
    from leadscraper.leadtypes.toolkit import secret

    token = secret("opencorporates-api-token")
    if not token:
        pytest.skip("no OpenCorporates token: leadscraper secrets set opencorporates-api-token")
    found = oc.OpenCorporatesIncorporations().search(_live_http(), query(max_pages=1),
                                                     key=token)
    assert all(c.number for c in found)
