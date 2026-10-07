"""The current workspace (A.2.2).

There is one default workspace until authentication arrives. Every service
takes a :class:`Workspace`; the API resolves it in one dependency
(``api/deps.py: current_workspace``) and the CLI with :func:`default_workspace`,
so adding login later changes exactly one place per layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db.models import Workspace as WorkspaceRow

DEFAULT_WORKSPACE = "default"
WORKSPACES = WorkspaceRow.__table__


@dataclass(frozen=True)
class Workspace:
    id: int
    name: str


def ensure_workspace(engine: Engine, name: str = DEFAULT_WORKSPACE) -> Workspace:
    """The workspace called `name`, created on first use."""
    with engine.begin() as conn:
        row = conn.execute(select(WORKSPACES.c.id).where(WORKSPACES.c.name == name)).first()
        if row is not None:
            return Workspace(int(row[0]), name)
        new_id = conn.execute(WORKSPACES.insert().values(
            name=name, created_at=datetime.now(timezone.utc), settings={})).inserted_primary_key[0]
    return Workspace(int(new_id), name)


def default_workspace(engine: Engine) -> Workspace:
    return ensure_workspace(engine, DEFAULT_WORKSPACE)
