"""What every export format renders: the records, their columns and the run's facts.

Built from the database by ``services/exports.build_table``; rendered by
``export/excel.py`` (the deliverable workbook) and by the CSV and JSON
renderers below. All three share the column model, so a value never differs
between formats -- only its presentation does (a hyperlink and a number
format in the workbook, plain text in the CSV, typed values in the JSON).
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .columns import Column
from .records import ExportRecord


@dataclass
class ExportTable:
    project: dict[str, Any]
    run: dict[str, Any] | None
    scope: str
    profile: str
    currency: str
    columns: list[Column]
    records: list[ExportRecord] = field(default_factory=list)
    excluded: int = 0
    against: int | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: How the records are ordered, in words ("score (highest first)").
    sort_label: str = ""
    #: IANA zone dates are shown in (the run's).
    timezone: str = "UTC"
    #: Excel prefix for money ("₹", "$", "AED ").
    money_prefix: str = ""
    #: Facts about the run and project for the Overview (services/exports.py).
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def headers(self) -> list[str]:
        return [c.header for c in self.columns]

    def local(self, value: datetime) -> datetime:
        """An aware datetime in the run's zone, made naive (spreadsheets have no zones)."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            zone = ZoneInfo(self.timezone)
        except Exception:
            zone = timezone.utc
        return value.astimezone(zone).replace(tzinfo=None, microsecond=0)

    def text(self, column: Column, value: Any) -> str:
        """A value as the CSV shows it."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, datetime):
            return self.local(value).strftime("%Y-%m-%d" if column.number_format == "dd-mmm-yy"
                                              else "%Y-%m-%d %H:%M")
        if isinstance(value, float) and value.is_integer() and column.kind != "score":
            return str(int(value))
        return str(value)

    def typed(self, value: Any) -> Any:
        """A value as the JSON carries it."""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return value


# --- CSV and JSON ------------------------------------------------------------------------------

#: A scraped value that a spreadsheet would run as a formula (CSV/formula injection):
#: it starts with = or @ (or a tab / CR), or with + / - and then something that is
#: not a number -- "+91 98200 12345" stays as it is, "+HYPERLINK(...)" does not.
_FORMULA = re.compile(r"^(?:[=@\t\r]|[+-](?![\d\s().+-]*$))")


def safe_cell(value: Any) -> Any:
    """Neutralise formula-looking text for CSV (a leading apostrophe, as Excel shows it)."""
    if isinstance(value, str) and value and _FORMULA.match(value):
        return "'" + value
    return value


def to_csv(table: ExportTable) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(table.headers)
    for record in table.records:
        writer.writerow([safe_cell(table.text(c, c.value(record))) for c in table.columns])
    return buffer.getvalue().encode("utf-8-sig")


def to_json(table: ExportTable) -> bytes:
    from .summary import summarise

    columns = [{"key": c.key, "header": c.header, "kind": c.kind, "group": c.group,
                "description": c.description or None} for c in table.columns]
    records = []
    for record in table.records:
        records.append({
            "id": record.id,
            "score": record.score,
            "values": {c.key: table.typed(c.value(record)) for c in table.columns},
            "lead_types": list(record.lead_types),
            "scores": {"total": record.overall.total, "fit": record.overall.fit,
                       "intent": record.overall.intent, "reach": record.overall.reach,
                       "reasons": list(record.overall.reasons),
                       "contributions": record.overall.contributions},
            "signals": [{k: table.typed(v) for k, v in s.items() if k != "type_title"}
                        for s in record.values.get("_signals", [])],
            "people": record.people,
            "inferred_contacts": record.inferred,
            "change": record.change,
            "suppressed": list(record.flags),
        })
    document = {
        "project": {"id": table.project["id"], "name": table.project["name"]},
        "run_id": table.run["id"] if table.run else None, "scope": table.scope,
        "against": table.against, "profile": table.profile, "currency": table.currency,
        "generated_at": table.typed(table.generated_at), "sorted_by": table.sort_label,
        "timezone": table.timezone, "columns": columns, "count": len(records),
        "excluded_by_suppression": table.excluded,
        "summary": _jsonable(summarise(table)), "records": records,
    }
    return json.dumps(document, ensure_ascii=False, indent=1, default=str).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ExportRecord):
        return value.id
    return value
