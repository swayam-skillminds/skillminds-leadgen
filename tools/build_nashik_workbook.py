#!/usr/bin/env python3
"""Build the Nashik venue workbook, styled like Pheraas-Leads-Formatted.xlsx.

Same reason as tools/build_leads_workbook.py for reading the local CSV rather
than the Google Sheets export: the sync writes USER_ENTERED, so Sheets parses
"+91 93200 22422" as a formula and the export's Phones column is all #ERROR!.
data/nashik/venues.csv holds the same rows with the numbers intact.

Rows run Zone -> Locality -> Tier -> Score, so the book can be worked region by
region with the best venues of each locality on top. The Excluded tier (not a
venue at all) follows the venues, and operator (brand) rows come last.

    .venv/bin/python tools/build_nashik_workbook.py
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
SRC = ROOT / "data" / "nashik" / "venues.csv"
OUT = ROOT / "Nashik-Venues-Formatted.xlsx"

FONT = "Arial"
NAVY = "1F3864"
SLATE = "44546A"
BAND = "F2F5F9"
RULE = "D6DCE4"
LINK = "0563C1"

TIERS = ["Operator", "Luxury", "Premium", "Standard", "Unverified", "Excluded"]
TIER_COLOUR = {"Operator": "5B2C83", "Luxury": "2E7D32", "Premium": "1565C0",
               "Standard": "B26A00", "Unverified": "7F7F7F", "Excluded": "B3261E"}

# Zones are OUR editorial grouping of the scraper's localities for territory
# planning. Edit here to re-cut the map.
ZONES = {
    "Nashik City": "Nashik City & Dam Belt",
    "Gangapur Dam Belt": "Nashik City & Dam Belt",
    "Igatpuri": "Igatpuri–Trimbak Ghats",
    "Trimbakeshwar": "Igatpuri–Trimbak Ghats",
    "Wine Country (Dindori / Ozar / Niphad)": "Wine Country & East",
    "Sinnar": "Wine Country & East",
    "North & East District": "Wine Country & East",
    "Bhandardara (adjacent)": "Adjacent Hill Escapes",
    "Saputara (adjacent)": "Adjacent Hill Escapes",
    "Jawhar / Mokhada (adjacent)": "Adjacent Hill Escapes",
}
ZONE_ORDER = ["Region-wide", "Nashik City & Dam Belt", "Igatpuri–Trimbak Ghats",
              "Wine Country & East", "Adjacent Hill Escapes", "Unplaced"]
ZONE_TINT = {"Nashik City & Dam Belt": "EAF1FB", "Igatpuri–Trimbak Ghats": "EDF7ED",
             "Wine Country & East": "FFF6E5", "Adjacent Hill Escapes": "F6EEF8"}

thin = Side(style="thin", color=RULE)
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
body = Font(name=FONT, size=10)
bold = Font(name=FONT, size=10, bold=True)
link_font = Font(name=FONT, size=10, color=LINK, underline="single")
top = Alignment(vertical="top")
ctr = Alignment(horizontal="center", vertical="top")
wrap = Alignment(vertical="top", wrap_text=True)


def zone_of(row: dict) -> str:
    if row["Record Type"] == "Brand" or row["Locality"] == "Region-wide":
        return "Region-wide"
    return ZONES.get((row["Locality"] or "").strip(), "Unplaced")


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


def section(ws, row: int, col: int, text: str) -> None:
    ws.cell(row, col, text).font = Font(name=FONT, size=11, bold=True, color=NAVY)


def boxed(ws, row: int, cols, fill: str | None = None) -> None:
    for cc in cols:
        cell = ws.cell(row, cc)
        cell.border = BORDER
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)


# --------------------------------------------------------------------------
if not SRC.exists():
    sys.exit(f"missing {SRC} -- run the Nashik scrape or --export-only first")

rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
for row in rows:
    row["_zone"] = zone_of(row)
    row["_tier"] = row["Tier"] if row["Tier"] in TIERS else "Unverified"


def sort_key(r):
    # Venues first, then the Excluded tier (not venues), then operators last.
    group = {"Excluded": 1, "Operator": 2}.get(r["_tier"], 0)
    zone = ZONE_ORDER.index(r["_zone"]) if r["_zone"] in ZONE_ORDER else len(ZONE_ORDER)
    return (group, zone, r["Locality"], TIERS.index(r["_tier"]), -(as_num(r["Score"]) or 0))


rows.sort(key=sort_key)
N = len(rows)
LAST = N + 1

wb = Workbook()
wb.remove(wb.active)

# ==========================================================================
# VENUES
# ==========================================================================
COLS = [
    ("Score", 8), ("Tier", 10), ("Venue Name", 38), ("Venue Type", 22),
    ("Record Type", 11), ("Managed By", 15), ("Zone", 20), ("Locality", 20),
    ("Distance (km)", 9),
    ("Price From (₹)", 12), ("Price To (₹)", 12), ("Price Basis", 10),
    ("Bedrooms", 9), ("Max Guests", 9), ("Event Capacity", 10),
    ("Private Pool", 8), ("Event Friendly", 9), ("Pet Friendly", 8),
    ("Best Rating", 8), ("Total Reviews", 9),
    ("Primary Phone", 18), ("All Phones", 26), ("Booking Contact", 30),
    ("Primary Email", 30), ("All Emails", 30),
    ("Website", 26), ("Instagram", 20), ("Facebook", 20), ("WhatsApp", 16), ("LinkedIn", 20),
    ("Google Maps", 12), ("Listing", 12), ("Platforms", 22),
    ("Address", 44), ("Amenities", 44),
    ("Maps Rating", 9), ("Maps Reviews", 10), ("Platform Ratings", 30), ("Hotel Stars", 8),
    ("Bathrooms", 9), ("Lawn / Garden", 8), ("Check-in / Check-out", 16),
    ("Tier Reasons", 46), ("Type Reason", 34), ("Price Source", 16),
    ("Sources", 22), ("Src #", 6), ("Notes", 46), ("Description", 50), ("Photo", 10),
    ("First Seen", 16), ("Last Updated", 16), ("Any Contact", 9),
]
C = {name: i for i, (name, _) in enumerate(COLS, 1)}
L = {name: get_column_letter(i) for name, i in C.items()}
NC = len(COLS)

ws = wb.create_sheet("Venues")
ws.sheet_properties.tabColor = NAVY
for i, (name, width) in enumerate(COLS, 1):
    ws.cell(1, i, name)
    ws.column_dimensions[get_column_letter(i)].width = width
style_header(ws, 1, NC)

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

    def put_link(name, url, text=None):
        url = (url or "").strip()
        put(name, (text or handle(url)) if url else "", font=link_font if url else body,
            url=url or None)

    put("Score", as_num(row["Score"]), fmt="0.0", align=ctr)
    put("Tier", row["_tier"], align=ctr, font=bold)
    put("Venue Name", row["Venue Name"], font=bold)
    put("Venue Type", row["Venue Type"])
    put("Record Type", row["Record Type"])
    put("Managed By", row["Managed By"])
    put("Zone", row["_zone"], font=Font(name=FONT, size=10, bold=True, color=SLATE))
    put("Locality", row["Locality"])
    put("Distance (km)", as_num(row["Distance from City (km)"]), fmt="0", align=ctr)

    put("Price From (₹)", as_num(row["Price From (INR)"]), fmt="#,##0", align=ctr)
    put("Price To (₹)", as_num(row["Price To (INR)"]), fmt="#,##0", align=ctr)
    put("Price Basis", row["Price Basis"].replace("per ", "/ "), align=ctr)
    put("Bedrooms", as_num(row["Bedrooms"]), fmt="0", align=ctr)
    put("Max Guests", as_num(row["Max Guests (stay)"]), fmt="0", align=ctr)
    put("Event Capacity", as_num(row["Event Capacity"]), fmt="#,##0", align=ctr)
    put("Private Pool", row["Private Pool"], align=ctr)
    put("Event Friendly", row["Event Friendly"], align=ctr)
    put("Pet Friendly", row["Pet Friendly"], align=ctr)
    put("Best Rating", as_num(row["Best Rating"]), fmt="0.0", align=ctr)
    put("Total Reviews", as_num(row["Total Reviews"]), fmt="#,##0", align=ctr)

    put("Primary Phone", first_of(row["Phones"]), fmt="@")
    put("All Phones", row["Phones"], fmt="@", align=wrap)
    put("Booking Contact", row.get("Booking Contact", ""), fmt="@", align=wrap)
    put("Primary Email", first_of(row["Emails"]))
    put("All Emails", row["Emails"], align=wrap)
    for name in ("Website", "Instagram", "Facebook", "WhatsApp", "LinkedIn"):
        put_link(name, row[name])
    put_link("Google Maps", row["Google Maps URL"], "View map")
    listings = [u.strip() for u in (row["Listing URLs"] or "").split("|") if u.strip()]
    put_link("Listing", listings[0] if listings else "",
             f"View{f' (+{len(listings) - 1})' if len(listings) > 1 else ''}")
    put("Platforms", row["Platforms"], align=wrap)

    put("Address", row["Address"], align=wrap)
    put("Amenities", row["Amenities"], align=wrap)
    put("Maps Rating", as_num(row["Maps Rating"]), fmt="0.0", align=ctr)
    put("Maps Reviews", as_num(row["Maps Reviews"]), fmt="#,##0", align=ctr)
    put("Platform Ratings", row["Platform Ratings"], align=wrap)
    put("Hotel Stars", as_num(row["Hotel Stars"]), fmt="0", align=ctr)
    put("Bathrooms", as_num(row["Bathrooms"]), fmt="0.#", align=ctr)
    put("Lawn / Garden", row["Lawn / Garden"], align=ctr)
    put("Check-in / Check-out", row["Check-in / Check-out"])
    put("Tier Reasons", row["Tier Reasons"], align=wrap)
    put("Type Reason", row["Type Reason"], align=wrap)
    put("Price Source", row["Price Source"])
    put("Sources", row["Sources"], align=wrap)
    put("Src #", as_num(row["Source Count"]), fmt="0", align=ctr)
    put("Notes", row["Notes"], align=wrap)
    put("Description", row["Description"], align=wrap)
    put_link("Photo", row["Photo URL"], "View")
    put("First Seen", as_date(row["First Seen"]), fmt="dd-mmm-yy hh:mm")
    put("Last Updated", as_date(row["Last Updated"]), fmt="dd-mmm-yy hh:mm")
    # Yes when ANY way to reach the venue exists: its own phone/email/site/
    # socials, or the operator's booking line for a managed villa.
    reach = [L[n] for n in ("Primary Phone", "Booking Contact", "Primary Email", "Website",
                            "Instagram", "Facebook", "WhatsApp")]
    put("Any Contact", "=IF(OR(" + ",".join(f'{c}{r}<>""' for c in reach) + '),"Yes","No")',
        align=ctr)
    ws.row_dimensions[r].height = 15

ws.freeze_panes = "D2"
ws.auto_filter.ref = f"A1:{get_column_letter(NC)}{LAST}"

full = f"A2:{get_column_letter(NC)}{LAST}"
ws.conditional_formatting.add(full, FormulaRule(
    formula=["MOD(ROW(),2)=0"], fill=PatternFill("solid", fgColor=BAND), stopIfTrue=False))
for zname, tint in ZONE_TINT.items():
    ws.conditional_formatting.add(full, FormulaRule(
        formula=[f'${L["Zone"]}2="{zname}"'],
        fill=PatternFill("solid", fgColor=tint), stopIfTrue=False))
ws.conditional_formatting.add(f'{L["Score"]}2:{L["Score"]}{LAST}', ColorScaleRule(
    start_type="num", start_value=10, start_color="F8C9C4",
    mid_type="num", mid_value=40, mid_color="FFF0C2",
    end_type="num", end_value=75, end_color="C6E7C8"))
for tier, colour in TIER_COLOUR.items():
    ws.conditional_formatting.add(f'{L["Tier"]}2:{L["Tier"]}{LAST}', FormulaRule(
        formula=[f'${L["Tier"]}2="{tier}"'], font=Font(name=FONT, size=10, bold=True, color=colour)))
ws.conditional_formatting.add(f'{L["Price From (₹)"]}2:{L["Price From (₹)"]}{LAST}', FormulaRule(
    formula=[f'AND(${L["Price Basis"]}2="/ night",${L["Price From (₹)"]}2>=25000)'],
    font=Font(name=FONT, size=10, bold=True, color="2E7D32")))


def col_rng(name):
    return f"Venues!${L[name]}$2:${L[name]}${LAST}"


VENUES = f'COUNTIFS({col_rng("Record Type")},"Property")'

# ==========================================================================
# OVERVIEW
# ==========================================================================
ov = wb.create_sheet("Overview", 0)
ov.sheet_properties.tabColor = "C00000"
ov.sheet_view.showGridLines = False
for col, width in zip("ABCDEFGH", (30, 12, 12, 12, 12, 12, 14, 4)):
    ov.column_dimensions[col].width = width

snapshot = datetime.now().strftime("%d %b %Y, %H:%M")
r = title_block(ov, "Pheraas — Nashik High-End Venue Book",
                f"Generated {snapshot} from data/nashik/venues.csv · {N:,} rows · "
                f"sorted by zone, locality, tier, then score", 7)


def kpi(row, col, label, formula, fmt="#,##0"):
    ov.cell(row, col, label).font = Font(name=FONT, size=9, color="7F7F7F")
    c = ov.cell(row + 1, col, formula)
    c.font = Font(name=FONT, size=18, bold=True, color=NAVY)
    c.number_format = fmt


kpi(r, 1, "VENUES", f"={VENUES}")
kpi(r, 2, "LUXURY", f'=COUNTIF({col_rng("Tier")},"Luxury")')
kpi(r, 3, "PREMIUM", f'=COUNTIF({col_rng("Tier")},"Premium")')
kpi(r, 4, "OPERATORS", f'=COUNTIF({col_rng("Tier")},"Operator")')
kpi(r, 5, "ANY CONTACT", f'=COUNTIFS({col_rng("Record Type")},"Property",{col_rng("Any Contact")},"Yes")')
kpi(r, 6, "WITH PRICE", f'=COUNTIFS({col_rng("Record Type")},"Property",{col_rng("Price From (₹)")},">0")')
kpi(r, 7, "AVG ₹/NIGHT",
    f'=IFERROR(AVERAGEIFS({col_rng("Price From (₹)")},{col_rng("Price Basis")},"/ night",'
    f'{col_rng("Record Type")},"Property"),"")', fmt="₹#,##0")
r += 3

# --- by tier ----------------------------------------------------------------
section(ov, r, 1, "BY TIER")
r += 1
for i, h in enumerate(["Tier", "Rows", "With phone", "With price", "Avg ₹/night", "Avg score"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 6, SLATE)
r += 1
tier_start = r
for tier in TIERS:
    ov.cell(r, 1, tier).font = Font(name=FONT, size=10, bold=True, color=TIER_COLOUR[tier])
    ov.cell(r, 2, f'=COUNTIF({col_rng("Tier")},A{r})')
    ov.cell(r, 3, f'=COUNTIFS({col_rng("Tier")},A{r},{col_rng("Primary Phone")},"?*")')
    ov.cell(r, 4, f'=COUNTIFS({col_rng("Tier")},A{r},{col_rng("Price From (₹)")},">0")')
    ov.cell(r, 5, f'=IFERROR(AVERAGEIFS({col_rng("Price From (₹)")},{col_rng("Tier")},A{r},'
                  f'{col_rng("Price Basis")},"/ night"),"")')
    ov.cell(r, 6, f'=IFERROR(AVERAGEIF({col_rng("Tier")},A{r},{col_rng("Score")}),"")')
    for cc, fmt in ((2, "#,##0"), (3, "#,##0"), (4, "#,##0"), (5, "₹#,##0"), (6, "0.0")):
        ov.cell(r, cc).number_format, ov.cell(r, cc).font = fmt, body
    boxed(ov, r, range(1, 7))
    r += 1
ov.cell(r, 1, "Total").font = bold
for cc in (2, 3, 4):
    col = get_column_letter(cc)
    ov.cell(r, cc, f"=SUM({col}{tier_start}:{col}{r - 1})").font = bold
boxed(ov, r, range(1, 7), BAND)
r += 3

# --- by zone ----------------------------------------------------------------
section(ov, r, 1, "BY ZONE")
r += 1
for i, h in enumerate(["Zone", "Venues", "Luxury", "Premium", "With phone", "Avg score", "Localities"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 7, SLATE)
r += 1
zone_start = r
for z in ZONE_ORDER[1:]:
    ov.cell(r, 1, z).font = bold
    zr = f'{col_rng("Zone")},A{r}'
    ov.cell(r, 2, f'=COUNTIFS({zr},{col_rng("Record Type")},"Property")')
    ov.cell(r, 3, f'=COUNTIFS({zr},{col_rng("Tier")},"Luxury")')
    ov.cell(r, 4, f'=COUNTIFS({zr},{col_rng("Tier")},"Premium")')
    ov.cell(r, 5, f'=COUNTIFS({zr},{col_rng("Record Type")},"Property",{col_rng("Primary Phone")},"?*")')
    ov.cell(r, 6, f'=IFERROR(AVERAGEIFS({col_rng("Score")},{zr},{col_rng("Record Type")},"Property"),"")')
    for cc, fmt in ((2, "#,##0"), (3, "#,##0"), (4, "#,##0"), (5, "#,##0"), (6, "0.0")):
        ov.cell(r, cc).number_format, ov.cell(r, cc).font = fmt, body
    places = ", ".join(k for k, v in ZONES.items() if v == z) or "no place name or pin matched"
    note = ov.cell(r, 7, places)
    note.font = Font(name=FONT, size=8, color="7F7F7F")
    note.alignment = Alignment(vertical="center", wrap_text=True)
    boxed(ov, r, range(1, 8))
    ov.row_dimensions[r].height = 30
    r += 1
ov.cell(r, 1, "Total").font = bold
for cc in (2, 3, 4, 5):
    col = get_column_letter(cc)
    ov.cell(r, cc, f"=SUM({col}{zone_start}:{col}{r - 1})").font = bold
boxed(ov, r, range(1, 8), BAND)
r += 3

# --- by venue type ------------------------------------------------------------
section(ov, r, 1, "BY VENUE TYPE")
r += 1
for i, h in enumerate(["Venue type", "Venues", "Luxury", "Premium", "Avg ₹/night", "Avg ₹/plate", "Avg score"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 7, SLATE)
r += 1
types = sorted({row["Venue Type"] for row in rows if row["Record Type"] == "Property"},
               key=lambda t: -sum(1 for row in rows if row["Venue Type"] == t))
for vt in types:
    ov.cell(r, 1, vt).font = body
    tr = f'{col_rng("Venue Type")},A{r}'
    ov.cell(r, 2, f'=COUNTIFS({tr},{col_rng("Record Type")},"Property")')
    ov.cell(r, 3, f'=COUNTIFS({tr},{col_rng("Tier")},"Luxury")')
    ov.cell(r, 4, f'=COUNTIFS({tr},{col_rng("Tier")},"Premium")')
    ov.cell(r, 5, f'=IFERROR(AVERAGEIFS({col_rng("Price From (₹)")},{tr},{col_rng("Price Basis")},"/ night"),"")')
    ov.cell(r, 6, f'=IFERROR(AVERAGEIFS({col_rng("Price From (₹)")},{tr},{col_rng("Price Basis")},"/ plate"),"")')
    ov.cell(r, 7, f'=IFERROR(AVERAGEIF({col_rng("Venue Type")},A{r},{col_rng("Score")}),"")')
    for cc, fmt in ((2, "#,##0"), (3, "#,##0"), (4, "#,##0"), (5, "₹#,##0"), (6, "₹#,##0"), (7, "0.0")):
        ov.cell(r, cc).number_format, ov.cell(r, cc).font = fmt, body
    boxed(ov, r, range(1, 8))
    r += 1
r += 2

# --- contactability / by locality -------------------------------------------
section(ov, r, 1, "CONTACTABILITY (venues)")
section(ov, r, 5, "BY LOCALITY")
r += 1
for i, h in enumerate(["Channel", "Venues", "Coverage"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 3, SLATE)
for i, h in enumerate(["Locality", "Venues", "Avg score"], 5):
    ov.cell(r, i, h)
style_header(ov, r, 3, SLATE, start=5)
r += 1
channels = ["Any Contact", "Primary Phone", "Booking Contact", "Primary Email", "Website",
            "Instagram", "Facebook", "WhatsApp", "LinkedIn", "Google Maps", "Listing"]
locs = sorted({row["Locality"] for row in rows if row["_zone"] != "Region-wide"},
              key=lambda x: (ZONE_ORDER.index(ZONES.get(x, "Unplaced")), x))
ch_start = r
for i in range(max(len(channels), len(locs))):
    if i < len(channels):
        name = channels[i]
        ov.cell(r + i, 1, name.replace("Primary ", "")).font = body
        crit = '"Yes"' if name == "Any Contact" else '"?*"'
        c2 = ov.cell(r + i, 2, f'=COUNTIFS({col_rng("Record Type")},"Property",{col_rng(name)},{crit})')
        c2.number_format, c2.font = "#,##0", body
        c3 = ov.cell(r + i, 3, f"=IFERROR(B{r + i}/{VENUES},0)")
        c3.number_format, c3.font = "0.0%", body
        boxed(ov, r + i, (1, 2, 3))
    if i < len(locs):
        ov.cell(r + i, 5, locs[i]).font = body
        c6 = ov.cell(r + i, 6, f'=COUNTIFS({col_rng("Locality")},E{r + i},{col_rng("Record Type")},"Property")')
        c6.number_format, c6.font = "#,##0", body
        c7 = ov.cell(r + i, 7, f'=IFERROR(AVERAGEIFS({col_rng("Score")},{col_rng("Locality")},E{r + i},'
                               f'{col_rng("Record Type")},"Property"),"")')
        c7.number_format, c7.font = "0.0", body
        boxed(ov, r + i, (5, 6, 7))
ov.conditional_formatting.add(f"C{ch_start}:C{ch_start + len(channels) - 1}", ColorScaleRule(
    start_type="num", start_value=0, start_color="F8C9C4",
    mid_type="num", mid_value=0.5, mid_color="FFF0C2",
    end_type="num", end_value=1, end_color="C6E7C8"))
r += max(len(channels), len(locs)) + 2

# --- operators --------------------------------------------------------------
section(ov, r, 1, "OPERATORS (run several venues)")
r += 1
for i, h in enumerate(["Operator", "Venues in book", "Luxury", "Premium", "Phone", "", "Portfolio note"], 1):
    ov.cell(r, i, h)
style_header(ov, r, 7, SLATE)
r += 1
for row in rows:
    if row["Record Type"] != "Brand":
        continue
    ov.cell(r, 1, row["Venue Name"]).font = bold
    mr = f'{col_rng("Managed By")},A{r}'
    ov.cell(r, 2, f'=COUNTIFS({mr},{col_rng("Record Type")},"Property")')
    ov.cell(r, 3, f'=COUNTIFS({mr},{col_rng("Tier")},"Luxury")')
    ov.cell(r, 4, f'=COUNTIFS({mr},{col_rng("Tier")},"Premium")')
    phone = ov.cell(r, 5, first_of(row["Phones"]))
    phone.number_format = "@"
    portfolio = next((n.strip() for n in row["Notes"].split(";") if n.strip().startswith("Manages")), "")
    note = ov.cell(r, 7, portfolio)
    note.font = Font(name=FONT, size=8, color="7F7F7F")
    for cc in (2, 3, 4, 5):
        ov.cell(r, cc).font = body
    boxed(ov, r, range(1, 8))
    r += 1
r += 1

ov.cell(r, 1, "Tiers describe the venue, not the lead: Luxury and Premium need one high-end "
              "criterion (premium platform/operator, ≥4.3 over ≥20 reviews, private pool or "
              "≥3 bedrooms, premium hotel/winery); price lifts a venue to Luxury and ranks it. "
              "Zones are an editorial grouping defined in tools/build_nashik_workbook.py."
        ).font = Font(name=FONT, size=8, italic=True, color="7F7F7F")
ov.cell(r, 1).alignment = wrap
ov.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
ov.row_dimensions[r].height = 36

# ==========================================================================
# DATA QUALITY
# ==========================================================================
dq = wb.create_sheet("Data Quality")
dq.sheet_properties.tabColor = "B26A00"
dq.sheet_view.showGridLines = False
for col, width in zip("ABCD", (34, 12, 12, 78)):
    dq.column_dimensions[col].width = width
r = title_block(dq, "Data Quality & Known Issues",
                "Field coverage is live (formulas over the Venues sheet, venue rows only). "
                "Findings below were observed at build time.", 4)
for i, h in enumerate(["Field", "Filled", "Coverage", "Reading"], 1):
    dq.cell(r, i, h)
style_header(dq, r, 4, SLATE)
r += 1
FIELDS = [  # (column, "text" | "num", note)
    ("Venue Type", "text", "Classified by rules from name, source category and amenities; the "
                           "AI decided the cases the rules could not."),
    ("Primary Phone", "text", "Strongest channel. Recovered from the local CSV (see finding 1)."),
    ("Booking Contact", "text", "Operator's booking line on every villa StayVista, SaffronStays, "
                                "Elivaas, EKOSTAY … manage — they hide owner numbers."),
    ("Primary Email", "text", "Mostly from venue websites; platforms never expose one."),
    ("Website", "text", "Platform pages (StayVista, Airbnb …) are never recorded as a website."),
    ("Instagram", "text", "Many venues market here first."),
    ("Google Maps", "text", "Anchors dedup and location for Maps-found venues."),
    ("Listing", "text", "Booking-platform pages: StayVista, SaffronStays, Elivaas, Airbnb, "
                        "Booking.com, WedMeGood."),
    ("Amenities", "text", ""),
    ("Price From (₹)", "num", "Per night for stays, per plate for banquets. Booking prices are "
                              "for a Saturday three weeks ahead; Airbnb's for its default dates."),
    ("Bedrooms", "num", "Platforms state it; Maps does not."),
    ("Max Guests", "num", ""),
    ("Event Capacity", "num", "Mostly WedMeGood and venue websites."),
    ("Best Rating", "num", "Highest-volume rating across Maps, WedMeGood and every platform."),
    ("Distance (km)", "num", "Blank where no source gave a map pin."),
]
start = r
for label, kind, note in FIELDS:
    dq.cell(r, 1, label).font = body
    crit = '"?*"' if kind == "text" else '">0"'
    c2 = dq.cell(r, 2, f'=COUNTIFS({col_rng("Record Type")},"Property",{col_rng(label)},{crit})')
    c2.number_format, c2.font = "#,##0", body
    c3 = dq.cell(r, 3, f"=IFERROR(B{r}/{VENUES},0)")
    c3.number_format, c3.font = "0.0%", body
    n4 = dq.cell(r, 4, note)
    n4.font = Font(name=FONT, size=9, color="404040")
    n4.alignment = wrap
    boxed(dq, r, range(1, 5))
    r += 1
dq.conditional_formatting.add(f"C{start}:C{r - 1}", ColorScaleRule(
    start_type="num", start_value=0, start_color="F8C9C4",
    mid_type="num", mid_value=0.5, mid_color="FFF0C2",
    end_type="num", end_value=1, end_color="C6E7C8"))
r += 2

section(dq, r, 1, "FINDINGS")
r += 1
phones_in_csv = sum(1 for row in rows if row["Phones"].strip())
NOTES = [
    ("1. Phone column in the Google Sheets export is unusable",
     f"All {phones_in_csv:,} phone numbers read #ERROR! in the 'Nashik Venues' tab of "
     "Pheraas-ScrapedLeads-16thSept.xlsx: the sync writes USER_ENTERED, so Sheets parses "
     "\"+91 …\" as a formula. This workbook rebuilds from data/nashik/venues.csv, where the "
     "numbers are intact. The live Sheet stays affected until sheets.py writes RAW."),
    ("2. Google Search was blocked for the whole run",
     "Google answered search queries with CAPTCHAs / HTTP 429 from this connection, so the "
     "search-driven sources (Google Search, Facebook, LinkedIn) added only a handful of "
     "venues. Maps, the stay platforms and WedMeGood were unaffected. Re-running those three "
     "from another network would add names and contacts."),
    ("3. Airbnb and Booking.com do not expose owners",
     "Their venues carry price, size and ratings but no contact. A targeted Google Maps "
     "lookup searched each by name and locality and took the phone / website only when both "
     "the name and the location matched (notes say 'contact from Google Maps listing …'). "
     "Those still without a contact had no confident Maps match."),
    ("4. Excluded rows are not venues",
     "Maps answers 'villa rental agency' with estate agents, builders selling plots, tour "
     "operators and bike rentals. They are kept at the very bottom (tier Excluded) so nothing "
     "vanishes silently, and can be filtered out."),
    ("5. Price is a ranking signal, not a filter",
     "A high nightly price with nothing else known (no pool, rating or platform) stays "
     "Standard by design; it still ranks near the top of Standard. A known price under "
     "₹4,000/night caps a venue at Standard."),
    ("6. Zone is our classification, not the scraper's",
     "Localities come from the venue's own place name or map pin; the zones group them for "
     "territory planning (ZONES in tools/build_nashik_workbook.py)."),
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
    ("Tier", "Operator: a brand running several venues. Luxury: qualifies AND ≥ ₹25,000/night "
             "(or ≥ ₹2,000/plate, or many premium signals). Premium: any one high-end criterion. "
             "Standard: no criterion met, or a budget price/operator. Unverified: little known. "
             "Excluded: not a venue."),
    ("Score", "0–100 rank within the book: 40% premium signal (price-led, then platform, pool, "
              "size), 25% reputation (rating × reviews across all sources), 20% contactability, "
              "15% number of sources."),
    ("Venue Type", "What kind of property it is — independent of how upscale (that's Tier)."),
    ("Managed By", "The operator that runs the venue (StayVista, SaffronStays, EKOSTAY …)."),
    ("Zone / Locality", "Locality from the venue's place name or map pin; Zone groups "
                        "localities. Rows sort Zone → Locality → Tier → Score; Excluded "
                        "rows follow the venues and operators come last."),
    ("Distance (km)", "Straight-line distance from Nashik city centre."),
    ("Price From / To", "Rupees. Basis says / night or / plate. Green bold = ≥ ₹25,000/night."),
    ("Max Guests / Event Capacity", "Overnight capacity vs guests for a function."),
    ("Best Rating / Total Reviews", "The rating backed by the most reviews, on a 5-point scale "
                                    "(Booking's 10-point scores are halved); reviews summed."),
    ("Primary Phone / Email", "First value; the All… column keeps every number / address."),
    ("Booking Contact", "For operator-run villas: the operator's booking line (the owner's "
                        "number is not published)."),
    ("Any Contact", "Formula: Yes when the row has any phone, booking line, email, website, "
                    "Instagram, Facebook or WhatsApp."),
    ("Website / Instagram / …", "Clickable; display text is shortened."),
    ("Listing / Platforms", "First booking-platform page (+ how many more) and every platform "
                            "the venue appears on."),
    ("Tier Reasons", "The facts that set the tier, e.g. 'private pool; ₹31,250/night'."),
    ("Type Reason", "Why the venue type was chosen (a rule, or 'ai')."),
    ("Sources / Src #", "Which scrapers found the venue, and how many."),
]
for label, meaning in GUIDE:
    lg.cell(r, 1, label).font = bold
    c = lg.cell(r, 2, meaning)
    c.font = Font(name=FONT, size=9, color="404040")
    c.alignment = wrap
    boxed(lg, r, (1, 2))
    lg.row_dimensions[r].height = 30
    r += 1

wb.active = 0
# openpyxl stores formulas without cached results; make Excel / Numbers compute
# every one when the file is opened.
wb.calculation.fullCalcOnLoad = True
wb.save(OUT)
print(f"wrote {OUT}  ({N:,} rows, sorted zone → locality → tier → score)")
