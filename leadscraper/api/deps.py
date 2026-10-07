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


MAX_UPLOAD_ENV = "LEADSCRAPER_MAX_UPLOAD_MB"


async def read_upload(file) -> bytes:
    """An uploaded file's bytes, refused past ``LEADSCRAPER_MAX_UPLOAD_MB`` (default 200)."""
    import os

    from ..services.errors import Invalid

    limit = int(float(os.environ.get(MAX_UPLOAD_ENV, "200")) * 1024 * 1024)
    chunks, size = [], 0
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise Invalid(f"the upload is larger than {limit // (1024 * 1024)} MB "
                          f"({MAX_UPLOAD_ENV})")
        chunks.append(chunk)
