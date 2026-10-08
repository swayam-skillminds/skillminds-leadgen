"""``signal_interpretation``: news items and job posts -> typed signals with strength.

For the signal lead types (Phase 6: hiring, funding news, tenders ...). Given a
lead type's :class:`~leadscraper.leadtypes.base.SignalSpec` list and the items
it fetched, the model picks a signal type (or none), a strength 0-1, a short
summary and an ``evidence`` quote. A verdict is kept only when:

* its type is one of the lead type's own signal types;
* its evidence quote appears verbatim in the item (grounded, like contacts).

A lead type calls :func:`interpret` and falls back to its own rules for any
item that comes back without a verdict.
"""

from __future__ import annotations

import json
from typing import Any, Union

from pydantic import BaseModel, Field

from ..grounding import SourceText

SYSTEM = (
    "You classify news items and job posts as buying signals for a B2B IT-services "
    "company. For each item return its id, `signal_type` (exactly one of the types given, "
    "or null when it is not a signal), `strength` from 0 to 1 (how strongly it suggests "
    "a need for IT services soon), `summary` (one factual line) and `evidence` (a short "
    "quote copied verbatim from the item that supports the type). Return ONLY JSON: "
    "{\"items\": [...]}."
)
#: Appended for news (``with_company``): the item's company, as written in it.
COMPANY = (" Also return `company`: the name of the organisation the item is about, copied "
           "exactly as the item writes it, or an empty string when it names none.")

Id = Union[str, int]


class SignalVerdict(BaseModel):
    id: Id
    signal_type: str | None = None
    strength: float = Field(0.5, description="0-1")
    summary: str = ""
    evidence: str = ""
    company: str = ""


class SignalBatch(BaseModel):
    items: list[SignalVerdict] = Field(default_factory=list)


def interpret(client, items: list[dict[str, Any]], specs: list, *, lead_type: str = "",
              batch_size: int = 10, with_company: bool = False) -> dict[str, dict[str, Any]]:
    """``{item id: {type, strength, summary, evidence}}`` for the items that are signals.

    ``with_company`` also asks which organisation an item is about (news): the name
    is kept only when it appears verbatim in the item, like the evidence quote.
    """
    if client is None or not client.uses("signals") or not items or not specs:
        return {}
    allowed = {spec.type: spec for spec in specs}
    types = "\n".join(f"- {spec.type}: {spec.title}. {spec.description}".strip()
                      for spec in specs)
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        payload = [{"id": str(item["id"]), "title": item.get("title", ""),
                    "text": str(item.get("text", ""))[:2500],
                    "published_at": str(item.get("published_at") or "")} for item in chunk]
        result = client.call("signals", system=SYSTEM + (COMPANY if with_company else ""),
                             user=f"Signal types:\n{types}\n\nItems:\n"
                                  + json.dumps(payload, ensure_ascii=False),
                             output=SignalBatch)
        if result is None:
            continue
        by_id = {str(item["id"]): item for item in chunk}
        for verdict in result.data.items:
            item = by_id.get(str(verdict.id))
            if item is None or verdict.signal_type not in allowed:
                continue
            source = SourceText(f"{item.get('title', '')} {item.get('text', '')}")
            if not verdict.evidence or not source.has_text(verdict.evidence, min_chars=8):
                continue                          # ungrounded: the quote isn't in the item
            company = " ".join((verdict.company or "").split())
            if company and not source.has_text(company, min_chars=2):
                company = ""                      # ungrounded: not a name the item gives
            out[str(verdict.id)] = {
                "type": verdict.signal_type, "lead_type": lead_type, "company": company[:200],
                "strength": round(min(1.0, max(0.0, float(verdict.strength))), 3),
                "summary": " ".join(verdict.summary.split())[:300],
                "evidence": verdict.evidence[:300], "source_url": item.get("url"),
                "interpreted_by": f"{result.provider}/{result.model}"}
    return out
