"""Import an original (pre-Phase-1) SQLite lead store into a project.

The originals are precious (A.2.6): ``copy_legacy_store`` makes a consistent
copy with SQLite's backup API, opening the original read-only, and the import
only ever reads that copy -- also read-only.

Two modes:

* ``replay`` (the default) -- every stored record, in id order, goes through
  today's identity rules as if it had just been found. Duplicates the old
  rules let through collapse; records keep their own timestamps.
* ``verbatim`` -- one organisation per stored row, exactly as stored, with the
  original phone/place claim order. Re-exporting it reproduces the legacy
  export row for row; it is what the characterisation suite checks.

Operational state comes along too: seen URLs (so a re-run skips pages it
already read), dead domains, per-unit timing stats and enrichment marks
(mapped onto wherever each record ended up).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select, update

from ..core.lead import Lead
from .models import DeadDomain, EnrichmentMark, SeenUrl, UnitStat
from .runs import finish_run, start_run
from .store import LeadStore

log = logging.getLogger(__name__)

ImportMode = Literal["replay", "verbatim"]
IMPORT_SOURCE = "legacy-import"


#: The original store's schema, for reference and for building test fixtures.
#: The importer reads whichever of these tables a store actually has.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name_key     TEXT NOT NULL,
    locality     TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    score        REAL NOT NULL DEFAULT 0,
    data         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_name   ON leads(name_key);
CREATE INDEX IF NOT EXISTS idx_leads_group  ON leads(locality, category, score DESC);

CREATE TABLE IF NOT EXISTS phone_index (
    phone   TEXT PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS place_index (
    place   TEXT PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS seen_urls (
    url        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_domains (
    host       TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    first_seen REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS unit_stats (
    source          TEXT NOT NULL,
    area            TEXT NOT NULL,
    category        TEXT NOT NULL,
    runs            INTEGER NOT NULL DEFAULT 0,
    total_seconds   REAL NOT NULL DEFAULT 0,
    total_new_leads INTEGER NOT NULL DEFAULT 0,
    last_run        REAL NOT NULL,
    PRIMARY KEY (source, area, category)
);

CREATE TABLE IF NOT EXISTS enriched (
    lead_id  INTEGER NOT NULL,
    enricher TEXT NOT NULL,
    done_at  REAL NOT NULL,
    PRIMARY KEY (lead_id, enricher)
);
"""


@dataclass
class ImportReport:
    mode: str
    source: str
    project: str
    legacy_rows: int = 0
    stored: int = 0
    actions: dict[str, int] = field(default_factory=lambda: {"new": 0, "merged": 0,
                                                             "unchanged": 0})
    rejected_ids: list[int] = field(default_factory=list)
    #: new organisation id -> the legacy ids it holds, in import order.
    clusters: dict[int, list[int]] = field(default_factory=dict)
    seen_urls: int = 0
    dead_domains: int = 0
    unit_stats: int = 0
    enrichment_marks: int = 0
    run_id: int | None = None

    @property
    def merged_clusters(self) -> list[list[int]]:
        return sorted(ids for ids in self.clusters.values() if len(ids) > 1)

    def legacy_to_new(self) -> dict[int, int]:
        return {legacy: new for new, legacies in self.clusters.items() for legacy in legacies}

    def summary(self) -> str:
        return (f"{self.mode} import of {self.legacy_rows} legacy rows into '{self.project}': "
                f"{self.stored} organisations ({self.actions['new']} new, "
                f"{self.actions['merged']} merged, {self.actions['unchanged']} unchanged), "
                f"{len(self.rejected_ids)} rejected, {len(self.merged_clusters)} merged clusters; "
                f"carried over {self.seen_urls} seen URLs, {self.dead_domains} dead domains, "
                f"{self.unit_stats} unit stats, {self.enrichment_marks} enrichment marks")


def copy_legacy_store(source: Path, dest: Path) -> Path:
    """Consistent copy via the SQLite backup API; the source is opened read-only."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{Path(source).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def is_legacy_store(path: Path) -> bool:
    if not Path(path).exists():
        return False
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        tables = _tables(conn)
    finally:
        conn.close()
    return "leads" in tables and "alembic_version" not in tables


def import_legacy_store(copy_path: Path, store: LeadStore, *, mode: ImportMode = "replay",
                        record_run: bool = True) -> ImportReport:
    """Import a legacy store *copy* into `store`'s project."""
    if mode not in ("replay", "verbatim"):
        raise ValueError(f"unknown import mode {mode!r}")
    legacy = sqlite3.connect(f"file:{Path(copy_path).as_posix()}?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    report = ImportReport(mode=mode, source=str(copy_path), project=store.project_name)
    previous_run, previous_source = store.run_id, store.source_override
    try:
        tables = _tables(legacy)
        if "leads" not in tables:
            raise ValueError(f"{copy_path} has no leads table; not a legacy lead store")
        rows = legacy.execute("SELECT id, data FROM leads ORDER BY id").fetchall()
        report.legacy_rows = len(rows)
        if record_run:
            report.run_id = start_run(store.engine, store.workspace_id, store.project_id,
                                      kind="import", config_snapshot={
                                          "import": {"mode": mode, "source": Path(copy_path).name}})
            store.run_id = report.run_id
        store.source_override = IMPORT_SOURCE

        if mode == "verbatim":
            _verbatim(legacy, tables, rows, store, report)
        else:
            _replay(rows, store, report)
        report.stored = store.count()
        _carry_state(legacy, tables, store, report)
        if record_run:
            finish_run(store.engine, report.run_id, stats={
                "legacy_rows": report.legacy_rows, "stored": report.stored,
                "actions": report.actions, "rejected": len(report.rejected_ids),
                "merged_clusters": len(report.merged_clusters)})
    except Exception as exc:
        if record_run and report.run_id is not None:
            finish_run(store.engine, report.run_id, status="failed", error=repr(exc))
        raise
    finally:
        store.run_id, store.source_override = previous_run, previous_source
        legacy.close()
    log.info("%s", report.summary())
    return report


def _replay(rows, store: LeadStore, report: ImportReport) -> None:
    for row in rows:
        lead = Lead.from_json(row["data"])
        try:
            new_id, action, _ = store.upsert(lead, legacy_id=row["id"], now=lead.last_updated)
        except ValueError:
            report.rejected_ids.append(int(row["id"]))
            continue
        report.actions[action] += 1
        report.clusters.setdefault(new_id, []).append(int(row["id"]))


def _verbatim(legacy: sqlite3.Connection, tables: set[str], rows, store: LeadStore,
              report: ImportReport) -> None:
    id_map: dict[int, int] = {}
    for row in rows:
        lead = Lead.from_json(row["data"])
        new_id = store.insert_record(lead, legacy_id=row["id"], index=False)
        id_map[int(row["id"])] = new_id
        report.actions["new"] += 1
        report.clusters[new_id] = [int(row["id"])]
    # The original indexes' claim order first, so "first claimant" means what
    # it meant before; then whatever the records carry that was never indexed
    # (place_index postdates many rows; domains are new).
    for kind, table, column in (("phone", "phone_index", "phone"),
                                ("place", "place_index", "place")):
        if table not in tables:
            continue
        for item in legacy.execute(f"SELECT {column}, lead_id FROM {table} ORDER BY rowid"):
            new_id = id_map.get(int(item["lead_id"]))
            if new_id is not None:
                store.claim(new_id, kind, [item[column]])
    for new_id in id_map.values():
        store.index_record(new_id)


def _carry_state(legacy: sqlite3.Connection, tables: set[str], store: LeadStore,
                 report: ImportReport) -> None:
    """Seen URLs, dead domains, unit stats, enrichment marks."""
    conn, ws = store._conn, store.workspace_id
    insert_ignore = store._insert_ignore
    if "seen_urls" in tables:
        seen = [{"project_id": store.project_id, "url": r["url"], "workspace_id": ws,
                 "source": r["source"], "fetched_at": r["fetched_at"]}
                for r in legacy.execute("SELECT url, source, fetched_at FROM seen_urls")]
        insert_ignore(SeenUrl.__table__, seen)
        report.seen_urls = len(seen)
    if "dead_domains" in tables:
        dead = [{"workspace_id": ws, "host": r["host"], "reason": r["reason"],
                 "first_seen": r["first_seen"], "hits": r["hits"]}
                for r in legacy.execute("SELECT host, reason, first_seen, hits FROM dead_domains")]
        insert_ignore(DeadDomain.__table__, dead)
        report.dead_domains = len(dead)
    if "unit_stats" in tables:
        table = UnitStat.__table__
        for r in legacy.execute("SELECT * FROM unit_stats"):
            key = (table.c.workspace_id == ws, table.c.source == r["source"],
                   table.c.area == r["area"], table.c.category == r["category"])
            existing = conn.execute(select(table.c.runs).where(*key)).first()
            if existing:
                conn.execute(update(table).where(*key).values(
                    runs=table.c.runs + r["runs"],
                    total_seconds=table.c.total_seconds + r["total_seconds"],
                    total_new_leads=table.c.total_new_leads + r["total_new_leads"],
                    last_run=max(r["last_run"], 0)))
            else:
                conn.execute(table.insert().values(
                    workspace_id=ws, source=r["source"], area=r["area"], category=r["category"],
                    runs=r["runs"], total_seconds=r["total_seconds"],
                    total_new_leads=r["total_new_leads"], last_run=r["last_run"]))
            report.unit_stats += 1
    if "enriched" in tables:
        where = report.legacy_to_new()
        marks = {}
        for r in legacy.execute("SELECT lead_id, enricher, done_at FROM enriched"):
            new_id = where.get(int(r["lead_id"]))
            if new_id is not None:
                key = (new_id, r["enricher"])
                marks[key] = max(marks.get(key, 0.0), float(r["done_at"]))
        table = EnrichmentMark.__table__
        for (new_id, enricher), done_at in marks.items():
            conn.execute(delete(table).where(table.c.organization_id == new_id,
                                             table.c.enricher == enricher))
            conn.execute(table.insert().values(organization_id=new_id, enricher=enricher,
                                               workspace_id=ws, done_at=done_at))
        report.enrichment_marks = len(marks)
    conn.commit()
