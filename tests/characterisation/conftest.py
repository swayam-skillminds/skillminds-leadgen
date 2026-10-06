"""Fixtures shared by the characterisation suite. See snapshot_lib.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from leadscraper.core import normalize  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_name_noise():
    """name_key's extra noise is process-global; never leak it between tests."""
    normalize.set_extra_name_noise([])
    yield
    normalize.set_extra_name_noise([])
