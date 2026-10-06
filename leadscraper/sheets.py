"""Incremental Google Sheets output via the free Sheets API.

Auth is a Google Cloud service account (free): create one, enable the Sheets +
Drive APIs, download the JSON key, and share the target spreadsheet with the
service account's email. See README.md for the click-path.

Write model: the sheet mirrors the store's sorted table. Every flush rewrites
the value range in one batch call rather than appending row-by-row, because
the ranking can reorder existing rows as new sources corroborate a lead.
Batching also keeps us well inside the free quota (60 writes/min/user).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

from .export import leads_to_rows
from .models import Lead

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _explain(exc: BaseException) -> str:
    """Turn a gspread/Google exception into something actionable.

    gspread raises a bare `PermissionError` with an EMPTY message for any HTTP
    403, which tells the user nothing. The real Google error -- "API not
    enabled", "caller does not have permission" -- is on the chained cause's
    response body, so dig it out.
    """
    for candidate in (exc, exc.__cause__, exc.__context__):
        if candidate is None:
            continue
        response = getattr(candidate, "response", None)
        if response is None:
            continue
        try:
            payload = response.json().get("error", {})
        except Exception:
            payload = {}
        message = payload.get("message") or getattr(response, "text", "")
        if message:
            status = payload.get("status") or getattr(response, "status_code", "")
            hint = ""
            if "has not been used in project" in message or payload.get(
                "details", [{}]
            )[0].get("reason") == "SERVICE_DISABLED":
                hint = " -- enable that API in Google Cloud Console, then retry"
            elif "does not have permission" in message.lower():
                hint = " -- share the spreadsheet with the service account as Editor"
            return f"{status} {str(message)[:300]}{hint}".strip()

    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else (
        f"{type(exc).__name__} (no detail provided by the library)"
    )


def column_letter(index: int) -> str:
    """1-based column number -> A1 letters: 1 -> A, 26 -> Z, 27 -> AA."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


class SheetsWriter:
    """Writes the running lead table to a Google Sheet.

    Degrades gracefully: if credentials are missing or the API errors, the
    run continues and everything still lands in the CSV export.
    """

    def __init__(self, config, failures=None):
        self.config = config
        self.failures = failures
        self.enabled = bool(config.get("sheets.enabled", False))
        self.spreadsheet_id = (config.get("sheets.spreadsheet_id", "") or "").strip()
        self.spreadsheet_name = config.get("sheets.spreadsheet_name", "Leads")
        self.worksheet_name = config.get("sheets.worksheet_name", "Leads")
        self.failures_worksheet_name = config.get("sheets.failures_worksheet_name", "Skipped")
        self.profile = str(config.get("profile", "vendors") or "vendors").lower()
        self._client = None
        self._spreadsheet = None
        self._worksheet = None
        self._last_row_count = 0
        self._unavailable_reason: str | None = None

    # --- connection -------------------------------------------------------

    @property
    def available(self) -> bool:
        if not self.enabled:
            self._unavailable_reason = "sheets.enabled is false in config"
            return False
        if self._worksheet is not None:
            return True
        return self._connect()

    def _connect(self) -> bool:
        creds_path = self.config.google_credentials_path
        if not creds_path or not Path(creds_path).exists():
            self._unavailable_reason = (
                "no service-account JSON found (set GOOGLE_SERVICE_ACCOUNT_JSON "
                "in .env or place credentials.json in the project root)"
            )
            log.warning("Google Sheets disabled: %s", self._unavailable_reason)
            return False
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
            self._client = gspread.authorize(creds)
            self._spreadsheet = self._open_spreadsheet()
            self._worksheet = self._ensure_worksheet(self.worksheet_name)
            log.info("Google Sheets connected: %s", self._spreadsheet.url)
            return True
        except Exception as exc:
            self._unavailable_reason = _explain(exc)
            log.warning("Google Sheets unavailable (%s) -- continuing with CSV output",
                        self._unavailable_reason)
            return False

    def _open_spreadsheet(self):
        import gspread

        if self.spreadsheet_id:
            return self._client.open_by_key(self.spreadsheet_id)
        try:
            return self._client.open(self.spreadsheet_name)
        except gspread.SpreadsheetNotFound:
            log.info("creating spreadsheet %r", self.spreadsheet_name)
            spreadsheet = self._client.create(self.spreadsheet_name)
            share_with = self.config.get("sheets.share_with", None)
            if share_with:
                spreadsheet.share(share_with, perm_type="user", role="writer")
            return spreadsheet

    def _ensure_worksheet(self, title: str):
        import gspread

        try:
            return self._spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return self._spreadsheet.add_worksheet(title=title, rows=1000, cols=60)

    # --- writing ----------------------------------------------------------

    def sync(self, leads: Iterable[Lead]) -> bool:
        """Rewrite the whole sorted table. Returns True on success."""
        if not self.available:
            return False
        rows = leads_to_rows(leads, self.profile)
        try:
            self._write_rows(self._worksheet, rows)
            self._format_header(self._worksheet)
            self._last_row_count = len(rows)
            log.info("synced %d leads to Google Sheets", len(rows) - 1)
            return True
        except Exception as exc:
            self._record_failure("sheet_sync_failed", repr(exc))
            log.warning("Sheets sync failed (%s) -- CSV export still written", exc)
            return False

    def sync_failures(self, rows: list[list]) -> bool:
        """Write the skipped/failed lookups to their own worksheet."""
        if not self.available or len(rows) <= 1:
            return False
        try:
            worksheet = self._ensure_worksheet(self.failures_worksheet_name)
            self._write_rows(worksheet, rows)
            return True
        except Exception as exc:
            log.warning("could not write failures worksheet: %s", exc)
            return False

    def _write_rows(self, worksheet, rows: list[list], attempts: int = 4) -> None:
        """Resize, batch-update, and clear any rows left over from a longer run."""
        needed_rows = max(len(rows) + 50, 100)
        needed_cols = max((len(r) for r in rows), default=10)

        for attempt in range(1, attempts + 1):
            try:
                if worksheet.row_count < needed_rows or worksheet.col_count < needed_cols:
                    worksheet.resize(rows=needed_rows, cols=max(needed_cols, worksheet.col_count))
                worksheet.update(
                    values=self._stringify(rows),
                    range_name="A1",
                    value_input_option="USER_ENTERED",
                )
                # A shorter table than last time would leave stale rows behind.
                if self._last_row_count > len(rows):
                    last_col = column_letter(max(needed_cols, 26))
                    worksheet.batch_clear([f"A{len(rows) + 1}:{last_col}{self._last_row_count + 1}"])
                return
            except Exception as exc:
                if attempt == attempts:
                    raise
                # 429 / transient 5xx: back off and retry.
                delay = 5 * (2 ** (attempt - 1))
                log.info("Sheets write failed (%s); retrying in %ds", exc, delay)
                time.sleep(delay)

    @staticmethod
    def _stringify(rows: list[list]) -> list[list]:
        """gspread rejects None; numbers are fine and stay numeric in the sheet."""
        return [["" if cell is None else cell for cell in row] for row in rows]

    def _format_header(self, worksheet) -> None:
        """Freeze and bold the header row. Cosmetic -- never fails the sync."""
        try:
            worksheet.freeze(rows=1)
            last_col = column_letter(max(worksheet.col_count, 26))
            worksheet.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
        except Exception:
            pass

    def _record_failure(self, reason: str, detail: str) -> None:
        if self.failures is not None:
            self.failures.record("google_sheets", reason, detail=detail)

    @property
    def url(self) -> str | None:
        return self._spreadsheet.url if self._spreadsheet is not None else None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason
