"""tech_stack's uploaded sites: ``targets``, ``domains`` and a CSV/XLSX ``targets_file``.

The table is read by ``core/uploads.py``, the same reader digital_audit uses, so
the list ``POST /audit/targets`` returns fits here unchanged.
"""

from __future__ import annotations

from pathlib import Path

from ...core.lead import Lead
from ...core.normalize import domain_of, normalize_phone, normalize_url
from ...core.uploads import parse_rows
from .config import TechStackConfig, TechTarget

UPLOAD = "upload"


def load_targets(section: TechStackConfig, base_dir: Path | None = None) -> list[TechTarget]:
    """Every uploaded target: ``targets``, then ``domains``, then ``targets_file``; repeats
    (the same domain, or the same name with no site) dropped."""
    out: list[TechTarget] = list(section.targets)
    out.extend(TechTarget(website=d.strip()) for d in section.domains if d.strip())
    if section.targets_file:
        path = Path(section.targets_file).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        rows, _ = parse_rows(path.read_bytes(), path.name)
        out.extend(TechTarget(name=r.get("name", ""), website=r.get("website"),
                              phone=r.get("phone"), category=r.get("category", ""),
                              place=r.get("place", "")) for r in rows)
    seen: set[str] = set()
    unique = []
    for target in out:
        key = (domain_of(target.website) if target.website else None) \
            or f"name:{target.name.strip().casefold()}"
        if key not in seen:
            seen.add(key)
            unique.append(target)
    return unique


def target_lead(target: TechTarget, region: str | None) -> Lead:
    site = normalize_url(target.website) if target.website else None
    phone = normalize_phone(target.phone, region) if target.phone else None
    return Lead(business_name=(target.name or domain_of(site) or "").strip(), website=site,
                phones=[phone] if phone else [], category=target.category,
                locality=target.place, sources=[UPLOAD])
