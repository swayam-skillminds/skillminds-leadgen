"""Suite-wide fixtures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadscraper.core import normalize  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_name_noise():
    """name_key's run noise is process-global; never leak it between tests."""
    normalize.reset_name_noise()
    yield
    normalize.reset_name_noise()


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    """Application state (DB, caches) goes to a temp dir, never the real one."""
    monkeypatch.setenv("LEADSCRAPER_DATA_DIR", str(tmp_path_factory.mktemp("appdata")))
    monkeypatch.delenv("LEADSCRAPER_DB_URL", raising=False)
    # Runs never hold a real OS keep-awake lock in tests (test_keepawake mocks it).
    monkeypatch.setenv("LEADSCRAPER_KEEP_AWAKE", "0")
