"""Helpers the lead types expose to the API and CLI (Phase 6).

``audit_targets``: an uploaded CSV/XLSX of businesses or domains, read into
``lead_types.digital_audit.targets`` entries for a config. Nothing is stored
or run: the caller puts the list in a config (the UI's upload button).
"""

from __future__ import annotations

from typing import Any

from .errors import Invalid


def audit_targets(data: bytes, filename: str) -> dict[str, Any]:
    """``{targets, read, skipped}`` from an uploaded table of businesses or domains."""
    from ..core.tables import TableError
    from ..leadtypes.digital_audit.targets import parse_targets

    try:
        targets, skipped = parse_targets(data, filename)
    except TableError as exc:
        raise Invalid(str(exc)) from None
    rows = [t.model_dump(exclude_defaults=True) for t in targets]
    return {"targets": rows, "read": len(rows) + skipped, "skipped": skipped}
