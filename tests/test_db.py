"""Persistence (APP-BUILD-PROMPT Phase 1.1 and 1.6): schema, migrations, both
backends, provenance rows, and the legacy-store import."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.schema import CreateIndex, CreateTable

from leadscraper.core.lead import Lead
from leadscraper.db.engine import (
    LegacyDatabaseError, alembic_config, head_revision, make_engine, open_database,
)
from leadscraper.db.legacy_import import (
    LEGACY_SCHEMA, copy_legacy_store, import_legacy_store, is_legacy_store,
)
from leadscraper.db.models import (
    Base, Contact, Evidence, Organization, Person, RunOrganization,
)
from leadscraper.db.runs import finish_run, get_run, start_run
from leadscraper.db.store import LeadStore

PLACE_URL = ("https://www.google.com/maps/place/Aayoni/data=!4m7!3m6"
             "!1s0x3be7c83806155505:0xd93628e9d1588bd3!8m2!3d19.1069285!4d72.8639382")


# --- schema and migrations ---------------------------------------------------------

def test_alembic_migrations_build_exactly_the_models(tmp_path):
    """Upgrading an empty DB through every migration == the models' schema."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    engine = make_engine(f"sqlite:///{tmp_path / 'migrated.sqlite3'}")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diffs == []
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() \
            == head_revision()
    engine.dispose()


def test_every_owned_table_carries_workspace_id():
    """A.2.2: auth can arrive later without remodelling the data."""
    unowned = {"workspaces"}
    for table in Base.metadata.sorted_tables:
        if table.name in unowned:
            continue
        assert "workspace_id" in table.c, f"{table.name} has no workspace_id"


def test_a6_entities_exist():
    names = set(Base.metadata.tables)
    assert {"workspaces", "projects", "runs", "saved_configs", "organizations",
            "identity_keys", "contacts", "people", "signals", "evidence", "run_organizations",
            "work_units", "failures", "ai_usage", "suppression_lists", "suppression_entries",
            "schedules"} <= names


def test_postgres_ddl_compiles():
    """The server-mode path: every table and index renders for Postgres."""
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert ddl.startswith("\nCREATE TABLE")
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=dialect))
    organizations = str(CreateTable(Organization.__table__).compile(dialect=dialect))
    assert "JSONB" in organizations          # records are JSONB on Postgres


def test_postgres_migration_sql_renders_offline():
    """`db upgrade --sql` for a Postgres URL: what a DBA would review."""
    from alembic import command

    out = io.StringIO()
    with redirect_stdout(out):
        command.upgrade(alembic_config(url="postgresql://user@localhost/leadscraper"),
                        "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE organizations" in sql
    assert "JSONB" in sql
    assert "CREATE TABLE alembic_version" in sql


def test_file_databases_use_wal(tmp_path):
    engine = open_database(tmp_path / "wal.sqlite3")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    engine.dispose()


def test_a_legacy_store_is_refused_and_left_untouched(tmp_path):
    legacy = tmp_path / "leads.sqlite3"
    conn = sqlite3.connect(legacy)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    with pytest.raises(LegacyDatabaseError, match="migrate-legacy"):
        LeadStore(legacy)
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    assert is_legacy_store(legacy)


# --- what a write leaves behind ---------------------------------------------------------

def rows(store: LeadStore, model, **where):
    table = model.__table__
    query = select(table).where(*[table.c[k] == v for k, v in where.items()])
    return [dict(r) for r in store._conn.execute(query).mappings().all()]


def test_writes_record_contacts_evidence_and_run_membership(tmp_path):
    store = LeadStore(tmp_path / "app.sqlite3", project="Pune dentists", country="IN")
    run_id = start_run(store.engine, store.workspace_id, store.project_id)
    store.run_id = run_id
    lead_id, action, _ = store.upsert(Lead(
        business_name="Smile Dental", locality="Baner", phones=["+919820012345"],
        sources=["google_maps"], source_urls=["https://maps.example/smile"],
        contact_name="Dr. Rao"))
    assert action == "new"
    store.merge_into(lead_id, Lead(business_name="", emails=["hello@smiledental.in"],
                                   sources=["website"],
                                   source_urls=["https://smiledental.in/contact"]))
    finish_run(store.engine, run_id, stats={"new": 1})

    contacts = {(c["channel"], c["value"]): c for c in rows(store, Contact, organization_id=lead_id)}
    assert contacts[("phone", "+919820012345")]["source"] == "google_maps"
    assert contacts[("email", "hello@smiledental.in")]["source"] == "website"
    assert all(c["kind"] == "published" for c in contacts.values())
    evidence = rows(store, Evidence, organization_id=lead_id)
    assert [e["source"] for e in evidence] == ["google_maps", "website"]
    assert evidence[1]["url"] == "https://smiledental.in/contact"
    assert "emails" in evidence[1]["fields"]
    assert all(e["run_id"] == run_id for e in evidence)
    membership = rows(store, RunOrganization, run_id=run_id)
    assert [(m["organization_id"], m["action"]) for m in membership] == [(lead_id, "new")]
    assert rows(store, Person, organization_id=lead_id)[0]["name"] == "Dr. Rao"
    org = rows(store, Organization, id=lead_id)[0]
    assert org["lead_types"] == ["local_business"] and org["country"] == "IN"
    assert get_run(store.engine, run_id)["status"] == "completed"
    store.close()


def test_scores_are_stored_with_their_parts_and_reasons(tmp_path):
    store = LeadStore(tmp_path / "app.sqlite3")
    lead_id, _, _ = store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                                      phones=["+919820012345"], gmaps_rating=4.5,
                                      gmaps_reviews=200, sources=["google_maps", "justdial"]))
    org = rows(store, Organization, id=lead_id)[0]
    assert org["score"] == store.get(lead_id).score > 0
    assert 0 < org["fit"] <= 1 and 0 < org["reach"] <= 1
    assert any("Google rating 4.5" in r for r in org["score_reasons"])
    store.close()


def test_projects_in_one_database_are_separate(tmp_path):
    path = tmp_path / "app.sqlite3"
    mumbai = LeadStore(path, project="Mumbai event vendors")
    nashik = LeadStore(path, project="Nashik venues")
    lead = Lead(business_name="Royal Caterers", locality="Andheri", phones=["+919820012345"])
    assert mumbai.upsert(Lead(**vars(lead)))[1] == "new"
    assert nashik.upsert(Lead(**vars(lead)))[1] == "new"
    assert mumbai.count() == nashik.count() == 1
    assert mumbai.project_id != nashik.project_id
    assert mumbai.workspace_id == nashik.workspace_id
    mumbai.close()
    nashik.close()


def test_dead_domains_are_shared_across_projects(tmp_path):
    path = tmp_path / "app.sqlite3"
    a, b = LeadStore(path, project="A"), LeadStore(path, project="B")
    a.mark_dead_domain("gone.example", "dns_failure")
    assert b.is_dead_domain("gone.example")
    a.close()
    b.close()


# --- legacy import ----------------------------------------------------------------------

def make_legacy_store(path: Path, leads: list[Lead], *, place_index: bool = True,
                      extra_sql: str = "") -> Path:
    """A store in the original format, built with the original schema."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    if not place_index:
        conn.execute("DELETE FROM place_index")
    for lead_id, lead in enumerate(leads, start=1):
        conn.execute("INSERT INTO leads (id, name_key, locality, category, score, data,"
                     " created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                     (lead_id, lead.name_key, lead.locality, lead.category, lead.to_json(),
                      lead.first_seen, lead.last_updated))
        for phone in lead.phones:
            conn.execute("INSERT OR IGNORE INTO phone_index VALUES (?, ?)", (phone, lead_id))
    if extra_sql:
        conn.executescript(extra_sql)
    conn.commit()
    conn.close()
    return path


def legacy_fixture() -> list[Lead]:
    return [
        Lead(business_name="Royal Caterers", locality="Andheri", phones=["+919820012345"],
             sources=["google_maps"], first_seen=1_700_000_000.0, last_updated=1_700_000_000.0),
        Lead(business_name="Royal Catering Co", locality="Dadar", phones=["+919820012345"],
             emails=["hi@royal.in"], sources=["justdial"],
             first_seen=1_700_000_100.0, last_updated=1_700_000_500.0),
        Lead(business_name="Aayoni Banquet", locality="Andheri", gmaps_url=PLACE_URL,
             sources=["google_maps"], first_seen=1_700_000_200.0, last_updated=1_700_000_200.0),
        Lead(business_name="Access Denied", locality="Powai", sources=["justdial"]),
    ]


def test_copy_never_writes_the_original(tmp_path):
    original = make_legacy_store(tmp_path / "leads.sqlite3", legacy_fixture())
    before = (hashlib.sha256(original.read_bytes()).hexdigest(), original.stat().st_mtime_ns)
    copy = copy_legacy_store(original, tmp_path / "copy" / "leads.sqlite3")
    store = LeadStore(tmp_path / "app.sqlite3", project="Mumbai event vendors")
    import_legacy_store(copy, store)
    store.close()
    assert (hashlib.sha256(original.read_bytes()).hexdigest(), original.stat().st_mtime_ns) \
        == before


def test_replay_import_collapses_duplicates_and_keeps_dates(tmp_path):
    copy = make_legacy_store(tmp_path / "leads.sqlite3", legacy_fixture(), extra_sql="""
        INSERT INTO seen_urls VALUES ('https://jd.example/1', 'justdial', 1700000000);
        INSERT INTO dead_domains VALUES ('gone.example', 'dns_failure', 1700000000, 2);
        INSERT INTO unit_stats VALUES ('google_maps', 'Andheri', 'Caterers', 2, 40.0, 6, 1700000000);
        INSERT INTO enriched VALUES (2, 'website', 1700000900);
    """)
    store = LeadStore(tmp_path / "app.sqlite3", project="Mumbai event vendors")
    report = import_legacy_store(copy, store, mode="replay")
    assert report.legacy_rows == 4
    assert report.stored == store.count() == 2
    assert report.actions == {"new": 2, "merged": 1, "unchanged": 0}
    assert report.rejected_ids == [4]                         # the bot-wall title
    assert report.merged_clusters == [[1, 2]]
    royal = store.get(report.legacy_to_new()[1])
    assert royal.emails == ["hi@royal.in"]
    assert royal.first_seen == 1_700_000_000.0
    assert royal.last_updated == 1_700_000_500.0              # not the import time
    # Operational state came along, mapped onto where records ended up.
    assert store.already_seen("https://jd.example/1")
    assert store.is_dead_domain("gone.example")
    assert store.unit_stat("google_maps", "Andheri", "Caterers") == (20.0, 3.0)
    assert store.is_enriched(report.legacy_to_new()[2], "website")
    # The import is a run in the project's history.
    assert get_run(store.engine, report.run_id)["kind"] == "import"
    contacts = rows(store, Contact, organization_id=report.legacy_to_new()[1])
    assert {c["source"] for c in contacts} == {"legacy-import"}
    store.close()


def test_verbatim_import_keeps_every_row(tmp_path):
    copy = make_legacy_store(tmp_path / "leads.sqlite3", legacy_fixture()[:3])
    store = LeadStore(tmp_path / "app.sqlite3", project="P")
    report = import_legacy_store(copy, store, mode="verbatim")
    assert report.stored == 3 and report.merged_clusters == []
    legacy_ids = [o["legacy_id"] for o in rows(store, Organization)]
    assert legacy_ids == [1, 2, 3]
    # The original phone index said row 1 claimed the shared number.
    assert store.first_claimant("phone", "+919820012345", frozenset()) == \
        report.legacy_to_new()[1]
    store.close()


def test_place_is_indexed_for_a_store_written_before_place_index(tmp_path):
    """Ported from test_core: a pre-place_index store still matches by Maps place."""
    copy = make_legacy_store(tmp_path / "old.sqlite3", [legacy_fixture()[2]], place_index=False)
    store = LeadStore(tmp_path / "app.sqlite3", project="P")
    import_legacy_store(copy, store, mode="verbatim")
    _, action, _ = store.upsert(Lead(business_name="Aayoni Banquet", locality="Powai",
                                     gmaps_url=PLACE_URL))
    assert action in ("merged", "unchanged")
    assert store.count() == 1
    store.close()


def test_import_report_summary_names_the_numbers(tmp_path):
    copy = make_legacy_store(tmp_path / "leads.sqlite3", legacy_fixture())
    store = LeadStore(tmp_path / "app.sqlite3", project="Mumbai event vendors")
    summary = import_legacy_store(copy, store).summary()
    store.close()
    assert "4 legacy rows" in summary and "1 rejected" in summary
    json.dumps(summary)
