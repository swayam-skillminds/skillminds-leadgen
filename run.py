#!/usr/bin/env python3
"""Event-industry lead scraper -- command line entry point.

Runs the Mumbai vendor scrape by default (config.yaml). Point it at another
config for another run, e.g. the Nashik high-end venue scrape:

  python run.py --config config.nashik.yaml --dry-run

Examples
--------
  # See what a full run would do, without making a single request
  python run.py --dry-run

  # Everything, all localities and categories (long: plan for hours)
  python run.py

  # One slice, fast feedback
  python run.py --locality Andheri --category Caterers --source google_maps

  # Re-rank and re-export what's already in the DB, no scraping
  python run.py --export-only

  # Fill in contact details for leads already discovered
  python run.py --enrich-only
"""

from __future__ import annotations

import argparse
import logging
import sys

from leadscraper.config import Config
from leadscraper.export import write_csv
from leadscraper.pipeline import Pipeline
from leadscraper.scoring import Scorer
from leadscraper.sheets import SheetsWriter
from leadscraper.sources import DISCOVERY_SOURCES, ENRICHMENT_SOURCES
from leadscraper.normalize import set_extra_name_noise
from leadscraper.store import DedupOptions, LeadStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape event-industry venues and vendors from free sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")

    scope = parser.add_argument_group("scope")
    scope.add_argument("--locality", action="append", dest="localities",
                       help="Locality name (repeatable; default: all in config)")
    scope.add_argument("--category", action="append", dest="categories",
                       help="Category name (repeatable; default: all in config)")
    scope.add_argument("--source", action="append", dest="sources",
                       help=f"Discovery source (repeatable). Available: "
                            f"{', '.join(sorted(DISCOVERY_SOURCES))}")
    scope.add_argument("--enricher", action="append", dest="enrichers",
                       help=f"Enricher (repeatable). Available: "
                            f"{', '.join(sorted(ENRICHMENT_SOURCES))}")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="Print the plan and exit without scraping")
    behaviour.add_argument("--export-only", action="store_true",
                           help="Rescore + re-export the existing DB, no scraping")
    behaviour.add_argument("--enrich-only", action="store_true",
                           help="Skip discovery; only enrich existing leads")
    behaviour.add_argument("--no-enrich", action="store_true",
                           help="Discovery only; skip the per-lead enrichment phase")
    behaviour.add_argument("--no-headless", action="store_true",
                           help="Show the browser window (useful for debugging selectors)")
    behaviour.add_argument("--no-sheets", action="store_true",
                           help="Skip Google Sheets; write CSV only")
    behaviour.add_argument("--limit-per-query", type=int, default=None,
                           help="Override max results per Google Maps query")
    behaviour.add_argument("--ai", action="store_true",
                           help="Enable the AI layer (uses your Claude Code subscription "
                                "via the `claude` CLI, or ANTHROPIC_API_KEY if set)")
    behaviour.add_argument("--no-ai", action="store_true",
                           help="Force the AI layer off even if enabled in config")
    behaviour.add_argument("--ai-budget", type=float, default=None,
                           help="Cap AI token usage this run, in list-price USD equivalent "
                                "(not a charge -- see README). Default from config.")
    behaviour.add_argument("--ai-unlimited", action="store_true",
                           help="No usage cap: run AI until your subscription allowance "
                                "is exhausted, then continue automatically without AI")
    behaviour.add_argument("--ai-feature", action="append", dest="ai_features",
                           choices=["extract", "names", "dedup"],
                           help="Limit AI to specific features (repeatable)")
    behaviour.add_argument("--fast", action="store_true",
                           help="Halve all delays. Raises the odds of being blocked.")

    output = parser.add_argument_group("output")
    output.add_argument("--csv", default=None, help="Override the CSV output path")
    output.add_argument("--sheet-id", default=None, help="Write to this spreadsheet ID")
    output.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    output.add_argument("-q", "--quiet", action="store_true", help="Warnings and errors only")
    return parser


def setup_logging(config: Config, *, verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    log_path = config.path_for("run_log", "logs/run.log")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass  # a read-only logs dir shouldn't stop the run
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # These libraries are extremely chatty at INFO.
    for noisy in ("urllib3", "websockets", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def apply_overrides(config: Config, args: argparse.Namespace) -> None:
    if args.no_sheets:
        config.set("sheets.enabled", False)
    if args.sheet_id:
        config.set("sheets.spreadsheet_id", args.sheet_id)
        config.set("sheets.enabled", True)
    if args.csv:
        config.set("runtime.csv_export", args.csv)
    if args.no_headless:
        config.set("runtime.headless", False)
    if args.limit_per_query is not None:
        config.set("sources.google_maps.max_results_per_query", args.limit_per_query)
    if args.ai:
        config.set("ai.enabled", True)
    if args.no_ai:
        config.set("ai.enabled", False)
    if args.ai_budget is not None:
        config.set("ai.max_cost_usd", args.ai_budget)
    if args.ai_unlimited:
        config.set("ai.max_cost_usd", None)
        config.set("ai.enabled", True)
    if args.ai_features:
        config.set("ai.features", args.ai_features)
    if args.fast:
        for name in list(config.get("sources", {}) or {}):
            for key in ("min_delay", "max_delay"):
                current = config.get(f"sources.{name}.{key}")
                if isinstance(current, (int, float)):
                    config.set(f"sources.{name}.{key}", max(0.5, float(current) / 2))
        logging.getLogger(__name__).warning(
            "--fast halves every delay; expect more CAPTCHAs and blocked sources"
        )


def export_only(config: Config) -> int:
    """Rescore and re-export whatever is already in the database."""
    set_extra_name_noise([config.city, *(config.get("dedup.extra_name_noise") or [])])
    scorer = Scorer(config)
    store = LeadStore(config.path_for("db_path", "data/leads.sqlite3"), scorer,
                      DedupOptions.from_config(config))
    try:
        if store.count() == 0:
            print("Nothing to export: the lead database is empty. Run a scrape first.")
            return 1
        store.rescore_all()
        leads = store.sorted_leads()
        csv_path = config.path_for("csv_export", "data/leads.csv")
        write_csv(leads, csv_path, config.profile)
        print(f"Re-scored and exported {len(leads)} leads -> {csv_path}")

        sheets = SheetsWriter(config)
        if sheets.sync(leads):
            print(f"Synced to {sheets.url}")
        elif sheets.unavailable_reason:
            print(f"Sheets not written: {sheets.unavailable_reason}")
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config.load(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config, verbose=args.verbose, quiet=args.quiet)
    apply_overrides(config, args)

    if args.export_only:
        return export_only(config)

    try:
        localities = config.select_localities(args.localities)
        categories = config.select_categories(args.categories)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    source_names = args.sources
    enricher_names = args.enrichers
    if args.enrich_only:
        source_names = []  # discovery off, enrichment only
    if args.no_enrich:
        if args.enrich_only:
            print("error: --enrich-only and --no-enrich contradict each other",
                  file=sys.stderr)
            return 2
        enricher_names = []

    try:
        pipeline = Pipeline(
            config,
            localities=localities,
            categories=categories,
            source_names=source_names,
            enricher_names=enricher_names,
            headless=bool(config.get("runtime.headless", True)),
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    stats = pipeline.run()
    return 0 if stats.combos_done or args.dry_run or args.enrich_only else 1


if __name__ == "__main__":
    sys.exit(main())
