"""CI: a bundle made on one OS imports on another (Phase 3 acceptance).

    python tools/ci_bundle.py make  OUT_DIR   # build a workspace, export bundle + expectations
    python tools/ci_bundle.py check IN_DIR    # import every bundle there into a fresh database

Each OS's ``make`` job uploads ``bundle-<os>.zip`` and ``bundle-<os>.expected.json``;
every ``check`` job downloads all of them and imports each one into its own
fresh database, then compares organisations, contacts, runs, saved configs
and suppression entries with what the maker saw. Synthetic data only, and a
scratch data directory: nothing touches a real database.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh_engine(directory: Path):
    os.environ["LEADSCRAPER_DATA_DIR"] = str(directory)
    os.environ.pop("LEADSCRAPER_DB_URL", None)
    from leadscraper.db.engine import open_database
    from leadscraper.paths import default_db_url
    from leadscraper.services.workspace import default_workspace

    engine = open_database(default_db_url())
    return engine, default_workspace(engine)


def _summary(engine, ws) -> dict:
    from leadscraper.services import configs, organizations, projects, runs, suppression

    out: dict = {"projects": {}, "configs": {}, "lists": {}}
    for project in projects.list_projects(engine, ws, limit=500).items:
        orgs = organizations.list_organizations(engine, ws, project_id=project["id"],
                                                sort="name", limit=500).items
        details = [organizations.get_organization(engine, ws, o["id"]) for o in orgs]
        out["projects"][project["name"]] = {
            "organizations": [o["name"] for o in orgs],
            "contacts": sorted(f"{c['channel']}:{c['value']}" for d in details
                               for c in d["contacts"]),
            "runs": sorted(r["status"] for r in runs.list_runs(
                engine, ws, project_id=project["id"], limit=500).items),
        }
    for item in configs.list_configs(engine, ws, limit=500).items:
        out["configs"][item["name"]] = item["versions"]
    for item in suppression.list_lists(engine, ws, limit=500).items:
        out["lists"][item["name"]] = item["entries"]
    return out


def make(out_dir: Path) -> None:
    from leadscraper.core.lead import Lead
    from leadscraper.db.runs import finish_run, start_run
    from leadscraper.db.store import LeadStore
    from leadscraper.services import bundle, configs, suppression

    tag = platform.system().lower()
    with tempfile.TemporaryDirectory() as scratch:
        engine, ws = _fresh_engine(Path(scratch))
        store = LeadStore(engine, project=f"Bundle test ({tag})", country="IN")
        run_id = start_run(engine, ws.id, store.project_id, config_snapshot={"name": tag})
        store.run_id = run_id
        for i, name in enumerate(["Smile Dental", "Tooth Fairy Clinic", "Café Ünïcode"]):
            store.upsert(Lead(business_name=name, locality="Baner", category="Dentists",
                              phones=[f"+91982001234{i}"], website=f"https://site{i}.example.in",
                              emails=[f"hello@site{i}.example.in"], sources=["google_maps"]))
        store.close()
        finish_run(engine, run_id, stats={})
        document = {"schema_version": 1, "name": f"Config {tag}", "project": "Bundle test",
                    "geography": {"country": "IN", "places": ["Pune"]},
                    "lead_types": {"local_business": {"categories": [{"name": "Dentists"}]}}}
        saved = configs.create_config(engine, ws, document)
        configs.save_version(engine, ws, saved["id"], {**document, "depth": "quick"})
        suppression.create_list(engine, ws, name=f"Customers {tag}",
                                entries=[{"value": "acme.example"}, {"value": "+919820099999"}])
        data = bundle.export_bundle(engine, ws, include_leads=True, include_history=True)
        expected = _summary(engine, ws)
        engine.dispose()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"bundle-{tag}.zip").write_bytes(data)
    (out_dir / f"bundle-{tag}.expected.json").write_text(
        json.dumps(expected, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"made bundle-{tag}.zip ({len(data)} bytes) on {platform.system()}")


def check(in_dir: Path) -> int:
    from leadscraper.services import bundle

    bundles = sorted(in_dir.rglob("bundle-*.zip"))
    if not bundles:
        print(f"no bundles under {in_dir}")
        return 1
    failures = 0
    for path in bundles:
        expected = json.loads(path.with_name(path.stem + ".expected.json")
                              .read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as scratch:
            engine, ws = _fresh_engine(Path(scratch))
            report = bundle.import_bundle(engine, ws, path.read_bytes())
            actual = _summary(engine, ws)
            engine.dispose()
        made_on = report["manifest"]["created_on"]["os"]
        if actual == expected:
            print(f"OK   {path.name}: made on {made_on}, imported on {platform.system()}")
        else:
            failures += 1
            print(f"FAIL {path.name}: made on {made_on}, imported on {platform.system()}\n"
                  f"  expected {json.dumps(expected, ensure_ascii=False)}\n"
                  f"  actual   {json.dumps(actual, ensure_ascii=False)}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("make", "check"):
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "make":
        make(Path(sys.argv[2]))
        sys.exit(0)
    sys.exit(check(Path(sys.argv[2])))
