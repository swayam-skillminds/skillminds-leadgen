"""Suppression lists (Phase 3.1): organisations to leave out of, or flag in, the output.

A list holds names, domains and phones -- existing customers, competitors,
erasure requests -- and has a ``mode``:

* ``exclude`` -- matching organisations are dropped from exports (the run's
  CSV/Sheet and every API/CLI export);
* ``flag`` -- they stay, marked ``Suppressed`` with the list's name.

A run config attaches lists by name (``filters.suppression_lists``). Matching
is by canonical name (``core.normalize.name_key``), registrable domain
(``core.identity.identity_domain``; a shared host never matches) and E.164
phone, normalised with the run's country.

Lists import from CSV or XLSX: a column headed name / domain / website /
phone (any case) is read as that kind; a file without such a header is
classified value by value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from ..core.identity import identity_domain
from ..core.normalize import name_key, normalize_phone
from ..db.models import SuppressionEntry, SuppressionList
from .errors import Conflict, Invalid, NotFound
from .query import Page, aware, clamp, fetch_page, order_by, utcnow
from .workspace import Workspace

LISTS = SuppressionList.__table__
ENTRIES = SuppressionEntry.__table__
KINDS = ("name", "domain", "phone")
MODES = ("exclude", "flag")
ERASURE_LIST = "Erasure requests"

_HEADERS = {"name": "name", "business": "name", "business name": "name", "company": "name",
            "organisation": "name", "organization": "name", "domain": "domain",
            "website": "domain", "url": "domain", "site": "domain", "phone": "phone",
            "phones": "phone", "telephone": "phone", "mobile": "phone", "phone number": "phone"}


# --- values --------------------------------------------------------------------------------

def normalize_value(kind: str, value: str, region: str | None = None) -> str | None:
    """The comparable form of a value, or None when it cannot match anything."""
    value = str(value or "").strip()
    if not value:
        return None
    if kind == "name":
        return name_key(value) or None
    if kind == "domain":
        url = value if "://" in value else f"https://{value.lstrip('@')}"
        return identity_domain(url)
    if kind == "phone":
        return normalize_phone(value, region) or normalize_phone(value)
    raise Invalid(f"unknown suppression kind {kind!r} ({', '.join(KINDS)})")


def guess_kind(value: str) -> str:
    text = str(value).strip()
    if re.fullmatch(r"[+()\d][\d\s().-]{6,}", text):
        return "phone"
    if "://" in text or re.fullmatch(r"(www\.)?[\w-]+(\.[\w-]+)+/?\S*", text, re.I):
        return "domain"
    return "name"


# --- lists ---------------------------------------------------------------------------------

def _shape(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": int(row["id"]), "name": row["name"], "mode": row["mode"],
            "entries": int(row.get("entries") or 0), "created_at": aware(row["created_at"])}


def _base(ws: Workspace):
    count = (select(func.count()).select_from(ENTRIES)
             .where(ENTRIES.c.list_id == LISTS.c.id).scalar_subquery().label("entries"))
    return select(LISTS, count).where(LISTS.c.workspace_id == ws.id)


def list_lists(engine: Engine, ws: Workspace, *, q: str | None = None, sort: str | None = None,
               limit: int | None = None, offset: int | None = None) -> Page[dict[str, Any]]:
    limit, offset = clamp(limit, offset)
    query = _base(ws)
    if q:
        query = query.where(LISTS.c.name.ilike(f"%{q}%"))
    query = query.order_by(*order_by(sort, {"name": LISTS.c.name, "id": LISTS.c.id,
                                            "created_at": LISTS.c.created_at}, "name"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    return Page([_shape(r) for r in rows], total, limit, offset)


def get_list(engine: Engine, ws: Workspace, list_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(_base(ws).where(LISTS.c.id == list_id)).mappings().first()
    if row is None:
        raise NotFound(f"no suppression list {list_id}")
    return _shape(dict(row))


def find_list(engine: Engine, ws: Workspace, name: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(_base(ws).where(LISTS.c.name == name)).mappings().first()
    return _shape(dict(row)) if row else None


def create_list(engine: Engine, ws: Workspace, *, name: str, mode: str = "exclude",
                entries: Iterable[dict[str, str]] = ()) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise Invalid("a suppression list needs a name")
    if mode not in MODES:
        raise Invalid(f"mode must be one of {', '.join(MODES)}")
    if find_list(engine, ws, name) is not None:
        raise Conflict(f"a suppression list named {name!r} already exists")
    with engine.begin() as conn:
        list_id = int(conn.execute(LISTS.insert().values(
            workspace_id=ws.id, name=name, mode=mode, created_at=utcnow())).inserted_primary_key[0])
    entries = list(entries)
    if entries:
        add_entries(engine, ws, list_id, entries)
    return get_list(engine, ws, list_id)


def ensure_list(engine: Engine, ws: Workspace, name: str, mode: str = "exclude") -> dict[str, Any]:
    return find_list(engine, ws, name) or create_list(engine, ws, name=name, mode=mode)


def update_list(engine: Engine, ws: Workspace, list_id: int, *, name: str | None = None,
                mode: str | None = None) -> dict[str, Any]:
    current = get_list(engine, ws, list_id)
    values: dict[str, Any] = {}
    if name is not None and name.strip() != current["name"]:
        if find_list(engine, ws, name.strip()) is not None:
            raise Conflict(f"a suppression list named {name!r} already exists")
        values["name"] = name.strip()
    if mode is not None:
        if mode not in MODES:
            raise Invalid(f"mode must be one of {', '.join(MODES)}")
        values["mode"] = mode
    if values:
        with engine.begin() as conn:
            conn.execute(update(LISTS).where(LISTS.c.id == list_id).values(**values))
    return get_list(engine, ws, list_id)


def delete_list(engine: Engine, ws: Workspace, list_id: int) -> dict[str, Any]:
    shaped = get_list(engine, ws, list_id)
    with engine.begin() as conn:
        conn.execute(delete(LISTS).where(LISTS.c.id == list_id))
    return shaped


# --- entries -------------------------------------------------------------------------------

def list_entries(engine: Engine, ws: Workspace, list_id: int, *, kind: str | None = None,
                 q: str | None = None, limit: int | None = None,
                 offset: int | None = None) -> Page[dict[str, Any]]:
    get_list(engine, ws, list_id)
    limit, offset = clamp(limit, offset)
    query = select(ENTRIES.c.id, ENTRIES.c.kind, ENTRIES.c.value, ENTRIES.c.created_at).where(
        ENTRIES.c.list_id == list_id)
    if kind:
        query = query.where(ENTRIES.c.kind == kind)
    if q:
        query = query.where(ENTRIES.c.value.ilike(f"%{q}%"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query.order_by(ENTRIES.c.id), limit=limit, offset=offset)
    return Page([{"id": int(r["id"]), "kind": r["kind"], "value": r["value"],
                  "created_at": aware(r["created_at"])} for r in rows], total, limit, offset)


def all_entries(engine: Engine, list_id: int) -> list[dict[str, str]]:
    with engine.connect() as conn:
        return [{"kind": r.kind, "value": r.value} for r in conn.execute(
            select(ENTRIES.c.kind, ENTRIES.c.value).where(ENTRIES.c.list_id == list_id)
            .order_by(ENTRIES.c.id))]


def add_entries(engine: Engine, ws: Workspace, list_id: int,
                entries: Iterable[dict[str, str]]) -> dict[str, int]:
    """Add {kind, value} entries; duplicates and values that match nothing are skipped."""
    get_list(engine, ws, list_id)
    with engine.connect() as conn:
        existing = {(r.kind, r.value) for r in conn.execute(
            select(ENTRIES.c.kind, ENTRIES.c.value).where(ENTRIES.c.list_id == list_id))}
    added, skipped, rows = 0, 0, []
    now = utcnow()
    for entry in entries:
        value = str(entry.get("value") or "").strip()
        kind = (entry.get("kind") or guess_kind(value)).lower()
        if kind not in KINDS:
            raise Invalid(f"unknown suppression kind {kind!r} ({', '.join(KINDS)})")
        if normalize_value(kind, value) is None or (kind, value) in existing:
            skipped += 1
            continue
        existing.add((kind, value))
        rows.append({"workspace_id": ws.id, "list_id": list_id, "kind": kind,
                     "value": value[:500], "created_at": now})
        added += 1
    if rows:
        with engine.begin() as conn:
            conn.execute(ENTRIES.insert(), rows)
    return {"added": added, "skipped": skipped}


def delete_entry(engine: Engine, ws: Workspace, list_id: int, entry_id: int) -> None:
    get_list(engine, ws, list_id)
    with engine.begin() as conn:
        if conn.execute(delete(ENTRIES).where(ENTRIES.c.id == entry_id,
                                              ENTRIES.c.list_id == list_id)).rowcount != 1:
            raise NotFound(f"no entry {entry_id} in suppression list {list_id}")


def parse_file(data: bytes, filename: str) -> list[dict[str, str]]:
    """Entries from an uploaded CSV or XLSX (see the module docstring)."""
    from ..core.tables import TableError, read_table

    try:
        table = read_table(data, filename)
    except TableError as exc:
        raise Invalid(str(exc)) from None
    if not table:
        return []
    header = [_HEADERS.get(cell.strip().lower()) for cell in table[0]]
    entries: list[dict[str, str]] = []
    if any(header):
        for row in table[1:]:
            for kind, cell in zip(header, row):
                if kind and cell.strip():
                    entries.append({"kind": kind, "value": cell.strip()})
        return entries
    for row in table:
        for cell in row:
            if cell.strip():
                entries.append({"kind": guess_kind(cell), "value": cell.strip()})
    return entries


def import_file(engine: Engine, ws: Workspace, list_id: int, data: bytes,
                filename: str) -> dict[str, int]:
    entries = parse_file(data, filename)
    result = add_entries(engine, ws, list_id, entries)
    return {**result, "read": len(entries)}


# --- matching ------------------------------------------------------------------------------

@dataclass
class Match:
    list_id: int
    list_name: str
    mode: str
    kind: str
    value: str


@dataclass
class Matcher:
    """Tests organisations against a set of lists."""

    region: str | None = None
    #: (kind, normalised value) -> matches
    index: dict[tuple[str, str], list[Match]] = field(default_factory=dict)

    @classmethod
    def load(cls, engine: Engine, workspace_id: int, *, list_ids: Iterable[int] = (),
             names: Iterable[str] = (), region: str | None = None) -> "Matcher":
        list_ids, names = list(list_ids), list(names)
        matcher = cls(region=region)
        if not list_ids and not names:
            return matcher
        query = (select(LISTS.c.id, LISTS.c.name, LISTS.c.mode, ENTRIES.c.kind, ENTRIES.c.value)
                 .join(ENTRIES, ENTRIES.c.list_id == LISTS.c.id)
                 .where(LISTS.c.workspace_id == workspace_id))
        if list_ids and names:
            query = query.where(LISTS.c.id.in_(list_ids) | LISTS.c.name.in_(names))
        elif list_ids:
            query = query.where(LISTS.c.id.in_(list_ids))
        else:
            query = query.where(LISTS.c.name.in_(names))
        with engine.connect() as conn:
            rows = conn.execute(query).all()
        for row in rows:
            key = normalize_value(row.kind, row.value, region)
            if key:
                matcher.index.setdefault((row.kind, key), []).append(
                    Match(int(row.id), row.name, row.mode, row.kind, row.value))
        return matcher

    @classmethod
    def for_names(cls, engine: Engine, workspace_id: int, names: Iterable[str],
                  region: str | None = None) -> "Matcher":
        return cls.load(engine, workspace_id, names=names, region=region)

    def __bool__(self) -> bool:
        return bool(self.index)

    def matches(self, lead: Any) -> list[Match]:
        if not self.index:
            return []
        found: list[Match] = []
        keys = [("name", name_key(getattr(lead, "business_name", "") or ""))]
        domain = identity_domain(getattr(lead, "website", None))
        if domain:
            keys.append(("domain", domain))
        for phone in getattr(lead, "phones", None) or []:
            keys.append(("phone", normalize_phone(phone, self.region) or phone))
        for key in keys:
            if key[1]:
                found.extend(self.index.get(key, []))
        return found

    def excluded(self, lead: Any) -> bool:
        return any(m.mode == "exclude" for m in self.matches(lead))

    def flags(self, lead: Any) -> list[str]:
        """Names of the "flag" lists the lead is on."""
        return sorted({m.list_name for m in self.matches(lead) if m.mode == "flag"})
