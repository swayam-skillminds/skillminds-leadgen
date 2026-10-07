"""AI services (Phase 5): providers, keys, config assist, candidate sources, usage.

The API and the CLI call these; nothing here scrapes.

* **Providers and keys** -- every provider kind and every one the workspace
  configures, with its capabilities, models and whether a key is configured
  (and where: env / file / keychain). Never the key. :func:`test_provider`
  makes one tiny call (a connectivity test the user asks for).
* **Config assist** -- the ICP builder, the planner and source discovery. Each
  request gets its own client under the workspace's ``ai.assist_budget_usd``,
  its usage is written to the ledger with no run, and the answer is returned
  for confirmation: nothing is saved as a config or run.
* **Candidate sources** -- what source discovery proposed, for approval.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine

from ..ai.client import AIClient
from ..ai.providers.registry import KINDS, build, make_spec
from ..ai.settings import bills_separately, resolve_assist
from ..db.models import AIUsage, CandidateSource
from .errors import Invalid, NotFound
from .query import Page, aware, clamp, fetch_page
from .settings import ai_settings
from .workspace import Workspace

CANDIDATES = CandidateSource.__table__
USAGE = AIUsage.__table__


def _cache_path():
    from ..paths import cache_dir

    return cache_dir() / "ai_cache.sqlite3"


def assist_client(engine: Engine, ws: Workspace, *, features: list[str] | None = None,
                  budget_usd: float | None = None) -> AIClient:
    """A client for one config-assist request: the workspace's chain and assist budget."""
    workspace_ai = ai_settings(engine, ws.id)
    overrides: dict[str, Any] = {}
    if features is not None:
        overrides["features"] = features
    if budget_usd is not None:
        overrides["max_cost_usd"] = budget_usd
    return AIClient(resolve_assist(workspace_ai, overrides), cache_path=_cache_path())


def _finish(engine: Engine, ws: Workspace, client: AIClient) -> dict[str, Any]:
    """Write the request's usage (no run) and describe it for the response."""
    try:
        client.ledger.flush(engine, ws.id, None, unattached=True)
    finally:
        client.close()
    return {"cost_usd": round(client.stats.cost_usd, 6), "calls": client.stats.calls,
            "cache_hits": client.stats.cache_hits,
            "providers": [p.name for p in client.chain],
            "stopped": client.stats.shutdown_reason or None}


# --- providers and keys --------------------------------------------------------------------

def _provider_row(entry: Any, *, in_chain: bool) -> dict[str, Any]:
    spec = make_spec(entry)
    provider = build(spec)
    problem = provider.problem()
    source = provider.key_source()
    return {**spec.public(), "key_configured": (source is not None) if provider.needs_key()
            else None, "key_source": source, "needs_key": provider.needs_key(),
            "usable": problem is None, "problem": problem,
            "bills_separately": bills_separately(spec), "in_workspace_chain": in_chain}


def providers(engine: Engine, ws: Workspace) -> dict[str, Any]:
    """Every provider kind plus the workspace's own entries; keys only as booleans."""
    workspace_ai = ai_settings(engine, ws.id)
    chain = list(workspace_ai.get("providers") or [])
    rows, named = [], set()
    for entry in chain:
        try:
            row = _provider_row(entry, in_chain=True)
        except ValueError as exc:
            rows.append({"name": str(entry), "usable": False, "problem": str(exc),
                         "in_workspace_chain": True})
            continue
        named.add(row["name"])
        rows.append(row)
    for kind in KINDS:
        if kind not in named:
            rows.append(_provider_row(kind, in_chain=False))
    return {"providers": rows,
            "chain": [r["name"] for r in rows if r.get("in_workspace_chain")]
            or ["claude_code_cli"],
            "assist_budget_usd": workspace_ai.get("assist_budget_usd"),
            "note": "Keys are never shown. Set one with `leadscraper secrets set NAME` "
                    "(OS keychain), LEADSCRAPER_SECRET_<NAME>, or a secret file."}


class Ping(BaseModel):
    ok: bool


def test_provider(engine: Engine, ws: Workspace, name: str) -> dict[str, Any]:
    """One tiny call to a provider: does the key work, is the model there?"""
    workspace_ai = ai_settings(engine, ws.id)
    entry = next((e for e in workspace_ai.get("providers") or []
                  if (e if isinstance(e, str) else e.get("name")) == name), name)
    try:
        spec = make_spec(entry)
    except ValueError as exc:
        raise NotFound(str(exc)) from None
    provider = build(spec)
    problem = provider.problem()
    if problem:
        return {"provider": spec.name, "ok": False, "error": problem, "model": None,
                "latency_ms": None, "cost_usd": 0.0}
    settings = resolve_assist(workspace_ai, {"max_cost_usd": 0.05})
    settings.tasks = frozenset({"connectivity"})
    client = AIClient(settings, providers=[provider])
    started = time.monotonic()
    result = client.call("connectivity", system="You answer connectivity checks.",
                         user='Reply with exactly {"ok": true}', output=Ping, max_tokens=50)
    latency = round((time.monotonic() - started) * 1000)
    usage = _finish(engine, ws, client)
    error = None
    if result is None:
        error = client.unavailable.get(spec.name) or client.stats.shutdown_reason or \
            "no valid answer"
    return {"provider": spec.name, "ok": bool(result and result.data.ok), "error": error,
            "model": result.model if result else spec.worker_model, "latency_ms": latency,
            "cost_usd": usage["cost_usd"]}


# --- config assist -------------------------------------------------------------------------

def offerings() -> list[dict[str, Any]]:
    from ..ai.features.icp_builder import offerings as load

    return [{"key": key, **value} for key, value in load().items()]


def build_icp(engine: Engine, ws: Workspace, *, brief: str, offering: str | None = None,
              country: str | None = None, places: list[str] | None = None) -> dict[str, Any]:
    from ..ai.features import icp_builder

    if not (brief or "").strip():
        raise Invalid("describe what you sell and to whom (brief)")
    client = assist_client(engine, ws)
    try:
        result = icp_builder.build(client, brief, offering=offering, country=country,
                                   places=places)
    except (ValueError, LookupError) as exc:
        client.close()
        raise Invalid(str(exc)) from None
    return {**result, "ai": _finish(engine, ws, client)}


def plan_config(engine: Engine, ws: Workspace, document: dict[str, Any], *,
                roles: list[str] | None = None) -> dict[str, Any]:
    from pydantic import ValidationError

    from ..ai.features import planner
    from ..config.schema import RunConfig
    from .errors import validation_errors

    try:
        RunConfig.model_validate(document)
    except ValidationError as exc:
        raise Invalid("invalid config", errors=validation_errors(exc)) from None
    client = assist_client(engine, ws)
    try:
        result = planner.plan(client, document, roles=roles)
    except ValueError as exc:
        client.close()
        raise Invalid(str(exc)) from None
    return {**result, "ai": _finish(engine, ws, client)}


def discover_sources(engine: Engine, ws: Workspace, *, country: str,
                     categories: list[str] | None = None, places: list[str] | None = None,
                     industry: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Ask for candidate sources and store them as ``proposed`` (never scraped yet)."""
    from ..ai.features import source_discovery
    from ..core.country import is_known_region

    country = (country or "").strip().upper()
    if not is_known_region(country):
        raise Invalid(f"{country!r} is not an ISO 3166-1 alpha-2 country code")
    if not (categories or industry):
        raise Invalid("give categories or an industry to find directories for")
    client = assist_client(engine, ws)
    result = source_discovery.discover(client, country=country, categories=categories or [],
                                       places=places, industry=industry, limit=limit)
    stored = []
    for item in result["candidates"]:
        row = _store_candidate(engine, ws, item, country=country,
                               proposed_by=result.get("provider") or "ai")
        if row is not None:
            stored.append(row)
    return {"candidates": stored, "rejected": result["rejected"],
            "searched": result["searched"], "note": result.get("note"),
            "ai": _finish(engine, ws, client)}


# --- candidate sources -----------------------------------------------------------------------

def _candidate_out(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "url": row["url"], "host": row["host"],
            "country": row["country"] or None, "place": row["place"] or None,
            "category": row["category"] or None, "kind": row["kind"],
            "page_type": row["page_type"], "rationale": row["rationale"],
            "terms_note": row["terms_note"], "status": row["status"],
            "proposed_by": row["proposed_by"], "created_at": aware(row["created_at"]),
            "decided_at": aware(row["decided_at"]), "last_run_at": aware(row["last_run_at"])}


def _store_candidate(engine: Engine, ws: Workspace, item: dict[str, Any], *, country: str,
                     proposed_by: str) -> dict[str, Any]:
    with engine.begin() as conn:
        existing = conn.execute(select(CANDIDATES).where(
            CANDIDATES.c.workspace_id == ws.id, CANDIDATES.c.url == item["url"])
        ).mappings().first()
        if existing is not None:
            return _candidate_out(dict(existing))
        new_id = conn.execute(CANDIDATES.insert().values(
            workspace_id=ws.id, name=str(item.get("name") or item["url"])[:300],
            url=item["url"][:2000],
            host=(urlparse(item["url"]).hostname or "").lower()[:255],
            country=(country or "")[:2].upper(),
            place=str(item.get("place") or "")[:200],
            category=str(item.get("category") or "")[:200],
            kind=item.get("kind") or "directory", page_type=item.get("page_type") or "listing",
            rationale=str(item.get("rationale") or "")[:2000],
            terms_note=str(item.get("terms_note") or "")[:2000], status="proposed",
            proposed_by=proposed_by[:160], created_at=datetime.now(timezone.utc))
        ).inserted_primary_key[0]
        row = conn.execute(select(CANDIDATES).where(CANDIDATES.c.id == new_id)).mappings().one()
    return _candidate_out(dict(row))


def add_candidate(engine: Engine, ws: Workspace, *, url: str, name: str | None = None,
                  country: str, place: str | None = None, category: str | None = None,
                  rationale: str = "") -> dict[str, Any]:
    """A candidate the user names themselves (it still needs approving)."""
    from ..ai.features.source_discovery import acceptable

    why = acceptable(url)
    if why:
        raise Invalid(f"{url}: {why}")
    return _store_candidate(engine, ws, {"url": url, "name": name or url, "place": place,
                                         "category": category, "rationale": rationale},
                            country=country, proposed_by="user")


def list_candidates(engine: Engine, ws: Workspace, *, status: str | None = None,
                    country: str | None = None, limit: int = 50, offset: int = 0) -> Page:
    limit, offset = clamp(limit, offset)
    query = select(CANDIDATES).where(CANDIDATES.c.workspace_id == ws.id)
    if status:
        query = query.where(CANDIDATES.c.status == status)
    if country:
        query = query.where(CANDIDATES.c.country == country.upper())
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query.order_by(CANDIDATES.c.id), limit=limit,
                                 offset=offset)
    return Page([_candidate_out(r) for r in rows], total, limit, offset)


def decide_candidate(engine: Engine, ws: Workspace, candidate_id: int,
                     status: str) -> dict[str, Any]:
    if status not in ("approved", "rejected", "proposed"):
        raise Invalid("status must be approved, rejected or proposed")
    with engine.begin() as conn:
        found = conn.execute(update(CANDIDATES).where(
            CANDIDATES.c.id == candidate_id, CANDIDATES.c.workspace_id == ws.id).values(
            status=status, decided_at=None if status == "proposed"
            else datetime.now(timezone.utc))).rowcount
        if not found:
            raise NotFound(f"no candidate source {candidate_id}")
        row = conn.execute(select(CANDIDATES).where(CANDIDATES.c.id == candidate_id)
                           ).mappings().one()
    return _candidate_out(dict(row))


def delete_candidate(engine: Engine, ws: Workspace, candidate_id: int) -> dict[str, Any]:
    with engine.begin() as conn:
        row = conn.execute(select(CANDIDATES).where(
            CANDIDATES.c.id == candidate_id, CANDIDATES.c.workspace_id == ws.id)
        ).mappings().first()
        if row is None:
            raise NotFound(f"no candidate source {candidate_id}")
        conn.execute(CANDIDATES.delete().where(CANDIDATES.c.id == candidate_id))
    return _candidate_out(dict(row))


# --- usage ------------------------------------------------------------------------------------

def usage(engine: Engine, ws: Workspace, *, run_id: int | None = None) -> dict[str, Any]:
    """AI spend from the ledger: totals, by provider, by feature (assist calls: run None)."""
    query = select(USAGE.c.provider, USAGE.c.model, USAGE.c.feature,
                   func.sum(USAGE.c.calls), func.sum(USAGE.c.cache_hits),
                   func.sum(USAGE.c.failures), func.sum(USAGE.c.input_tokens),
                   func.sum(USAGE.c.output_tokens), func.sum(USAGE.c.searches),
                   func.sum(USAGE.c.cost_usd)).where(USAGE.c.workspace_id == ws.id)
    if run_id is not None:
        query = query.where(USAGE.c.run_id == run_id)
    query = query.group_by(USAGE.c.provider, USAGE.c.model, USAGE.c.feature)
    with engine.connect() as conn:
        rows = conn.execute(query.order_by(USAGE.c.provider, USAGE.c.model,
                                           USAGE.c.feature)).all()
    out = [{"provider": r[0], "model": r[1], "feature": r[2], "calls": int(r[3] or 0),
            "cache_hits": int(r[4] or 0), "failures": int(r[5] or 0),
            "input_tokens": int(r[6] or 0), "output_tokens": int(r[7] or 0),
            "searches": int(r[8] or 0), "cost_usd": round(float(r[9] or 0.0), 6)}
           for r in rows]

    def total(key: str) -> dict[str, float]:
        acc: dict[str, float] = {}
        for row in out:
            acc[row[key]] = round(acc.get(row[key], 0.0) + row["cost_usd"], 6)
        return acc

    return {"run_id": run_id, "cost_usd": round(sum(r["cost_usd"] for r in out), 6),
            "calls": sum(r["calls"] for r in out), "by_provider": total("provider"),
            "by_feature": total("feature"), "rows": out}
