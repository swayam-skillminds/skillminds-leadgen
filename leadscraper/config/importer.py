"""Legacy ``config.yaml`` files: their implicit defaults, and conversion to
run configs / presets.

Configs written before Phase 1 leaned on values the code supplied silently:
every run was in India, the city defaulted to Mumbai, the map centre to
Mumbai's, and the state name "Maharashtra" plus the words "mumbai" / "india"
were stripped from every business name. Those values now live here, applied
only to a file in the legacy format, so the code itself assumes no country
or city -- and a legacy run still behaves exactly as it did.
"""

from __future__ import annotations

import copy
from typing import Any

#: What the pre-Phase-1 code assumed when a legacy config said nothing.
LEGACY_COUNTRY = "IN"
LEGACY_CITY = "Mumbai"
LEGACY_CENTER = [19.0760, 72.8777]          # Mumbai
LEGACY_ADMIN_AREA = "Maharashtra"
#: Place words the old code stripped from every name and SERP title, whatever
#: the run's city ("india" and "maharashtra" now come from country/admin area).
LEGACY_PLACE_NOISE = ["mumbai"]


def is_legacy(data: dict[str, Any]) -> bool:
    """A config without ``schema_version`` predates the run-config schema."""
    return "schema_version" not in (data or {})


def apply_legacy_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of a legacy config with every implicit default made explicit."""
    out = copy.deepcopy(data or {})
    out.setdefault("country", LEGACY_COUNTRY)
    if not out.get("city"):
        out["city"] = LEGACY_CITY
    geo = out.setdefault("geo", {}) if isinstance(out.get("geo", {}), dict) else {}
    out["geo"] = geo
    if not geo.get("center"):
        geo["center"] = list(LEGACY_CENTER)
    geo.setdefault("admin_area", LEGACY_ADMIN_AREA)
    geo.setdefault("place_noise", list(LEGACY_PLACE_NOISE))
    return out
