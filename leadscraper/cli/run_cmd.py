"""``leadscraper run`` (and ``python run.py``): scrape, enrich, rank, export.

Examples
--------
  # See what a full run would do, without making a single request
  python run.py --dry-run

  # A preset (or any run config), one slice, fast feedback
  python run.py --config presets/mumbai-event-vendors.yaml --locality Andheri \\
      --category Caterers --source google_maps

  # Re-rank and re-export what's already in the project, no scraping
  python run.py --export-only

  # Fill in contact details for leads already discovered
  python run.py --enrich-only

A legacy config.yaml works as before: it runs as the preset it imports to,
in its own project ("Mumbai event vendors", "Nashik venues"). Its records
live in the application database; import the old store once with
``python -m leadscraper migrate-legacy --config <file>``.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import Config
from ..db.legacy_import import is_legacy_store
from ..db.store import LeadStore, store_target
from ..export.csv_writer import write_csv
from ..export.sheets import SheetsWriter
from ..leadtypes.local_business.scoring import Scorer
from ..sources.registry import DISCOVERY_SOURCES, ENRICHMENT_SOURCES


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Find B2B leads from free public sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=None,
                        help="A run config / preset, or a legacy config.yaml "
                             "(default: config.yaml)")

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
                           help="Rescore + re-export the existing project, no scraping")
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
    behaviour.add_argument("--allow-empty-project", action="store_true",
                           help="Start an empty project even though a legacy store for "
                                "this config exists and has not been migrated")

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
    for noisy in ("urllib3", "websockets", "asyncio", "charset_normalizer", "alembic"):
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


def unmigrated_legacy_store(config: Config):
    """The config's original lead store, when it exists and is still in the old format."""
    if not config.get("runtime.db_path"):
        return None
    path = config.path_for("db_path", "data/leads.sqlite3")
    return path if is_legacy_store(path) else None


def migration_hint(config: Config) -> str:
    source = config.path.name if config.path else "<config>"
    return (f"This project is empty, but the legacy store "
            f"{unmigrated_legacy_store(config)} holds this config's leads.\n"
            f"Import a copy of it once (the original is never modified):\n"
            f"  python -m leadscraper migrate-legacy --config {source}")


def open_project(config: Config) -> LeadStore:
    from ..db.store import DedupOptions

    target, kwargs = store_target(config)
    return LeadStore(target, Scorer(config), DedupOptions.from_config(config), **kwargs)


def export_only(config: Config) -> int:
    """Rescore and re-export whatever is already in the project."""
    config.apply_name_noise()
    store = open_project(config)
    try:
        if store.count() == 0:
            if unmigrated_legacy_store(config):
                print(migration_hint(config))
            else:
                print("Nothing to export: the project is empty. Run a scrape first.")
            return 1
        store.rescore_all()
        leads = store.sorted_leads()
        csv_path = config.path_for("csv_export", "data/leads.csv")
        write_csv(leads, csv_path, config.profile, config.currency)
        print(f"Re-scored and exported {len(leads)} leads of '{store.project_name}' -> {csv_path}")

        sheets = SheetsWriter(config)
        if sheets.sync(leads):
            print(f"Synced to {sheets.url}")
        elif sheets.unavailable_reason:
            print(f"Sheets not written: {sheets.unavailable_reason}")
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    from ..engine.runner import RunRequest, execute

    args = build_parser(prog).parse_args(argv)

    try:
        config = Config.load(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:      # schema validation
        print(f"error: {args.config or 'config.yaml'} is not a valid run config:\n{exc}",
              file=sys.stderr)
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
    for given, registry in ((source_names, DISCOVERY_SOURCES),
                            (enricher_names, ENRICHMENT_SOURCES)):
        unknown = {n.strip().lower() for n in (given or [])} - set(registry) - {"all"}
        if unknown:
            print(f"error: Unknown source(s): {sorted(unknown)}. Available: {sorted(registry)}",
                  file=sys.stderr)
            return 2

    if not args.dry_run and not args.allow_empty_project and unmigrated_legacy_store(config):
        config.apply_name_noise()
        store = open_project(config)
        empty = store.count() == 0
        store.close()
        if empty:
            print(migration_hint(config) + "\nor pass --allow-empty-project to start afresh.",
                  file=sys.stderr)
            return 2

    outcome = execute(RunRequest(
        config=config, localities=localities, categories=categories,
        source_names=source_names, enricher_names=enricher_names, dry_run=args.dry_run,
    ))
    local = outcome.results.get("local_business")
    if local is None or local.failed:
        return 1
    return 0 if (local.stats.get("combos_done") or args.dry_run or args.enrich_only) else 1
