"""The Records column model, shared by the workbook, the CSV and the JSON export (Phase 4).

Columns are assembled per export from:

* the **core** columns every lead type shares: name, lead types, location,
  published contacts, the maps link, decision-makers;
* each lead type's own ``columns()`` (``LeadType.column_values`` fills them),
  placed by their ``placement``: "front" right after the name, "body" before
  the contacts, "contacts" after them, "tail" after the scores and signals;
* scores (overall, fit, intent, reach, reasons), top signals and provenance
  (sources, first/last seen, the run's change, suppression flags);

keeping only the fields the run selected (``fields.include``). Optional
columns -- socials, signals, decision-makers, inferred contacts, notes, and
every lead-type column -- are left out when no record has a value, so a run
that never looks for signals has no empty signal columns. With no records at
all, every column that could apply stays, so the headers still explain the
layout.

Inferred contacts (Phase 6's email pattern) have their own labelled column
and never appear in the published-contact columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime
from typing import Any, Callable

from ..core.normalize import phone_display
from .records import ExportRecord, signal_date

Getter = Callable[[ExportRecord], Any]

#: Default Excel widths and number formats by column kind.
KIND_WIDTH = {"text": 18, "number": 10, "url": 24, "phone": 18, "email": 28, "date": 15,
              "score": 8, "flag": 9, "money": 12}
KIND_FORMAT = {"score": "0", "number": "0.##", "date": "dd-mmm-yy hh:mm", "phone": "@",
               "money": "#,##0"}


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    kind: str = "text"
    #: "core", or the lead type that contributed it.
    group: str = "core"
    description: str = ""
    #: The ``fields.include`` entry that switches it on; None = always.
    field: str | None = None
    width: float | None = None
    number_format: str | None = None
    breakdown: bool = False
    palette: tuple[tuple[str, str], ...] = ()
    link_label: str | None = None
    #: Dropped from an export where no record has a value.
    optional: bool = False
    #: Bold in the workbook (the name).
    strong: bool = False
    get: Getter | None = dataclass_field(default=None, compare=False, repr=False)

    def value(self, record: ExportRecord) -> Any:
        if self.get is not None:
            return self.get(record)
        return record.values.get(self.key)

    @property
    def excel_width(self) -> float:
        return float(self.width or KIND_WIDTH.get(self.kind, 18))

    @property
    def excel_format(self) -> str | None:
        return self.number_format or KIND_FORMAT.get(self.kind)


# --- core value getters ------------------------------------------------------------------------

def _joined(values: list[str], sep: str = " | ") -> str | None:
    return sep.join(v for v in values if v) or None


def _phones(record: ExportRecord) -> list[str]:
    return [phone_display(p) for p in record.lead.phones]


def _instagram(record: ExportRecord) -> str | None:
    handle = record.lead.instagram
    return f"https://instagram.com/{handle}" if handle else None


def _people(record: ExportRecord) -> str | None:
    names = [f"{p['name']} ({p['title']})" if p.get("title") else p["name"]
             for p in record.people if p.get("name")]
    if not names and record.lead.contact_name:
        names = [record.lead.contact_name]
    return _joined(names, "; ")


def _person_link(record: ExportRecord) -> str | None:
    return next((p["profile_url"] for p in record.people if p.get("profile_url")), None)


def _attribute(record: ExportRecord, key: str) -> str | None:
    item = (record.attributes or {}).get(key)
    if not item:
        return None
    value = item.get("value")
    if isinstance(value, list):
        return _joined([str(v) for v in value], ", ")
    return str(value) if value not in (None, "") else None


def _inferred_email(record: ExportRecord) -> str | None:
    return _joined([c["value"] for c in record.inferred if c.get("channel") == "email"])


def _pct(value: float | None) -> float | None:
    return None if value is None else round(100.0 * value, 1)


def _top_signals(record: ExportRecord) -> list[dict[str, Any]]:
    return record.values.get("_signals") or []


def _signal_text(signal: dict[str, Any]) -> str:
    label = signal.get("type_title") or signal.get("type") or ""
    title = signal.get("title") or ""
    return f"{label}: {title}" if title and label else (title or label)


def _top_signal(record: ExportRecord) -> str | None:
    signals = _top_signals(record)
    return _signal_text(signals[0]) if signals else None


def _top_signal_date(record: ExportRecord) -> datetime | None:
    signals = _top_signals(record)
    return signal_date(signals[0]) if signals else None


def _top_signal_link(record: ExportRecord) -> str | None:
    signals = _top_signals(record)
    return signals[0].get("source_url") if signals else None


def _other_signals(record: ExportRecord) -> str | None:
    signals = _top_signals(record)[1:]
    if not signals:
        return None
    shown = []
    for signal in signals[:3]:
        when = signal_date(signal)
        shown.append(_signal_text(signal) + (f" ({when:%Y-%m-%d})" if when else ""))
    more = len(signals) - len(shown)
    return "; ".join(shown) + (f"; +{more} more" if more > 0 else "")


CHANGE_TEXT = {"new": "New", "updated": "Updated", "unchanged": "Unchanged"}

SCORE_HELP = ("Overall rank, 0–100: the run's weights over Fit, Intent and Reach. A component "
              "no lead type in the run scores is left out and the other weights are scaled up; "
              "one that some lead type scores counts 0 for an organisation without it.")


def core_columns(*, places: int, titles: dict[str, str], has_run: bool) -> dict[str, list[Column]]:
    """The core columns, by the slot they fill. ``titles``: lead type key -> title."""
    lead = lambda attr: (lambda r: getattr(r.lead, attr) or None)  # noqa: E731
    return {
        "lead": [
            Column("score", "Score", "score", number_format="0.0", width=8,
                   description=SCORE_HELP, get=lambda r: r.score),
            Column("name", "Name", width=38, strong=True,
                   get=lambda r: r.lead.business_name or None),
        ],
        "location": [
            Column("lead_types", "Lead Types", width=18,
                   description="The lead types that found or enriched the organisation.",
                   get=lambda r: _joined([titles.get(k, k) for k in r.lead_types], ", ")),
            *([Column("place", "Place", width=16, breakdown=True,
                      description="Which of the run's places it is in.",
                      get=lambda r: r.place or None)] if places > 1 else []),
            Column("locality", "Locality", width=20, breakdown=places <= 1,
                   get=lead("locality")),
        ],
        "contacts": [
            Column("primary_phone", "Primary Phone", "phone", field="phones",
                   get=lambda r: (_phones(r) or [None])[0]),
            Column("other_phones", "Other Phones", "phone", field="phones", width=26,
                   optional=True, get=lambda r: _joined(_phones(r)[1:])),
            Column("primary_email", "Primary Email", "email", field="emails",
                   get=lambda r: (r.lead.emails or [None])[0]),
            Column("other_emails", "Other Emails", "email", field="emails", width=30,
                   optional=True, get=lambda r: _joined(r.lead.emails[1:])),
            Column("inferred_email", "Inferred Email (unverified)", "email", field="emails",
                   width=30, optional=True,
                   description="Guessed from the email pattern of published addresses at the "
                               "same domain (MX-checked only). Never published, never verified, "
                               "never used for matching.",
                   get=_inferred_email),
            Column("website", "Website", "url", field="website", width=26, get=lead("website")),
            Column("instagram", "Instagram", "url", field="socials", width=20, optional=True,
                   get=_instagram),
            Column("facebook", "Facebook", "url", field="socials", width=20, optional=True,
                   get=lead("facebook")),
            Column("linkedin", "LinkedIn", "url", field="socials", width=20, optional=True,
                   get=lead("linkedin")),
            Column("whatsapp", "WhatsApp", "url", field="socials", width=16, optional=True,
                   get=lead("whatsapp")),
            Column("maps", "Google Maps", "url", width=12, link_label="View map", optional=True,
                   get=lead("gmaps_url")),
        ],
        "address": [
            Column("address", "Address", field="address", width=44, get=lead("address")),
            Column("decision_makers", "Decision Makers", field="decision_makers", width=36,
                   optional=True,
                   description="People found on public pages: name (title).",
                   get=_people),
            Column("decision_maker_profile", "Decision Maker Profile", "url",
                   field="decision_makers", width=24, optional=True,
                   description="The first decision-maker's public profile (search-indexed only).",
                   get=_person_link),
        ],
        "scores": [
            Column("fit", "Fit", "score", width=7,
                   description="Is it the kind of organisation we sell to? 0–100.",
                   get=lambda r: _pct(r.overall.fit)),
            Column("intent", "Intent", "score", width=7,
                   description="Is there a reason to buy now (hiring, funding, tenders…)? "
                               "0–100: 0 when nothing was found, blank when no lead type in "
                               "the run looks for one.",
                   get=lambda r: _pct(r.overall.intent)),
            Column("reach", "Reach", "score", width=7,
                   description="Can we get hold of someone? 0–100, from the published "
                               "contact channels.",
                   get=lambda r: _pct(r.overall.reach)),
            Column("score_reasons", "Score Reasons", width=50,
                   description="Why it ranks where it does: each lead type's reasons, "
                               "strongest first.",
                   get=lambda r: _joined(r.overall.reasons, "; ")),
        ],
        "signals": [
            Column("top_signal", "Top Signal", width=40, optional=True,
                   description="The strongest unexpired signal (strength fades with age).",
                   get=_top_signal),
            Column("signal_date", "Signal Date", "date", number_format="dd-mmm-yy", width=11,
                   optional=True,
                   description="When the top signal happened. Green: within 30 days; "
                               "amber: within 90.",
                   get=_top_signal_date),
            Column("signal_link", "Signal Link", "url", width=14, link_label="Source",
                   optional=True, get=_top_signal_link),
            Column("other_signals", "Other Signals", width=44, optional=True,
                   get=_other_signals),
        ],
        # Phase 5: what AI wrote about an organisation -- never a published fact,
        # so each header says so. Optional: absent unless AI produced them.
        "ai": [
            Column("ai_summary", "Summary (AI)", width=50, optional=True,
                   description="One line by AI on what it does and why it matched. Not a "
                               "published fact.",
                   get=lambda r: _attribute(r, "summary")),
            Column("ai_industry", "Industry (AI)", width=24, optional=True,
                   description="Industry tags inferred by AI from its name, category and "
                               "site.",
                   get=lambda r: _attribute(r, "industry")),
            Column("ai_company_size", "Company Size (AI estimate)", width=14, optional=True,
                   breakdown=False,
                   description="Headcount band estimated by AI. An estimate, not a fact.",
                   get=lambda r: _attribute(r, "company_size")),
        ],
        "provenance": [
            Column("sources", "Sources", width=22,
                   description="Which sources found or enriched it.",
                   get=lambda r: _joined(r.lead.sources, ", ")),
            Column("source_count", "Src #", "number", number_format="0", width=6,
                   get=lambda r: len(r.lead.sources)),
            Column("notes", "Notes", width=46, optional=True,
                   get=lambda r: _joined(r.lead.notes, "; ")),
            *([Column("change", "Change", width=11, optional=True,
                      description="What this run did: New (first found), Updated (added "
                                  "something), Unchanged (found again, nothing to add).",
                      get=lambda r: CHANGE_TEXT.get(r.change or ""))] if has_run else []),
            Column("suppressed", "Suppressed", width=18, optional=True,
                   description="Suppression lists in flag mode it matched.",
                   get=lambda r: _joined(r.flags, ", ")),
            Column("first_seen", "First Seen", "date", width=15,
                   get=lambda r: r.first_seen),
            Column("last_seen", "Last Seen", "date", width=15,
                   description="The last run that found it (or changed it).",
                   get=lambda r: r.last_seen),
            Column("org_id", "Org ID", "number", number_format="0", width=8,
                   description="The organisation's id in the API (/organizations/{id}).",
                   get=lambda r: r.id),
        ],
    }


def lead_type_columns(lead_type: Any, cfg: Any) -> list[tuple[str, Column]]:
    """(placement, column) for a lead type's ``ColumnSpec``s; keys namespaced by the lead type."""
    out = []
    for spec in lead_type.columns(cfg):
        out.append((spec.placement, Column(
            key=f"{lead_type.key}.{spec.key}", header=spec.header, kind=spec.kind,
            group=lead_type.key, description=spec.description, field=spec.field,
            width=spec.width, number_format=spec.number_format, breakdown=spec.breakdown,
            palette=tuple(spec.palette), link_label=spec.link_label, optional=True)))
    return out


def assemble(records: list[ExportRecord], *, cfg: Any, lead_type_keys: list[str],
             fields: list[str] | None, places: int, has_run: bool,
             signal_specs: dict[tuple[str, str], Any], now: datetime) -> list[Column]:
    """The export's columns, with every record's lead-type values and top signals filled in."""
    from ..leadtypes import registry

    lead_types = [lt for lt in (registry.get(k) for k in lead_type_keys) if lt is not None]
    titles = {lt.key: lt.title for lt in registry.all_lead_types()}
    core = core_columns(places=places, titles=titles, has_run=has_run)

    slots: dict[str, list[Column]] = {"front": [], "body": [], "contacts": [], "tail": []}
    seen_headers = {c.header for group in core.values() for c in group}
    for lead_type in lead_types:
        try:
            columns = lead_type_columns(lead_type, cfg)
        except Exception:  # a lead type's bug costs its columns, not the export
            continue
        for placement, column in columns:
            if column.header in seen_headers:   # two lead types both say "Category"
                column = replace(column, header=f"{column.header} ({lead_type.title})")
            seen_headers.add(column.header)
            slots.get(placement, slots["body"]).append(column)

    for record in records:
        for lead_type in lead_types:
            if lead_type.key not in (record.lead_types or ["local_business"]):
                continue
            try:
                values = lead_type.column_values(record, cfg) or {}
            except Exception:
                values = {}
            for key, value in values.items():
                record.values[f"{lead_type.key}.{key}"] = value
        signals = record.effective_signals(now, signal_specs)
        for signal in signals:
            spec = signal_specs.get((signal.get("lead_type") or "", signal.get("type") or ""))
            signal["type_title"] = getattr(spec, "title", None) or signal.get("type")
        record.values["_signals"] = signals

    ordered = [*core["lead"], *slots["front"], *core["location"], *slots["body"],
               *core["contacts"], *slots["contacts"], *core["address"], *core["scores"],
               *core["signals"], *slots["tail"], *core["ai"], *core["provenance"]]
    wanted = None if fields is None else set(fields)
    ordered = [c for c in ordered if c.field is None or wanted is None or c.field in wanted]
    if records:
        return [c for c in ordered if not c.optional or any(_present(c.value(r)) for r in records)]
    # Nothing to show: keep what could apply, minus columns only data can justify.
    signals_possible = any(key for key, _ in signal_specs)
    return [c for c in ordered if c.key not in DATA_ONLY
            and (signals_possible or c.key not in SIGNAL_COLUMNS)]


#: Columns an empty export leaves out: they only exist to show data.
DATA_ONLY = frozenset({"ai_summary", "ai_industry", "ai_company_size",
                       "other_phones", "other_emails", "inferred_email", "suppressed",
                       "decision_maker_profile", "notes", "change", "signal_link",
                       "other_signals"})


SIGNAL_COLUMNS = frozenset({"top_signal", "signal_date", "signal_link", "other_signals"})


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != []
