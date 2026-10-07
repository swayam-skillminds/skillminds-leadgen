"""Organisations: list, filter, sort, inspect, erase (Phase 3.1).

The list reads the indexed columns of ``organizations`` (name, locality,
category, scores) and filters on contacts through ``contacts`` (so "has an
email" works the same on SQLite and Postgres). The detail view returns the
merged record with every fact's provenance: contacts with their source,
people, signals, evidence rows (source, URL, fields, when), identity keys and
the runs that touched it.

Erasure (data-subject deletion requests) deletes every organisation holding a
domain or phone, in every project of the workspace, with everything hanging
off it; by default the value also goes on the "Erasure requests" suppression
list, which every export applies, so it never reappears in an export even if
a later run finds it again. Failure rows naming the business go too.

Not reached by erasure (documented in docs/api.md): files already written to
disk or to a Google Sheet before the request (a project's CSV, skipped.csv,
failure logs, earlier exports) -- regenerate or delete those; the next run
or ``export`` writes them without the erased organisation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.engine import Engine

from ..core.identity import identity_domain
from ..core.normalize import normalize_phone
from ..db.models import (
    Contact, Evidence, FailureRow, IdentityKey, Organization, OrgAttribute, Person, Run,
    RunOrganization,
    Signal,
)
from .errors import Invalid, NotFound
from .query import Page, aware, clamp, fetch_page, order_by
from .workspace import Workspace

ORGS = Organization.__table__
CONTACTS = Contact.__table__
PEOPLE = Person.__table__
SIGNALS = Signal.__table__
ATTRS = OrgAttribute.__table__
EVIDENCE = Evidence.__table__
KEYS = IdentityKey.__table__
MEMBERS = RunOrganization.__table__
RUNS = Run.__table__
FAILURES = FailureRow.__table__

SORTABLE = {"score": ORGS.c.score, "name": ORGS.c.name, "locality": ORGS.c.locality,
            "category": ORGS.c.category, "created_at": ORGS.c.created_at,
            "updated_at": ORGS.c.updated_at, "fit": ORGS.c.fit, "reach": ORGS.c.reach,
            "intent": ORGS.c.intent, "id": ORGS.c.id}
HAS = {"phone": ("phone",), "email": ("email",), "website": ("website",),
       "socials": ("instagram", "facebook", "linkedin", "whatsapp"), "address": ("address",)}


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    record = row.get("record") or {}
    return {
        "id": int(row["id"]), "project_id": int(row["project_id"]), "name": row["name"],
        "locality": row["locality"] or None, "category": row["category"] or None,
        "country": row["country"] or None, "domain": row.get("domain"),
        "lead_types": list(row.get("lead_types") or []),
        "score": row["score"], "fit": row.get("fit"), "intent": row.get("intent"),
        "reach": row.get("reach"), "score_reasons": list(row.get("score_reasons") or []),
        "phones": list(record.get("phones") or []), "emails": list(record.get("emails") or []),
        "website": record.get("website"), "gmaps_url": record.get("gmaps_url"),
        "sources": list(record.get("sources") or []),
        "latitude": row.get("latitude"), "longitude": row.get("longitude"),
        "created_at": aware(row["created_at"]), "updated_at": aware(row.get("updated_at")),
    }


def list_organizations(engine: Engine, ws: Workspace, *, project_id: int | None = None,
                       run_id: int | None = None, q: str | None = None,
                       locality: str | None = None, category: str | None = None,
                       lead_type: str | None = None, has: list[str] | None = None,
                       min_score: float | None = None, max_score: float | None = None,
                       domain: str | None = None, change: str | None = None,
                       sort: str | None = None, limit: int | None = None,
                       offset: int | None = None) -> Page[dict[str, Any]]:
    """Organisations of a project (or of one run), filtered and sorted.

    ``change`` with ``run_id`` keeps only what that run did to them:
    new / updated / unchanged (its ``run_organizations`` action).
    """
    if project_id is None and run_id is None:
        raise Invalid("give a project_id or a run_id")
    limit, offset = clamp(limit, offset)
    query = select(ORGS).where(ORGS.c.workspace_id == ws.id)
    if project_id is not None:
        query = query.where(ORGS.c.project_id == project_id)
    if run_id is not None:
        member = select(MEMBERS.c.organization_id).where(MEMBERS.c.run_id == run_id)
        if change:
            if change not in ("new", "updated", "unchanged"):
                raise Invalid("change must be new, updated or unchanged")
            member = member.where(MEMBERS.c.action == change)
        query = query.where(ORGS.c.id.in_(member))
    if q:
        query = query.where(ORGS.c.name.ilike(f"%{q}%"))
    if locality:
        query = query.where(ORGS.c.locality == locality)
    if category:
        query = query.where(ORGS.c.category == category)
    if domain:
        query = query.where(ORGS.c.domain == (identity_domain(
            domain if "://" in domain else f"https://{domain}") or domain))
    if min_score is not None:
        query = query.where(ORGS.c.score >= float(min_score))
    if max_score is not None:
        query = query.where(ORGS.c.score <= float(max_score))
    for wanted in has or []:
        channels = HAS.get(wanted)
        if channels is None:
            raise Invalid(f"unknown 'has' filter {wanted!r}; known: {', '.join(HAS)}")
        query = query.where(exists().where(CONTACTS.c.organization_id == ORGS.c.id,
                                           CONTACTS.c.channel.in_(channels)))
    if lead_type:
        # lead_types is a JSON list: filter through the evidence rows each lead
        # type writes, which needs no backend-specific JSON operator.
        query = query.where(ORGS.c.id.in_(select(EVIDENCE.c.organization_id).where(
            EVIDENCE.c.lead_type == lead_type)))
    query = query.order_by(*order_by(sort, SORTABLE, "-score,name"), ORGS.c.id)
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    return Page([_summary(r) for r in rows], total, limit, offset)


def get_organization(engine: Engine, ws: Workspace, org_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(ORGS).where(ORGS.c.id == org_id,
                                              ORGS.c.workspace_id == ws.id)).mappings().first()
        if row is None:
            raise NotFound(f"no organization {org_id}")
        contacts = conn.execute(select(CONTACTS).where(CONTACTS.c.organization_id == org_id)
                                .order_by(CONTACTS.c.id)).mappings().all()
        people = conn.execute(select(PEOPLE).where(PEOPLE.c.organization_id == org_id)
                              .order_by(PEOPLE.c.id)).mappings().all()
        signals = conn.execute(select(SIGNALS).where(SIGNALS.c.organization_id == org_id)
                               .order_by(SIGNALS.c.observed_at.desc())).mappings().all()
        evidence = conn.execute(select(EVIDENCE).where(EVIDENCE.c.organization_id == org_id)
                                .order_by(EVIDENCE.c.id).limit(500)).mappings().all()
        attributes = conn.execute(select(ATTRS).where(ATTRS.c.organization_id == org_id)
                                  .order_by(ATTRS.c.key)).mappings().all()
        keys = conn.execute(select(KEYS.c.kind, KEYS.c.value).where(
            KEYS.c.organization_id == org_id).order_by(KEYS.c.id)).all()
        runs = conn.execute(select(MEMBERS.c.run_id, MEMBERS.c.action, MEMBERS.c.lead_type,
                                   RUNS.c.created_at)
                            .join(RUNS, RUNS.c.id == MEMBERS.c.run_id)
                            .where(MEMBERS.c.organization_id == org_id)
                            .order_by(MEMBERS.c.run_id)).mappings().all()
    out = _summary(dict(row))
    out.update({
        "record": dict(row["record"] or {}),
        "contacts": [{"channel": c["channel"], "value": c["value"], "kind": c["kind"],
                      "source": c["source"], "confidence": c["confidence"],
                      "first_seen": aware(c["created_at"])} for c in contacts],
        "people": [{"name": p["name"], "title": p["title"], "profile_url": p["profile_url"],
                    "source": p["source"], "first_seen": aware(p["created_at"])} for p in people],
        "signals": [{"type": s["type"], "lead_type": s["lead_type"], "title": s["title"],
                     "payload": s["payload"] or {}, "strength": s["strength"],
                     "observed_at": aware(s["observed_at"]),
                     "published_at": aware(s["published_at"]),
                     "expires_at": aware(s["expires_at"]), "source_url": s["source_url"]}
                    for s in signals],
        "evidence": [{"source": e["source"], "url": e["url"], "fields": list(e["fields"] or []),
                      "lead_type": e["lead_type"] or None, "run_id": e["run_id"],
                      "fetched_at": aware(e["fetched_at"])} for e in evidence],
        "identity_keys": [{"kind": k, "value": v} for k, v in keys],
        # Not published facts: AI tags, estimates and summaries, always labelled.
        "attributes": [{"key": a["key"], "value": a["value"], "origin": a["origin"],
                        "estimated": bool(a["estimated"]), "producer": a["producer"],
                        "updated_at": aware(a["updated_at"])} for a in attributes],
        "runs": [{"run_id": r["run_id"], "action": r["action"],
                  "lead_type": r["lead_type"] or None, "at": aware(r["created_at"])}
                 for r in runs],
    })
    return out


def erase(engine: Engine, ws: Workspace, *, domain: str | None = None, phone: str | None = None,
          project_id: int | None = None, dry_run: bool = False,
          suppress: bool = True, region: str | None = None) -> dict[str, Any]:
    """Delete every organisation holding `domain` or `phone` (data-subject requests)."""
    if not domain and not phone:
        raise Invalid("give a domain or a phone to erase")
    owners = []
    values: list[dict[str, str]] = []
    if domain:
        key = identity_domain(domain if "://" in domain else f"https://{domain}")
        if key is None:
            raise Invalid(f"{domain!r} is a shared host (social network, free email, "
                          "directory); it identifies nobody, so nothing can be erased by it")
        owners.append(select(KEYS.c.organization_id).where(
            and_(KEYS.c.kind == "domain", KEYS.c.value == key)))
        owners.append(select(CONTACTS.c.organization_id).where(
            CONTACTS.c.channel == "email", CONTACTS.c.value.ilike(f"%@{key}")))
        values.append({"kind": "domain", "value": key})
    if phone:
        e164 = normalize_phone(phone, region) or normalize_phone(phone)
        if e164 is None:
            raise Invalid(f"{phone!r} is not a valid phone number (give it with +country code)")
        owners.append(select(KEYS.c.organization_id).where(
            and_(KEYS.c.kind == "phone", KEYS.c.value == e164)))
        values.append({"kind": "phone", "value": e164})
    query = select(ORGS.c.id, ORGS.c.project_id, ORGS.c.name).where(
        ORGS.c.workspace_id == ws.id, or_(*[ORGS.c.id.in_(o) for o in owners]))
    if project_id is not None:
        query = query.where(ORGS.c.project_id == project_id)
    with engine.connect() as conn:
        found = [{"id": int(r.id), "project_id": int(r.project_id), "name": r.name}
                 for r in conn.execute(query.order_by(ORGS.c.id))]
    failures_removed = 0
    if not dry_run and found:
        with engine.begin() as conn:
            conn.execute(delete(ORGS).where(ORGS.c.id.in_([f["id"] for f in found])))
            # Failure rows that name the business (a page that could not be read).
            for item in found:
                failures_removed += conn.execute(delete(FAILURES).where(
                    FAILURES.c.project_id == item["project_id"],
                    FAILURES.c.business == item["name"])).rowcount or 0
    suppressed = None
    if suppress and not dry_run:
        from .suppression import ERASURE_LIST, add_entries, ensure_list

        listing = ensure_list(engine, ws, ERASURE_LIST, "exclude")
        add_entries(engine, ws, listing["id"], values)
        suppressed = listing["name"]
    return {"erased": found, "count": len(found), "dry_run": dry_run,
            "suppression_list": suppressed, "failures_removed": failures_removed}
