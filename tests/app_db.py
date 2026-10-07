"""The application database for the Phase 3 suites (see fresh_application_db)."""

from __future__ import annotations


def fresh_application_db():
    """An engine on the test's application database, emptied first when it is Postgres.

    With ``LEADSCRAPER_TEST_ENGINE_DB_URL`` (a THROWAWAY Postgres) every test
    shares one database; the Phase 3 suites assume an empty one, so the schema
    is dropped and rebuilt per test. SQLite (the default) is a fresh file per
    test already.
    """
    from sqlalchemy import create_engine, text

    from leadscraper.db.engine import open_database
    from leadscraper.paths import default_db_url

    url = default_db_url()
    if url.startswith("postgresql"):
        reset = create_engine(url)
        with reset.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        reset.dispose()
    return open_database(url)
