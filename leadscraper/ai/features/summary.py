"""``lead_summary``: one line on what the organisation does and why it matched.

Descriptive only (A.2.3): no outreach copy. A line that addresses anyone
("you", "we"), pitches, offers or calls to action is rejected, as is anything
over 200 characters. Stored as the AI attribute ``summary``.
"""

from __future__ import annotations

import json
import re
from typing import Union

from pydantic import BaseModel, Field

SYSTEM = (
    "You write one factual line (at most 25 words) per business for a B2B lead list: what "
    "the business does, and why it matches the search (its category, place, ratings or "
    "other facts given). Third person, plain description. Never address anyone, never "
    "pitch, never suggest contacting them, never write marketing or outreach copy. Use only "
    "the given fields. Return ONLY JSON: {\"items\": [{\"id\": ..., \"summary\": ...}]}."
)

#: Outreach, pitch or address: a summary that reads like copy is dropped.
_COPY = re.compile(
    r"\b(you|your|we|our|us|dear|reach out|contact (?:them|now)|call (?:them|now)|"
    r"email them|pitch|offer(?:ing)? them|discount|book (?:a|your) (?:demo|call)|"
    r"don't miss|act now|sign up)\b|!", re.I)

Id = Union[str, int]


class Summary(BaseModel):
    id: Id
    summary: str = Field(..., description="One line, third person, no outreach copy.")


class SummaryBatch(BaseModel):
    items: list[Summary] = Field(default_factory=list)


def acceptable(text: str) -> str | None:
    """The cleaned summary, or None when it is empty, too long or reads like copy."""
    text = " ".join((text or "").split()).strip()
    if not text or len(text) > 200 or _COPY.search(text):
        return None
    return text


def summarise(client, store, *, why: str = "", batch_size: int = 12, limit: int = 300,
              stop=lambda: False) -> int:
    """Summaries for organisations without one; returns how many were stored."""
    if not client.uses("summary"):
        return 0
    todo = [(lead_id, lead) for lead_id, lead in store.all_with_ids()
            if "summary" not in store.attributes(lead_id)][:limit]
    stored = 0
    for start in range(0, len(todo), batch_size):
        if stop() or not client.active:
            break
        chunk = todo[start:start + batch_size]
        payload = [{"id": str(lead_id), "name": lead.business_name, "category": lead.category,
                    "locality": lead.locality, "website": lead.website,
                    "rating": lead.gmaps_rating, "reviews": lead.gmaps_reviews,
                    "description": (lead.description or "")[:300],
                    "sources": lead.sources[:5]} for lead_id, lead in chunk]
        user = (f"Search: {why}\n\n" if why else "") + json.dumps(payload, ensure_ascii=False)
        result = client.call("summary", system=SYSTEM, user=user, output=SummaryBatch,
                             max_tokens=2000)
        if result is None:
            continue
        known = {str(lead_id) for lead_id, _ in chunk}
        producer = f"{result.provider}/{result.model}"
        for item in result.data.items:
            text = acceptable(item.summary)
            if text and str(item.id) in known and store.set_attribute(
                    int(item.id), "summary", text, origin="ai", producer=producer):
                stored += 1
    return stored
