"""Dependencies every route shares: the database and the current workspace.

A.2.2: all request handling goes through :func:`current_workspace`. Today it
is the single default workspace; when login arrives, this one function
resolves the caller's workspace from their credentials, and nothing else in
the API changes.
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.engine import Engine

from ..services.workspace import Workspace, default_workspace


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def current_workspace(request: Request, engine: Engine = Depends(get_engine)) -> Workspace:
    cached = getattr(request.app.state, "workspace", None)
    if cached is None:
        cached = default_workspace(engine)
        request.app.state.workspace = cached
    return cached
