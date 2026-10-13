"""The AI features a config can switch on (Phase 5.4), and the tasks behind them.

A *feature* is what a user toggles (``ai.features``); a *task* is one kind of
call the code makes. Features map onto tasks, and the legacy feature names of
the original configs (``extract``, ``names``, ``venues``, ``dedup``) are tasks
themselves, so the Mumbai and Nashik presets keep doing exactly what they did.

An empty ``ai.features`` means the legacy set (extract, names, venues): what
"all features" meant before Phase 5. Every new feature is opt-in by name.
"""

from __future__ import annotations

FEATURES: dict[str, str] = {
    "icp_builder": "A plain-language brief -> a draft run config, returned for confirmation.",
    "planner": "Areas for places, query synonyms for categories, title variants for roles; "
               "suggests packs and lead types.",
    "source_discovery": "Proposes extra directories and feeds as candidate sources; approved "
                        "ones are scraped by the generic AI-assisted extractor (ai_directory).",
    "web_search_discovery": "Provider web search as a discovery source; an organisation is "
                            "kept only with a source page we fetched ourselves.",
    "extraction_fallback": "Reads contacts off pages whose selectors came up empty "
                           "(grounded: verbatim in the page or dropped).",
    "normalisation": "Clean names, industry tags, a company-size estimate (labelled), "
                     "duplicate adjudication (judge tier).",
    "venue_classification": "Types venues the rules could not (hospitality vertical).",
    "signal_interpretation": "Classifies news items and job posts into signal types and "
                             "strength, and app reviews into complaint themes (grounded "
                             "quotes).",
    "lead_summary": "One line on what the organisation does and why it matched. No "
                    "outreach copy.",
}

TASKS_OF: dict[str, set[str]] = {
    "icp_builder": {"icp_builder"},
    "planner": {"planner"},
    "source_discovery": {"source_discovery", "ai_directory"},
    "web_search_discovery": {"web_search"},
    "extraction_fallback": {"extract"},
    "normalisation": {"names", "industry", "size", "dedup"},
    "venue_classification": {"venues"},
    "signal_interpretation": {"signals"},
    "lead_summary": {"summary"},
}

#: Pre-Phase-5 feature names: each is one task.
LEGACY = {"extract", "names", "venues", "dedup"}
#: What an empty ``ai.features`` means.
DEFAULT_TASKS = frozenset({"extract", "names", "venues"})
#: Features that run while setting a config up, not during a run.
CONFIG_TIME = ("icp_builder", "planner", "source_discovery")

FEATURE_OF_TASK: dict[str, str] = {task: feature for feature, tasks in TASKS_OF.items()
                                   for task in tasks}
FEATURE_OF_TASK.update({"extract": "extraction_fallback", "names": "normalisation",
                        "venues": "venue_classification", "dedup": "normalisation",
                        "connectivity": "connectivity"})


def known(name: str) -> bool:
    return name in FEATURES or name in LEGACY


def tasks_for(features: list[str] | None) -> frozenset[str]:
    if not features:
        return DEFAULT_TASKS
    out: set[str] = set()
    for name in features:
        out |= TASKS_OF.get(name, {name} if name in LEGACY else set())
    return frozenset(out)


def feature_of(task: str) -> str:
    return FEATURE_OF_TASK.get(task, task)
