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
