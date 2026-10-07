#!/usr/bin/env python3
"""Build a presentation-grade Excel workbook from the scraper's CSV export.

Superseded (Phase 4): every run now writes its workbook from the database
(Overview + Records, ``leadscraper/export/excel.py``), and ``python run.py
--export-only`` or ``python -m leadscraper export`` regenerates it at any time.
This script stays as the reference for the visual language and for its
editorial extras (zones), and it still reads the legacy CSV.

Why this exists
---------------
The Google Sheets sync writes with ``value_input_option="USER_ENTERED"``
(leadscraper/sheets.py), so Sheets parses a phone like ``+91 93200 22422`` as a
formula and stores ``#ERROR!``.  Any .xlsx exported from that sheet therefore has
an unusable Phones column.  This script reads the *local* CSV, which is intact,
so the phone numbers survive.

Rows are ordered by Zone, then Locality, then Score (best first) so the book can
be worked region by region.

Re-run after any scrape/enrichment pass to refresh the workbook:

    .venv/bin/python tools/build_leads_workbook.py
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "leads.csv"
OUT = ROOT / "Pheraas-Leads-Formatted.xlsx"

FONT = "Arial"
NAVY = "1F3864"
SLATE = "44546A"
BAND = "F2F5F9"
RULE = "D6DCE4"
GREEN, AMBER, RED = "2E7D32", "B26A00", "B3261E"
LINK = "0563C1"

TIER_A, TIER_B = 55.0, 45.0

# Mumbai zones. This grouping is OUR editorial classification for territory
# planning -- the scraper only knows the locality. Edit here to re-cut the map.
ZONES = {
    "Andheri": "Western Suburbs",
    "Andheri West": "Western Suburbs",
    "Goregaon": "Western Suburbs",
    "WEH / Airport Road Corridor": "Western Suburbs",
    "Ghatkopar": "Eastern Suburbs",
    "Kanjur Marg": "Eastern Suburbs",
    "Powai": "Eastern Suburbs",
    "Dadar": "Central Mumbai",
    "Marine Lines & Fort": "South Mumbai",
}
ZONE_ORDER = ["Western Suburbs", "Eastern Suburbs", "Central Mumbai", "South Mumbai"]

thin = Side(style="thin", color=RULE)
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)


def zone_of(locality: str) -> str:
    return ZONES.get((locality or "").strip(), "Unclassified")


def first_of(value: str) -> str:
    return (value or "").split("|")[0].strip()


def handle(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"(?:instagram\.com|facebook\.com)/([^/?#]+)", url)
    if m:
        return "@" + m.group(1)
    m = re.search(r"linkedin\.com/(?:company|in)/([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"wa\.me/(\d+)", url)
    if m:
        return "+" + m.group(1)
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")[:40]


def as_num(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_date(value: str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime((value or "").strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


def style_header(ws, row: int, ncols: int, fill: str = NAVY, start: int = 1) -> None:
    for c in range(start, start + ncols):
        cell = ws.cell(row, c)
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[row].height = 30


def title_block(ws, title: str, subtitle: str, width: int) -> int:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    ws.cell(1, 1, title).font = Font(name=FONT, size=16, bold=True, color=NAVY)
    ws.cell(1, 1).alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 26
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
    ws.cell(2, 1, subtitle).font = Font(name=FONT, size=9, italic=True, color="7F7F7F")
    ws.row_dimensions[2].height = 16
    return 4


# --------------------------------------------------------------------------
if not SRC.exists():
    sys.exit(f"missing {SRC}")

rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
for row in rows:
    row["_zone"] = zone_of(row["Locality"])
# Region first, then locality, then best leads at the top of each locality.
rows.sort(key=lambda r: (
    ZONE_ORDER.index(r["_zone"]) if r["_zone"] in ZONE_ORDER else len(ZONE_ORDER),
    r["Locality"],
    -(as_num(r["Score"]) or 0),
))
N = len(rows)
LAST = N + 1

wb = Workbook()
wb.remove(wb.active)

# ==========================================================================
# LEADS
# ==========================================================================
COLS = [
    ("Score", 8), ("Tier", 6), ("Business Name", 38), ("Category", 20),
    ("Zone", 18), ("Locality", 20),
    ("Primary Phone", 18), ("All Phones", 26),
    ("Primary Email", 30), ("All Emails", 30), ("Website", 26),
    ("Instagram", 20), ("LinkedIn", 20), ("Facebook", 20), ("WhatsApp", 16),
    ("Google Maps", 14), ("Address", 44), ("Maps Rating", 9),
    ("Maps Reviews", 10), ("WMG Rating", 9), ("WMG Reviews", 10),
    ("Sources", 22), ("Src #", 6), ("Notes", 46),
    ("First Seen", 16), ("Last Updated", 16),
]
C = {name: i for i, (name, _) in enumerate(COLS, 1)}
L = {name: get_column_letter(i) for name, i in C.items()}
NC = len(COLS)

ws = wb.create_sheet("Leads")
ws.sheet_properties.tabColor = NAVY
for i, (name, width) in enumerate(COLS, 1):
    ws.cell(1, i, name)
    ws.column_dimensions[get_column_letter(i)].width = width
style_header(ws, 1, NC)

body = Font(name=FONT, size=10)
link_font = Font(name=FONT, size=10, color=LINK, underline="single")
top = Alignment(vertical="top")
ctr = Alignment(horizontal="center", vertical="top")
wrap = Alignment(vertical="top", wrap_text=True)

for r, row in enumerate(rows, start=2):
    def put(name, value, *, fmt=None, font=None, align=None, url=None):
        cell = ws.cell(r, C[name], value)
        cell.font = font or body
        cell.border = BORDER
        cell.alignment = align or top
        if fmt:
            cell.number_format = fmt
        if url:
            cell.hyperlink = url
        return cell

    put("Score", as_num(row["Score"]), fmt="0.0", align=ctr)
    put("Tier", f'=IF({L["Score"]}{r}="","",IF({L["Score"]}{r}>={TIER_A},"A",'
                f'IF({L["Score"]}{r}>={TIER_B},"B","C")))',
        align=ctr, font=Font(name=FONT, size=10, bold=True))
    put("Business Name", row["Business Name"], font=Font(name=FONT, size=10, bold=True))
    put("Category", row["Category"])
    put("Zone", row["_zone"], font=Font(name=FONT, size=10, bold=True, color=SLATE))
    put("Locality", row["Locality"])

    put("Primary Phone", first_of(row["Phones"]), fmt="@")
    put("All Phones", row["Phones"], fmt="@", align=wrap)
    put("Primary Email", first_of(row["Emails"]))
    put("All Emails", row["Emails"], align=wrap)

    for name, key in (("Website", "Website"), ("Instagram", "Instagram"),
                      ("LinkedIn", "LinkedIn"), ("Facebook", "Facebook"),
                      ("WhatsApp", "WhatsApp")):
        url = (row[key] or "").strip()
        put(name, handle(url) if url else "", font=link_font if url else body,
            url=url or None)

    maps = (row["Google Maps URL"] or "").strip()
    put("Google Maps", "View map" if maps else "", font=link_font if maps else body,
        url=maps or None)

    put("Address", row["Address"], align=wrap)
    put("Maps Rating", as_num(row["Maps Rating"]), fmt="0.0", align=ctr)
    put("Maps Reviews", as_num(row["Maps Reviews"]), fmt="#,##0", align=ctr)
    put("WMG Rating", as_num(row["WedMeGood Rating"]), fmt="0.0", align=ctr)
    put("WMG Reviews", as_num(row["WedMeGood Reviews"]), fmt="#,##0", align=ctr)
    put("Sources", row["Sources"], align=wrap)
    put("Src #", as_num(row["Source Count"]), fmt="0", align=ctr)
    put("Notes", row["Notes"], align=wrap)
    put("First Seen", as_date(row["First Seen"]), fmt="dd-mmm-yy hh:mm")
    put("Last Updated", as_date(row["Last Updated"]), fmt="dd-mmm-yy hh:mm")
    ws.row_dimensions[r].height = 15

ws.freeze_panes = "D2"
ws.auto_filter.ref = f"A1:{get_column_letter(NC)}{LAST}"

full = f"A2:{get_column_letter(NC)}{LAST}"
ws.conditional_formatting.add(full, FormulaRule(
    formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", fgColor=BAND), stopIfTrue=False))
# Tint whole rows faintly by zone so region boundaries are visible at a glance.
ZONE_TINT = {"Western Suburbs": "EAF1FB", "Eastern Suburbs": "EDF7ED",
             "Central Mumbai": "FFF6E5", "South Mumbai": "F6EEF8"}
for zname, tint in ZONE_TINT.items():
    ws.conditional_formatting.add(full, FormulaRule(
        formula=[f'${L["Zone"]}2="{zname}"'],
        fill=PatternFill("solid", fgColor=tint), stopIfTrue=False))
ws.conditional_formatting.add(f'{L["Score"]}2:{L["Score"]}{LAST}', ColorScaleRule(
    start_type="num", start_value=10, start_color="F8C9C4",
    mid_type="num", mid_value=44, mid_color="FFF0C2",
    end_type="num", end_value=72, end_color="C6E7C8"))
for tier, colour in (("A", GREEN), ("B", AMBER), ("C", RED)):
    ws.conditional_formatting.add(f'{L["Tier"]}2:{L["Tier"]}{LAST}', FormulaRule(
        formula=[f'${L["Tier"]}2="{tier}"'],
        font=Font(name=FONT, size=10, bold=True, color=colour)))

LEADS_TOTAL = f'COUNTA(Leads!{L["Business Name"]}2:{L["Business Name"]}{LAST})'


def col_rng(name):
    return f'Leads!{L[name]}2:{L[name]}{LAST}'


# ==========================================================================
# OVERVIEW
# ==========================================================================
ov = wb.create_sheet("Overview", 0)
ov.sheet_properties.tabColor = "C00000"
ov.sheet_view.showGridLines = False
for col, width in zip("ABCDEFG", (30, 14, 14, 4, 30, 14, 14)):
    ov.column_dimensions[col].width = width

snapshot = datetime.now().strftime("%d %b %Y, %H:%M")
r = title_block(ov, "Pheraas — Mumbai Event-Industry Lead Book",
                f"Generated {snapshot} from data/leads.csv · {N:,} leads · "
                f"sorted by zone, then locality, then score", 7)


def kpi(row, col, label, formula, fmt="#,##0"):
    ov.cell(row, col, label).font = Font(name=FONT, size=9, color="7F7F7F")
    c = ov.cell(row + 1, col, formula)
    c.font = Font(name=FONT, size=18, bold=True, color=NAVY)
    c.number_format = fmt


kpi(r, 1, "TOTAL LEADS", f"={LEADS_TOTAL}")
kpi(r, 2, "WITH PHONE", f'=COUNTIF({col_rng("Primary Phone")},"?*")')
kpi(r, 3, "WITH EMAIL", f'=COUNTIF({col_rng("Primary Email")},"?*")')
kpi(r, 5, "TIER A (score ≥ 55)", f'=COUNTIF({col_rng("Tier")},"A")')
kpi(r, 6, "WITH WEBSITE", f'=COUNTIF({col_rng("Website")},"?*")')
kpi(r, 7, "AVG SCORE", f'=AVERAGE({col_rng("Score")})', fmt="0.0")
r += 3

# --- by zone --------------------------------------------------------------
ov.cell(r, 1, "BY ZONE").font = Font(name=FONT, size=11, bold=True, color=NAVY)
r += 1
for i, h in enumerate(["Zone", "Leads", "With phone", "", "Localities", "Avg score"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 6, SLATE)
r += 1
zone_start = r
for z in ZONE_ORDER:
    ov.cell(r, 1, z).font = Font(name=FONT, size=10, bold=True)
    ov.cell(r, 2, f'=COUNTIF({col_rng("Zone")},A{r})').number_format = "#,##0"
    ov.cell(r, 3, f'=COUNTIFS({col_rng("Zone")},A{r},{col_rng("Primary Phone")},"?*")'
            ).number_format = "#,##0"
    ov.cell(r, 5, ", ".join(sorted(k for k, v in ZONES.items() if v == z))).font = Font(
        name=FONT, size=8, color="7F7F7F")
    ov.cell(r, 5).alignment = Alignment(vertical="center", wrap_text=True)
    ov.cell(r, 6, f'=IFERROR(AVERAGEIF({col_rng("Zone")},A{r},{col_rng("Score")}),"")'
            ).number_format = "0.0"
    for cc in (2, 3, 6):
        ov.cell(r, cc).font = body
    for cc in range(1, 7):
        ov.cell(r, cc).border = BORDER
    ov.row_dimensions[r].height = 26
    r += 1
ov.cell(r, 1, "Total").font = Font(name=FONT, size=10, bold=True)
ov.cell(r, 2, f"=SUM(B{zone_start}:B{r - 1})").font = Font(name=FONT, size=10, bold=True)
ov.cell(r, 3, f"=SUM(C{zone_start}:C{r - 1})").font = Font(name=FONT, size=10, bold=True)
for cc in range(1, 7):
    ov.cell(r, cc).border = BORDER
    ov.cell(r, cc).fill = PatternFill("solid", fgColor=BAND)
r += 3

# --- contactability -------------------------------------------------------
ov.cell(r, 1, "CONTACTABILITY").font = Font(name=FONT, size=11, bold=True, color=NAVY)
r += 1
for i, h in enumerate(["Channel", "Leads", "Coverage"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 3, SLATE)
r += 1
channels = ["Primary Phone", "Primary Email", "Website", "Instagram",
            "Facebook", "WhatsApp", "LinkedIn", "Google Maps"]
ch_start = r
for name in channels:
    ov.cell(r, 1, name.replace("Primary ", "")).font = body
    c2 = ov.cell(r, 2, f'=COUNTIF({col_rng(name)},"?*")')
    c2.number_format, c2.font = "#,##0", body
    c3 = ov.cell(r, 3, f'=IFERROR(B{r}/{LEADS_TOTAL},0)')
    c3.number_format, c3.font = "0.0%", body
    for cc in range(1, 4):
        ov.cell(r, cc).border = BORDER
    r += 1
ov.conditional_formatting.add(f"C{ch_start}:C{r - 1}", ColorScaleRule(
    start_type="num", start_value=0, start_color="F8C9C4",
    mid_type="num", mid_value=0.5, mid_color="FFF0C2",
    end_type="num", end_value=1, end_color="C6E7C8"))
r += 2

# --- by category / by locality -------------------------------------------
ov.cell(r, 1, "BY CATEGORY").font = Font(name=FONT, size=11, bold=True, color=NAVY)
ov.cell(r, 5, "BY LOCALITY").font = Font(name=FONT, size=11, bold=True, color=NAVY)
r += 1
for i, h in enumerate(["Category", "Leads", "Avg score"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 3, SLATE)
for i, h in enumerate(["Locality", "Leads", "Avg score"], 5):
    ov.cell(r, i, h)
style_header(ov, r, 3, SLATE, start=5)
r += 1

cats = sorted({row["Category"] for row in rows})
locs = sorted({row["Locality"] for row in rows},
              key=lambda x: (ZONE_ORDER.index(zone_of(x)) if zone_of(x) in ZONE_ORDER
                             else 99, x))
for i in range(max(len(cats), len(locs))):
    if i < len(cats):
        ov.cell(r + i, 1, cats[i]).font = body
        ov.cell(r + i, 2, f'=COUNTIF({col_rng("Category")},A{r + i})').number_format = "#,##0"
        ov.cell(r + i, 3,
                f'=IFERROR(AVERAGEIF({col_rng("Category")},A{r + i},{col_rng("Score")}),"")'
                ).number_format = "0.0"
    if i < len(locs):
        ov.cell(r + i, 5, locs[i]).font = body
        ov.cell(r + i, 6, f'=COUNTIF({col_rng("Locality")},E{r + i})').number_format = "#,##0"
        ov.cell(r + i, 7,
                f'=IFERROR(AVERAGEIF({col_rng("Locality")},E{r + i},{col_rng("Score")}),"")'
                ).number_format = "0.0"
    for cc in (1, 2, 3, 5, 6, 7):
        cell = ov.cell(r + i, cc)
        cell.border = BORDER
        if not cell.font.b:
            cell.font = body
r += max(len(cats), len(locs)) + 2

ov.cell(r, 1, "Tier A = score ≥ 55 · B = 45–54.9 · C < 45 (roughly the 90th and 75th "
              "percentile). Zones are an editorial grouping of the scraper's localities, "
              "defined in tools/build_leads_workbook.py — edit ZONES there to re-cut them."
        ).font = Font(name=FONT, size=8, italic=True, color="7F7F7F")
ov.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)

# ==========================================================================
# DATA QUALITY
# ==========================================================================
dq = wb.create_sheet("Data Quality")
dq.sheet_properties.tabColor = "B26A00"
dq.sheet_view.showGridLines = False
for col, width in zip("ABCD", (34, 12, 12, 78)):
    dq.column_dimensions[col].width = width
r = title_block(dq, "Data Quality & Known Issues",
                "Field coverage is live (formulas over the Leads sheet). "
                "Findings below were observed at build time.", 4)
for i, h in enumerate(["Field", "Filled", "Coverage", "Reading"], 1):
    dq.cell(r, i, h)
style_header(dq, r, 4, SLATE)
r += 1
FIELDS = [
    ("Business Name", "Complete."),
    ("Category", "Complete — set by the query that found the lead."),
    ("Locality", "Complete — set by the query that found the lead."),
    ("Primary Phone", "Strongest channel. Recovered from the local CSV."),
    ("Primary Email", "Low — website enrichment was still running at build time."),
    ("Website", "Half the book. Drives email discovery."),
    ("Instagram", "Useful for vendors with no website."),
    ("Facebook", ""),
    ("WhatsApp", "Highest-intent channel where present."),
    ("LinkedIn", "Sparse — expected for this vendor type."),
    ("Google Maps", "Near-complete; anchors dedupe and location."),
    ("Address", "See note 3 — some entries carry scraped page text."),
    ("Maps Rating", ""),
    ("WMG Rating", "Only WedMeGood-sourced leads carry this — 13 of 1,333."),
]
start = r
for label, note in FIELDS:
    dq.cell(r, 1, label).font = body
    c2 = dq.cell(r, 2, f'=COUNTIF({col_rng(label)},"?*")')
    c2.number_format, c2.font = "#,##0", body
    c3 = dq.cell(r, 3, f'=IFERROR(B{r}/{LEADS_TOTAL},0)')
    c3.number_format, c3.font = "0.0%", body
    n4 = dq.cell(r, 4, note)
    n4.font = Font(name=FONT, size=9, color="404040")
    n4.alignment = wrap
    for cc in range(1, 5):
        dq.cell(r, cc).border = BORDER
    r += 1
dq.conditional_formatting.add(f"C{start}:C{r - 1}", ColorScaleRule(
    start_type="num", start_value=0, start_color="F8C9C4",
    mid_type="num", mid_value=0.5, mid_color="FFF0C2",
    end_type="num", end_value=1, end_color="C6E7C8"))
r += 2

dq.cell(r, 1, "FINDINGS").font = Font(name=FONT, size=11, bold=True, color=NAVY)
r += 1
NOTES = [
    ("1. Phone column in the Google Sheets export is unusable",
     "All 1,077 phone numbers read #ERROR! in Pheraas-ScrapedLeads-16thSept.xlsx. "
     "leadscraper/sheets.py:189 syncs with value_input_option=\"USER_ENTERED\", so Sheets "
     "parses \"+91 93200 22422\" as a formula. This workbook rebuilds from data/leads.csv, "
     "where the numbers are intact. The live Google Sheet is still affected — fix by "
     "switching that call to \"RAW\"."),
    ("2. Zone is our classification, not the scraper's",
     "The scraper records only the locality it searched. The four zones are an editorial "
     "grouping for territory planning, defined in ZONES in tools/build_leads_workbook.py. "
     "Re-cut them there and rebuild if your teams split Mumbai differently."),
    ("3. Some Address values contain page text, not an address",
     "Website enrichment occasionally captured boilerplate (e.g. \"Get In Touch I agree to "
     "the Privacy Policy...\"). Longest entry is 318 characters. Treat Address as "
     "indicative; the Google Maps link is authoritative."),
    ("4. Two columns were empty and have been dropped",
     "\"Contact Name\" and \"WedMeGood Featured\" were 0% populated across all rows."),
    ("5. Score compresses toward the middle",
     "20% of the score is WedMeGood directory signal, which only 13 of 1,333 leads carry, "
     "so the effective ceiling is 80 rather than 100 (observed max 71.6). Single-source "
     "leads also bank a 0.43 corroboration floor. Ranking within a category or zone is "
     "more meaningful than the absolute number."),
]
for head_text, detail in NOTES:
    dq.cell(r, 1, head_text).font = Font(name=FONT, size=10, bold=True, color=NAVY)
    dq.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    r += 1
    c = dq.cell(r, 1, detail)
    c.font = Font(name=FONT, size=9, color="404040")
    c.alignment = wrap
    dq.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    dq.row_dimensions[r].height = 44
    r += 2

# ==========================================================================
# LEGEND
# ==========================================================================
lg = wb.create_sheet("Legend")
lg.sheet_properties.tabColor = SLATE
lg.sheet_view.showGridLines = False
for col, width in zip("AB", (24, 92)):
    lg.column_dimensions[col].width = width
r = title_block(lg, "Column Guide", "What each field means and where it comes from.", 2)
for i, h in enumerate(["Column", "Meaning"], 1):
    lg.cell(r, i, h)
style_header(lg, r, 2, SLATE)
r += 1
GUIDE = [
    ("Score", "Composite 0–100 ranking: 40% Google rating × review volume, 25% how many "
              "sources found them, 20% WedMeGood signal, 15% contact completeness."),
    ("Tier", "Formula over Score. A ≥ 55, B 45–54.9, C < 45."),
    ("Zone", "Editorial grouping of localities for territory planning. Rows are sorted "
             "Zone → Locality → Score, and each zone carries a faint row tint."),
    ("Locality", "The search area the scraper used to find the lead."),
    ("Primary Phone / Email", "First value where a lead has several. The All… column keeps "
                              "the full pipe-separated set."),
    ("Website / Instagram / …", "Clickable. Display text is shortened; the underlying link "
                                "is the full URL."),
    ("Maps Rating / Reviews", "Google Maps star rating and review count at scrape time."),
    ("WMG Rating / Reviews", "WedMeGood equivalents. Only 13 leads carry these."),
    ("Sources", "Which scrapers found this lead. More sources = higher confidence."),
    ("Src #", "Count of the above."),
    ("Notes", "Free text from the source, typically the Maps category label."),
    ("First Seen / Last Updated", "When the scraper first stored and last touched the row."),
]
for label, meaning in GUIDE:
    lg.cell(r, 1, label).font = Font(name=FONT, size=10, bold=True)
    c = lg.cell(r, 2, meaning)
    c.font = Font(name=FONT, size=9, color="404040")
    c.alignment = wrap
    for cc in (1, 2):
        lg.cell(r, cc).border = BORDER
    lg.row_dimensions[r].height = 30
    r += 1

wb.active = 0
wb.save(OUT)
print(f"wrote {OUT}  ({N:,} leads, sorted by zone → locality → score)")
