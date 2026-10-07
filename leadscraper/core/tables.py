"""Uploaded tables (CSV / XLSX) as rows of text: suppression lists, audit targets."""

from __future__ import annotations

import csv
import io


class TableError(ValueError):
    """The upload is not a table we can read."""


def read_table(data: bytes, filename: str) -> list[list[str]]:
    """Non-empty rows of an uploaded .csv/.txt or .xlsx/.xlsm, every cell as text."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        except Exception as exc:
            raise TableError(f"could not read {filename}: {exc}") from None
        table = [["" if v is None else str(v) for v in row]
                 for row in sheet.iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt")) or not name:
        text = data.decode("utf-8-sig", errors="replace")
        table = list(csv.reader(io.StringIO(text)))
    else:
        raise TableError("upload a .csv or .xlsx file")
    return [row for row in table if any(cell.strip() for cell in row)]
