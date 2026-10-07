"""The FastAPI application: ``create_app()``; served by ``leadscraper api``.

Everything lives under ``/api/v1`` (OpenAPI at ``/api/v1/openapi.json``,
interactive docs at ``/api/v1/docs``). The API process never scrapes: it
reads and writes the database, and a worker (``leadscraper worker``) executes
the runs it queues.

**No authentication yet** (A.2.2). Serve it on localhost (the default) or on
a private network; ``docs/deploy.md`` covers putting it behind a reverse
proxy with access control on a server. When login arrives it goes into
``api/deps.py: current_workspace``.

Because there is no login, two browser attacks are closed off here:

* **DNS rebinding** -- only ``Host`` headers in ``LEADSCRAPER_ALLOWED_HOSTS``
  (default: localhost names; ``*`` allows any) are served;
* **cross-site requests** -- a POST / PATCH / DELETE carrying an ``Origin``
  header is refused unless that origin is a configured CORS origin or an
  allowed host, so a web page cannot cancel runs or purge data through the
  user's browser.

Errors are JSON ``{"detail": str, "errors": [...]}``: 404 not found, 409 a
conflict with the current state (a duplicate name, a run that cannot be
paused), 422 invalid input (a config that fails the schema, with the
schema's error list).
"""

from __future__ import annotations

import os

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.engine import Engine

from .. import __version__
from ..services.errors import ServiceError
from . import schemas as s
from .routers import ai, configs, operations, projects, runs, system

PREFIX = "/api/v1"
CORS_ENV = "LEADSCRAPER_CORS_ORIGINS"
HOSTS_ENV = "LEADSCRAPER_ALLOWED_HOSTS"
DEFAULT_HOSTS = "localhost,127.0.0.1,::1,testserver"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]

TAGS = [
    {"name": "system", "description": "Health and the run-config schema with UI hints."},
    {"name": "catalog", "description": "Lead types and sources: where they apply, health."},
    {"name": "projects", "description": "Long-lived lead databases runs merge into."},
    {"name": "configs", "description": "Saved, versioned run configs; presets."},
    {"name": "runs", "description": "Estimate, preview, run, control, history, events, diff."},
    {"name": "organizations", "description": "The leads: filter, inspect, erase."},
    {"name": "exports", "description": "XLSX / CSV / JSON / Google Sheets, from the database."},
    {"name": "suppression", "description": "Organisations to exclude or flag."},
    {"name": "lead types", "description": "Lead-type helpers: uploaded audit targets."},
    {"name": "schedules", "description": "Recurring runs of saved configs."},
    {"name": "notifications", "description": "Telling our own users that a run finished."},
    {"name": "settings", "description": "Retention, keep-awake, robots.txt."},
    {"name": "bundles", "description": "Move configs, projects and leads between machines."},
]

ERRORS = {404: {"model": s.Message, "description": "Not found."},
          409: {"model": s.Message, "description": "Conflicts with the current state."}}


def create_app(engine: Engine | None = None, *, db_url: str | None = None) -> FastAPI:
    if engine is None:
        from ..db.engine import open_database
        from ..paths import default_db_url

        engine = open_database(db_url or default_db_url())
    app = FastAPI(
        title="Skillminds lead generator",
        version=__version__,
        summary="Generate B2B leads from free public sources; never contacts them.",
        description=__doc__,
        openapi_tags=TAGS,
        openapi_url=f"{PREFIX}/openapi.json",
        docs_url=f"{PREFIX}/docs",
        redoc_url=f"{PREFIX}/redoc",
    )
    app.state.engine = engine

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"detail": exc.detail, "errors": exc.errors})

    origins = _env_list(CORS_ENV)
    hosts = _env_list(HOSTS_ENV, DEFAULT_HOSTS)

    @app.middleware("http")
    async def refuse_cross_site_writes(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in UNSAFE and origin and "*" not in hosts:
            origin_host = (urlsplit(origin).hostname or "").lower()
            if origin not in origins and origin_host not in hosts:
                return JSONResponse(status_code=403, content={
                    "detail": f"cross-site request from {origin} refused; add it to "
                              f"{CORS_ENV} to allow it", "errors": []})
        return await call_next(request)

    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                           allow_headers=["*"], expose_headers=["Content-Disposition"])
    if "*" not in hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    for module in (system, projects, configs, runs, operations, ai):
        app.include_router(module.router, prefix=PREFIX, responses=ERRORS)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(f"{PREFIX}/docs")

    @app.get("/health", include_in_schema=False)
    def health_alias(request: Request):
        """For container health checks that cannot follow the prefix."""
        return system.get_health(request.app.state.engine)

    return app
