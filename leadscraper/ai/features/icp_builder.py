"""``icp_builder``: a plain-language brief -> a draft run config, for confirmation.

"We sell ERP to mid-size manufacturers in Pune and Nashik" becomes a schema-valid
run config: geography, ``local_business`` categories with search phrases,
fields, filters and the ``icp`` section, plus notes on what the brief asked for
that this build can't do yet (lead types of later phases, roles and signals
the decision-maker and signal lead types will use).

The draft is **returned, never run**. It starts from one of Skillminds'
offerings (``ai/offerings.yaml``) when the brief names one or the caller picks
one; without a usable AI provider, the offering alone makes the draft.

The model answers in a fixed shape (:class:`ICPDraft`); the draft is then
validated as a ``RunConfig``. A draft that fails validation is sent back once
with the errors, then the problems are returned as warnings with a draft built
from what was valid.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

OFFERINGS_FILE = Path(__file__).resolve().parent.parent / "offerings.yaml"


@lru_cache(maxsize=1)
def offerings() -> dict[str, dict[str, Any]]:
    with open(OFFERINGS_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class CategoryDraft(BaseModel):
    name: str = Field(..., description="A category of business to find, e.g. 'Dentists'.")
    queries: list[str] = Field(default_factory=list,
                               description="2-4 search phrases that find it locally.")


class ICPDraft(BaseModel):
    name: str = Field(..., description="A short name for the run.")
    country: str = Field(..., description="ISO 3166-1 alpha-2 code, e.g. IN, US, AE.")
    places: list[str] = Field(..., description="Cities or regions to search.")
    categories: list[CategoryDraft] = Field(..., description="1-8 business categories.")
    roles: list[str] = Field(default_factory=list,
                             description="Decision-maker titles to look for.")
    signals: list[str] = Field(default_factory=list,
                               description="Buying signals that make a lead hot.")
    industries: list[str] = Field(default_factory=list)
    company_size: str | None = Field(None, description="e.g. '11-50', '51-200', or null.")
    technologies: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list,
                                 description="Any of: phone, email, website.")
    lead_types: list[str] = Field(default_factory=list,
                                  description="Lead types that fit (see the list given).")
    notes: list[str] = Field(default_factory=list)


SYSTEM = (
    "You turn a sales team's description of who they want to sell to into a lead-search "
    "plan for a B2B lead generator. The team is Skillminds: custom software, web and app "
    "development, ERP, IT staff augmentation and training. Fill the JSON fields from the "
    "brief. Use only countries, cities and categories the brief implies; when it names no "
    "country, infer it from the cities. Categories are kinds of local businesses as they "
    "appear on maps and directories. Choose lead types only from the list given. Never "
    "write outreach messages, pitches or email copy. Return ONLY JSON matching the schema."
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "icp-draft"


def available_lead_types() -> list[str]:
    from ...leadtypes import registry

    return [lt.key for lt in registry.all_lead_types()]


def match_offering(brief: str) -> str | None:
    """The offering a brief names, by key or by words of its title."""
    text = (brief or "").lower()
    hints = {"erp": ("erp",), "staff_augmentation": ("staff aug", "augmentation", "contract dev"),
             "training": ("training", "upskill"),
             "web_app_dev": ("website", "web app", "mobile app", "app develop", "web develop"),
             "custom_software": ("custom software", "bespoke", "automation")}
    for key, words in hints.items():
        if any(word in text for word in words):
            return key
    return None


def draft_from_offering(key: str, *, country: str | None = None,
                        places: list[str] | None = None) -> ICPDraft:
    offer = offerings()[key]
    return ICPDraft(
        name=offer["title"], country=(country or "IN").upper(), places=places or [],
        categories=[CategoryDraft(name=c[:1].upper() + c[1:]) for c in offer["categories"]],
        roles=list(offer.get("roles") or []), signals=list(offer.get("signals") or []),
        industries=list(offer.get("industries") or []),
        must_have=list(offer.get("must_have") or []),
        lead_types=list(offer.get("lead_types") or []), notes=[])


def to_document(draft: ICPDraft, *, offering: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """A run-config document from a draft, and notes on what it leaves out."""
    notes: list[str] = list(draft.notes)
    known = set(available_lead_types())
    wanted = [lt for lt in draft.lead_types if lt]
    later = [lt for lt in wanted if lt not in known]
    if later:
        notes.append("lead types not in this build yet (later phases): " + ", ".join(later))
    must_have = [m for m in draft.must_have if m in ("phone", "email", "website")]
    categories = []
    for category in draft.categories[:8]:
        item: dict[str, Any] = {"name": category.name.strip()[:100]}
        queries = [q.strip() for q in category.queries if q and q.strip()][:4]
        if queries:
            item["queries"] = queries
        categories.append(item)
    if draft.roles:
        notes.append("decision-maker roles to look for: " + ", ".join(draft.roles[:8]))
    if draft.signals:
        notes.append("buying signals: " + ", ".join(draft.signals[:8]))
    description = "Drafted by the ICP builder" + (f" from the '{offering}' offering"
                                                    if offering else "") + "."
    doc: dict[str, Any] = {
        "schema_version": 1,
        "name": draft.name.strip()[:120] or "ICP draft",
        "project": _slug(draft.name),
        "description": description,
        "geography": {"country": draft.country.strip().upper(),
                      "places": [p.strip() for p in draft.places if p and p.strip()]},
        "lead_types": {"local_business": {"enabled": True, "categories": categories}},
        "filters": {"must_have": must_have},
        "icp": {"industries": draft.industries[:10], "company_size": draft.company_size,
                "technologies": draft.technologies[:10]},
        "ai": {"enabled": False},
    }
    return doc, notes


def _validate(doc: dict[str, Any]) -> tuple[Any | None, list[str]]:
    from ...config.schema import RunConfig

    try:
        return RunConfig.model_validate(doc), []
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def build(client, brief: str, *, offering: str | None = None, country: str | None = None,
          places: list[str] | None = None) -> dict[str, Any]:
    """The draft: ``{config, valid, errors, notes, offering, source, ai}``. Never runs it."""
    offering = offering or match_offering(brief)
    if offering is not None and offering not in offerings():
        raise ValueError(f"unknown offering {offering!r} (known: {', '.join(offerings())})")
    base = draft_from_offering(offering, country=country, places=places) if offering else None
    lead_types = ", ".join(available_lead_types()) + " (built); later phases: digital_audit, " \
        "hiring, funding_news, decision_makers, tenders, registrations, tech_stack, app_store"
    user = f"Brief: {brief.strip()}\n\nLead types: {lead_types}\n"
    if base is not None:
        user += ("\nStart from this offering and adapt it to the brief:\n"
                 + base.model_dump_json(exclude={"notes"}))
    if country:
        user += f"\nCountry: {country}"
    if places:
        user += f"\nPlaces: {', '.join(places)}"

    draft, source, errors = None, "preset", []
    if client is not None and client.uses("icp_builder"):
        for _ in range(2):
            result = client.call("icp_builder", system=SYSTEM, user=user, output=ICPDraft,
                                 tier="judge", max_tokens=4000)
            if result is None:
                break
            doc, _notes = to_document(result.data, offering=offering)
            valid, errors = _validate(doc)
            draft, source = result.data, "ai"
            if valid is not None:
                break
            user += ("\n\nYour previous draft failed validation: " + "; ".join(errors[:6])
                     + ". Fix those fields.")
    if draft is None:
        if base is None:
            raise LookupError("no AI provider answered and the brief names no offering; "
                              "pick one of: " + ", ".join(offerings()))
        draft = base
        if places:
            draft.places = places
    doc, notes = to_document(draft, offering=offering)
    valid, errors = _validate(doc)
    if source == "preset":
        notes.insert(0, "built from the offering preset (no AI answer); review the places "
                        "and categories")
    return {"config": valid.to_document() if valid is not None else doc,
            "valid": valid is not None, "errors": errors,
            "notes": notes, "offering": offering, "source": source,
            "confirm": "This is a draft. Review it, save it as a config, then run it."}
