"""``python -m leadscraper <command>`` -- the command line over the same core
the future API uses (nothing business-critical lives here).

Commands:
  run              scrape / enrich / export (same flags as ``python run.py``)
  migrate-legacy   import an original SQLite lead store (a copy) into a project
  config           import-legacy | validate | schema
  presets          list the shipped presets
  db               upgrade | info
  worker           take queued runs off the queue (and resume interrupted ones)
  runs             list | show | pause | resume | cancel | events
  estimate         how long a config would take; what fits a time budget
  preview          a ~2-minute sample run plus a full-run estimate
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from ..paths import PROJECT_ROOT, data_dir, default_db_url

PRESETS_DIR = PROJECT_ROOT / "presets"


def _cmd_run(argv: list[str]) -> int:
    from .run_cmd import main as run_main

    return run_main(argv, prog="leadscraper run")


def _cmd_migrate(args: argparse.Namespace) -> int:
    from ..config import Config
    from ..db.engine import url_for
    from ..db.legacy_import import copy_legacy_store, import_legacy_store, is_legacy_store
    from ..db.store import DedupOptions, LeadStore
    from ..leadtypes.local_business.scoring import Scorer

    config = Config.load(args.config)
    project = args.project or config.get("project")
    source = Path(args.source) if args.source else config.path_for("db_path", "data/leads.sqlite3")
    if not is_legacy_store(source):
        print(f"error: {source} is not an original lead store", file=sys.stderr)
        return 2
    db = args.db or config.get("runtime.db_url") or default_db_url()

    # The original is precious: copy it (read-only) and import only the copy.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    copy = copy_legacy_store(source, data_dir() / "legacy-copies" / f"{source.stem}-{stamp}.sqlite3")
    config.apply_name_noise()
    store = LeadStore(db, Scorer(config), DedupOptions.from_config(config), project=project,
                      country=config.country,
                      project_description=f"Imported from {source.name} on {stamp}.")
    try:
        if store.count() and not args.into_existing:
            print(f"error: project '{project}' already holds {store.count()} organisations; "
                  "pass --into-existing to merge the legacy store into it", file=sys.stderr)
            return 2
        report = import_legacy_store(copy, store, mode=args.mode)
    finally:
        store.close()
    print(report.summary())
    print(f"  source (untouched): {source}\n  copy imported:      {copy}\n"
          f"  database:           {url_for(db)}\n  project:            {project}")
    if args.report:
        Path(args.report).write_text(json.dumps({
            "mode": report.mode, "legacy_rows": report.legacy_rows, "stored": report.stored,
            "actions": report.actions, "rejected_ids": report.rejected_ids,
            "merged_clusters": report.merged_clusters, "run_id": report.run_id,
        }, indent=1), encoding="utf-8")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    import yaml

    from ..config.importer import dump_document, import_legacy_file
    from ..config.schema import RunConfig, run_config_json_schema

    if args.config_cmd == "import-legacy":
        document = import_legacy_file(args.source, name=args.name, project=args.project)
        text = dump_document(RunConfig.model_validate(document).to_document())
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            sys.stdout.write(text)
        return 0
    if args.config_cmd == "validate":
        from ..config.importer import is_legacy

        data = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
        if is_legacy(data):
            print(f"{args.file}: legacy config (no schema_version); it runs, and "
                  "`config import-legacy` turns it into a run config")
            return 0
        try:
            rc = RunConfig.model_validate(data)
        except ValueError as exc:
            print(f"{args.file}: INVALID\n{exc}")
            return 1
        print(f"{args.file}: valid run config '{rc.name}' (project '{rc.project}', "
              f"lead types: {', '.join(rc.enabled_lead_types())})")
        for note in rc.unapplied():
            print(f"  not applied yet: {note}")
        return 0
    if args.config_cmd == "schema":
        text = json.dumps(run_config_json_schema(), indent=2)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text)
        return 0
    return 2


def _cmd_presets(args: argparse.Namespace) -> int:
    import yaml

    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        geo = data.get("geography", {})
        print(f"{path.relative_to(PROJECT_ROOT)}: {data.get('name')} -- project "
              f"'{data.get('project')}', {geo.get('country')} {', '.join(geo.get('places', []))}")
    return 0


def _cmd_db(args: argparse.Namespace) -> int:
    from alembic import command
    from sqlalchemy import func, select

    from ..db.engine import alembic_config, open_database, url_for
    from ..db.models import Organization, Project

    url = url_for(args.db or default_db_url())
    if args.db_cmd == "upgrade":
        if args.sql:
            command.upgrade(alembic_config(url=url), "head", sql=True)
            return 0
        engine = open_database(url)
        engine.dispose()
        print(f"{url}: schema is current")
        return 0
    if args.db_cmd == "info":
        engine = open_database(url)
        orgs, projects = Organization.__table__, Project.__table__
        with engine.connect() as conn:
            rows = conn.execute(
                select(projects.c.name, func.count(orgs.c.id))
                .select_from(projects.outerjoin(orgs, orgs.c.project_id == projects.c.id))
                .group_by(projects.c.id, projects.c.name).order_by(projects.c.name)).all()
        engine.dispose()
        print(url)
        for name, count in rows:
            print(f"  {name}: {count} organisations")
        return 0
    return 2


def _engine(args: argparse.Namespace):
    from ..db.engine import open_database

    return open_database(getattr(args, "db", None) or default_db_url())


def _cmd_worker(args: argparse.Namespace) -> int:
    import logging

    from ..engine.keepawake import KeepAwake
    from ..engine.worker import Worker, list_workers, request_worker_stop

    if args.worker_cmd == "stop":
        engine = _engine(args)
        try:
            asked = request_worker_stop(engine, args.name)
        finally:
            engine.dispose()
        print(f"asked {asked} worker(s) to stop; each finishes its in-flight work first")
        return 0
    if args.worker_cmd == "list":
        engine = _engine(args)
        try:
            for row in list_workers(engine):
                print(f"{row['name']:<32} {row['status']:<9} pid {row['pid']:<7} "
                      f"run {row['current_run_id'] or '-':<6} heartbeat {row['heartbeat_at']}")
        finally:
            engine.dispose()
        return 0
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    for noisy in ("urllib3", "websockets", "asyncio", "charset_normalizer", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    worker = Worker(db_url=args.db, name=args.name, poll_seconds=args.poll,
                    stale_seconds=args.stale_after, heartbeat_seconds=args.heartbeat,
                    keep_awake=KeepAwake(enabled=False) if args.no_keep_awake else None)
    executed = worker.run(once=args.once, max_runs=args.max_runs)
    print(f"worker {worker.name} stopped after {executed} run(s)")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    import time as _time

    from ..db.runs import TransitionError, get_run, list_runs, request_control, unit_rows
    from ..engine.events import describe, read_events

    engine = _engine(args)
    try:
        if args.runs_cmd == "list":
            for row in list_runs(engine, statuses=args.status, limit=args.limit):
                stats = (row.get("stats") or {}).get("engine") or {}
                print(f"{row['id']:>5}  {row['status']:<10} {row['kind']:<8} "
                      f"{str(row['created_at'])[:19]}  project {row['project_id']}  "
                      f"{stats.get('units_done', '-')}/{stats.get('units_total', '-')} units  "
                      f"{row.get('worker_id') or ''}")
            return 0
        row = get_run(engine, args.run_id)
        if row is None:
            print(f"error: no run {args.run_id}", file=sys.stderr)
            return 2
        if args.runs_cmd == "show":
            engine_stats = (row.get("stats") or {}).get("engine") or {}
            units = unit_rows(engine, args.run_id)
            counts: dict[str, int] = {}
            for unit in units:
                counts[unit["status"]] = counts.get(unit["status"], 0) + 1
            print(json.dumps({
                "id": row["id"], "status": row["status"], "kind": row["kind"],
                "project_id": row["project_id"], "worker": row.get("worker_id"),
                "attempts": row.get("attempts"), "active_seconds": row.get("active_seconds"),
                "created_at": str(row["created_at"]), "finished_at": str(row["finished_at"]),
                "checkpoint": row.get("checkpoint"), "units": counts,
                "estimate": {k: (row.get("estimate") or {}).get(k)
                             for k in ("p50_seconds", "p90_seconds", "expected_new", "budget")},
                "summary": engine_stats, "error": row.get("error"),
            }, indent=1, default=str))
            return 0
        if args.runs_cmd in ("pause", "resume", "cancel"):
            try:
                status = request_control(engine, args.run_id, args.runs_cmd)
            except TransitionError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            hint = {"queued": " -- a worker will resume it from its checkpoint",
                    "pausing": " -- in-flight work finishes, then it pauses",
                    "stopping": " -- in-flight work finishes, then it stops"}.get(status, "")
            print(f"run {args.run_id}: {status}{hint}")
            return 0
        if args.runs_cmd == "events":
            after = args.after
            while True:
                batch = read_events(engine, args.run_id, after=after)
                for event in batch:
                    if args.json:
                        print(event.model_dump_json())
                    else:
                        print(f"{event.seq:>5} {event.at:%H:%M:%S} {describe(event)}")
                    after = event.seq
                if not args.follow:
                    return 0
                current = get_run(engine, args.run_id)
                if current and current["status"] in ("completed", "failed", "cancelled",
                                                     "paused") and not batch:
                    return 0
                _time.sleep(1.0)
        return 2
    finally:
        engine.dispose()


def _load_config(path: str):
    from ..config import Config
    from ..config.compile import compile_run_config
    from ..config.geography import needs_geocoding, resolve_geography

    config = Config.load(path)
    if config.run_config is not None and needs_geocoding(config.run_config):
        config = compile_run_config(resolve_geography(config.run_config), path=config.path)
    return config


def _cmd_estimate(args: argparse.Namespace) -> int:
    from ..engine.estimator import describe_estimate
    from ..engine.runner import RunRequest, execute

    config = _load_config(args.config)
    outcome = execute(RunRequest(config=config, dry_run=True, print_plan=False,
                                 install_signals=False, depth=args.depth,
                                 time_budget_minutes=args.time_budget,
                                 discovery_workers=args.workers))
    if args.json:
        print(json.dumps(outcome.estimate, indent=1, default=str))
    else:
        print(describe_estimate(outcome.estimate))
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    from ..engine.preview import preview
    from .run_cmd import print_preview

    config = _load_config(args.config)
    result = preview(config, seconds=args.seconds, area=args.area, category=args.category)
    if args.json:
        print(json.dumps(result.__dict__, indent=1, default=str))
    else:
        print_preview(result)
    return 0 if result.status in ("completed", "paused") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leadscraper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="scrape / enrich / export (see `leadscraper run --help`)",
                   add_help=False)

    migrate = sub.add_parser("migrate-legacy",
                             help="import an original lead store (from a copy) into a project")
    migrate.add_argument("--config", required=True,
                         help="the legacy config or preset whose store to import")
    migrate.add_argument("--source", help="legacy store path (default: the config's db_path)")
    migrate.add_argument("--project", help="project name (default: the config's)")
    migrate.add_argument("--mode", choices=["replay", "verbatim"], default="replay",
                         help="replay through today's dedup rules (default) or keep every row")
    migrate.add_argument("--db", help="database URL (default: LEADSCRAPER_DB_URL or the "
                                      "per-user database)")
    migrate.add_argument("--into-existing", action="store_true",
                         help="merge into a project that already has organisations")
    migrate.add_argument("--report", help="also write the import report as JSON here")

    config = sub.add_parser("config", help="run configs: import, validate, schema")
    config_sub = config.add_subparsers(dest="config_cmd", required=True)
    imp = config_sub.add_parser("import-legacy", help="legacy config.yaml -> run config (preset)")
    imp.add_argument("source")
    imp.add_argument("-o", "--output")
    imp.add_argument("--name")
    imp.add_argument("--project")
    val = config_sub.add_parser("validate", help="check a run config")
    val.add_argument("file")
    schema = config_sub.add_parser("schema", help="print the run-config JSON Schema")
    schema.add_argument("-o", "--output")

    sub.add_parser("presets", help="list shipped presets")

    db = sub.add_parser("db", help="the application database")
    db_sub = db.add_subparsers(dest="db_cmd", required=True)
    up = db_sub.add_parser("upgrade", help="create / migrate the schema")
    up.add_argument("--db")
    up.add_argument("--sql", action="store_true", help="print the SQL instead of running it")
    info = db_sub.add_parser("info", help="projects and their sizes")
    info.add_argument("--db")

    worker = sub.add_parser("worker", help="run a worker (or: worker stop | worker list)")
    worker.add_argument("worker_cmd", nargs="?", choices=["start", "stop", "list"],
                        default="start")
    worker.add_argument("--db", help="database URL (default: LEADSCRAPER_DB_URL or per-user)")
    worker.add_argument("--name", help="worker name (default: host-pid); `stop` targets it")
    worker.add_argument("--once", action="store_true",
                        help="exit when the queue is empty instead of waiting")
    worker.add_argument("--max-runs", type=int, default=None)
    worker.add_argument("--poll", type=float, default=2.0, help="seconds between queue polls")
    worker.add_argument("--heartbeat", type=float, default=5.0, help="heartbeat seconds")
    worker.add_argument("--stale-after", type=float, default=60.0,
                        help="a run without a heartbeat this long is resumed by this worker")
    worker.add_argument("--no-keep-awake", action="store_true",
                        help="let the machine sleep while runs are active")
    worker.add_argument("-v", "--verbose", action="store_true")

    runs = sub.add_parser("runs", help="run history and control")
    runs_sub = runs.add_subparsers(dest="runs_cmd", required=True)
    listing = runs_sub.add_parser("list", help="recent runs")
    listing.add_argument("--status", action="append")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--db")
    for name, text in (("show", "one run: status, units, estimate vs actual"),
                       ("pause", "pause (in-flight work finishes, then it flushes)"),
                       ("resume", "queue a paused or failed run to resume from its checkpoint"),
                       ("cancel", "cancel (in-flight work finishes, then it flushes)")):
        cmd = runs_sub.add_parser(name, help=text)
        cmd.add_argument("run_id", type=int)
        cmd.add_argument("--db")
    events = runs_sub.add_parser("events", help="a run's event stream (replay, or --follow)")
    events.add_argument("run_id", type=int)
    events.add_argument("--after", type=int, default=0, help="only events after this seq")
    events.add_argument("--follow", action="store_true", help="keep printing new events")
    events.add_argument("--json", action="store_true")
    events.add_argument("--db")

    estimate = sub.add_parser("estimate", help="estimate a run config without running it")
    estimate.add_argument("--config", required=True)
    estimate.add_argument("--time-budget", type=float, default=None, metavar="MINUTES")
    estimate.add_argument("--depth", choices=["quick", "balanced", "deep"])
    estimate.add_argument("--workers", type=int, default=None)
    estimate.add_argument("--json", action="store_true")

    preview = sub.add_parser("preview", help="a ~2-minute sample run plus a full-run estimate")
    preview.add_argument("--config", required=True)
    preview.add_argument("--seconds", type=float, default=120)
    preview.add_argument("--area")
    preview.add_argument("--category")
    preview.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        return _cmd_run(argv[1:])
    args = build_parser().parse_args(argv)
    handlers = {"migrate-legacy": _cmd_migrate, "config": _cmd_config,
                "presets": _cmd_presets, "db": _cmd_db, "worker": _cmd_worker,
                "runs": _cmd_runs, "estimate": _cmd_estimate, "preview": _cmd_preview}
    if args.command not in handlers:
        build_parser().print_help()
        return 2
    return handlers[args.command](args)
