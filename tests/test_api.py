"""Phase 3: the HTTP API, end to end. All offline.

The end-to-end test drives what a UI will: save a config, estimate it, queue
a run, let a real worker execute it (local_business with clocked fixture
sources -- no network), follow its Server-Sent Events, then export it in
every format and compare it with a re-run.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leadscraper.api import create_app  # noqa: E402
from leadscraper.core.lead import Lead  # noqa: E402
from leadscraper.engine.clock import FakeClock  # noqa: E402
from leadscraper.engine.worker import Worker  # noqa: E402
from leadscraper.leadtypes.local_business import pipeline as pipeline_mod  # noqa: E402
from leadscraper.sources.base import Source  # noqa: E402

CLOCK = FakeClock()
PREFIX = "/api/v1"


def clocked_source(name: str, leads: int, host: str):
    """A discovery source that yields `leads` businesses per combination, no network."""
    def discover(self, locality, category) -> Iterator[Lead]:
        for i in range(leads):
            CLOCK.sleep(5)
            digits = sum(ord(c) for c in f"{locality.name}{category.name}") * 100 + i
            yield self.make_lead(business_name=f"{category.name} House {locality.name} {i}",
                                 locality=locality.name, category=category.name,
                                 phones=[f"+9198{digits % 10**8:08d}"],
                                 website=f"https://{category.name.lower()}-{i}-"
                                         f"{locality.name.lower()}.example.in")

    return type(f"Fixture_{name}", (Source,), {
        "name": name, "discover": discover, "rate_keys": lambda self: [host]})


@pytest.fixture
def fixture_sources(monkeypatch):
    from leadscraper.sources import registry as source_registry

    def install(registry: dict) -> dict:
        monkeypatch.setattr(pipeline_mod, "DISCOVERY_SOURCES", registry)
        monkeypatch.setattr(pipeline_mod, "ENRICHMENT_SOURCES", {})
        monkeypatch.setattr(source_registry, "DISCOVERY_SOURCES", registry)
        return registry
    return install


def config_document(name: str = "API dentists", sources=("google_maps",)) -> dict:
    return {
        "schema_version": 1, "name": name, "project": name,
        "geography": {"country": "IN", "places": ["Pune"], "center": [18.52, 73.85],
                      "admin_area": "Maharashtra",
                      "areas": [{"name": "Baner"}, {"name": "Kothrud"}]},
        "lead_types": {"local_business": {
            "categories": [{"name": "Dentists"}],
            "sources": {n: {"enabled": True, "min_delay": 0, "max_delay": 0} for n in sources}}},
        "fields": {"include": ["phones", "website"]},
        "runtime": {"heartbeat_seconds": 0.05},
    }


@pytest.fixture
def engine():
    from app_db import fresh_application_db

    engine = fresh_application_db()
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as client:
        yield client


def run_worker(engine) -> Worker:
    worker = Worker(engine=engine, install_signals=False, poll_seconds=0.05,
                    heartbeat_seconds=0.05)
    worker.run(once=True)
    return worker


# --- OpenAPI ------------------------------------------------------------------------------------

def _has_example(spec: dict, node: dict) -> bool:
    if "example" in node or "examples" in node:
        return True
    schema = node.get("schema") or node
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        component = spec["components"]["schemas"][name]
        return "examples" in component or "example" in component
    if schema.get("type") == "array" and "items" in schema:
        return _has_example(spec, {"schema": schema["items"]})
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            return any(_has_example(spec, {"schema": s}) for s in schema[key]
                       if s.get("type") != "null")
    return False


def test_openapi_covers_every_endpoint_with_examples(client):
    spec = client.get(f"{PREFIX}/openapi.json").json()
    operations = [(path, method, op) for path, item in spec["paths"].items()
                  for method, op in item.items()]
    assert len(operations) >= 45
    missing = []
    for path, method, op in operations:
        assert path.startswith(PREFIX), path
        assert op.get("summary") and op.get("tags"), f"{method} {path}"
        success = {code: r for code, r in op["responses"].items() if code.startswith("2")}
        assert success, f"{method} {path} documents no success response"
        for code, response in success.items():
            if code == "204":
                continue
            content = response.get("content") or {}
            if not content or not all(_has_example(spec, media) for media in content.values()):
                missing.append(f"{method.upper()} {path} -> {code}")
        body = (op.get("requestBody") or {}).get("content") or {}
        for media_type, media in body.items():
            if media_type == "application/json" and not _has_example(spec, media):
                missing.append(f"{method.upper()} {path} request body")
    assert not missing, "no example for:\n" + "\n".join(missing)
    # The paths a UI needs, as Phase 3.1 lists them.
    for needed in ("/health", "/schema/run-config", "/catalog/lead-types", "/catalog/sources",
                   "/estimate", "/preview", "/projects", "/configs", "/configs/{config_id}/clone",
                   "/runs", "/runs/{run_id}/pause", "/runs/{run_id}/resume",
                   "/runs/{run_id}/cancel", "/runs/{run_id}/rerun", "/runs/{run_id}/events",
                   "/runs/{run_id}/failures", "/runs/{run_id}/diff", "/runs/{run_id}/export.{fmt}",
                   "/runs/{run_id}/export/google-sheets", "/organizations",
                   "/organizations/{org_id}", "/organizations/erase", "/suppression-lists",
                   "/suppression-lists/{list_id}/import", "/schedules", "/notifications",
                   "/settings", "/bundle/export", "/bundle/import"):
        assert PREFIX + needed in spec["paths"], needed


def test_errors_are_json_with_the_right_status(client):
    assert client.get(f"{PREFIX}/runs/999").status_code == 404
    assert client.get(f"{PREFIX}/runs/999").json()["detail"] == "no run 999"
    bad = client.post(f"{PREFIX}/configs", json={"document": {"schema_version": 1, "name": "x"}})
    assert bad.status_code == 422 and bad.json()["errors"]
    assert client.post(f"{PREFIX}/projects", json={"name": "P"}).status_code == 201
    assert client.post(f"{PREFIX}/projects", json={"name": "p"}).status_code == 409
    assert client.get(f"{PREFIX}/projects", params={"sort": "nope"}).status_code == 422


# --- the end-to-end flow ------------------------------------------------------------------------------

def test_end_to_end_config_estimate_run_events_export(client, engine, fixture_sources):
    fixture_sources({"google_maps": clocked_source("google_maps", 3, "www.google.com")})

    # 1. Save a config; the schema endpoint says what applies in its country.
    schema = client.get(f"{PREFIX}/schema/run-config", params={"country": "IN"}).json()
    assert schema["ui"]["geography.country"]["label"] == "Country"
    saved = client.post(f"{PREFIX}/configs", json={"document": config_document()})
    assert saved.status_code == 201, saved.text
    config_id = saved.json()["id"]

    # 2. Estimate it (a dry run: no requests, no worker, no project left behind).
    estimate = client.post(f"{PREFIX}/estimate", json={"saved_config_id": config_id})
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["units_queued"] == 2 and estimate.json()["p50_seconds"] > 0
    assert client.get(f"{PREFIX}/projects").json()["total"] == 0

    # 3. Queue a run; the API does not execute it.
    created = client.post(f"{PREFIX}/runs", json={"saved_config_id": config_id,
                                                  "options": {"depth": "quick"}})
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "queued" and run["saved_config_id"] == saved.json()["version_id"]
    run_id = run["id"]

    # 4. A worker executes it while a client follows the live event stream.
    worker = threading.Thread(target=run_worker, args=(engine,))
    worker.start()
    received = []
    with client.stream("GET", f"{PREFIX}/runs/{run_id}/events",
                       params={"poll": 0.05}) as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        for line in stream.iter_lines():
            if line.startswith("data: "):
                received.append(json.loads(line[6:]))
    worker.join(timeout=60)
    assert not worker.is_alive()
    types = [e["type"] for e in received]
    assert types[-2:] == ["run_finished", "run_state"]
    assert received[-1]["status"] == "completed"
    assert {"plan", "unit_started", "unit_finished", "progress"} <= set(types)
    assert [e["seq"] for e in received] == sorted(e["seq"] for e in received)

    # Replay equals what the live client saw; Last-Event-ID resumes after it.
    history = client.get(f"{PREFIX}/runs/{run_id}/events/history").json()
    assert [e["seq"] for e in history] == [e["seq"] for e in received]
    with client.stream("GET", f"{PREFIX}/runs/{run_id}/events",
                       headers={"Last-Event-ID": str(received[-3]["seq"])}) as stream:
        tail = [json.loads(line[6:]) for line in stream.iter_lines() if line.startswith("data: ")]
    assert [e["seq"] for e in tail] == [e["seq"] for e in received[-2:]]

    # 5. History: the run in detail.
    detail = client.get(f"{PREFIX}/runs/{run_id}").json()
    assert detail["status"] == "completed" and detail["new"] == 6
    assert detail["units"] == {"done": 2}
    assert detail["per_source"][0]["source"] == "google_maps"
    assert detail["per_source"][0]["found"] == 6
    assert detail["organizations"] == {"new": 6}
    assert detail["estimate"]["p50_seconds"] > 0 and detail["config"]["name"] == "API dentists"
    assert any(f["kind"] == "csv" for f in detail["files"])
    orgs = client.get(f"{PREFIX}/organizations", params={"run_id": run_id, "has": "phone",
                                                         "sort": "name"}).json()
    assert orgs["total"] == 6 and orgs["items"][0]["name"].startswith("Dentists House")
    org = client.get(f"{PREFIX}/organizations/{orgs['items'][0]['id']}").json()
    assert org["contacts"] and org["evidence"][0]["run_id"] == run_id
    assert [(r["run_id"], r["action"]) for r in org["runs"]] == [(run_id, "new")]

    # 6. Exports, regenerated from the database.
    csv_text = client.get(f"{PREFIX}/runs/{run_id}/export.csv").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Score,Business Name")
    assert len(csv_text.strip().splitlines()) == 7
    data = client.get(f"{PREFIX}/runs/{run_id}/export.json").json()
    assert data["count"] == 6 and data["scope"] == "run"
    from openpyxl import load_workbook

    response = client.get(f"{PREFIX}/runs/{run_id}/export.xlsx")
    assert "attachment" in response.headers["content-disposition"]
    book = load_workbook(io.BytesIO(response.content))
    assert book.sheetnames == ["Overview", "Records"]
    records = book["Records"]
    assert records.max_row == 7
    phones = [c.value for c in records[1]].index("Phones") + 1
    assert records.cell(2, phones).data_type == "s"           # phones as text
    assert records.cell(2, phones).value.startswith("+91")

    # 7. A re-run finds the same places again (+ one new each): the diff says so.
    fixture_sources({"google_maps": clocked_source("google_maps", 4, "www.google.com")})
    again = client.post(f"{PREFIX}/runs/{run_id}/rerun").json()
    run_worker(engine)
    assert client.get(f"{PREFIX}/runs/{again['id']}").json()["status"] == "completed"
    diff = client.get(f"{PREFIX}/runs/{again['id']}/diff").json()
    assert diff["against"] == run_id
    assert diff["counts"] == {"new": 2, "updated": 0, "unchanged": 6, "only_in_against": 0}
    new_only = client.get(f"{PREFIX}/runs/{again['id']}/export.json",
                          params={"scope": "new"}).json()
    assert new_only["count"] == 2 and new_only["against"] == run_id
    listed = client.get(f"{PREFIX}/runs", params={"project_id": run["project_id"]}).json()
    assert [r["id"] for r in listed["items"]] == [again["id"], run_id]


def test_run_control_through_the_api(client, engine):
    created = client.post(f"{PREFIX}/runs", json={"config": config_document("Ctl")}).json()
    run_id = created["id"]
    assert client.post(f"{PREFIX}/runs/{run_id}/pause").json()["status"] == "paused"
    assert client.post(f"{PREFIX}/runs/{run_id}/resume").json()["status"] == "queued"
    assert client.post(f"{PREFIX}/runs/{run_id}/cancel").json()["status"] == "cancelled"
    conflict = client.post(f"{PREFIX}/runs/{run_id}/pause")
    assert conflict.status_code == 409 and "cancelled" in conflict.json()["detail"]
    bad = client.post(f"{PREFIX}/runs", json={"config": config_document(),
                                              "options": {"areas": ["Atlantis"]}})
    assert bad.status_code == 422
    db_url = {**config_document(), "runtime": {"db_url": "sqlite:///elsewhere.sqlite3"}}
    assert client.post(f"{PREFIX}/runs", json={"config": db_url}).status_code == 422


def test_preview_is_queued_and_its_sample_lands_on_the_run(client, engine, fixture_sources):
    fixture_sources({"google_maps": clocked_source("google_maps", 2, "www.google.com")})
    queued = client.post(f"{PREFIX}/preview", json={"config": config_document("Prev"),
                                                    "seconds": 60})
    assert queued.status_code == 202, queued.text
    assert queued.json()["kind"] == "preview" and queued.json()["status"] == "queued"
    run_worker(engine)
    run = client.get(f"{PREFIX}/runs/{queued.json()['id']}").json()
    assert run["status"] == "completed", run["error"]
    assert run["project"] == "Prev (preview)"                  # the scratch project
    preview = run["preview"]
    assert preview["area"] == "Baner" and len(preview["records"]) == 2
    assert preview["estimate"]["units_queued"] == 2
    names = {p["name"] for p in client.get(f"{PREFIX}/projects").json()["items"]}
    assert "Prev" not in names                                 # the real project is untouched


def test_cross_site_writes_and_foreign_hosts_are_refused(client, monkeypatch):
    """No login yet: a web page must not drive the API through the user's browser."""
    evil = client.post(f"{PREFIX}/projects", json={"name": "X"},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and "cross-site" in evil.json()["detail"]
    local = client.post(f"{PREFIX}/projects", json={"name": "Y"},
                        headers={"Origin": "http://localhost:3000"})
    assert local.status_code == 201
    assert client.get(f"{PREFIX}/projects", headers={"Origin": "https://evil.example"}) \
        .status_code == 200                                   # reads are not side effects
    rebinding = client.get(f"{PREFIX}/projects", headers={"Host": "attacker.example"})
    assert rebinding.status_code == 400                      # DNS rebinding: unknown Host


def test_uploads_are_capped(client, monkeypatch):
    monkeypatch.setenv("LEADSCRAPER_MAX_UPLOAD_MB", "0.001")
    response = client.post(f"{PREFIX}/bundle/import",
                           files={"file": ("b.zip", b"x" * 5000, "application/zip")})
    assert response.status_code == 422 and "larger than" in response.json()["detail"]
