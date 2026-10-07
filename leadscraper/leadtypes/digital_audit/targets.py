"""Uploaded audit targets: a list of businesses or domains, inline or as CSV / XLSX.

With a header row, columns are matched by name (name, website, phone,
category, place and common synonyms). Without one, each cell that looks like
a domain or URL is a website and anything else a business name.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...core.normalize import domain_of, normalize_url
from ...core.tables import read_table
from .config import AuditTarget, DigitalAuditConfig

HEADERS = {
    "name": "name", "business": "name", "business name": "name", "company": "name",
    "company name": "name", "organisation": "name", "organization": "name",
    "website": "website", "site": "website", "url": "website", "domain": "website",
    "web": "website", "homepage": "website",
    "phone": "phone", "telephone": "phone", "mobile": "phone", "contact number": "phone",
    "category": "category", "type": "category", "industry": "category",
    "place": "place", "city": "place", "town": "place", "locality": "place", "area": "place",
}
_DOMAINISH = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/:?#].*)?$", re.I)


def looks_like_site(value: str) -> bool:
    value = value.strip()
    return bool(_DOMAINISH.match(value)) and " " not in value


def parse_targets(data: bytes, filename: str) -> tuple[list[AuditTarget], int]:
    """(targets, rows skipped) from an uploaded table. Raises ``TableError`` if unreadable."""
    table = read_table(data, filename)
    if not table:
        return [], 0
    header = [HEADERS.get(cell.strip().lower()) for cell in table[0]]
    targets: list[AuditTarget] = []
    skipped = 0
    if any(header):
        rows = [{kind: cell.strip() for kind, cell in zip(header, row) if kind and cell.strip()}
                for row in table[1:]]
    else:
        rows = [{"website": cell.strip()} if looks_like_site(cell) else {"name": cell.strip()}
                for row in table for cell in row if cell.strip()]
    for values in rows:
        target = _target(values)
        if target is None:
            skipped += 1
        else:
            targets.append(target)
    return targets, skipped


def _target(values: dict[str, str]) -> AuditTarget | None:
    website = values.get("website")
    if website and not normalize_url(website):
        website = None
    if not (values.get("name") or website):
        return None
    return AuditTarget(name=values.get("name", ""), website=website,
                       phone=values.get("phone"), category=values.get("category", ""),
                       place=values.get("place", ""))


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
