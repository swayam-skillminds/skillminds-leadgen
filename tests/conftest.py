"""Suite-wide fixtures."""

import os
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
    # Opt-in: run the suites that use the application DB (test_engine.py) on a
    # THROWAWAY Postgres database instead of a per-test SQLite file.
    engine_db = os.environ.get("LEADSCRAPER_TEST_ENGINE_DB_URL")
    if engine_db:
        monkeypatch.setenv("LEADSCRAPER_DB_URL", engine_db)
    # Runs never hold a real OS keep-awake lock in tests (test_keepawake mocks it).
    monkeypatch.setenv("LEADSCRAPER_KEEP_AWAKE", "0")



@pytest.fixture(autouse=True)
def _no_real_ai(request, monkeypatch):
    """No test reaches a real AI provider (Phase 5): no keys, no `claude` CLI.

    Provider tests inject fake transports; a test that forgets would find every
    provider "not configured" and AI off -- never a live call. Tests marked
    ``live`` (opt-in, ``-m live``) keep the real environment.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("LEADSCRAPER_SECRET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEADSCRAPER_NO_KEYRING", "1")
    monkeypatch.delenv("LEADSCRAPER_SECRETS_DIR", raising=False)
    from leadscraper.ai.providers import claude_code_cli

    monkeypatch.setattr(claude_code_cli, "_which", lambda name: None)
