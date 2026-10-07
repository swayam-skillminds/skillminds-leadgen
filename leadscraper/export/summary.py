"""The Overview's content, as data (Phase 4.2).

``summarise(table)`` answers what the Overview tab shows -- run summary,
results, data quality, the column legend, and for an empty export why it is
empty -- as plain values, so the workbook and the JSON export carry the same
numbers. Everything is computed from the exported records and the run's
facts (``table.context``, read from the database), never from live formulas:
the numbers are the same in every viewer, deterministic, and testable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Callable

from .records import SCORE_BANDS, ExportRecord, score_band
from .table import ExportTable

#: Rows a breakdown shows before folding the rest into "Other".
BREAKDOWN_ROWS = 12
TOP_N = 10

STATUS_TEXT = {"completed": "completed", "failed": "failed", "cancelled": "cancelled",
               "paused": "paused (resumable)", "running": "still running",
               "queued": "queued", "pausing": "pausing", "stopping": "stopping"}


def _minutes(seconds: float | None) -> str:
    if not seconds:
        return ""
    minutes = float(seconds) / 60
    if minutes < 90:
        return f"{minutes:.0f} min" if minutes >= 1 else f"{float(seconds):.0f} s"
    return f"{minutes / 60:.1f} h"


def _has(value: Any) -> bool:
    return value not in (None, "", [], {})


def _any_contact(r: ExportRecord) -> bool:
    lead = r.lead
    return any(_has(v) for v in (lead.phones, lead.emails, lead.website, lead.instagram,
                                 lead.facebook, lead.whatsapp, lead.linkedin,
                                 lead.booking_contact))


def _column_filled(table: ExportTable, field: str) -> Callable[[ExportRecord], bool] | None:
    """A test for "some lead-type column of this field has a value", if any column has it."""
    columns = [c for c in table.columns if c.group != "core" and c.field == field]
    if not columns:
        return None
    return lambda r: any(_has(c.value(r)) for c in columns)


def coverage(table: ExportTable) -> list[dict[str, Any]]:
    """Field coverage: how many records have each kind of fact."""
    fields = table.context.get("fields")

    def wanted(field: str) -> bool:
        return fields is None or field in fields

    checks: list[tuple[str, Callable[[ExportRecord], bool], str]] = [
        ("Any contact", _any_contact, "A phone, email, website, social profile or booking line."),
    ]
    if wanted("phones"):
        checks.append(("Phone", lambda r: bool(r.lead.phones), "Published numbers only."))
    if wanted("emails"):
        checks.append(("Email", lambda r: bool(r.lead.emails),
                       "Published addresses only; inferred ones are counted separately."))
    if wanted("website"):
        checks.append(("Website", lambda r: bool(r.lead.website),
                       "The organisation's own site (directory and platform pages excluded)."))
    if wanted("address"):
        checks.append(("Address", lambda r: bool(r.lead.address), ""))
    if wanted("socials"):
        checks.append(("Social profile", lambda r: any(_has(v) for v in (
            r.lead.instagram, r.lead.facebook, r.lead.linkedin, r.lead.whatsapp)),
            "Instagram, Facebook, LinkedIn or WhatsApp."))
    checks.append(("Google Maps listing", lambda r: bool(r.lead.gmaps_url), ""))
    if wanted("decision_makers"):
        checks.append(("Decision-maker", lambda r: bool(r.people or r.lead.contact_name),
                       "A named person (and title) from a public page."))
    if wanted("emails") and any(r.inferred for r in table.records):
        checks.append(("Inferred email", lambda r: bool(r.inferred),
                       "Unverified pattern guesses, never mixed with published emails."))
    for field, label in (("ratings", "Rating"), ("prices", "Price")):
        if wanted(field):
            test = _column_filled(table, field)
            if test is not None:
                checks.append((label, test, ""))
    if table.context.get("signal_types"):
        checks.append(("Signal", lambda r: bool(r.values.get("_signals")),
                       "At least one unexpired buying signal."))
    n = len(table.records)
    out = []
    for label, test, note in checks:
        filled = sum(1 for r in table.records if test(r))
        out.append({"field": label, "filled": filled,
                    "coverage": round(filled / n, 4) if n else None, "note": note})
    return out


def _row(label: str, group: list[ExportRecord], total: int) -> dict[str, Any]:
    scores = [r.score for r in group]
    return {"label": label, "records": len(group),
            "share": round(len(group) / total, 4) if total else 0.0,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "with_phone": sum(1 for r in group if r.lead.phones)}


def _breakdown(title: str, records: list[ExportRecord], label_of: Callable[[ExportRecord], Any],
               order: list[str] | None = None) -> dict[str, Any]:
    """Records counted by a label (a list label counts the record under each item)."""
    groups: dict[str, list[ExportRecord]] = defaultdict(list)
    for record in records:
        label = label_of(record)
        items = label if isinstance(label, (list, tuple)) else [label]
        for item in [i for i in items if _has(i)] or ["(none)"]:
            groups[str(item)].append(record)
    if order:
        rank = {v: i for i, v in enumerate(order)}
        labels = sorted(groups, key=lambda k: (rank.get(k, len(rank)), -len(groups[k]), k))
    else:
        labels = sorted(groups, key=lambda k: (k == "(none)", -len(groups[k]), k.casefold()))
    rows, other = [], []
    for i, label in enumerate(labels):
        if order is None and i >= BREAKDOWN_ROWS:
            other.append(label)
        else:
            rows.append(_row(label, groups[label], len(records)))
    if other:
        folded = {id(r): r for label in other for r in groups[label]}
        rows.append(_row(f"Other ({len(other)} more)", list(folded.values()), len(records)))
    return {"title": title, "rows": rows}


def breakdowns(table: ExportTable) -> list[dict[str, Any]]:
    records = table.records
    if not records:
        return []
    ctx = table.context
    titles = dict(ctx.get("lead_types") or [])
    out = []
    if len({k for r in records for k in r.lead_types}) > 1 or len(titles) > 1:
        out.append(_breakdown("By lead type", records,
                              lambda r: [titles.get(k, k) for k in r.lead_types]))
    for column in table.columns:
        if column.breakdown:
            order = [v for v, _ in column.palette] if column.palette else None
            out.append(_breakdown(f"By {column.header.lower()}", records, column.value,
                                  order=order))
    type_titles = ctx.get("signal_types") or {}
    if type_titles:
        out.append(_breakdown("By signal type", records, lambda r: sorted(
            {type_titles.get((s.get("lead_type"), s.get("type")), s.get("type"))
             for s in r.values.get("_signals", [])}) or ["(no signal)"]))
    out.append(_breakdown("By score band", records, lambda r: score_band(r.score),
                          order=list(SCORE_BANDS)))
    return out


def top(table: ExportTable, n: int = TOP_N) -> list[dict[str, Any]]:
    """The best-scored records, whatever order Records is sorted in."""
    ranked = sorted(table.records,
                    key=lambda r: (-r.score, (r.lead.business_name or "").casefold(), r.id))[:n]
    what = next((c for c in table.columns
                 if c.breakdown and c.key not in ("place", "locality")), None)
    phone = next((c for c in table.columns if c.key == "primary_phone"), None)
    out = []
    for i, record in enumerate(ranked, start=1):
        signals = record.values.get("_signals") or []
        where = [record.lead.locality or ""]
        if record.place and record.place != record.lead.locality:
            where.append(record.place)
        out.append({
            "rank": i, "id": record.id, "name": record.lead.business_name,
            "score": record.score, "where": ", ".join(w for w in where if w),
            "what_header": what.header if what is not None else None,
            "what": what.value(record) if what is not None else None,
            "phone": phone.value(record) if phone is not None else None,
            "website": record.lead.website,
            "signal": signals[0].get("title") if signals else None,
        })
    return out


def totals(table: ExportTable) -> dict[str, Any]:
    records = table.records
    changes = Counter(r.change for r in records if r.change)
    scores = [r.score for r in records]
    return {
        "records": len(records),
        "new": changes.get("new", 0), "updated": changes.get("updated", 0),
        "unchanged": changes.get("unchanged", 0),
        "excluded_by_suppression": table.excluded,
        "left_out_by_filters": int(table.context.get("filtered") or 0),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "with_phone": sum(1 for r in records if r.lead.phones),
        "with_email": sum(1 for r in records if r.lead.emails),
        "with_website": sum(1 for r in records if r.lead.website),
        "with_signal": sum(1 for r in records if r.values.get("_signals")),
    }


def run_summary(table: ExportTable) -> list[tuple[str, str]]:
    ctx, run = table.context, table.run or {}
    rows: list[tuple[str, str]] = [("Project", table.project.get("name") or "")]
    if ctx.get("config_name"):
        rows.append(("Config", ctx["config_name"]))
    if run:
        kind = "" if (run.get("kind") or "run") == "run" else f" ({run['kind']})"
        status = STATUS_TEXT.get(run.get("status") or "", run.get("status") or "")
        rows.append(("Run", f"#{run['id']}{kind} · {status}"))
        started = ctx.get("started_at")
        if started is not None:
            rows.append(("Date", table.local(started).strftime("%d %b %Y, %H:%M")
                         + f" ({table.timezone})"))
        if ctx.get("duration_seconds"):
            rows.append(("Duration", _minutes(ctx["duration_seconds"])))
    places = ctx.get("places") or []
    if places:
        areas = ctx.get("areas") or []
        rows.append(("Places", ", ".join(places) + (f" · {len(areas)} areas" if areas else "")))
    if ctx.get("country"):
        rows.append(("Country", ctx["country"]))
    if ctx.get("lead_types"):
        rows.append(("Lead types", ", ".join(title for _, title in ctx["lead_types"])))
    sources = ctx.get("sources") or []
    if sources:
        used = [s for s in sources if s.get("done") or s.get("found")]
        rows.append(("Sources used", ", ".join(
            f"{s['source']} ({s['found']} found)" if s.get("found") else s["source"]
            for s in used) or "none finished a unit"))
    units = ctx.get("units") or {}
    if units.get("total"):
        skipped = f", {units['skipped']} skipped" if units.get("skipped") else ""
        failed = f", {units['failed']} failed" if units.get("failed") else ""
        rows.append(("Work units", f"{units.get('done', 0)} of {units['total']} done"
                     f"{failed}{skipped}"))
    if ctx.get("depth"):
        budget = ctx.get("time_budget_minutes")
        rows.append(("Depth", ctx["depth"] + (f" · {budget}-minute budget" if budget else "")))
    ai = ctx.get("ai")
    if ai is not None:
        if ai.get("enabled"):
            text = "on"
            if ai.get("provider"):
                text += f" · {ai['provider']}"
            text += f" · ${float(ai.get('spend_usd') or 0):.2f} spent"
            if ai.get("cap_usd"):
                text += f" of ${float(ai['cap_usd']):.2f}"
            if ai.get("calls"):
                text += f" ({ai['calls']} calls)"
            rows.append(("AI", text))
        else:
            rows.append(("AI", "off"))
    weights = ctx.get("weights")
    if weights:
        total = sum(weights.values()) or 1.0
        rows.append(("Score weights", " · ".join(
            f"{k.capitalize()} {round(100 * v / total)}%" for k, v in weights.items())))
    scope = {"run": "organisations this run found",
             "new": (f"only organisations new since run #{table.against}" if table.against
                     else "only new organisations"),
             "project": "the whole project"}.get(table.scope, table.scope)
    rows.append(("This workbook", scope))
    if table.excluded:
        rows.append(("Suppressed", f"{table.excluded} left out by suppression lists"))
    if ctx.get("filters"):
        rows.append(("Filters", f"{ctx['filters']} · {ctx.get('filtered', 0)} left out"))
    rows.append(("Sorted by", table.sort_label))
    rows.append(("Generated", table.local(table.generated_at).strftime("%d %b %Y, %H:%M")
                 + f" ({table.timezone}), from the project database"))
    return rows


def empty_reasons(table: ExportTable) -> list[str]:
    """Why an export has no records, as far as the run's own record says."""
    if table.records:
        return []
    ctx, run = table.context, table.run or {}
    reasons = []
    found = ctx.get("run_found")
    if table.scope == "new" and found:
        reasons.append(f"Nothing new: this run found {found} organisations, all of them already "
                       f"in the project before run #{table.against}.")
    if table.excluded:
        reasons.append(f"{table.excluded} organisations matched suppression lists and were "
                       "left out.")
    if ctx.get("filtered"):
        reasons.append(f"{ctx['filtered']} organisations failed the run's filters "
                       f"({ctx.get('filters')}).")
    status = run.get("status")
    if status and status != "completed":
        error = (run.get("error") or "").strip()
        reasons.append(f"The run ended as {STATUS_TEXT.get(status, status)}"
                       + (f": {error[:300]}" if error else "."))
    units = ctx.get("units") or {}
    if units.get("total"):
        if not units.get("done"):
            reasons.append(f"None of its {units['total']} work units finished.")
        elif not found:
            reasons.append(f"{units['done']} of its {units['total']} work units finished "
                           "without finding an organisation.")
        if units.get("skipped"):
            reasons.append(f"{units['skipped']} work units were skipped "
                           f"({units.get('skip_reason') or 'time budget or stop'}).")
    failures = ctx.get("failures") or []
    if failures:
        listed = ", ".join(f"{f['reason']} ×{f['count']} ({f['source']})" for f in failures[:5])
        reasons.append(f"Most frequent failures: {listed}. "
                       f"GET /runs/{run.get('id', '{id}')}/failures lists every one.")
    if not reasons:
        reasons.append("The sources returned no organisations for these places and categories. "
                       "Try more categories or areas, a deeper depth, or other sources.")
    return reasons


def legend(table: ExportTable) -> list[tuple[str, str]]:
    """The non-obvious columns, in Records order (including how scores work)."""
    return [(c.header, c.description) for c in table.columns if c.description]


def summarise(table: ExportTable) -> dict[str, Any]:
    return {
        "run_summary": run_summary(table),
        "totals": totals(table),
        "breakdowns": breakdowns(table),
        "coverage": coverage(table),
        "top": top(table),
        "legend": legend(table),
        "empty_reasons": empty_reasons(table),
    }
