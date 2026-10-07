"""Workspace settings (Phase 3.1): retention, keep-awake, robots.txt.

Stored as JSON on the workspace row, merged over :data:`DEFAULTS`, validated
by :class:`WorkspaceSettings`. They are defaults for runs: a run config that
sets ``runtime.keep_awake`` or ``runtime.robots_txt`` itself wins.

The data directory and database are process settings (``LEADSCRAPER_DATA_DIR``,
``LEADSCRAPER_DB_URL``): shown here, changed by restarting with another value.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from ..core.secrets import mask_db_url
from ..db.models import Workspace as WorkspaceRow
from .errors import Invalid, validation_errors
from .workspace import Workspace

WORKSPACES = WorkspaceRow.__table__


class Retention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs_days: int | None = Field(
        30, ge=1, description="Delete run logs and failure logs (files) older than this.")
    events_days: int | None = Field(
        90, ge=1, description="Delete the progress events of finished runs older than this.")
    seen_urls_days: int | None = Field(
        90, ge=1, description="Forget fetched-URL memos older than this (pages are re-fetched).")


class WorkspaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention: Retention = Field(default_factory=Retention)
    keep_awake: bool = Field(True, description="Hold a keep-awake lock while a run is active "
                                               "(local mode).")
    robots_txt: bool = Field(True, description="Honour robots.txt when crawling businesses' own "
                                               "websites.")


DEFAULTS: dict[str, Any] = WorkspaceSettings().model_dump()


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def stored(engine: Engine, workspace_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(WORKSPACES.c.settings).where(
            WORKSPACES.c.id == workspace_id)).first()
    return dict(row[0] or {}) if row else {}


def effective(engine: Engine, workspace_id: int) -> WorkspaceSettings:
    merged = _merge(DEFAULTS, stored(engine, workspace_id))
    try:
        return WorkspaceSettings.model_validate(merged)
    except ValueError:
        return WorkspaceSettings()


def process_info() -> dict[str, Any]:
    import os

    from ..paths import data_dir, default_db_url

    return {"data_dir": str(data_dir()), "database": mask_db_url(default_db_url()),
            "mode": os.environ.get("LEADSCRAPER_MODE", "local")}


def get_settings(engine: Engine, ws: Workspace) -> dict[str, Any]:
    return {**effective(engine, ws.id).model_dump(), **process_info()}


def update_settings(engine: Engine, ws: Workspace, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the workspace's settings (nested keys merge, not replace)."""
    readonly = {"data_dir", "database", "mode"} & set(patch or {})
    if readonly:
        raise Invalid(f"{', '.join(sorted(readonly))} cannot be changed here: set "
                      "LEADSCRAPER_DATA_DIR / LEADSCRAPER_DB_URL / LEADSCRAPER_MODE and restart")
    merged = _merge(_merge(DEFAULTS, stored(engine, ws.id)), patch or {})
    try:
        valid = WorkspaceSettings.model_validate(merged)
    except ValueError as exc:
        raise Invalid("invalid settings", errors=validation_errors(exc)) from None
    with engine.begin() as conn:
        conn.execute(update(WORKSPACES).where(WORKSPACES.c.id == ws.id)
                     .values(settings=valid.model_dump()))
    return get_settings(engine, ws)


def apply_to_config(engine: Engine, workspace_id: int, config: Any) -> None:
    """Fill ``runtime.keep_awake`` / ``runtime.robots_txt`` a run config leaves unset."""
    settings = effective(engine, workspace_id)
    for key, value in (("keep_awake", settings.keep_awake), ("robots_txt", settings.robots_txt)):
        if config.get(f"runtime.{key}") is None:
            config.set(f"runtime.{key}", value, record=False)
