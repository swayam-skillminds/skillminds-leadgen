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
AI_FEATURES = ("extract", "names", "dedup", "venues")


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
            put("ai.features", [f for f in features if f != "names"])
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
        mean += batches * 15.0
        var += (batches * 8.0) ** 2
    return mean, math.sqrt(var)


def _pending_targets(pipeline, enricher) -> float:
    try:
        targets = enricher.targets(pipeline.store)
        return float(len(pipeline._not_yet_enriched(enricher, targets)))
    except Exception:
        return 0.0


def ai_estimate(pipeline, expected_new: float) -> dict[str, Any] | None:
    """Rough AI calls and cost for this run, when AI is on."""
    ai = pipeline.ai
    if not ai.active:
        return None
    from ...ai.assistant import AnthropicAPIBackend

    items = 0.0
    if ai.uses("names"):
        items += 0.35 * expected_new
    if ai.uses("extract"):
        items += 0.10 * expected_new
    if pipeline.config.is_venue_run and ai.uses("venues"):
        items += 0.30 * expected_new
    calls = math.ceil(items / max(1, ai.batch_size)) if items else 0
    tokens_in = calls * 1500 + items * 150
    tokens_out = items * 60
    rate_in, rate_out = AnthropicAPIBackend.PRICES.get(ai.model, (1.0, 5.0))
    cost = (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000
    cap = None if ai.max_cost == float("inf") else ai.max_cost
    return {"calls": calls, "items": round(items), "model": ai.model,
            "cost_usd": round(min(cost, cap) if cap is not None else cost, 3),
            "cap_usd": cap, "backend": ai.backend.name if ai.backend else None,
            "note": "rough; list-price equivalent per million tokens"}
