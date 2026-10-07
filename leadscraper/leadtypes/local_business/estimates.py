"""Cold-start costs, depth presets and AI cost for local_business.

**Priors** (REBUILD-PROMPT Part 3.1) are a unit's expected seconds and new
organisations before any history exists. Seconds come from the requests a
unit makes at the run's depth times the configured polite delays; yields come
from what each kind of source produced historically -- Maps about 95% of the
original Mumbai leads -- so the cold-start order is Maps > directories > SERP.
Learned ``unit_stats`` replace them after the first run.

**Depth** (Part 3.3) scales the per-unit knobs. ``balanced`` is each source's
configured settings, unchanged (the shipped defaults are close to Part 3.3's
balanced row; the legacy presets keep their exact settings). ``quick`` caps
them and switches enrichment and AI name polish off; ``deep`` raises them and
switches the Instagram enricher on.
"""

from __future__ import annotations

import math
from typing import Any

from ...engine.costs import UnitCost, prior_cost

#: Seconds a plain HTTP fetch takes beyond its polite delay.
FETCH = 2.0
#: Seconds a browser page load takes beyond its polite delay.
RENDER = 6.0
#: Seconds per Maps feed scroll round.
SCROLL = 1.5

DEPTH_KNOBS: dict[str, dict[str, int]] = {
    "quick": {"max_results_per_query": 10, "scroll_rounds": 3, "pages": 1, "max_pages": 1,
              "max_properties": 30},
    "deep": {"max_results_per_query": 60, "scroll_rounds": 20, "pages": 5, "max_pages": 5},
}
#: Which knob each source reads (and its default when unset).
SOURCE_KNOBS: dict[str, dict[str, int]] = {
    "google_maps": {"max_results_per_query": 25, "scroll_rounds": 8},
    "google_search": {"pages": 2},
    "justdial": {"max_pages": 1}, "sulekha": {"max_pages": 1}, "indiamart": {"max_pages": 2},
    "wedmegood": {"max_pages": 3}, "weddingwire": {"max_pages": 3},
    "airbnb": {"max_pages": 2}, "booking": {"max_pages": 2},
    "stayvista": {"max_properties": 400}, "saffronstays": {"max_properties": 400},
    "elivaas": {"max_properties": 400},
}
#: What an empty ai.features means (ai.catalog.DEFAULT_TASKS), for the quick preset.
AI_FEATURES = ("extract", "names", "venues")
#: Features the quick preset drops: the polish passes, not the extraction.
QUICK_DROPS = ("names", "normalisation", "lead_summary")


def apply_depth(config, depth: str) -> list[str]:
    """Scale the run's source knobs for `depth`; returns "path: old -> new" notes."""
    if depth not in DEPTH_KNOBS:
        return []
    knobs = DEPTH_KNOBS[depth]
    notes: list[str] = []

    def put(path: str, value: Any) -> None:
        old = config.get(path)
        if old != value:
            config.set(path, value, record=False)
            notes.append(f"{path}: {old!r} -> {value!r}")

    for name, defaults in SOURCE_KNOBS.items():
        if config.get(f"sources.{name}") is None:
            continue
        for knob, default in defaults.items():
            if knob not in knobs:
                continue
            current = int(config.get(f"sources.{name}.{knob}", default) or default)
            target = min(current, knobs[knob]) if depth == "quick" else max(current, knobs[knob])
            put(f"sources.{name}.{knob}", target)

    if depth == "quick":
        put("runtime.enrichment", False)
        if config.get("ai.enabled", False):
            features = list(config.get("ai.features") or AI_FEATURES)
            kept = [f for f in features if f not in QUICK_DROPS]
            if kept:
                put("ai.features", kept)
            else:                   # an empty list would mean the default set
                put("ai.enabled", False)
    elif depth == "deep" and config.get("sources.instagram") is not None:
        put("sources.instagram.enabled", True)
    return notes


def _delay(config, name: str) -> float:
    low, high = config.delays(name)
    return (float(low) + float(high)) / 2


def unit_prior(source, locality, category, config) -> UnitCost:
    """Expected seconds and new organisations of one discovery unit, cold."""
    name = source.name
    cfg = source.cfg or {}
    delay = _delay(config, name)
    serp_delay = max(delay, _delay(config, "google_search"))
    queries = list(getattr(category, "queries", None) or [getattr(category, "name", "x")])
    scope = getattr(source, "scope", "combo")

    if name == "google_maps":
        phrasings = int(cfg.get("locality_phrasings", 2))
        terms = len(locality.search_terms[:phrasings]) if locality is not None else 1
        searches = len(queries) * max(1, terms)
        scroll = int(cfg.get("scroll_rounds", 8))
        places = int(cfg.get("max_results_per_query", 25)) * 0.8
        seconds = searches * (delay + RENDER + scroll * SCROLL) + places * (delay + RENDER / 2)
        return prior_cost(seconds, places * 0.6)
    if name == "google_search":
        pages = int(cfg.get("pages", 2))
        searches = min(3, len(queries))
        return prior_cost(searches * pages * (serp_delay + FETCH), searches * pages * 0.3)
    if name == "facebook":
        searches = min(2, len(queries))
        return prior_cost(searches * (serp_delay + FETCH) + searches * 4 * (delay + FETCH),
                          searches * 0.5)
    if name == "linkedin_serp":
        return prior_cost(min(2, len(queries)) * (serp_delay + FETCH), 0.2)
    if scope == "run":
        properties = int(cfg.get("max_properties", 120))
        return prior_cost(properties * (delay + FETCH) * 1.2, properties * 0.5)
    pages = int(cfg.get("max_pages", 1))
    fetch = RENDER if getattr(source, "uses_browser", False) else FETCH
    if scope == "locality":
        return prior_cost(pages * 3 * (delay + fetch), pages * 6.0)
    # Directories: a quarter of a new organisation per listing page. Most of
    # what they list, Maps already has (the original runs: ~95% from Maps).
    return prior_cost(pages * (delay + fetch), pages * 0.25)


def post_estimate(pipeline, expected_new: float) -> tuple[float, float]:
    """(mean, sd) seconds of enrichment and AI passes after discovery."""
    config = pipeline.config
    workers = max(1, pipeline.enrich_workers)
    mean = var = 0.0
    for enricher in pipeline.enrichers:
        name = enricher.name
        delay = _delay(config, name)
        pending = _pending_targets(pipeline, enricher)
        if name == "website":
            count = pending + 0.45 * expected_new
            pages = min(int(enricher.cfg.get("max_pages_per_site", 4)), 3)
            wall = count * pages * (delay + FETCH) / min(workers, max(1.0, count))
        elif name == "instagram":
            count = min(float(enricher.cfg.get("max_profiles_per_run", 150)),
                        pending + 0.08 * expected_new)
            wall = count * (delay + FETCH)          # one host: one at a time
        elif name == "maps_lookup":
            count = pending + 0.2 * expected_new
            wall = count * (delay + 2 * RENDER)
        else:
            count = pending + 0.3 * expected_new
            wall = count * (delay + FETCH) / workers
        mean += wall
        var += (0.5 * wall) ** 2
    if pipeline.ai.active:
        batches = math.ceil(0.5 * expected_new / max(1, pipeline.ai.batch_size))
        # Phase 5 passes (industry/size, summaries, duplicate judge): a batch each.
        extra = sum(1 for task in ("industry", "summary", "dedup") if pipeline.ai.uses(task))
        batches += extra * math.ceil(expected_new / max(1, pipeline.ai.batch_size))
        mean += batches * 15.0
        var += (batches * 8.0) ** 2
    return mean, math.sqrt(var)


def _pending_targets(pipeline, enricher) -> float:
    try:
        targets = enricher.targets(pipeline.store)
        return float(len(pipeline._not_yet_enriched(enricher, targets)))
    except Exception:
        return 0.0


#: Per feature: (items per new organisation, prompt tokens per item, answer tokens
#: per item, tier). Rough on purpose: the cap, not this, is what bounds spend.
_AI_ITEMS = {
    "names": (0.35, 150, 60, "worker"),
    "extract": (0.10, 1500, 120, "worker"),
    "venues": (0.30, 250, 60, "worker"),
    "industry": (1.0, 120, 40, "worker"),
    "summary": (1.0, 150, 50, "worker"),
    "dedup": (0.02, 300, 80, "judge"),
}
#: One call's overhead (system prompt, envelope) per batch.
_CALL_TOKENS = 1500


def ai_estimate(pipeline, expected_new: float) -> dict[str, Any] | None:
    """Expected AI calls and cost for this run, per feature, when AI is on.

    Priced at the first usable provider's rates (``ai.prices`` / the price
    table), capped by the run's budget: the cap is a hard stop, so the run
    can't spend more than it whatever this guesses.
    """
    ai = pipeline.ai
    client = ai.client
    if not client.active:
        return None
    provider = client.first()
    prices = client.prices
    batch = max(1, ai.batch_size)
    by_feature: dict[str, dict[str, Any]] = {}
    for task, (per_org, tokens_in, tokens_out, tier) in _AI_ITEMS.items():
        if not client.uses(task) or (task == "venues" and not pipeline.config.is_venue_run):
            continue
        items = per_org * expected_new
        if not items:
            continue
        calls = math.ceil(items / batch)
        model = provider.spec.model_for(tier) or ""
        cost = prices.cost(provider.spec, model, int(calls * _CALL_TOKENS + items * tokens_in),
                           int(items * tokens_out))
        _add(by_feature, task, calls, items, cost)
    units = [u for u in pipeline.discovery_units() if u.source == "ai_web_search"]
    if units:
        searcher = next((p for p in client.chain if p.capabilities.web_search), None)
        if searcher is not None:
            model = searcher.spec.model_for("worker") or ""
            searches = int(pipeline.config.get("sources.ai_web_search.max_searches", 3) or 3)
            cost = len(units) * prices.cost(searcher.spec, model, 6000, 1500, searches)
            _add(by_feature, "web_search", len(units), len(units), cost)
    directory = [u for u in pipeline.discovery_units() if u.source == "ai_directory"]
    if directory and client.uses("ai_directory"):
        model = provider.spec.model_for("worker") or ""
        # First contact with a layout: read 2 pages + propose selectors; later runs: 0.
        cost = 3 * prices.cost(provider.spec, model, 4000, 1200)
        _add(by_feature, "ai_directory", 3, 0, cost)
    total = sum(f["cost_usd"] for f in by_feature.values())
    calls = sum(f["calls"] for f in by_feature.values())
    items = sum(f["items"] for f in by_feature.values())
    cap = None if client.cap == float("inf") else client.cap
    _, _, priced = prices.rates(provider.spec, provider.spec.model_for("worker") or "")
    return {"calls": calls, "items": round(items), "model": provider.spec.worker_model,
            "provider": provider.name, "backend": provider.name,
            "cost_usd": round(min(total, cap) if cap is not None else total, 3),
            "uncapped_cost_usd": round(total, 3), "cap_usd": cap,
            "by_feature": by_feature, "priced": priced,
            "note": "rough; list prices per million tokens from the price table"
                    + ("" if priced else " (model not in the table: its provider's default "
                                         "rate)")}


def _add(by_feature: dict, task: str, calls: int, items: float, cost: float) -> None:
    from ...ai.catalog import feature_of

    feature = feature_of(task)
    row = by_feature.setdefault(feature, {"calls": 0, "items": 0, "cost_usd": 0.0})
    row["calls"] += calls
    row["items"] += round(items)
    row["cost_usd"] = round(row["cost_usd"] + cost, 4)
