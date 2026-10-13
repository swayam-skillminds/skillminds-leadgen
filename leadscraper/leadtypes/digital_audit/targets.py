"""Uploaded audit targets: a list of businesses or domains, inline or as CSV / XLSX.

The table itself is read by ``core/uploads.py`` (shared with ``tech_stack``):
with a header row, columns are matched by name (name, website, phone, category,
place and common synonyms); without one, each cell that looks like a domain or
URL is a website and anything else a business name.
"""

from __future__ import annotations

from pathlib import Path

from ...core.normalize import domain_of
from ...core.uploads import HEADERS, looks_like_site, parse_rows  # noqa: F401
from .config import AuditTarget, DigitalAuditConfig


def parse_targets(data: bytes, filename: str) -> tuple[list[AuditTarget], int]:
    """(targets, rows skipped) from an uploaded table. Raises ``TableError`` if unreadable."""
    rows, skipped = parse_rows(data, filename)
    return [AuditTarget(name=values.get("name", ""), website=values.get("website"),
                        phone=values.get("phone"), category=values.get("category", ""),
                        place=values.get("place", "")) for values in rows], skipped


def load_targets(section: DigitalAuditConfig, base_dir: Path | None = None) -> list[AuditTarget]:
    """Every uploaded target of a config: ``targets``, then ``domains``, then ``targets_file``.

    Repeats (the same domain, or the same name with no site) are dropped.
    """
    out: list[AuditTarget] = list(section.targets)
    out.extend(AuditTarget(website=d.strip()) for d in section.domains if d.strip())
    if section.targets_file:
        path = Path(section.targets_file).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        rows, _ = parse_targets(path.read_bytes(), path.name)
        out.extend(rows)
    seen: set[str] = set()
    unique = []
    for target in out:
        key = (domain_of(target.website) if target.website else None) \
            or f"name:{target.name.strip().casefold()}"
        if key not in seen:
            seen.add(key)
            unique.append(target)
    return unique
