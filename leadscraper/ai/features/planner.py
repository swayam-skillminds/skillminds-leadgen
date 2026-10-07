"""``planner``: widen a config before it runs -- returned as suggestions, never applied.

* places -> sub-areas (neighbourhoods, districts, industrial belts);
* categories -> search-phrase synonyms ("Dentists" -> "dental clinic", "orthodontist");
* roles -> title variants ("CTO" -> "Chief Technology Officer", "Head of Technology");
* which packs and lead types fit -- checked against what really exists and
  applies to the country, so a suggestion can always be acted on.

The answer is a revised config document (validated) plus a list of changes;
the user confirms by saving it. Areas are only proposed into the document for
a single-place config: an area belongs to one place, and a run's areas apply
to all of them.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, Field

SYSTEM = (
    "You help plan a local-business lead search. For each place, list sub-areas worth "
    "searching separately (neighbourhoods, districts, business or industrial areas) -- real "
    "ones only, at most 8 per place. For each category, list up to 4 extra search phrases "
    "people would type to find such a business locally (synonyms, not broader categories). "
    "For each role, list up to 4 title variants. Suggest lead types only from the list "
    "given. Never invent places you are unsure exist. Return ONLY JSON matching the schema."
)


class PlaceAreas(BaseModel):
    place: str
    areas: list[str] = Field(default_factory=list)


class CategoryQueries(BaseModel):
    category: str
    queries: list[str] = Field(default_factory=list)


class RoleTitles(BaseModel):
    role: str
    titles: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    areas: list[PlaceAreas] = Field(default_factory=list)
    category_queries: list[CategoryQueries] = Field(default_factory=list)
    role_titles: list[RoleTitles] = Field(default_factory=list)
    lead_types: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def applicable(country: str, verticals: list[str]) -> dict[str, Any]:
    """Packs and lead types that exist and apply to this country (the catalog's truth)."""
    from ...leadtypes import registry
    from ...packs import all_packs

    packs = [{"key": p.key, "title": p.title,
              "applies": p.inactive_reason(country, verticals) is None,
              "why_not": p.inactive_reason(country, verticals)} for p in all_packs()]
    lead_types = [{"key": lt.key, "title": lt.title} for lt in registry.all_lead_types()
                  if lt.applicable_countries is None or country in lt.applicable_countries]
    return {"packs": packs, "lead_types": lead_types}


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def plan(client, document: dict[str, Any], *, roles: list[str] | None = None) -> dict[str, Any]:
    """``{config, valid, errors, changes, suggestions, applicable, source}``."""
    from pydantic import ValidationError

    from ...config.schema import RunConfig

    base = RunConfig.model_validate(document)          # the caller validated; re-check
    lb = base.lead_type("local_business")
    country = base.geography.country
    verticals = list(lb.verticals) if lb is not None else []
    facts = applicable(country, verticals)
    categories = [c.name for c in lb.categories] if lb is not None else []

    suggestion: Plan | None = None
    if client is not None and client.uses("planner"):
        user = (f"Country: {country}\nPlaces: {', '.join(base.geography.places)}\n"
                f"Categories: {', '.join(categories) or 'none'}\n"
                f"Roles: {', '.join(roles or []) or 'none'}\n"
                f"Lead types available: {', '.join(t['key'] for t in facts['lead_types'])}")
        result = client.call("planner", system=SYSTEM, user=user, output=Plan, max_tokens=3000)
        suggestion = result.data if result else None
    if suggestion is None:
        return {"config": base.to_document(), "valid": True, "errors": [], "changes": [],
                "suggestions": None, "applicable": facts, "source": "none",
                "note": "no AI answer: nothing suggested"}

    doc = copy.deepcopy(base.to_document())
    changes: list[str] = []
    lb_doc = doc.get("lead_types", {}).get("local_business")
    if lb_doc is not None:
        for item in suggestion.category_queries:
            target = next((c for c in lb_doc.get("categories", [])
                           if _norm(c.get("name")) == _norm(item.category)), None)
            if target is None:
                continue
            existing = [_norm(q) for q in target.get("queries") or [target["name"]]]
            added = [q.strip() for q in item.queries
                     if q and q.strip() and _norm(q) not in existing][:4]
            if added:
                target["queries"] = [*(target.get("queries") or [target["name"]]), *added]
                changes.append(f"category {target['name']}: + queries {', '.join(added)}")
    places = base.geography.places
    if len(places) == 1 and not base.geography.areas:
        mine = next((a for a in suggestion.areas if _norm(a.place) == _norm(places[0])), None)
        names = [a.strip() for a in (mine.areas if mine else []) if a and a.strip()][:8]
        if names:
            doc["geography"]["areas"] = [{"name": n} for n in names]
            changes.append(f"{places[0]}: search {len(names)} areas separately "
                           f"({', '.join(names)})")
    known_types = {t["key"] for t in facts["lead_types"]}
    suggested_types = [t for t in suggestion.lead_types if t in known_types]
    try:
        valid = RunConfig.model_validate(doc)
        errors: list[str] = []
    except ValidationError as exc:
        valid, errors = None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
    return {"config": valid.to_document() if valid is not None else base.to_document(),
            "valid": valid is not None, "errors": errors, "changes": changes,
            "suggestions": {"areas": [a.model_dump() for a in suggestion.areas],
                            "category_queries": [c.model_dump()
                                                 for c in suggestion.category_queries],
                            "role_titles": [r.model_dump() for r in suggestion.role_titles],
                            "lead_types": suggested_types, "notes": suggestion.notes},
            "applicable": facts, "source": "ai",
            "confirm": "Suggestions only: review the changes, then save the config."}
