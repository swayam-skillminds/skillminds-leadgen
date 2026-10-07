"""``normalisation``: industry tags, a company-size estimate, duplicate adjudication.

(Name clean-up, the fourth part, is the original ``names`` task in
``ai.assistant``.)

* **Industry** -- 1-3 short tags per organisation, from its name, category,
  description and site. Stored as an AI attribute (``industry``).
* **Company size** -- one band (``1-10`` ... ``1000+``) or nothing; stored as
  an AI attribute with ``estimated: true``, and always shown as an estimate.
* **Duplicate adjudication** (judge tier) -- pairs the identity rules leave
  apart but that look alike (same locality, names 80-91 % similar, phones not
  contradicting) go to the judge model; a confident "same business" (>= 0.85)
  merges them, the lower id surviving. When unsure, the judge is told to say
  no: a wrong merge destroys data, a missed one only leaves a duplicate.

Organisations that already carry an attribute are not asked again.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Union

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SIZE_BANDS = ("1-10", "11-50", "51-200", "201-1000", "1000+")

SYSTEM = (
    "You tag businesses for a B2B lead list. For each input record return its id, "
    "`industry`: 1-3 short industry tags (e.g. 'dental care', 'catering', 'metal "
    "fabrication'), and `company_size`: your estimate of its headcount band, exactly one "
    f"of {', '.join(SIZE_BANDS)}, or null when the record gives no basis for a guess. "
    "Judge only from the given fields; never invent facts about the business. Return ONLY "
    "JSON: {\"items\": [...]}."
)

Id = Union[str, int]


class OrgFacts(BaseModel):
    id: Id
    industry: list[str] = Field(default_factory=list)
    company_size: str | None = None


class OrgFactsBatch(BaseModel):
    items: list[OrgFacts] = Field(default_factory=list)


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        tag = " ".join(str(tag or "").split()).strip(" .,;").lower()
        if 2 <= len(tag) <= 40 and tag not in out:
            out.append(tag)
    return out[:3]


def tag_organisations(client, store, *, batch_size: int = 12, limit: int = 500,
                      stop=lambda: False) -> int:
    """Industry tags and size estimates for organisations without them; returns updates."""
    wants_industry, wants_size = client.uses("industry"), client.uses("size")
    if not (wants_industry or wants_size):
        return 0
    todo = []
    for lead_id, lead in store.all_with_ids():
        attrs = store.attributes(lead_id)
        if (wants_industry and "industry" not in attrs) or (wants_size and
                                                            "company_size" not in attrs):
            todo.append((lead_id, lead))
        if len(todo) >= limit:
            break
    updates = 0
    for start in range(0, len(todo), batch_size):
        if stop() or not client.active:
            break
        chunk = todo[start:start + batch_size]
        payload = [{"id": str(lead_id), "name": lead.business_name, "category": lead.category,
                    "website": lead.website, "description": (lead.description or "")[:300],
                    "notes": [n for n in lead.notes if not n.startswith("AI")][:3]}
                   for lead_id, lead in chunk]
        result = client.call("industry", system=SYSTEM,
                             user=json.dumps(payload, ensure_ascii=False), output=OrgFactsBatch)
        if result is None:
            continue
        producer = f"{result.provider}/{result.model}"
        known = {str(lead_id) for lead_id, _ in chunk}
        for facts in result.data.items:
            if str(facts.id) not in known:
                continue
            lead_id = int(facts.id)
            tags = _clean_tags(facts.industry)
            if wants_industry and tags and store.set_attribute(
                    lead_id, "industry", tags, origin="ai", producer=producer):
                updates += 1
            size = (facts.company_size or "").strip()
            if wants_size and size in SIZE_BANDS and store.set_attribute(
                    lead_id, "company_size", size, origin="ai", estimated=True,
                    producer=producer):
                updates += 1
    return updates


# --- duplicate adjudication --------------------------------------------------------

def candidate_pairs(store, *, low: int = 80, high: int = 92, limit: int = 50) -> list[tuple]:
    """Look-alike pairs the identity rules kept apart: same locality, close names."""
    from rapidfuzz import fuzz

    from ...core.normalize import normalize_name

    by_locality: dict[str, list[tuple[int, Any]]] = {}
    for lead_id, lead in store.all_with_ids():
        if getattr(lead, "record_type", "") == "Brand":
            continue
        by_locality.setdefault(lead.locality or "", []).append((lead_id, lead))
    pairs = []
    for rows in by_locality.values():
        keys = [(lead_id, lead, normalize_name(lead.business_name)) for lead_id, lead in rows]
        for i, (a_id, a, a_key) in enumerate(keys):
            for b_id, b, b_key in keys[i + 1:]:
                if not a_key or not b_key:
                    continue
                score = fuzz.token_set_ratio(a_key, b_key)
                if not (low <= score < high):
                    continue
                if a.phones and b.phones and not set(a.phones) & set(b.phones):
                    continue                     # contradicting phones: rules already decided
                pairs.append((a_id, a, b_id, b, score))
                if len(pairs) >= limit:
                    return pairs
    return pairs


def _record(lead) -> dict[str, Any]:
    return {"name": lead.business_name, "category": lead.category, "locality": lead.locality,
            "address": lead.address, "phones": lead.phones[:3], "website": lead.website,
            "rating": lead.gmaps_rating}


def adjudicate_duplicates(assistant, store, *, min_confidence: float = 0.85,
                          limit: int = 50) -> int:
    """Ask the judge about look-alike pairs; merge the confident ones. Returns merges."""
    if not assistant.uses("dedup"):
        return 0
    pairs = candidate_pairs(store, limit=limit)
    if not pairs:
        return 0
    verdicts = assistant.judge_duplicates([
        {"id": f"{a_id}-{b_id}", "a": _record(a), "b": _record(b)}
        for a_id, a, b_id, b, _ in pairs])
    merged = 0
    for a_id, _a, b_id, _b, _ in pairs:
        verdict = verdicts.get(f"{a_id}-{b_id}")
        if not verdict or not verdict.get("same_business"):
            continue
        if float(verdict.get("confidence") or 0) < min_confidence:
            continue
        if store.get(a_id) is None or store.get(b_id) is None:
            continue                             # already folded by an earlier pair
        reason = str(verdict.get("reason") or "")[:120]
        if store.fold_pair(a_id, b_id, note=f"AI judge merged a duplicate: {reason}"):
            merged += 1
    if merged:
        log.info("AI duplicate adjudication: %d pair(s) merged", merged)
    return merged
