"""The deliverable workbook: exactly two tabs, Overview and Records (Phase 4).

The visual language is the reference workbook's (``tools/build_nashik_workbook.py``
-> ``Nashik-Venues-Formatted.xlsx``): Arial, a navy header row, thin grey
rules, banded rows, a red-amber-green colour scale on scores, clickable short
links. Its four tabs become two: **Records** (one row per organisation, the
shared column model) and **Overview** (run summary, results, data quality and
the column legend that used to be tabs of their own).

Rules the workbook keeps:

* every value comes from the database through ``ExportTable`` -- the Overview's
  figures are values, not formulas, so they read the same in Excel, Numbers,
  LibreOffice, Google Sheets and a file previewer. One small block, "Filtered
  view", is live formulas over Records (``SUBTOTAL`` counts visible rows only),
  so it follows the autofilter. Each of those formulas also carries its
  unfiltered result in the file (openpyxl cannot store one, so ``_stamped``
  writes it in), so a viewer that never calculates -- Quick Look, a mail
  preview -- shows the right numbers rather than 0;
* text is always written as a literal string (a scraped ``=HYPERLINK(...)``
  never becomes a live formula), phones and IDs as text (``@``), so "+91 …"
  can never turn into ``#ERROR!`` the way the Sheets USER_ENTERED sync did;
* links are http(s) only;
* output is deterministic: same database, same ``generated_at``, same bytes.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .columns import Column
from .summary import summarise
from .table import ExportTable

FONT = "Arial"
NAVY = "1F3864"
SLATE = "44546A"
BAND = "F2F5F9"
RULE = "D6DCE4"
LINK = "0563C1"
GREY = "7F7F7F"
INK = "404040"
OVERVIEW_TAB = "C00000"

RED, AMBER, GREEN = "F8C9C4", "FFF0C2", "C6E7C8"
FRESH_FILL, FRESH_FONT = "E2F0D9", "2E7D32"
RECENT_FILL, RECENT_FONT = "FFF2CC", "B26A00"

_thin = Side(style="thin", color=RULE)
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
BODY = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
LINK_FONT = Font(name=FONT, size=10, color=LINK, underline="single")
NOTE = Font(name=FONT, size=9, color=INK)
TOP = Alignment(vertical="top")
CENTRE = Alignment(horizontal="center", vertical="top")
WRAP = Alignment(vertical="top", wrap_text=True)

#: Kinds whose cells are centred.
_CENTRED = {"score", "number", "flag", "money", "date"}
#: Text columns at least this wide wrap.
_WRAP_WIDTH = 30


# --- small helpers -----------------------------------------------------------------------------

def short_link(url: str) -> str:
    """A link's display text: @handle for socials, the bare host/path otherwise."""
    match = re.search(r"(?:instagram\.com|facebook\.com)/([^/?#]+)", url)
    if match:
        return "@" + match.group(1)
    match = re.search(r"linkedin\.com/(?:company|in)/([^/?#]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"wa\.me/(\d+)", url)
    if match:
        return "+" + match.group(1)
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")[:40]


#: Excel refuses longer hyperlinks (the file needs "repair"); the text stays.
MAX_LINK = 2000
#: Excel's limit for one cell's text.
MAX_TEXT = 32767


def _is_link(value: Any) -> bool:
    return (isinstance(value, str) and value.lower().startswith(("http://", "https://"))
            and len(value) <= MAX_LINK)


def _literal(cell, value: str) -> None:
    """Text, never a formula. Control characters a scraped page may carry (vertical
    tab, form feed …) are not allowed in a workbook: they are dropped, not fatal."""
    cell.value = ILLEGAL_CHARACTERS_RE.sub("", value)[:MAX_TEXT]
    cell.data_type = "s"


def style_header(ws: Worksheet, row: int, ncols: int, fill: str = NAVY, start: int = 1) -> None:
    for col in range(start, start + ncols):
        cell = ws.cell(row, col)
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[row].height = 30


def _lines(text: str, width_chars: float) -> int:
    """Roughly how many wrapped lines `text` takes in a cell `width_chars` wide."""
    return sum(max(1, math.ceil(len(part) / max(width_chars, 1)))
               for part in str(text).split("\n"))


# --- Records -----------------------------------------------------------------------------------

def _money_format(prefix: str) -> str:
    return f'"{prefix}"#,##0' if prefix else "#,##0"


def _write_value(table: ExportTable, ws: Worksheet, row: int, col: int, column: Column,
                 value: Any) -> None:
    cell = ws.cell(row, col)
    cell.border = BORDER
    cell.font = BOLD if column.strong else BODY
    cell.alignment = CENTRE if column.kind in _CENTRED else TOP
    fmt = column.excel_format
    if column.kind == "money":
        fmt = column.number_format or _money_format(table.money_prefix)
    if fmt:
        cell.number_format = fmt
    if value is None or value == "":
        return
    if column.kind == "url":
        text = str(value)
        if _is_link(text):
            _literal(cell, column.link_label or short_link(text))
            cell.hyperlink = text
            cell.font = LINK_FONT
        else:
            _literal(cell, text)
        return
    if isinstance(value, bool):
        _literal(cell, "Yes" if value else "No")
        return
    if isinstance(value, datetime):
        cell.value = table.local(value)
        return
    if isinstance(value, (int, float)) and column.kind in ("score", "number", "money"):
        cell.value = value
        return
    text = str(value)
    _literal(cell, text)
    if column.kind == "text" and column.excel_width >= _WRAP_WIDTH:
        cell.alignment = WRAP
    if column.palette:
        colour = dict(column.palette).get(text)
        if colour:
            cell.font = Font(name=FONT, size=10, bold=True, color=colour)


def write_records(table: ExportTable, ws: Worksheet) -> None:
    ws.sheet_properties.tabColor = NAVY
    columns = table.columns
    for col, column in enumerate(columns, start=1):
        _literal(ws.cell(1, col), column.header)
        ws.column_dimensions[get_column_letter(col)].width = column.excel_width
    style_header(ws, 1, len(columns))

    for row, record in enumerate(table.records, start=2):
        for col, column in enumerate(columns, start=1):
            _write_value(table, ws, row, col, column, column.value(record))
        ws.row_dimensions[row].height = 15

    last_col = get_column_letter(len(columns))
    last_row = len(table.records) + 1
    ws.freeze_panes = "C2"          # the header, the score and the name stay in view
    ws.auto_filter.ref = f"A1:{last_col}{last_row}"
    if not table.records:
        return
    ws.conditional_formatting.add(f"A2:{last_col}{last_row}", FormulaRule(
        formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", fgColor=BAND), stopIfTrue=False))
    for col, column in enumerate(columns, start=1):
        letter = get_column_letter(col)
        rng = f"{letter}2:{letter}{last_row}"
        if column.key == "score":
            ws.conditional_formatting.add(rng, ColorScaleRule(
                start_type="num", start_value=10, start_color=RED,
                mid_type="num", mid_value=40, mid_color=AMBER,
                end_type="num", end_value=75, end_color=GREEN))
        elif column.kind == "score":
            ws.conditional_formatting.add(rng, ColorScaleRule(
                start_type="num", start_value=0, start_color=RED,
                mid_type="num", mid_value=50, mid_color=AMBER,
                end_type="num", end_value=100, end_color=GREEN))
        elif column.key == "signal_date":
            # Judged when the book is opened, so the highlight stays true as it ages.
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=[f"AND(ISNUMBER({letter}2),TODAY()-{letter}2<=30)"],
                fill=PatternFill("solid", fgColor=FRESH_FILL),
                font=Font(name=FONT, size=10, bold=True, color=FRESH_FONT), stopIfTrue=True))
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=[f"AND(ISNUMBER({letter}2),TODAY()-{letter}2<=90)"],
                fill=PatternFill("solid", fgColor=RECENT_FILL),
                font=Font(name=FONT, size=10, color=RECENT_FONT), stopIfTrue=True))


# --- Overview ----------------------------------------------------------------------------------

OVERVIEW_WIDTHS = (34, 12, 12, 12, 14, 18, 22, 30)
OVERVIEW_COLS = len(OVERVIEW_WIDTHS)
#: Characters a merged B:H cell holds per line.
_WIDE = sum(OVERVIEW_WIDTHS[1:])


class _Overview:
    def __init__(self, table: ExportTable, ws: Worksheet):
        self.table, self.ws = table, ws
        self.row = 1
        #: cell -> the result its formula has without a filter (written into the file)
        self.cached: dict[str, float] = {}
        ws.sheet_properties.tabColor = OVERVIEW_TAB
        ws.sheet_view.showGridLines = False
        for col, width in enumerate(OVERVIEW_WIDTHS, start=1):
            ws.column_dimensions[get_column_letter(col)].width = width

    # building blocks ---------------------------------------------------------------------

    def put(self, row: int, col: int, value: Any, fmt: str | None = None,
            font: Font = BODY, align: Alignment = TOP) -> Any:
        cell = self.ws.cell(row, col)
        if isinstance(value, str):
            _literal(cell, value)
        else:
            cell.value = value
        cell.font, cell.alignment = font, align
        if fmt:
            cell.number_format = fmt
        return cell

    def merged(self, row: int, start: int, end: int, value: Any, font: Font,
               align: Alignment = WRAP) -> Any:
        if end > start:
            self.ws.merge_cells(start_row=row, start_column=start, end_row=row, end_column=end)
        return self.put(row, start, value, font=font, align=align)

    def title(self, text: str, subtitle: str) -> None:
        self.merged(1, 1, OVERVIEW_COLS, text, Font(name=FONT, size=16, bold=True, color=NAVY),
                    Alignment(vertical="center"))
        self.ws.row_dimensions[1].height = 26
        self.merged(2, 1, OVERVIEW_COLS, subtitle,
                    Font(name=FONT, size=9, italic=True, color=GREY), Alignment(vertical="center"))
        self.ws.row_dimensions[2].height = 16
        self.row = 4

    def section(self, text: str) -> None:
        self.put(self.row, 1, text, font=Font(name=FONT, size=11, bold=True, color=NAVY))
        self.row += 1

    def boxed(self, row: int, cols, fill: str | None = None) -> None:
        for col in cols:
            cell = self.ws.cell(row, col)
            cell.border = BORDER
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)

    def header(self, labels: list[str], spans: list[int] | None = None) -> None:
        col = 1
        for label, span in zip(labels, spans or [1] * len(labels)):
            if span > 1:
                self.ws.merge_cells(start_row=self.row, start_column=col, end_row=self.row,
                                    end_column=col + span - 1)
            _literal(self.ws.cell(self.row, col), label)
            col += span
        style_header(self.ws, self.row, col - 1, SLATE)
        self.row += 1

    def live(self, items: list[tuple[str, str, str, float]]) -> None:
        """Formula tiles (label, formula, format, unfiltered result) that follow the filter."""
        self.section("FILTERED VIEW")
        note = self.ws.cell(self.row - 1, 2)
        _literal(note, "follows the filter on Records (recalculated when the workbook opens)")
        note.font = Font(name=FONT, size=8, italic=True, color=GREY)
        for col, (label, formula, fmt, value) in enumerate(items, start=1):
            self.put(self.row, col, label.upper(), font=Font(name=FONT, size=9, color=GREY))
            cell = self.ws.cell(self.row + 1, col)
            cell.value = formula            # our own formula over Records, never scraped text
            cell.number_format = fmt
            cell.font = Font(name=FONT, size=14, bold=True, color=SLATE)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            self.cached[cell.coordinate] = value
        self.ws.row_dimensions[self.row + 1].height = 22
        self.row += 3

    def kpis(self, items: list[tuple[str, Any, str]]) -> None:
        for col, (label, value, fmt) in enumerate(items, start=1):
            self.put(self.row, col, label.upper(), font=Font(name=FONT, size=9, color=GREY))
            self.put(self.row + 1, col, value, fmt,
                     font=Font(name=FONT, size=18, bold=True, color=NAVY),
                     align=Alignment(horizontal="left", vertical="center"))
        self.ws.row_dimensions[self.row + 1].height = 26
        self.row += 3

    def key_values(self, rows: list[tuple[str, str]]) -> None:
        for label, value in rows:
            self.put(self.row, 1, label, font=BOLD)
            self.merged(self.row, 2, OVERVIEW_COLS, value, BODY)
            self.boxed(self.row, range(1, OVERVIEW_COLS + 1))
            lines = _lines(value, _WIDE)
            if lines > 1:
                self.ws.row_dimensions[self.row].height = 13 * lines + 2
            self.row += 1
        self.row += 1

    def colour_scale(self, rng: str, low: float, mid: float, high: float) -> None:
        self.ws.conditional_formatting.add(rng, ColorScaleRule(
            start_type="num", start_value=low, start_color=RED,
            mid_type="num", mid_value=mid, mid_color=AMBER,
            end_type="num", end_value=high, end_color=GREEN))

    # sections ----------------------------------------------------------------------------

    def reasons(self, items: list[str]) -> None:
        self.section("WHY THERE ARE NO RECORDS")
        for text in items:
            self.merged(self.row, 1, OVERVIEW_COLS, f"•  {text}", NOTE)
            self.ws.row_dimensions[self.row].height = max(
                15, 12 * _lines(text, sum(OVERVIEW_WIDTHS)) + 3)
            self.row += 1
        self.row += 1

    def top(self, items: list[dict[str, Any]]) -> None:
        self.section(f"TOP {len(items)} LEADS")
        what = next((i["what_header"] for i in items if i.get("what_header")), "")
        signals = any(i.get("signal") for i in items)
        if signals:
            self.header(["Name", "Score", "Where", what, "Phone", "Website", "Top signal"],
                        [1, 1, 2, 1, 1, 1, 1])
        else:
            self.header(["Name", "Score", "Where", what, "Phone", "Website"], [1, 1, 2, 1, 1, 2])
        start = self.row
        for item in items:
            row = self.row
            self.put(row, 1, item["name"] or "", font=BOLD, align=WRAP)
            self.put(row, 2, item["score"], "0.0", align=CENTRE)
            self.merged(row, 3, 4, item["where"] or "", BODY)
            self.put(row, 5, "" if item["what"] is None else item["what"], align=WRAP)
            self.put(row, 6, item["phone"] or "", "@")
            if not signals:
                self.ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=8)
            site = item.get("website")
            if _is_link(site):
                self.put(row, 7, short_link(site), font=LINK_FONT).hyperlink = site
            if signals:
                self.put(row, 8, item.get("signal") or "", align=WRAP)
            self.boxed(row, range(1, OVERVIEW_COLS + 1))
            self.row += 1
        if self.row > start:
            self.colour_scale(f"B{start}:B{self.row - 1}", 10, 40, 75)
        self.row += 1

    def breakdown(self, block: dict[str, Any]) -> None:
        self.section(block["title"].upper())
        self.header([block["title"].split(" ", 1)[-1].capitalize(), "Records", "Share",
                     "Avg score", "With phone"])
        start = self.row
        for item in block["rows"]:
            self.put(self.row, 1, item["label"], align=WRAP,
                     font=BODY if item["label"] == "(none)" else BOLD)
            self.put(self.row, 2, item["records"], "#,##0")
            self.put(self.row, 3, item["share"], "0.0%")
            self.put(self.row, 4, item["avg_score"], "0.0")
            self.put(self.row, 5, item["with_phone"], "#,##0")
            self.boxed(self.row, range(1, 6))
            self.row += 1
        if self.row > start:
            self.colour_scale(f"D{start}:D{self.row - 1}", 10, 40, 75)
        self.row += 1

    def coverage(self, items: list[dict[str, Any]]) -> None:
        self.section("DATA QUALITY")
        self.header(["Field", "Filled", "Coverage", "Reading"], [1, 1, 1, OVERVIEW_COLS - 3])
        start = self.row
        for item in items:
            self.put(self.row, 1, item["field"], font=BOLD)
            self.put(self.row, 2, item["filled"], "#,##0")
            self.put(self.row, 3, item["coverage"], "0.0%")
            self.merged(self.row, 4, OVERVIEW_COLS, item["note"], NOTE)
            self.boxed(self.row, range(1, OVERVIEW_COLS + 1))
            self.row += 1
        if self.row > start and self.table.records:
            self.colour_scale(f"C{start}:C{self.row - 1}", 0, 0.5, 1)
        self.row += 1

    def legend(self, items: list[tuple[str, str]]) -> None:
        self.section("COLUMN LEGEND")
        self.header(["Column", "Meaning"], [1, OVERVIEW_COLS - 1])
        for label, meaning in items:
            self.put(self.row, 1, label, font=BOLD, align=WRAP)
            self.merged(self.row, 2, OVERVIEW_COLS, meaning, NOTE)
            self.boxed(self.row, range(1, OVERVIEW_COLS + 1))
            self.ws.row_dimensions[self.row].height = max(15, 12 * _lines(meaning, _WIDE) + 3)
            self.row += 1
        self.row += 1

    def footnote(self, text: str) -> None:
        self.merged(self.row, 1, OVERVIEW_COLS, text,
                    Font(name=FONT, size=8, italic=True, color=GREY))
        self.ws.row_dimensions[self.row].height = 24


def _live_tiles(table: ExportTable, n: int) -> list[tuple[str, str, str, float]]:
    """SUBTOTAL formulas over the Records rows (they count only rows the filter shows),
    each with its result for the unfiltered table."""
    letters = {c.key: get_column_letter(i) for i, c in enumerate(table.columns, start=1)}
    last = n + 1

    def rng(key: str) -> str:
        col = letters[key]
        return f"Records!${col}$2:${col}${last}"

    def visible_filled(key: str) -> str:
        col = letters[key]
        return (f"=SUMPRODUCT(SUBTOTAL(103,OFFSET(Records!${col}$2,"
                f"ROW({rng(key)})-ROW(Records!${col}$2),0)))")

    def filled(key: str) -> int:
        column = next(c for c in table.columns if c.key == key)
        return sum(1 for r in table.records if column.value(r) not in (None, ""))

    scores = [r.score for r in table.records]
    tiles = [(f"Showing (of {n:,})", f"=SUBTOTAL(103,{rng('name')})", "#,##0", filled("name")),
             ("Avg score", f'=IFERROR(SUBTOTAL(101,{rng("score")}),"–")', "0.0",
              sum(scores) / len(scores))]
    for key, label in (("primary_phone", "With phone"), ("primary_email", "With email"),
                       ("top_signal", "With signal")):
        if key in letters:
            tiles.append((label, visible_filled(key), "#,##0", filled(key)))
    return tiles


def write_overview(table: ExportTable, ws: Worksheet,
                   summary: dict[str, Any]) -> dict[str, float]:
    """Fill the Overview; returns the live cells' unfiltered results (see ``_stamped``)."""
    ov = _Overview(table, ws)
    totals = summary["totals"]
    n = totals["records"]
    ov.title(f"{table.project.get('name') or 'Leads'} — Lead Book",
             f"{n:,} records · sorted by {table.sort_label} · generated "
             f"{table.local(table.generated_at):%d %b %Y, %H:%M} ({table.timezone})")

    kpis: list[tuple[str, Any, str]] = [("Records", n, "#,##0")]
    if table.run is not None:
        kpis += [("New", totals["new"], "#,##0"), ("Updated", totals["updated"], "#,##0")]
    kpis += [("With phone", totals["with_phone"], "#,##0"),
             ("With email", totals["with_email"], "#,##0")]
    if table.context.get("signal_types"):
        kpis.append(("With signal", totals["with_signal"], "#,##0"))
    kpis.append(("Avg score", totals["avg_score"] if totals["avg_score"] is not None else "–",
                 "0.0"))
    ov.kpis(kpis[:OVERVIEW_COLS])
    if n:
        ov.live(_live_tiles(table, n))

    if not n:
        ov.reasons(summary["empty_reasons"])
    ov.section("RUN SUMMARY")
    ov.key_values(summary["run_summary"])
    if n:
        ov.section("RESULTS")
        results = [("Records", f"{n:,}")]
        if table.run is not None:
            results.append(("Change in this run",
                            f"{totals['new']:,} new · {totals['updated']:,} updated · "
                            f"{totals['unchanged']:,} found again unchanged"))
        left_out = [f"{table.excluded:,} by suppression lists"] if table.excluded else []
        if totals["left_out_by_filters"]:
            left_out.append(f"{totals['left_out_by_filters']:,} by the run's filters")
        if left_out:
            results.append(("Left out", " · ".join(left_out)))
        ov.key_values(results)
        ov.top(summary["top"])
        for block in summary["breakdowns"]:
            ov.breakdown(block)
    ov.coverage(summary["coverage"])
    ov.legend(summary["legend"])
    ov.footnote("Every figure is computed from the project database when the workbook is "
                "generated; Records holds the rows behind each. Contact details appear only "
                "where they were published. Re-export at any time from the API or CLI, "
                "without re-scraping.")
    return ov.cached


# --- the workbook ------------------------------------------------------------------------------

def to_xlsx(table: ExportTable) -> bytes:
    workbook = Workbook()
    overview = workbook.active
    overview.title = "Overview"
    records = workbook.create_sheet("Records")
    write_records(table, records)
    cached = write_overview(table, overview, summarise(table))
    workbook.active = 0
    # The filtered-view formulas have no stored result: compute them on open.
    workbook.calculation.fullCalcOnLoad = True
    stamp = table.local(table.generated_at)
    workbook.properties.creator = "Skillminds lead generator"
    workbook.properties.title = f"{table.project.get('name') or 'Leads'} — Lead Book"
    workbook.properties.created = stamp
    workbook.properties.modified = stamp
    buffer = io.BytesIO()
    workbook.save(buffer)
    return _stamped(buffer.getvalue(), stamp, {OVERVIEW_PART: cached})


#: The Overview is the first sheet openpyxl writes.
OVERVIEW_PART = "xl/worksheets/sheet1.xml"


def _with_results(xml: bytes, results: dict[str, float]) -> bytes:
    """Write each formula cell's result into its empty ``<v></v>`` (openpyxl leaves it empty)."""
    text = xml.decode("utf-8")
    for coordinate, value in results.items():
        number = round(float(value), 6)
        shown = str(int(number)) if number.is_integer() else repr(number)
        pattern = re.compile(rf'(<c r="{coordinate}"[^>]*><f>[^<]*</f>)<v\s*/?>(?:</v>)?')
        text, count = pattern.subn(lambda m, v=shown: f"{m.group(1)}<v>{v}</v>", text, count=1)
        if not count:
            raise ValueError(f"no formula cell {coordinate} to give a result")
    return text.encode("utf-8")


def _stamped(content: bytes, stamp: datetime,
             results: dict[str, dict[str, float]] | None = None) -> bytes:
    """The same zip with every entry dated `stamp` (same table, same bytes), and the
    formulas' unfiltered results written in."""
    date_time = (max(stamp.year, 1980), stamp.month, stamp.day, stamp.hour, stamp.minute,
                 stamp.second)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            info = zipfile.ZipInfo(item.filename, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = item.external_attr
            data = source.read(item.filename)
            if results and results.get(item.filename):
                data = _with_results(data, results[item.filename])
            target.writestr(info, data)
    return out.getvalue()
