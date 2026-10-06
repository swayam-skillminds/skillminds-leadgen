"""Failure/skip logging.

Nothing in this pipeline is allowed to crash a run. Anything that fails lands
here as a JSONL record so it can be retried or chased manually later.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class FailureLog:
    """Append-only JSONL log of everything we couldn't fetch or parse."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []

    def record(
        self,
        source: str,
        reason: str,
        *,
        url: str | None = None,
        query: str | None = None,
        locality: str | None = None,
        category: str | None = None,
        business: str | None = None,
        detail: str | None = None,
    ) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "reason": reason,
            "url": url,
            "query": query,
            "locality": locality,
            "category": category,
            "business": business,
            "detail": (detail or "")[:500],
        }
        with self._lock:
            self._records.append(entry)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:  # never let logging break the run
                log.warning("Could not write failure log: %s", exc)
        log.debug("skipped [%s] %s (%s)", source, business or url or query, reason)

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def summary(self) -> dict[str, int]:
        """Counts per source, for the end-of-run report."""
        counts: dict[str, int] = {}
        for entry in self.records:
            counts[entry["source"]] = counts.get(entry["source"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def rows(self) -> list[list[Any]]:
        """Tabular form for the 'Skipped' worksheet."""
        headers = ["Time", "Source", "Reason", "Business", "Query", "Locality",
                   "Category", "URL", "Detail"]
        rows = [headers]
        for entry in self.records:
            rows.append([
                entry["ts"], entry["source"], entry["reason"], entry.get("business") or "",
                entry.get("query") or "", entry.get("locality") or "",
                entry.get("category") or "", entry.get("url") or "", entry.get("detail") or "",
            ])
        return rows
