"""The real-data characterisation, migrated into Postgres instead of SQLite.

Server mode must make the same decisions as local mode: the same rows in the
same order, the same scores and tiers, the same dedup clusters. This runs both
migrations of each frozen store into a Postgres project and compares with the
SQLite goldens (it never rewrites them).

Opt-in: needs ``LEADSCRAPER_TEST_POSTGRES_URL`` (a THROWAWAY database; its
application tables are dropped) and the frozen baselines. Use a database with
a locale collation (``createdb -T template0 -l en_US.UTF-8 ...``) to prove the
ordering does not depend on it.
"""

import json
import os

import pytest

from snapshot_lib import GOLDEN_DIR, PROFILES, Scorer, baseline_dir, real_snapshot

URL = os.environ.get("LEADSCRAPER_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.characterisation,
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="LEADSCRAPER_TEST_POSTGRES_URL not set"),
]


def _postgres_opener():
    from sqlalchemy import text

    from leadscraper.db.engine import make_engine
    from leadscraper.db.models import Base
    from leadscraper.db.store import DedupOptions, LeadStore

    engine = make_engine(URL)

    def opener(_path, config):
        with engine.begin() as conn:
            Base.metadata.drop_all(conn)
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        return LeadStore(engine, Scorer(config), DedupOptions.from_config(config),
                         project="characterisation", country=config.country)

    return engine, opener


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_postgres_reproduces_the_real_data_baseline(profile, tmp_path):
    baseline = baseline_dir() / PROFILES[profile][2]
    if not baseline.exists():
        pytest.skip(f"no frozen baseline at {baseline}")
    engine, opener = _postgres_opener()
    try:
        actual = json.loads(json.dumps(real_snapshot(profile, baseline, tmp_path, opener)))
    finally:
        engine.dispose()
    expected = json.loads((GOLDEN_DIR / f"real_{profile}.json").read_text(encoding="utf-8"))
    assert actual == expected
