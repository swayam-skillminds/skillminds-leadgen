"""Shared machinery for the characterisation suite (APP-BUILD-PROMPT.md Phase 0).

Characterisation tests pin what the code does *today*, right or wrong, so the
Phase 1 refactor can prove it changed nothing it did not mean to change.

Two layers use this module:

* synthetic goldens  -- hand-built leads, always run, goldens committed;
* real-data snapshots -- the Mumbai and Nashik lead stores, frozen once into
  ``data/characterisation/`` (gitignored) and opened only as *copies*. The
  committed snapshot holds ids, scores, tiers, counts and hashes -- never a
  business name, phone or address.

Both are computed against the frozen configs in ``baseline_configs/`` rather
than the live ``config*.yaml``, so retuning a weight later does not read as a
behaviour change in the code.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from leadscraper.config import Config  # noqa: E402
from leadscraper.export.csv_writer import leads_to_rows  # noqa: E402
from leadscraper.core.lead import Lead  # noqa: E402
from leadscraper.core.normalize import set_extra_name_noise  # noqa: E402
from leadscraper.leadtypes.local_business.scoring import Scorer  # noqa: E402
from leadscraper.db.store import DedupOptions, LeadStore  # noqa: E402

GOLDEN_DIR = HERE / "golden"
CONFIG_DIR = HERE / "baseline_configs"

#: profile -> (frozen config, live DB the baseline is frozen from, baseline file name)
PROFILES: dict[str, tuple[str, str, str]] = {
    "mumbai": ("config.yaml", "data/leads.sqlite3", "mumbai_leads.sqlite3"),
    "nashik": ("config.nashik.yaml", "data/nashik/venues.sqlite3", "nashik_venues.sqlite3"),
}

#: Wall-clock columns render through time.localtime, so they depend on the
#: machine's timezone; the raw epoch floats are pinned instead.
TIMESTAMP_COLUMNS = ("First Seen", "Last Updated")

UPDATE_ENV = "CHAR_UPDATE"


def baseline_dir() -> Path:
    override = os.environ.get("LEADSCRAPER_CHAR_BASELINE_DIR")
    return Path(override) if override else PROJECT_ROOT / "data" / "characterisation"


def load_config(profile: str) -> Config:
    """The frozen config for a profile."""
    return Config.load(CONFIG_DIR / PROFILES[profile][0])


def apply_name_noise(config: Config) -> None:
    """Exactly what Pipeline/export_only do before touching the store."""
    set_extra_name_noise([config.city, *(config.get("dedup.extra_name_noise") or [])])


def open_store(path: Path, config: Config) -> LeadStore:
    return LeadStore(path, Scorer(config), DedupOptions.from_config(config))


# --- goldens -----------------------------------------------------------------

def updating() -> bool:
    return os.environ.get(UPDATE_ENV) == "1"


def check_golden(name: str, actual: Any) -> None:
    """Compare against golden/<name>.json; rewrite it when CHAR_UPDATE=1."""
    path = GOLDEN_DIR / f"{name}.json"
    # Round-trip so tuples, int keys etc. compare the way they are stored.
    actual = json.loads(json.dumps(actual, ensure_ascii=False))
    if updating() or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8")
        if not updating():
            raise AssertionError(f"golden {path.name} did not exist; wrote it -- review and re-run")
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"behaviour differs from golden {path.name}. If the change is intended, "
        f"re-run with {UPDATE_ENV}=1 and list it in the phase report."
    )


# --- row helpers -------------------------------------------------------------

def comparable_row(lead: Lead, profile: str) -> dict[str, Any]:
    """A CSV row with the timezone-dependent columns swapped for epoch floats."""
    row = lead.row_for(profile)
    for column in TIMESTAMP_COLUMNS:
        row.pop(column, None)
    row["_first_seen"] = round(lead.first_seen, 3)
    row["_last_updated"] = round(lead.last_updated, 3)
    return row


def digest(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --- real-data snapshots -----------------------------------------------------

def fingerprint(db_path: Path) -> str:
    """Hash of every stored lead, so a snapshot is tied to one exact baseline."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        hasher = hashlib.sha256()
        for lead_id, data in conn.execute("SELECT id, data FROM leads ORDER BY id"):
            hasher.update(f"{lead_id}\x00{data}\x01".encode("utf-8"))
        return hasher.hexdigest()
    finally:
        conn.close()


def copy_db(source: Path, dest: Path) -> Path:
    """Consistent copy via the sqlite backup API; the source is opened read-only."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def export_snapshot(baseline: Path, config: Config, scratch: Path) -> dict[str, Any]:
    """What `run.py --export-only` produces from a copy of the baseline."""
    apply_name_noise(config)
    work = copy_db(baseline, scratch / "export.sqlite3")
    store = open_store(work, config)
    try:
        store.rescore_all()
        by_id = store.all_with_ids()
        sorted_leads = store.sorted_leads()
    finally:
        store.close()

    profile = config.profile
    rows = leads_to_rows(sorted_leads, profile)
    snapshot: dict[str, Any] = {
        "lead_count": len(by_id),
        "headers": rows[0],
        "row_digests_in_export_order": [digest(comparable_row(lead, profile))
                                        for lead in sorted_leads],
        "scores": {str(lead_id): lead.score for lead_id, lead in by_id},
    }
    if config.is_venue_run:
        snapshot["tiers"] = {
            str(lead_id): [lead.venue_tier, lead.tier_points, lead.venue_type, lead.record_type]
            for lead_id, lead in by_id
        }
    return snapshot


def dedup_snapshot(baseline: Path, config: Config, scratch: Path) -> dict[str, Any]:
    """Replay every stored lead, in id order, into an empty store.

    Pins today's dedup decisions on real data: which records the current
    rules would fold together, and which they refuse to store at all.
    """
    apply_name_noise(config)
    source = sqlite3.connect(f"file:{baseline}?mode=ro", uri=True)
    try:
        originals = [(lead_id, Lead.from_json(data)) for lead_id, data in
                     source.execute("SELECT id, data FROM leads ORDER BY id")]
    finally:
        source.close()

    target = scratch / "replay.sqlite3"
    if target.exists():
        target.unlink()
    store = open_store(target, config)
    clusters: dict[int, list[int]] = {}
    rejected: list[int] = []
    actions = {"new": 0, "merged": 0, "unchanged": 0}
    try:
        for original_id, lead in originals:
            try:
                new_id, action, _ = store.upsert(lead)
            except ValueError:
                rejected.append(original_id)
                continue
            actions[action] += 1
            clusters.setdefault(new_id, []).append(original_id)
        stored = store.count()
    finally:
        store.close()

    return {
        "replayed": len(originals),
        "stored": stored,
        "actions": actions,
        "rejected_ids": rejected,
        "merged_clusters": sorted(ids for ids in clusters.values() if len(ids) > 1),
    }


def real_snapshot(profile: str, baseline: Path, scratch: Path) -> dict[str, Any]:
    config = load_config(profile)
    return {
        "baseline_fingerprint": fingerprint(baseline),
        "export": export_snapshot(baseline, config, scratch),
        "dedup": dedup_snapshot(baseline, config, scratch),
    }
