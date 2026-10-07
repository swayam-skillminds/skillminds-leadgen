"""Database engines and schema management.

One schema, two backends: SQLite (local mode; WAL, so a reader never blocks
the writer) and Postgres (server mode). The URL decides; nothing else does.

``open_database`` brings a database to the current schema: a brand-new one
is created from the models and stamped at the latest Alembic revision; an
older one is upgraded through the migrations. A file that is one of the
original lead stores (a ``leads`` table, no ``alembic_version``) is refused --
those are precious and are only ever read, from a copy, by the legacy
importer.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine

from .models import Base

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent


class LegacyDatabaseError(RuntimeError):
    """The path is an original (pre-Phase-1) lead store, not an application DB."""


def url_for(target: str | Path) -> str:
    """A path becomes a SQLite URL; a URL is returned as it is."""
    text = str(target)
    if "://" in text:
        return text
    return f"sqlite:///{Path(target).expanduser().resolve().as_posix()}"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def make_engine(url: str) -> Engine:
    if is_sqlite(url):
        memory = url in ("sqlite://", "sqlite:///:memory:") or "mode=memory" in url
        if not memory:
            Path(url.split("///", 1)[-1].split("?", 1)[0]).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            # Per-connection settings only: none of these writes to the file.
            # WAL is persistent and is switched on by init_schema, once the
            # file is known to be ours (never on an original lead store).
            cursor = dbapi_conn.cursor()
            if not memory:
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def alembic_config(connection=None, url: str | None = None):
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["connection"] = connection
    return cfg


@lru_cache(maxsize=1)
def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(connection) -> str | None:
    from alembic.runtime.migration import MigrationContext

    return MigrationContext.configure(connection).get_current_revision()


_init_lock = threading.Lock()


def _sqlite_file(url: str) -> Path | None:
    if not is_sqlite(url) or url in ("sqlite://", "sqlite:///:memory:") or "mode=memory" in url:
        return None
    return Path(url.split("///", 1)[-1].split("?", 1)[0])


def refuse_legacy_store(url: str) -> None:
    """Raise before anything opens an original lead store for writing.

    Checked through a read-only connection: even a pragma can rewrite a
    SQLite file's header, and those files are precious (A.2.6).
    """
    import sqlite3

    path = _sqlite_file(url)
    if path is None or not path.exists() or path.stat().st_size == 0:
        return
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    if "leads" in tables and "alembic_version" not in tables:
        raise LegacyDatabaseError(
            f"{path} is an original lead store; import it with "
            "`python -m leadscraper migrate-legacy` instead of opening it")


def init_schema(engine: Engine) -> None:
    """Create, upgrade, or refuse -- see the module docstring."""
    refuse_legacy_store(str(engine.url))
    sqlite = is_sqlite(str(engine.url))
    with _init_lock, engine.connect() as connection:
        if sqlite:
            # SQLite migrations run in batch mode, which rebuilds a table (copy,
            # DROP the old one, rename). With foreign keys enforced, that DROP
            # cascade-deletes every child row -- a run's units, failures and
            # memberships -- so they are off for the migration. Set outside any
            # transaction: SQLite ignores the pragma inside one.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            try:
                with connection.begin():
                    if not sqlite:
                        # Two processes (the API and a worker) starting at once
                        # must not both migrate: one waits, then finds it done.
                        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(4207211)")
                    _create_or_upgrade(connection)
            except Exception:
                # Another process may have migrated under us (SQLite has no
                # advisory lock): fine if the schema is current now.
                with connection.begin():
                    if current_revision(connection) != head_revision():
                        raise
                log.info("schema at %s was brought current by another process",
                         engine.url.render_as_string(hide_password=True))
        finally:
            if sqlite:
                connection.rollback()        # whatever a failed migration left open
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


def _create_or_upgrade(connection) -> None:
    from alembic import command

    url = str(connection.engine.url)
    if _sqlite_file(url) is not None:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    tables = set(inspect(connection).get_table_names())
    if "leads" in tables and "alembic_version" not in tables:
        raise LegacyDatabaseError(
            f"{url} is an original lead store; import it with "
            "`python -m leadscraper migrate-legacy` instead of opening it")
    if not (tables - {"alembic_version"}):
        # Fresh database: the models are the schema; record it as current.
        Base.metadata.create_all(connection)
        command.stamp(alembic_config(connection), "head")
        log.debug("created schema at %s (revision %s)", url, head_revision())
        return
    if current_revision(connection) != head_revision():
        log.info("upgrading %s to schema revision %s", url, head_revision())
        command.upgrade(alembic_config(connection), "head")


def open_database(target: str | Path) -> Engine:
    url = url_for(target)
    refuse_legacy_store(url)
    engine = make_engine(url)
    try:
        init_schema(engine)
    except Exception:
        engine.dispose()
        raise
    return engine
