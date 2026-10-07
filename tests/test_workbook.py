"""Phase 4: the deliverable workbook (Overview + Records), CSV and JSON on one column model.

Golden files (``tests/golden/workbooks/<scenario>.json``) pin each fixture
workbook's structure: tabs, headers, cell values and types, styles, number
formats, hyperlinks, merged cells, widths, freeze panes, the autofilter and
conditional formatting. A difference is a change to the deliverable: look at
it, then accept it with ``WORKBOOK_UPDATE=1 python -m pytest tests/test_workbook.py``
and regenerate ``docs/samples/`` (``tools/build_sample_workbooks.py``).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import update

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workbook_fixtures as wf  # noqa: E402
from leadscraper.core.lead import Lead  # noqa: E402
from leadscraper.db.models import Run  # noqa: E402
from leadscraper.services import exports  # noqa: E402
from leadscraper.services.workspace import default_workspace  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "workbooks"
UPDATE = os.environ.get("WORKBOOK_UPDATE") == "1"
RUNS = Run.__table__


@pytest.fixture
def engine():
    from app_db import fresh_application_db

    engine = fresh_application_db()
    yield engine
    engine.dispose()
    wf.unregister_demo_lead_types()


def build(engine, scenario: str, fmt: str = "xlsx", **kwargs):
    run_id = wf.build(engine, scenario)
    ws = default_workspace(engine)
    _, content, table = exports.export(engine, ws, fmt, run_id=run_id,
                                       generated_at=wf.FIXED_NOW, **kwargs)
    return run_id, content, table


def patch_snapshot(engine, run_id: int, **changes) -> None:
    with engine.begin() as conn:
        snapshot = conn.execute(RUNS.select().where(RUNS.c.id == run_id)).mappings() \
            .first()["config_snapshot"]
        conn.execute(update(RUNS).where(RUNS.c.id == run_id).values(
            config_snapshot={**snapshot, **changes}))


# --- describing a workbook ------------------------------------------------------------------

def _colour(color) -> str | None:
    if color is None or color.type != "rgb" or not isinstance(color.rgb, str):
        return None
    return color.rgb[-6:]


def _cell(cell) -> dict:
    value = cell.value.isoformat() if isinstance(cell.value, datetime) else cell.value
    out = {"v": value, "t": cell.data_type}
    if cell.number_format != "General":
        out["fmt"] = cell.number_format
    font = cell.font
    style = "".join(k for k, on in (("b", font.b), ("i", font.i), ("u", font.u)) if on)
    colour = _colour(font.color)
    out["font"] = f"{font.name} {font.sz:g}" + (f" {style}" if style else "") + \
        (f" #{colour}" if colour else "")
    if cell.fill is not None and cell.fill.fill_type == "solid":
        out["fill"] = _colour(cell.fill.fgColor)
    if cell.hyperlink is not None:
        out["link"] = cell.hyperlink.target
    align = cell.alignment
    flags = [align.horizontal or "", align.vertical or "", "wrap" if align.wrap_text else ""]
    if any(flags):
        out["align"] = " ".join(f for f in flags if f)
    return out


def describe(content: bytes) -> dict:
    """Everything a golden file pins, as JSON-able data."""
    book = load_workbook(io.BytesIO(content))
    out: dict = {"sheets": book.sheetnames, "active": book.active.title}
    for ws in book.worksheets:
        out[ws.title] = {
            "dimensions": ws.dimensions,
            "freeze": ws.freeze_panes,
            "autofilter": ws.auto_filter.ref,
            "tab": _colour(ws.sheet_properties.tabColor),
            "gridlines": ws.sheet_view.showGridLines,
            "merged": sorted(str(r) for r in ws.merged_cells.ranges),
            "widths": {k: round(v.width, 1) for k, v in sorted(ws.column_dimensions.items())
                       if v.width},
            "conditional": sorted(
                f"{rng.sqref} {rule.type} {rule.formula or ''}"
                + (f" {[_colour(c) for c in rule.colorScale.color]}" if rule.colorScale else "")
                for rng in ws.conditional_formatting for rule in rng.rules),
            "cells": {c.coordinate: _cell(c) for row in ws.iter_rows() for c in row
                      if c.value is not None},
        }
    return out


@pytest.mark.parametrize("scenario", wf.SCENARIOS)
def test_workbook_matches_its_golden_file(engine, scenario):
    """Tabs, headers, styles, number formats, hyperlinks: the deliverable, pinned."""
    _, content, _ = build(engine, scenario)
    got = describe(content)
    path = GOLDEN / f"{scenario}.json"
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"wrote a new golden file {path.name}; check it and run again")
    want = json.loads(path.read_text(encoding="utf-8"))
    assert got == want, (f"{scenario} workbook changed; if intended: "
                         "WORKBOOK_UPDATE=1 python -m pytest tests/test_workbook.py")


def test_samples_are_current():
    """docs/samples/ holds exactly the golden workbooks (tools/build_sample_workbooks.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from build_sample_workbooks import NAMES, OUT

    for scenario, name in NAMES.items():
        want = json.loads((GOLDEN / f"{scenario}.json").read_text(encoding="utf-8"))
        assert describe((OUT / name).read_bytes()) == want, (
            f"docs/samples/{name} is stale: run tools/build_sample_workbooks.py")


# --- the rules, stated plainly ----------------------------------------------------------------

def test_exactly_two_tabs_with_the_reference_style(engine):
    _, content, table = build(engine, "local_business")
    book = load_workbook(io.BytesIO(content))
    assert book.sheetnames == ["Overview", "Records"] and book.active.title == "Overview"
    records = book["Records"]
    headers = [c.value for c in records[1]]
    assert headers == table.headers
    head = records["A1"]
    assert head.font.b and _colour(head.font.color) == "FFFFFF" and head.font.name == "Arial"
    assert _colour(head.fill.fgColor) == "1F3864"
    assert records.freeze_panes == "C2"                          # header + score + name
    last = records.cell(1, len(headers)).column_letter
    assert records.auto_filter.ref == f"A1:{last}11"
    # Widths tuned per column type.
    widths = {h: records.column_dimensions[records.cell(1, i).column_letter].width
              for i, h in enumerate(headers, start=1)}
    assert widths["Name"] == 38 and widths["Score"] == 8 and widths["Address"] == 44
    assert widths["Primary Phone"] == 18 and widths["First Seen"] == 15
    # A colour scale on every score column.
    scales = {str(r.sqref) for r in records.conditional_formatting
              for rule in r.rules if rule.type == "colorScale"}
    for header in ("Score", "Fit", "Intent", "Reach"):
        letter = records.cell(1, headers.index(header) + 1).column_letter
        assert f"{letter}2:{letter}11" in scales


def test_phones_and_ids_are_text_and_links_are_clickable(engine):
    _, content, _ = build(engine, "local_business")
    records = load_workbook(io.BytesIO(content))["Records"]
    col = {c.value: c.column for c in records[1]}
    rows = range(2, records.max_row + 1)
    for row in rows:
        phone = records.cell(row, col["Primary Phone"])
        if phone.value:
            # Text, never a number or a formula: the Sheets "#ERROR!" problem cannot recur.
            assert phone.data_type == "s" and phone.number_format == "@"
            assert phone.value.startswith("+91 ")
    site = next(records.cell(r, col["Website"]) for r in rows
                if records.cell(r, col["Website"]).value)
    assert site.hyperlink.target.startswith("https://") and "://" not in site.value
    assert site.font.u == "single"
    maps = next(records.cell(r, col["Google Maps"]) for r in rows
                if records.cell(r, col["Google Maps"]).value)
    assert maps.value == "View map" and maps.hyperlink.target.startswith("https://maps.google")
    assert records.cell(2, col["First Seen"]).is_date
    assert records.cell(2, col["Org ID"]).data_type == "n"


def test_records_sort_by_score_grouped_by_place_and_other_orders(engine):
    run_id, _, table = build(engine, "local_business")
    assert table.sort_label == "place, then score"
    places = [r.place for r in table.records]
    assert places == sorted(places, key=["Pune", "Mumbai"].index)   # the run's place order
    for place in ("Pune", "Mumbai"):
        scores = [r.score for r in table.records if r.place == place]
        assert scores == sorted(scores, reverse=True)
    ws = default_workspace(engine)
    for mode, label in (("score", "score (highest first)"), ("category", "category, then score"),
                        ("legacy", "the run's profile order")):
        patch_snapshot(engine, run_id, output={"sort": mode})
        again = exports.build_table(engine, ws, run_id=run_id)
        ordered = again.records
        assert again.sort_label == label
        if mode == "score":
            assert [r.score for r in ordered] == sorted((r.score for r in ordered), reverse=True)
        if mode == "category":
            assert [r.lead.category for r in ordered] == sorted(r.lead.category for r in ordered)
        if mode == "legacy":   # locality, category, then score: the run CSV's order
            keys = [(r.lead.locality, r.lead.category) for r in ordered]
            assert keys == sorted(keys)


def test_three_lead_types_signals_people_and_inferred_contacts(engine):
    _, content, table = build(engine, "mix")
    headers = table.headers
    # Each lead type's own columns, placed where the lead type asked.
    for header in ("Category", "Open Tech Roles", "Last Round", "Amount Raised", "Roles Hiring"):
        assert header in headers
    assert headers.index("Open Tech Roles") < headers.index("Primary Phone") \
        < headers.index("Roles Hiring")
    for header in ("Top Signal", "Signal Date", "Signal Link", "Decision Makers",
                   "Inferred Email (unverified)"):
        assert header in headers
    by_name = {r.lead.business_name: r for r in table.records}
    blue = by_name["Bluebonnet Data Labs"]
    assert blue.lead_types == ["local_business", "demo_hiring", "demo_funding"]
    values = {c.header: c.value(blue) for c in table.columns}
    assert values["Lead Types"] == "Local businesses, Hiring (fixture), Funding news (fixture)"
    assert values["Open Tech Roles"] == 3
    # The strongest unexpired signal leads; strength fades with age.
    assert values["Top Signal"] == "Funding round: Raised a $12M Series A"
    assert values["Decision Makers"] == "Avery Example (CTO); Jordan Sample (Head of Engineering)"
    # Inferred contacts sit in their own column, never among published ones.
    assert values["Inferred Email (unverified)"] == "avery.example@bluebonnetdata.example"
    assert values["Primary Email"] is None          # nothing published: never promoted
    assert not any("avery" in str(values.get(h) or "") for h in ("Primary Email", "Other Emails"))
    # Weights renormalise per run: intent counts 0 where nothing was found.
    lonestar = by_name["Lonestar Ledger Software"]
    assert lonestar.overall.intent == 0.0 and blue.overall.intent == 0.8
    assert blue.score > lonestar.score
    records = load_workbook(io.BytesIO(content))["Records"]
    letter = records.cell(1, headers.index("Signal Date") + 1).column_letter
    rules = [rule.formula[0] for rng in records.conditional_formatting
             for rule in rng.rules if str(rng.sqref).startswith(letter + "2")]
    assert any("TODAY()" in f and "<=30" in f for f in rules)       # signal recency highlighted


def test_venues_keep_the_vertical_columns_and_tier_colours(engine):
    _, content, table = build(engine, "venues")
    headers = table.headers
    assert headers[:5] == ["Score", "Name", "Tier", "Venue Type", "Record Type"]
    assert "Price From (INR)" in headers and "Bedrooms" in headers
    book = load_workbook(io.BytesIO(content))
    tier = book["Records"].cell(2, 3)
    assert tier.value == "Luxury" and _colour(tier.font.color) == "2E7D32" and tier.font.b
    price = book["Records"].cell(2, headers.index("Price From (INR)") + 1)
    assert price.number_format == '"₹"#,##0' and isinstance(price.value, (int, float))
    text = [c.value for row in book["Overview"].iter_rows() for c in row
            if isinstance(c.value, str)]
    assert "BY TIER" in text and any(t.startswith("on · claude_cli · $0.42 spent") for t in text)


def test_an_empty_run_explains_itself(engine):
    _, content, table = build(engine, "empty")
    book = load_workbook(io.BytesIO(content))
    assert book.sheetnames == ["Overview", "Records"]
    assert book["Records"].max_row == 1 and table.headers[:2] == ["Score", "Name"]
    text = [c.value for row in book["Overview"].iter_rows() for c in row
            if isinstance(c.value, str)]
    assert "WHY THERE ARE NO RECORDS" in text
    assert any("no_businesses_parsed ×2 (google_maps)" in t for t in text)
    assert any("work units finished without finding an organisation" in t for t in text)


def test_output_is_deterministic(engine, tmp_path, monkeypatch):
    first = describe(build(engine, "mix")[1])
    from leadscraper.db.engine import open_database

    other = open_database(f"sqlite:///{tmp_path / 'again.sqlite3'}")
    try:
        second = describe(build(other, "mix")[1])
    finally:
        other.dispose()
    assert first == second


def test_csv_and_json_share_the_records_column_model(engine):
    run_id, _, table = build(engine, "mix")
    ws = default_workspace(engine)
    csv_rows = list(csv.reader(io.StringIO(
        exports.export(engine, ws, "csv", run_id=run_id)[1].decode("utf-8-sig"))))
    assert csv_rows[0] == table.headers
    data = json.loads(exports.export(engine, ws, "json", run_id=run_id)[1])
    assert [c["header"] for c in data["columns"]] == table.headers
    assert [r["id"] for r in data["records"]] == [r.id for r in table.records]
    first = data["records"][0]
    assert first["values"]["name"] == csv_rows[1][1] == table.records[0].lead.business_name
    assert data["summary"]["totals"]["records"] == len(table.records)
    assert first["signals"] and first["people"] and first["inferred_contacts"]
    # Dates: ISO with an offset in JSON, the run's local time in the CSV.
    assert first["values"]["first_seen"].endswith("+00:00")
    assert len(csv_rows[1][table.headers.index("First Seen")]) == len("2026-08-12 04:30")


def test_fields_the_run_left_out_have_no_columns(engine):
    run_id = wf.build(engine, "local_business")
    patch_snapshot(engine, run_id, fields={"include": ["phones", "website"]})
    headers = exports.build_table(engine, default_workspace(engine), run_id=run_id).headers
    assert "Primary Phone" in headers and "Website" in headers
    for gone in ("Primary Email", "Address", "Instagram", "Maps Rating"):
        assert gone not in headers


def test_filters_leave_organisations_out_of_exports(engine):
    run_id = wf.build(engine, "local_business")
    patch_snapshot(engine, run_id, filters={"min_rating": 4.4, "must_have": ["phone"]})
    table = exports.build_table(engine, default_workspace(engine), run_id=run_id,
                                generated_at=wf.FIXED_NOW)
    names = {r.lead.business_name for r in table.records}
    assert "Harbour Dental Clinic" not in names            # no phone (and rated 3.9)
    assert "Riverside Dental Care" not in names            # rated 4.2 < 4.4
    assert "Green Leaf Kitchens" in names                  # unrated: kept
    assert table.context["filtered"] == 10 - len(names)


def test_legacy_rankings_are_unchanged(engine):
    """With legacy weights (fit only), the export's score is the legacy score."""
    from leadscraper.engine.runner import config_from_snapshot
    from leadscraper.leadtypes.local_business.scoring import Scorer

    _, _, table = build(engine, "venues")
    scorer = Scorer(config_from_snapshot(wf.venues_document()))
    for record in table.records:
        lead = Lead.from_dict(dict(record.lead.__dict__))
        assert record.score == pytest.approx(scorer.score(scorer.apply(lead)), abs=0.01)


def test_text_is_never_a_formula_and_links_are_http_only(engine):
    from leadscraper.db.store import LeadStore

    run_id = wf.build(engine, "empty")
    store = LeadStore(engine, project="Sample telescope makers", country="IN")
    store.run_id = run_id
    store.upsert(Lead(business_name='=HYPERLINK("http://evil.example","x")', locality="Nagpur",
                      website="javascript:alert(1)", phones=["+917100000001"],
                      sources=["google_maps"]))
    store.close()
    content = exports.export(engine, default_workspace(engine), "xlsx", run_id=run_id)[1]
    records = load_workbook(io.BytesIO(content))["Records"]
    col = {c.value: c.column for c in records[1]}
    name = records.cell(2, col["Name"])
    assert name.data_type == "s" and name.value.startswith("=HYPERLINK")
    assert records.cell(2, col["Website"]).hyperlink is None


def test_sheets_sync_writes_raw_values():
    """USER_ENTERED made Sheets parse "+91 …" as a formula (#ERROR!); RAW stores it as typed."""
    from leadscraper.config import Config
    from leadscraper.export.sheets import SheetsWriter

    class FakeSheet:
        row_count, col_count = 1000, 60

        def __init__(self):
            self.calls = []

        def update(self, **kwargs):
            self.calls.append(kwargs)

        def resize(self, **kwargs):
            pass

    writer = SheetsWriter(Config({"sheets": {"enabled": True}}))
    sheet = FakeSheet()
    writer._write_rows(sheet, [["Phones", "Business Name"], ["+91 93200 22422", "=1+1"]])
    assert sheet.calls[0]["value_input_option"] == "RAW"
    assert sheet.calls[0]["values"][1] == ["+91 93200 22422", "=1+1"]


def test_a_finished_run_writes_its_workbook(engine, tmp_path):
    """output.excel / output.json: the files are written and recorded on the run."""
    from leadscraper.engine.runner import config_from_snapshot
    from leadscraper.services.runs import run_row

    run_id = wf.build(engine, "local_business")
    ws = default_workspace(engine)
    snapshot = run_row(engine, ws, run_id)["config_snapshot"]
    config = config_from_snapshot({**snapshot, "output": {"excel": True, "json": True},
                                   "runtime": {"csv_export": str(tmp_path / "x.csv")}})
    written = exports.write_run_outputs(engine, run_id, config)
    assert [Path(p).name for p in written] == ["sample-dentists-and-caterers-run1.xlsx",
                                               "sample-dentists-and-caterers-run1.json"]
    assert all(Path(p).parent == tmp_path for p in written)
    files = run_row(engine, ws, run_id)["files"]
    assert [(f["kind"], f["rows"]) for f in files] == [("xlsx", 10), ("json", 10)]
    assert load_workbook(written[0]).sheetnames == ["Overview", "Records"]
    off = config_from_snapshot({**snapshot, "output": {"excel": False}})
    assert exports.write_run_outputs(engine, run_id, off) == []


def test_lead_types_keep_one_order_whatever_the_document_says(engine):
    """Postgres stores snapshots as JSONB (no key order): the schema imposes the registry's."""
    from leadscraper.config.schema import RunConfig

    wf.register_demo_lead_types()
    doc = {"schema_version": 1, "name": "x", "project": "x",
           "geography": {"country": "US", "places": ["Austin"]},
           "lead_types": {"demo_funding": {}, "demo_hiring": {},
                          "local_business": {"categories": [{"name": "Dentists"}]}}}
    order = RunConfig.model_validate(doc).enabled_lead_types()
    assert order == ["local_business", "demo_hiring", "demo_funding"]


# --- fixes from the Phase 4 review --------------------------------------------------------------

def test_control_characters_in_scraped_text_never_break_the_workbook(engine):
    """openpyxl refuses \\x0b, \\x0c, \\x00 …: they are dropped, and the export still works."""
    from leadscraper.db.store import LeadStore

    run_id = wf.build(engine, "empty")
    store = LeadStore(engine, project="Sample telescope makers", country="IN")
    store.run_id = run_id
    store.upsert(Lead(business_name="Star\x0bGazer Optics", locality="Nagpur",
                      address="Plot 4, Ring Road", notes=["seen\x0bon a map", "x" * 40000],
                      website="https://stargazer.example/" + "a" * 2100, sources=["google_maps"]))
    store.close()
    content = exports.export(engine, default_workspace(engine), "xlsx", run_id=run_id)[1]
    records = load_workbook(io.BytesIO(content))["Records"]
    col = {c.value: c.column for c in records[1]}
    assert records.cell(2, col["Name"]).value == "Star Gazer Optics"    # the store cleans names
    notes = records.cell(2, col["Notes"]).value                          # … but not notes
    assert notes.startswith("seenon a map; xxx") and len(notes) == 32767  # Excel's cell limit
    assert records.cell(2, col["Website"]).hyperlink is None             # too long to link


def test_the_same_table_gives_the_same_bytes(engine, monkeypatch):
    """Zip entries carry generated_at, not the wall clock at save time."""
    import time

    run_id = wf.build(engine, "venues")
    ws = default_workspace(engine)
    first = exports.export(engine, ws, "xlsx", run_id=run_id, generated_at=wf.FIXED_NOW)[1]
    later = time.localtime(time.time() + 3600)
    monkeypatch.setattr(time, "localtime", lambda *args: later)       # an hour on
    second = exports.export(engine, ws, "xlsx", run_id=run_id, generated_at=wf.FIXED_NOW)[1]
    assert first == second


def test_export_only_scores_with_the_current_config(engine):
    """A weights change needs only --export-only: the workbook follows the live config."""
    from leadscraper.engine.runner import config_from_snapshot
    from leadscraper.services.runs import run_row

    run_id = wf.build(engine, "local_business")
    ws = default_workspace(engine)
    snapshot = run_row(engine, ws, run_id)["config_snapshot"]
    project_id = run_row(engine, ws, run_id)["project_id"]
    stored = exports.build_table(engine, ws, project_id=project_id)
    live = config_from_snapshot({**snapshot, "scoring": {"weights": {"fit": 0, "intent": 0,
                                                                      "reach": 1}}})
    changed = exports.build_table(engine, ws, project_id=project_id, config=live)
    from leadscraper.export.summary import run_summary

    assert ("Score weights", "Fit 0% · Intent 0% · Reach 100%") in run_summary(changed)
    by_id = {r.id: r.score for r in stored.records}
    assert any(abs(r.score - by_id[r.id]) > 1 for r in changed.records)
    assert all(r.score == pytest.approx(100 * r.overall.reach, abs=0.01) for r in changed.records)


def test_one_failing_format_does_not_stop_the_others(engine, tmp_path, monkeypatch):
    run_id = wf.build(engine, "local_business")
    table = exports.build_table(engine, default_workspace(engine), run_id=run_id)

    def broken(_table):
        raise RuntimeError("disk full")

    monkeypatch.setitem(exports.RENDERERS, "xlsx", broken)
    written = exports.write_files(table, ["xlsx", "json"], tmp_path)
    assert [fmt for fmt, _ in written] == ["json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "sample-dentists-and-caterers-run1.json"]                       # no .partial left


def test_an_unknown_timezone_falls_back_to_utc_everywhere(engine):
    run_id = wf.build(engine, "local_business")
    patch_snapshot(engine, run_id, geography={"country": "IN", "places": ["Pune", "Mumbai"],
                                              "center": [18.52, 73.85],
                                              "admin_area": "Maharashtra",
                                              "timezone": "Mars/Olympus_Mons"})
    table = exports.build_table(engine, default_workspace(engine), run_id=run_id)
    assert table.timezone == "UTC"


def test_the_filtered_view_follows_records_and_reads_right_without_calculation(engine):
    """Live SUBTOTAL formulas over Records, each stored with its unfiltered result."""
    _, content, table = build(engine, "mix")
    book = load_workbook(io.BytesIO(content))
    computed = load_workbook(io.BytesIO(content), data_only=True)["Overview"]
    overview = book["Overview"]
    formulas = [(c.coordinate, c.value) for ws in book.worksheets for row in ws.iter_rows()
                for c in row if c.data_type == "f"]
    # The only formulas in the workbook: the filtered-view tiles (no scraped text, ever).
    assert {coord[1:] for coord, _ in formulas} == {"9"}
    assert all(ws_title == "Overview" for ws_title in
               {ws.title for ws in book.worksheets for row in ws.iter_rows() for c in row
                if c.data_type == "f"})
    labels = {overview.cell(8, c).value: overview.cell(9, c) for c in range(1, 6)}
    assert labels["SHOWING (OF 7)"].value == "=SUBTOTAL(103,Records!$B$2:$B$8)"
    assert "SUBTOTAL(103,OFFSET(Records!" in labels["WITH PHONE"].value
    results = {label: computed[cell.coordinate].value for label, cell in labels.items()}
    phones = sum(1 for r in table.records if r.lead.phones)
    signals = sum(1 for r in table.records if r.values.get("_signals"))
    assert results == {"SHOWING (OF 7)": 7, "AVG SCORE": pytest.approx(
        sum(r.score for r in table.records) / 7, abs=1e-6), "WITH PHONE": phones,
        "WITH EMAIL": sum(1 for r in table.records if r.lead.emails), "WITH SIGNAL": signals}
    assert book.calculation.fullCalcOnLoad
