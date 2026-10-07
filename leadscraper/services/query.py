"""Pagination and sorting shared by every list operation.

List endpoints all take ``limit`` / ``offset`` and a ``sort`` string: field
names separated by commas, ``-`` in front for descending (``-score,name``).
Only the fields a list declares are sortable, so a sort can never reach an
arbitrary column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from .errors import Invalid

T = TypeVar("T")
MAX_LIMIT = 500


@dataclass
class Page(Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"items": self.items, "total": self.total, "limit": self.limit,
                "offset": self.offset, **self.extra}


def clamp(limit: int | None, offset: int | None) -> tuple[int, int]:
    limit = 50 if limit is None else int(limit)
    offset = 0 if offset is None else int(offset)
    if limit < 1 or limit > MAX_LIMIT:
        raise Invalid(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise Invalid("offset must be 0 or more")
    return limit, offset


def order_by(sort: str | None, allowed: dict[str, Any], default: str) -> list[Any]:
    """ORDER BY clauses for `sort` ("-score,name"), restricted to `allowed` columns."""
    clauses = []
    for part in [p.strip() for p in (sort or default).split(",") if p.strip()]:
        desc = part.startswith("-")
        name = part.lstrip("-+")
        column = allowed.get(name)
        if column is None:
            raise Invalid(f"cannot sort by {name!r}; sortable: {', '.join(sorted(allowed))}")
        clauses.append(column.desc() if desc else column.asc())
    return clauses


def fetch_page(conn: Connection, query, *, limit: int, offset: int) -> tuple[list[dict], int]:
    """Rows of one page, and the total without paging."""
    total = conn.execute(select(func.count()).select_from(query.order_by(None).subquery())
                         ).scalar_one()
    rows = conn.execute(query.limit(limit).offset(offset)).mappings().all()
    return [dict(r) for r in rows], int(total)


def aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
