"""Local CSV export.

Always written, even when Sheets is configured -- a run should never be lost
because of a credentials or quota problem at the very end.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable

from ..core.lead import Lead

log = logging.getLogger(__name__)


def leads_to_rows(leads: Iterable[Lead], profile: str = "vendors",
                  currency: str = "INR") -> list[list]:
    """Header row followed by one row per lead, in the profile's column layout."""
    headers = Lead.row_headers(profile, currency)
    rows: list[list] = [headers]
    for lead in leads:
        row = lead.row_for(profile, currency)
        rows.append([row.get(header, "") for header in headers])
    return rows


def write_csv(leads: Iterable[Lead], path: str | Path, profile: str = "vendors",
              currency: str = "INR") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = leads_to_rows(leads, profile, currency)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        csv.writer(handle).writerows(rows)
    log.info("wrote %d leads to %s", len(rows) - 1, path)
    return path


def write_failures_csv(rows: list[list], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        csv.writer(handle).writerows(rows)
    return path
