"""Typed service errors; the API maps them to HTTP statuses, the CLI to exit codes."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base class. ``detail`` is safe to show to a user."""

    status = 400

    def __init__(self, detail: str, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    """The request clashes with the current state (a duplicate name, a run that is busy)."""

    status = 409


class Invalid(ServiceError):
    """The input is well-formed but not acceptable (a config that fails validation)."""

    status = 422


def validation_errors(exc: Exception) -> list[dict[str, Any]]:
    """Pydantic's error list, JSON-safe, or one entry with the message."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")),
                     "type": str(e.get("type", ""))} for e in errors()]
        except Exception:
            pass
    return [{"loc": [], "msg": str(exc), "type": "value_error"}]
