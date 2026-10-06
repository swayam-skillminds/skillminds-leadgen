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
