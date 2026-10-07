"""Where things live on disk.

Two roots, kept apart on purpose:

* ``PROJECT_ROOT`` -- the source checkout. Legacy config paths ("data/leads.csv")
  are relative to it, so existing runs keep writing where they always did.
* ``data_dir()`` -- per-user application state (the lead database, caches),
  from ``platformdirs`` so it lands in the right place on macOS, Windows and
  Linux. ``LEADSCRAPER_DATA_DIR`` overrides it, e.g. for a server volume.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

APP_NAME = "leadscraper"
APP_AUTHOR = "Skillminds"

DATA_DIR_ENV = "LEADSCRAPER_DATA_DIR"
DB_URL_ENV = "LEADSCRAPER_DB_URL"


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        path = Path(override).expanduser()
    else:
        from platformdirs import user_data_dir

        path = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db_url() -> str:
    """The application database: ``LEADSCRAPER_DB_URL``, else SQLite in data_dir()."""
    url = os.environ.get(DB_URL_ENV)
    if url:
        return url
    return f"sqlite:///{(data_dir() / 'leadscraper.sqlite3').as_posix()}"
