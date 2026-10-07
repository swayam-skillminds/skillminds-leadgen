"""The command line over the core: config tools, migration, export."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from leadscraper.cli import main
from leadscraper.cli import run_cmd
from leadscraper.core.lead import Lead
from leadscraper.db.legacy_import import LEGACY_SCHEMA
from leadscraper.paths import PROJECT_ROOT


def legacy_setup(tmp_path: Path) -> Path:
    """A legacy config whose db_path is a legacy-format store with two duplicates."""
    store = tmp_path / "leads.sqlite3"
    conn = sqlite3.connect(store)
    conn.executescript(LEGACY_SCHEMA)
    for lead_id, lead in enumerate([
        Lead(business_name="Royal Caterers", locality="Andheri", phones=["+919820012345"]),
        Lead(business_name="Royal Catering Co", locality="Dadar", phones=["+919820012345"]),
        Lead(business_name="Sharma Decorators", locality="Powai"),
    ], start=1):
        conn.execute("INSERT INTO leads VALUES (?, ?, ?, ?, 0, ?, 0, 0)",
                     (lead_id, lead.name_key, lead.locality, lead.category, lead.to_json()))
    conn.commit()
    conn.close()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "city": "Mumbai", "country": "IN",
        "localities": [{"name": "Andheri"}], "categories": [{"name": "Caterers",
                                                             "queries": ["caterers"]}],
        "sources": {"google_maps": {"enabled": True}},
        "runtime": {"db_path": str(store), "csv_export": str(tmp_path / "out.csv"),
                    "failure_log": str(tmp_path / "failures.jsonl"),
                    "run_log": str(tmp_path / "run.log")},
        "sheets": {"enabled": False},
    }))
    return config


def test_config_schema_and_validate(tmp_path, capsys):
    out = tmp_path / "schema.json"
    assert main(["config", "schema", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["title"] == "Run config"
    assert main(["config", "validate", str(PROJECT_ROOT / "presets" / "nashik-venues.yaml")]) == 0
    printed = capsys.readouterr().out
    assert "valid run config 'Nashik venues'" in printed
    assert "not applied yet: output.excel" in printed


def test_config_import_legacy_writes_the_preset(tmp_path):
    out = tmp_path / "mumbai.yaml"
    assert main(["config", "import-legacy", str(PROJECT_ROOT / "config.yaml"), "-o", str(out)]) == 0
    assert yaml.safe_load(out.read_text()) == yaml.safe_load(
        (PROJECT_ROOT / "presets" / "mumbai-event-vendors.yaml").read_text())


def test_unmigrated_legacy_store_blocks_a_run_with_instructions(tmp_path, capsys):
    config = legacy_setup(tmp_path)
    assert run_cmd.main(["--config", str(config), "--source", "google_maps"]) == 2
    err = capsys.readouterr().err
    assert "migrate-legacy --config config.yaml" in err
    assert run_cmd.main(["--config", str(config), "--export-only"]) == 1


def test_migrate_then_export(tmp_path, capsys):
    config = legacy_setup(tmp_path)
    before = (tmp_path / "leads.sqlite3").read_bytes()
    report = tmp_path / "report.json"
    assert main(["migrate-legacy", "--config", str(config), "--report", str(report)]) == 0
    assert (tmp_path / "leads.sqlite3").read_bytes() == before          # original untouched
    assert json.loads(report.read_text())["merged_clusters"] == [[1, 2]]
    # A second import into the same project is refused rather than doubled.
    assert main(["migrate-legacy", "--config", str(config)]) == 2

    capsys.readouterr()
    assert run_cmd.main(["--config", str(config), "--export-only", "--no-sheets"]) == 0
    assert "Re-scored and exported 2 leads of 'Mumbai event vendors'" in capsys.readouterr().out
    rows = (tmp_path / "out.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(rows) == 3 and rows[0].startswith("Score,Business Name")
    assert main(["db", "info"]) == 0
    assert "Mumbai event vendors: 2 organisations" in capsys.readouterr().out


def test_db_upgrade_sql_for_postgres(capsys):
    assert main(["db", "upgrade", "--db", "postgresql://u@localhost/x", "--sql"]) == 0
    assert "CREATE TABLE organizations" in capsys.readouterr().out


# --- Phase 3: CLI parity over the services ------------------------------------------------------

def test_phase3_cli_configs_exports_diff_bundles_status(tmp_path, capsys, monkeypatch):
    from leadscraper.db.engine import open_database
    from leadscraper.db.runs import finish_run, start_run
    from leadscraper.db.store import LeadStore
    from leadscraper.paths import default_db_url

    preset = PROJECT_ROOT / "presets" / "nashik-venues.yaml"
    assert main(["config", "save", str(preset)]) == 0
    assert "saved config 1: Nashik venues v1" in capsys.readouterr().out
    assert main(["config", "save", str(preset)]) == 0             # unchanged: still v1
    assert "v1 (1 version(s))" in capsys.readouterr().out
    assert main(["config", "list"]) == 0 and "Nashik venues" in capsys.readouterr().out
    out = tmp_path / "nashik.json"
    assert main(["config", "export", "1", "--format", "json", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["name"] == "Nashik venues"

    engine = open_database(default_db_url())
    store = LeadStore(engine, project="CLI dentists", country="IN")
    runs = []
    for names in (["Smile Dental"], ["Smile Dental", "Bright Smiles"]):
        run_id = start_run(engine, store.workspace_id, store.project_id,
                           config_snapshot={"schema_version": 1, "name": "CLI dentists",
                                            "project": "CLI dentists",
                                            "geography": {"country": "IN", "places": ["Pune"]},
                                            "lead_types": {"local_business": {
                                                "categories": [{"name": "Dentists"}]}}})
        store.run_id = run_id
        for i, name in enumerate(names):
            store.upsert(Lead(business_name=name, locality="Baner",
                              phones=[f"+91982001234{i}"], sources=["google_maps"]))
        finish_run(engine, run_id, stats={})
        runs.append(run_id)
    store.close()
    engine.dispose()
    capsys.readouterr()

    assert main(["runs", "diff", str(runs[1])]) == 0
    assert "1 new, 0 updated, 1 unchanged" in capsys.readouterr().out
    xlsx = tmp_path / "out.xlsx"
    assert main(["export", "--run", str(runs[1]), "--scope", "new", "-o", str(xlsx)]) == 0
    assert "wrote 1 organisations (new)" in capsys.readouterr().out
    from openpyxl import load_workbook

    assert load_workbook(xlsx).sheetnames == ["Overview", "Records"]
    assert main(["projects"]) == 0 and "CLI dentists" in capsys.readouterr().out
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["health"]["checks"]["database"]["status"] == "ok"

    bundle = tmp_path / "bundle.zip"
    assert main(["bundle", "export", "-o", str(bundle), "--leads", "--history"]) == 0
    capsys.readouterr()
    other = f"sqlite:///{(tmp_path / 'other.sqlite3').as_posix()}"
    assert main(["bundle", "import", str(bundle), "--db", other, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["organizations"]["cli-dentists"] == {"mode": "verbatim", "imported": 2}
    assert report["runs"] == 2
    assert main(["runs", "failures", str(runs[0])]) == 0
    assert "0 failure(s)" in capsys.readouterr().out
