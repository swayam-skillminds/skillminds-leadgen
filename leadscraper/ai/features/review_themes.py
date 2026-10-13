"""App-review complaint themes the rules couldn't place (``signal_interpretation``).

For the ``app_store`` lead type (Phase 8): given low-star reviews no theme rule
matched, the model picks themes from the fixed list (``sources/apps/themes.py``)
and must quote each review verbatim. A theme is kept only when its quote is in
the review (grounded, like every AI fact). It runs under the same feature and
task as news and job-post interpretation (``signals``), so the same switch, cap
and ledger apply.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..grounding import SourceText

SYSTEM = (
    "You read app-store reviews that gave an app one or two stars and say what each "
    "complains about, for a company that builds and fixes apps. For each review return its "
    "id, `themes` (zero or more of the theme keys given; none when it complains about "
    "nothing an app developer could fix) and `evidence` (a short quote copied verbatim from "
    "the review). Return ONLY JSON: {\"items\": [...]}."
)


class ReviewVerdict(BaseModel):
    id: str | int
    themes: list[str] = Field(default_factory=list)
    evidence: str = ""


class ReviewBatch(BaseModel):
    items: list[ReviewVerdict] = Field(default_factory=list)


def classify(client, reviews: list[dict[str, Any]], themes: dict[str, str], *,
             batch_size: int = 10) -> dict[str, dict[str, Any]]:
    """``{review id: {themes, evidence, interpreted_by}}`` for reviews the model placed.

    ``reviews``: ``{id, text}``; ``themes``: theme key -> label.
    """
    if client is None or not client.uses("signals") or not reviews or not themes:
        return {}
    listing = "\n".join(f"- {key}: {label}" for key, label in themes.items())
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(reviews), batch_size):
        chunk = reviews[start:start + batch_size]
        payload = [{"id": str(r["id"]), "text": str(r.get("text", ""))[:1500]} for r in chunk]
        result = client.call("signals", system=SYSTEM,
                             user=f"Themes:\n{listing}\n\nReviews:\n"
                                  + json.dumps(payload, ensure_ascii=False),
                             output=ReviewBatch)
        if result is None:
            continue
        by_id = {str(r["id"]): r for r in chunk}
        for verdict in result.data.items:
            review = by_id.get(str(verdict.id))
            if review is None:
                continue
            kept = [t for t in dict.fromkeys(verdict.themes) if t in themes]
            if not kept or not verdict.evidence or \
                    not SourceText(str(review.get("text", ""))).has_text(verdict.evidence,
                                                                        min_chars=8):
                continue                      # ungrounded or no known theme
            out[str(verdict.id)] = {"themes": kept, "evidence": verdict.evidence[:300],
                                    "interpreted_by": f"{result.provider}/{result.model}"}
    return out
