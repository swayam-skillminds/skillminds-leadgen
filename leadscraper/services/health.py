"""``GET /health`` (Phase 3.1): is this installation able to run?

Checks, each ``ok`` / ``warn`` / ``fail`` with a detail line:

* ``database`` -- reachable, at the current schema revision;
* ``browser`` -- the Playwright package and a downloaded Chromium (found on
  disk, without launching it, so the check is cheap enough to poll);
* ``disk`` -- free space where the data directory lives (warn under 1 GB);
* ``keys`` -- which optional credentials are configured (never their values);
* ``worker`` -- a worker heartbeating in the last minute (runs need one).

Overall: ``down`` if the database fails, ``degraded`` if anything else is
not ``ok``, else ``ok``. The HTTP status is 503 only when ``down``.
"""

from __future__ import annotations

import os
import platform
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from .. import __version__
from ..db.models import Worker
from .query import aware, utcnow

WORKERS = Worker.__table__
WORKER_STALE_SECONDS = 60
LOW_DISK_BYTES = 1 << 30


def _check(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def database(engine: Engine) -> dict[str, Any]:
    from ..db.engine import current_revision, head_revision

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            revision = current_revision(conn)
    except Exception as exc:
        return _check("fail", f"unreachable: {type(exc).__name__}: {str(exc)[:200]}")
    head = head_revision()
    url = engine.url.render_as_string(hide_password=True)
    if revision != head:
        return _check("warn", f"schema at {revision}, code expects {head}; run "
                              "`leadscraper db upgrade`", revision=revision, url=url)
    return _check("ok", f"{engine.dialect.name}, schema {revision}", revision=revision, url=url)


def browser_dirs() -> list[Path]:
    """Where Playwright keeps downloaded browsers on this OS."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return [Path(override)]
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        candidates = [home / "Library" / "Caches" / "ms-playwright"]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        candidates = [Path(local) / "ms-playwright"]
    else:
        candidates = [Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "ms-playwright"]
    if override == "0":
        try:
            import playwright

            candidates.insert(0, Path(playwright.__file__).parent / "driver" / "package"
                              / ".local-browsers")
        except Exception:
            pass
    return candidates


def browser() -> dict[str, Any]:
    try:
        from importlib.metadata import version

        package = version("playwright")
    except Exception:
        return _check("fail", "the playwright package is not installed")
    found: list[str] = []
    for directory in browser_dirs():
        if directory.is_dir():
            found += sorted(p.name for p in directory.iterdir()
                            if p.is_dir() and p.name.startswith("chromium"))
    if not found:
        return _check("fail", f"Playwright {package} is installed but Chromium is not: run "
                              "`playwright install chromium`", playwright=package)
    return _check("ok", f"Playwright {package}, {', '.join(found)}", playwright=package,
                  browsers=found)


def disk() -> dict[str, Any]:
    from ..paths import data_dir

    path = data_dir()
    usage = shutil.disk_usage(path)
    status = "warn" if usage.free < LOW_DISK_BYTES else "ok"
    return _check(status, f"{usage.free / 1e9:.1f} GB free at {path}", free_bytes=usage.free,
                  total_bytes=usage.total, path=str(path))


def keys() -> dict[str, Any]:
    """Which optional credentials exist -- booleans only, never values."""
    service_account = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")) or any(
        p.exists() for p in (Path.home() / ".config" / "lead-scraper" / "credentials.json",))
    configured = {
        "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "claude_cli": shutil.which("claude") is not None,
        "google_service_account": service_account,
    }
    names = [k for k, v in configured.items() if v]
    return _check("ok", ", ".join(names) or "none configured (all optional)",
                  configured=configured)


def worker(engine: Engine) -> dict[str, Any]:
    cutoff = utcnow() - timedelta(seconds=WORKER_STALE_SECONDS)
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(WORKERS.c.name, WORKERS.c.status, WORKERS.c.heartbeat_at,
                                       WORKERS.c.current_run_id)
                                .where(WORKERS.c.status != "stopped")).all()
    except Exception as exc:
        return _check("fail", f"cannot read workers: {exc}")
    alive = [r for r in rows if aware(r.heartbeat_at) and aware(r.heartbeat_at) >= cutoff]
    workers = [{"name": r.name, "status": r.status, "run_id": r.current_run_id,
                "heartbeat_at": aware(r.heartbeat_at)} for r in alive]
    if not alive:
        return _check("warn", "no worker is running: queued runs wait until one starts "
                              "(`leadscraper worker`)", workers=[])
    return _check("ok", f"{len(alive)} worker(s) alive", workers=workers)


def health(engine: Engine) -> dict[str, Any]:
    checks = {"database": database(engine)}
    for name, fn in (("browser", browser), ("disk", disk), ("keys", keys)):
        try:
            checks[name] = fn()
        except Exception as exc:
            checks[name] = _check("warn", f"check failed: {exc}")
    checks["worker"] = (worker(engine) if checks["database"]["status"] != "fail"
                        else _check("fail", "database unreachable"))
    if checks["database"]["status"] == "fail":
        status = "down"
    elif any(c["status"] != "ok" for c in checks.values()):
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "version": __version__, "time": utcnow(),
            "mode": os.environ.get("LEADSCRAPER_MODE", "local"), "checks": checks}
