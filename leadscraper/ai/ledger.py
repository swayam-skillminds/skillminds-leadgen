"""The AI usage ledger: per run, feature, provider and model (Phase 5.3).

The client adds every call (and every cache hit) here; the runner writes it to
``ai_usage`` when the run's post-discovery passes are done, and again at the
end. Rows are written as deltas, so writing twice never counts twice and a
resumed run adds to what its earlier attempt recorded.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        for name, value in asdict(other).items():
            setattr(self, name, getattr(self, name) + value)

    def minus(self, other: "Usage") -> "Usage":
        return Usage(**{name: value - getattr(other, name)
                        for name, value in asdict(self).items()})

    def empty(self) -> bool:
        return not any(asdict(self).values())


Key = tuple[str, str, str]          # (provider, model, feature)


class UsageLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[Key, Usage] = {}
        self._written: dict[Key, Usage] = {}

    def add(self, provider: str, model: str, feature: str, **values: Any) -> None:
        with self._lock:
            self._rows.setdefault((provider, model, feature), Usage()).add(Usage(**values))

    @property
    def cost_usd(self) -> float:
        with self._lock:
            return sum(u.cost_usd for u in self._rows.values())

    @property
    def calls(self) -> int:
        with self._lock:
            return sum(u.calls for u in self._rows.values())

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"provider": p, "model": m, "feature": f, **asdict(u)}
                    for (p, m, f), u in sorted(self._rows.items())]

    def by(self, field: str) -> dict[str, dict[str, Any]]:
        """Totals per provider or per feature (the run report, the Overview)."""
        index = {"provider": 0, "model": 1, "feature": 2}[field]
        out: dict[str, Usage] = {}
        with self._lock:
            for key, usage in self._rows.items():
                out.setdefault(key[index], Usage()).add(usage)
        return {k: {**asdict(v), "cost_usd": round(v.cost_usd, 6)} for k, v in sorted(out.items())}

    def flush(self, engine, workspace_id: int, run_id: int | None, *,
              unattached: bool = False) -> int:
        """Write what was added since the last flush to ``ai_usage``; rows written.

        ``unattached``: config-assist calls (ICP builder, planner, source
        discovery, connectivity tests) belong to no run and are written with
        ``run_id`` NULL.
        """
        if engine is None or (run_id is None and not unattached):
            return 0
        from ..db.models import AIUsage

        with self._lock:
            deltas = []
            for key, usage in self._rows.items():
                delta = usage.minus(self._written.get(key, Usage()))
                if not delta.empty():
                    deltas.append((key, delta))
            if not deltas:
                return 0
            now = datetime.now(timezone.utc)
            with engine.begin() as conn:
                for (provider, model, feature), delta in deltas:
                    conn.execute(AIUsage.__table__.insert().values(
                        workspace_id=workspace_id, run_id=run_id, provider=provider[:50],
                        model=model[:100], feature=feature[:50], calls=delta.calls,
                        cache_hits=delta.cache_hits, failures=delta.failures,
                        input_tokens=delta.input_tokens, output_tokens=delta.output_tokens,
                        searches=delta.searches, cost_usd=round(delta.cost_usd, 6),
                        created_at=now))
            for key, delta in deltas:
                self._written.setdefault(key, Usage()).add(delta)
            return len(deltas)
