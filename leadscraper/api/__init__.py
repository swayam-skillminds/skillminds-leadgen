"""The HTTP API (Phase 3): FastAPI routers over ``leadscraper.services``.

``create_app()`` builds the application; ``python -m leadscraper api`` serves it.
"""

from .app import create_app

__all__ = ["create_app"]
