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

  # The best leads obtainable in 15 minutes, at quick depth
  python run.py --config presets/nashik-venues.yaml --time-budget 15 --depth quick

  # Hand the run to a worker (python -m leadscraper worker), or resume one
  python run.py --config my-run.yaml --queue
  python run.py --resume 42

Ctrl-C pauses: the units in flight finish, everything is flushed, and the run
can be resumed from its checkpoint. A second Ctrl-C stops at once.

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
from ..db.store import LeadStore, slugify_project, store_target
from ..export.csv_writer import write_csv
from ..export.outputs import wants_csv
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
    engine = parser.add_argument_group("engine (time budget, depth, queue)")
    engine.add_argument("--time-budget", type=float, default=None, metavar="MINUTES",
                        help="Stop starting new work so the run ends within this many minutes "
                             "(+ a reserved tail for flush/score/export); best units first")
    engine.add_argument("--depth", choices=["quick", "balanced", "deep"], default=None,
                        help="Per-unit thoroughness (default: the config's, else balanced)")
    engine.add_argument("--workers", type=int, default=None, metavar="N",
                        help="Discovery units run in parallel across hosts (default 3)")
    engine.add_argument("--force-enrich", action="store_true",
                        help="Re-enrich leads an enricher already went through on earlier runs")
    engine.add_argument("--resume", type=int, default=None, metavar="RUN_ID",
                        help="Resume a paused / interrupted / failed run from its checkpoint")
    engine.add_argument("--queue", action="store_true",
                        help="Queue the run for a worker (`leadscraper worker`) and exit")
    engine.add_argument("--preview", action="store_true",
                        help="A ~2-minute sample (one area x one category x top sources) "
                             "plus a full-run estimate, in a scratch project")

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


def write_project_outputs(store: LeadStore, config: Config) -> list[str]:
    """The project's workbook (and JSON) per ``output.*``, from the database. Never raises.

    Scored with *this* config, so a weights or tiers change needs only --export-only.
    """
    from ..export.outputs import output_dir, run_formats
    from ..services import exports
    from ..services.workspace import Workspace

    formats = run_formats(config)
    if not formats:
        return []
    try:
        table = exports.build_table(store.engine, Workspace(store.workspace_id, ""),
                                    project_id=store.project_id, config=config)
    except Exception as exc:
        print(f"workbook not written: {exc}", file=sys.stderr)
        return []
    written = exports.write_files(table, formats, output_dir(config),
                                  name=slugify_project(store.project_name))
    return [path for _, path in written]


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
        if wants_csv(config):
            csv_path = config.path_for("csv_export", "data/leads.csv")
            write_csv(leads, csv_path, config.profile, config.currency)
            print(f"Re-scored and exported {len(leads)} leads of '{store.project_name}' "
                  f"-> {csv_path}")
        for path in write_project_outputs(store, config):
            print(f"Wrote {path}")

        sheets = SheetsWriter(config)
        if sheets.sync(leads):
            print(f"Synced to {sheets.url}")
        elif sheets.unavailable_reason:
            print(f"Sheets not written: {sheets.unavailable_reason}")
        return 0
    finally:
        store.close()


def resume(args: argparse.Namespace) -> int:
    """`--resume RUN_ID`: continue a stored run in this process."""
    from ..db.engine import open_database
    from ..db.runs import get_run
    from ..engine.runner import RunBusyError, execute, request_from_row
    from ..paths import default_db_url

    engine = open_database(default_db_url())
    try:
        row = get_run(engine, args.resume)
    finally:
        engine.dispose()
    if row is None:
        print(f"error: no run {args.resume}", file=sys.stderr)
        return 2
    if row["status"] in ("completed", "cancelled"):
        print(f"error: run {args.resume} is {row['status']}; only paused, interrupted or "
              "failed runs resume", file=sys.stderr)
        return 2
    request = request_from_row(row, install_signals=True)
    setup_logging(request.config, verbose=args.verbose, quiet=args.quiet)
    print(f"Resuming run {args.resume} ({row['status']}) of project "
          f"'{request.config.get('project')}' from its checkpoint")
    try:
        # The claim is atomic: a run another live executor holds is refused.
        outcome = execute(request)
    except RunBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report_outcome(outcome)
    return 0 if outcome.status in ("completed", "paused") else 1


def report_outcome(outcome) -> None:
    """What the engine adds to the pipeline's report: status, budget, skips."""
    if outcome.run_id is None:
        return
    print(f"Run {outcome.run_id}: {outcome.status} after {outcome.elapsed_seconds / 60:.1f} min"
          + (f" -- {outcome.error}" if outcome.error else ""))
    est = outcome.estimate or {}
    span = est.get("budget") or est
    if span.get("p50_seconds") is not None:
        print(f"  estimated {span['p50_seconds'] / 60:.1f}-{span['p90_seconds'] / 60:.1f} min "
              "(p50-p90)")
    if outcome.skipped:
        reasons: dict[str, int] = {}
        for _key, reason in outcome.skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        print(f"  skipped units: {len(outcome.skipped)} "
              f"({'; '.join(f'{n} {r}' for r, n in reasons.items())})")
        for key, reason in outcome.skipped[:15]:
            print(f"    {key}  -- {reason}")
        if len(outcome.skipped) > 15:
            print(f"    ... and {len(outcome.skipped) - 15} more "
                  f"(leadscraper runs show {outcome.run_id})")
    for path in outcome.files:
        print(f"  wrote {path}")
    if outcome.status == "paused":
        print(f"  resume with: python -m leadscraper runs resume {outcome.run_id}"
              f"  (or: python run.py --resume {outcome.run_id})")


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    from ..engine.runner import RunRequest, execute

    args = build_parser(prog).parse_args(argv)
    if args.resume is not None:
        return resume(args)

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
    if config.run_config is not None and not (args.dry_run or args.export_only):
        from ..config.compile import compile_run_config
        from ..config.geography import needs_geocoding, resolve_geography

        if needs_geocoding(config.run_config):
            # A place name is enough: centre and state come from the geocoder.
            config = compile_run_config(resolve_geography(config.run_config), path=config.path)
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

    if args.preview:
        from ..engine.preview import preview

        result = preview(config)
        print_preview(result)
        return 0 if result.status in ("completed", "paused") else 1

    request = RunRequest(
        config=config, localities=localities, categories=categories,
        source_names=source_names, enricher_names=enricher_names, dry_run=args.dry_run,
        depth=args.depth, time_budget_minutes=args.time_budget, force_enrich=args.force_enrich,
        discovery_workers=args.workers,
    )
    if args.queue:
        from ..engine.worker import submit_run

        run_id = submit_run(request)
        print(f"Queued run {run_id} for project '{config.get('project')}'. Start a worker to "
              f"execute it:\n  python -m leadscraper worker\n"
              f"and follow it with:\n  python -m leadscraper runs events {run_id} --follow")
        return 0
    outcome = execute(request)
    report_outcome(outcome)
    if outcome.status == "paused":
        return 0
    local = outcome.results.get("local_business")
    if local is None:               # a run of other lead types only: its own outcome decides
        return 0 if outcome.status == "completed" else 1
    if local.failed:
        return 1
    return 0 if (local.stats.get("combos_done") or args.dry_run or args.enrich_only
                 or outcome.skipped) else 1


def print_preview(result) -> None:
    from ..engine.estimator import describe_estimate

    print(f"\nPREVIEW ({result.status}, {result.seconds:.0f}s): {result.area} x {result.category}"
          f" via {', '.join(result.sources) or 'no source'}")
    if result.records:
        for record in result.records:
            print(f"  {str(record.get('Score', '')):>5}  {str(record.get('Business Name'))[:40]:<40}"
                  f"  {str(record.get('Phones') or '')[:22]:<22} {str(record.get('Website') or '')[:30]}")
    else:
        print("  (no records found in the preview window)")
    print("\nFull run estimate:")
    print(describe_estimate(result.estimate))
    calibrated = result.estimate.get("calibrated")
    if calibrated:
        print(f"Calibrated by the preview (x{calibrated['factor']}): "
              f"{calibrated['p50_seconds'] / 60:.1f}-{calibrated['p90_seconds'] / 60:.1f} min")
