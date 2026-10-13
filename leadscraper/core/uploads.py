"""An uploaded list of businesses or domains (CSV / XLSX), read into plain rows.

With a header row, columns are matched by name (name, website, phone, category,
place and common synonyms). Without one, each cell that looks like a domain or
URL is a website and anything else a business name. Lead types that take
uploads (``digital_audit``, ``tech_stack``) turn the rows into their own target
models; neither depends on the other.
"""

from __future__ import annotations

import re

from .normalize import normalize_url
from .tables import read_table

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


def parse_rows(data: bytes, filename: str) -> tuple[list[dict[str, str]], int]:
    """(rows, rows skipped) from an uploaded table; each row has some of name, website,
    phone, category, place. A row with neither a name nor a usable website is skipped.
    Raises ``TableError`` if the file is unreadable."""
    table = read_table(data, filename)
    if not table:
        return [], 0
    header = [HEADERS.get(cell.strip().lower()) for cell in table[0]]
    if any(header):
        rows = [{kind: cell.strip() for kind, cell in zip(header, row) if kind and cell.strip()}
                for row in table[1:]]
    else:
        rows = [{"website": cell.strip()} if looks_like_site(cell) else {"name": cell.strip()}
                for row in table for cell in row if cell.strip()]
    kept: list[dict[str, str]] = []
    skipped = 0
    for values in rows:
        website = values.get("website")
        if website and not normalize_url(website):
            values = {k: v for k, v in values.items() if k != "website"}
        if not (values.get("name") or values.get("website")):
            skipped += 1
        else:
            kept.append(values)
    return kept, skipped
