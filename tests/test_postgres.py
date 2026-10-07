"""Server mode on a real Postgres -- opt-in.

Point ``LEADSCRAPER_TEST_POSTGRES_URL`` at a THROWAWAY database (the test
drops and recreates the application tables in it), with the driver installed
(``pip install -e ".[postgres]"``), e.g.::

    docker run --rm -e POSTGRES_PASSWORD=pw -p 5433:5432 postgres:16
    LEADSCRAPER_TEST_POSTGRES_URL=postgresql+psycopg://postgres:pw@localhost:5433/postgres \\
        python -m pytest tests/test_postgres.py -q

Without it the test skips; the default suite still checks the Postgres DDL and
the offline migration SQL (tests/test_db.py).
"""

from __future__ import annotations

import os

import pytest

from leadscraper.core.lead import Lead

URL = os.environ.get("LEADSCRAPER_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="LEADSCRAPER_TEST_POSTGRES_URL not set"),
]


@pytest.fixture
def engine():
    from sqlalchemy import text

    from leadscraper.db.engine import make_engine
    from leadscraper.db.models import Base

    engine = make_engine(URL)
    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    yield engine
    engine.dispose()


def test_migrations_and_store_round_trip_on_postgres(engine):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from leadscraper.db.engine import alembic_config
    from leadscraper.db.models import Base
    from leadscraper.db.store import LeadStore

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []

    store = LeadStore(engine, project="Postgres check", country="IN")
    first, action, _ = store.upsert(Lead(business_name="Royal Caterers", locality="Andheri",
                                         phones=["+919820012345"], sources=["google_maps"]))
    assert action == "new"
    assert store.upsert(Lead(business_name="Royal Catering Co", locality="Dadar",
                             phones=["+919820012345"], sources=["justdial"]))[1] == "merged"
    second, _, _ = store.upsert(Lead(business_name="Sharma Decorators", locality="Powai",
                                     sources=["sulekha"]))
    store.merge_into(second, Lead(business_name="", phones=["+919820012345"],
                                  sources=["website"]))       # consolidates into `first`
    assert store.count() == 1
    assert set(store.get(first).sources) == {"google_maps", "justdial", "sulekha", "website"}
    store.mark_dead_domain("gone.example", "dns_failure")
    store.mark_dead_domain("gone.example", "dns_failure")
    assert store.dead_domains() == {"gone.example": "dns_failure"}
    store.rescore_all()
    assert store.sorted_leads()[0].business_name == "Royal Caterers"
    store.close()


def test_locality_scan_orders_categories_bytewise_on_postgres(engine):
    # The fuzzy-name rule breaks ties by the locality scan's order, which the
    # original SQLite store gave bytewise ("DJs" < "Decorators"). A Postgres
    # database with a locale collation would sort them the other way round.
    from leadscraper.db.store import LeadStore

    store = LeadStore(engine, project="Collation check", country="IN")
    for name, category in (("Asha Lights", "Decorators"), ("Bharat Sound", "DJs"),
                           ("Chandra Foods", "caterers"), ("Devi Events", "Event Planners")):
        store.upsert(Lead(business_name=name, locality="Andheri", category=category,
                          sources=["sulekha"]))
    scanned = [store.get(org_id).category
               for org_id, _, _ in store.in_locality("Andheri", frozenset())]
    assert scanned == sorted(scanned, key=lambda c: c.encode()) == [
        "DJs", "Decorators", "Event Planners", "caterers"]
    store.close()


# --- Phase 2: the engine on Postgres --------------------------------------------------------

def test_migration_0002_upgrades_existing_rows_on_postgres(engine):
    from alembic import command
    from sqlalchemy import text

    from leadscraper.db.engine import alembic_config
    from leadscraper.db.runs import get_run

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "0001")
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', now())"))
        conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug, description, "
                          "created_at, updated_at) VALUES (1, 1, 'p', 'p', '', now(), now())"))
        conn.execute(text("INSERT INTO runs (id, workspace_id, project_id, status, "
                          "config_snapshot, kind, stats, error, created_at) VALUES "
                          "(1, 1, 1, 'completed', '{}', 'run', '{}', '', now())"))
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    run = get_run(engine, 1)
    assert run["request"] == {} and run["checkpoint"] == {} and run["attempts"] == 0


def test_engine_run_pause_resume_and_claims_on_postgres(engine, monkeypatch, tmp_path):
    import sys
    from collections import Counter
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engine_fixtures import HOOKS, LEAD_TYPE, synthetic_config

    from leadscraper.config.compile import compile_run_config
    from leadscraper.config.schema import RunConfig
    from leadscraper.db.engine import init_schema
    from leadscraper.db.runs import claim_run, get_run, request_control, unit_rows
    from leadscraper.engine.events import read_events
    from leadscraper.engine.runner import RunRequest, execute, request_from_row
    from leadscraper.engine.worker import submit_run
    from leadscraper.leadtypes import registry

    init_schema(engine)
    monkeypatch.setenv("LEADSCRAPER_DB_URL", URL)
    registry.register(LEAD_TYPE)
    log = tmp_path / "pg.log"
    try:
        def pause_at_three(session, unit, index):
            if index == 3:
                request_control(session.store.engine, session.context.run_id, "pause")
        HOOKS["after_unit"] = pause_at_three
        config = compile_run_config(RunConfig.model_validate(synthetic_config(
            "pg-engine", units=[{"source": "src", "area": f"A{i}", "category": "C",
                                 "host": "h", "seconds": 1, "new": 1} for i in range(8)],
            real_seconds=0.03, log_path=str(log))))
        paused = execute(RunRequest(config=config, install_signals=False, discovery_workers=1))
        assert paused.status == "paused"
        HOOKS.clear()
        request_control(engine, paused.run_id, "resume")
        resumed = execute(request_from_row(get_run(engine, paused.run_id)))
        assert resumed.status == "completed"
        assert set(Counter(log.read_text().split()).values()) == {1}
        assert len(unit_rows(engine, paused.run_id)) == 8
        seqs = [e.seq for e in read_events(engine, paused.run_id)]
        assert seqs == list(range(1, len(seqs) + 1))

        queued = submit_run(RunRequest(config=config))
        assert claim_run(engine, queued, "worker:a") and not claim_run(engine, queued, "worker:b")
    finally:
        HOOKS.clear()
        registry.unregister("synthetic")
