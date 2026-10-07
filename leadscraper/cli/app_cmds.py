"""Phase 3 CLI commands: the same services the HTTP API uses.

  api          serve the HTTP API (uvicorn)
  status       health checks, active and queued runs
  export       a run's or project's results as XLSX / CSV / JSON
  bundle       export | import -- move configs, projects, leads between machines
  projects     list
  suppression  list | create | import
  schedules    list | create | delete | run
  purge        apply the retention settings now
  secrets      set | delete | check -- secrets in the OS keychain

``config save|list|show|versions|export`` and ``runs rerun|diff|failures``
extend the existing ``config`` and ``runs`` commands (``cli/__init__.py``).
Every command takes ``--db`` (default: ``LEADSCRAPER_DB_URL`` or the
per-user database) and prints a short, human summary; ``--json`` where a
script may want the data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..paths import default_db_url


def _open(args: argparse.Namespace):
    from ..db.engine import open_database
    from ..services.workspace import default_workspace

    engine = open_database(getattr(args, "db", None) or default_db_url())
    return engine, default_workspace(engine)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=1, default=str, ensure_ascii=False))


def _service_call(fn):
    """Run a command; a service error becomes a message and exit code 2."""
    from ..services.errors import ServiceError

    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except ServiceError as exc:
            print(f"error: {exc.detail}", file=sys.stderr)
            for error in exc.errors[:10]:
                where = ".".join(str(p) for p in error.get("loc") or [])
                print(f"  {where}: {error.get('msg')}", file=sys.stderr)
            return 2
    return wrapper


# --- api, status ------------------------------------------------------------------------------

def cmd_api(args: argparse.Namespace) -> int:
    from ..api import create_app

    if args.openapi:
        spec = create_app(db_url=args.db).openapi()
        Path(args.openapi).write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.openapi} ({len(spec['paths'])} paths)")
        return 0
    import uvicorn

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: serving on {args.host} without authentication -- keep it on a "
              "private network or behind a proxy with access control (docs/deploy.md)",
              file=sys.stderr)
    uvicorn.run(create_app(db_url=args.db), host=args.host, port=args.port,
                log_level="debug" if args.verbose else "info")
    return 0


@_service_call
def cmd_status(args: argparse.Namespace) -> int:
    from ..services import health, runs

    engine, ws = _open(args)
    try:
        report = health.health(engine)
        active = runs.list_runs(engine, ws, status=["queued", "running", "pausing", "paused",
                                                    "stopping"], limit=20)
    finally:
        engine.dispose()
    if args.json:
        _print_json({"health": report, "runs": active.items})
        return 0
    print(f"{report['status'].upper()}  (version {report['version']}, {report['mode']} mode)")
    for name, check in report["checks"].items():
        print(f"  {name:<9} {check['status']:<5} {check['detail']}")
    print(f"\nActive and queued runs: {active.total}")
    for run in active.items:
        print(f"  {run['id']:>5}  {run['status']:<9} {run['kind']:<8} {run['project']}  "
              f"{run['units_done'] or 0}/{run['units_total'] or '?'} units")
    return 0 if report["status"] != "down" else 1


# --- export -------------------------------------------------------------------------------------

@_service_call
def cmd_export(args: argparse.Namespace) -> int:
    from ..db.runs import add_run_file
    from ..services import exports

    if (args.run is None) == (args.project is None):
        print("error: give --run or --project", file=sys.stderr)
        return 2
    engine, ws = _open(args)
    try:
        filename, content, table = exports.export(engine, ws, args.format, run_id=args.run,
                                                  project_id=args.project, scope=args.scope,
                                                  against=args.against)
        path = Path(args.output) if args.output else Path.cwd() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if args.run is not None:
            add_run_file(engine, args.run, kind=f"export_{args.format}", path=str(path),
                         rows=len(table.records))
    finally:
        engine.dispose()
    excluded = f", {table.excluded} excluded by suppression lists" if table.excluded else ""
    print(f"wrote {len(table.records)} organisations ({table.scope}{excluded}) -> {path}")
    return 0


# --- bundles ------------------------------------------------------------------------------------

@_service_call
def cmd_bundle(args: argparse.Namespace) -> int:
    from ..services import bundle

    engine, ws = _open(args)
    try:
        if args.bundle_cmd == "export":
            data = bundle.export_bundle(engine, ws, project_ids=args.project or None,
                                        include_leads=args.leads, include_history=args.history)
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            counts = bundle.read_manifest(data)["counts"]
            summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items())
            print(f"wrote {path} ({len(data) / 1024:.0f} KB): {summary}")
            return 0
        report = bundle.import_bundle(engine, ws, Path(args.file).read_bytes(),
                                      leads=not args.no_leads, history=not args.no_history)
    finally:
        engine.dispose()
    if args.json:
        _print_json(report)
        return 0
    made = report["manifest"].get("created_on", {}).get("os", "?")
    print(f"imported a bundle made on {made} by version {report['manifest']['app_version']}")
    for key in ("saved_configs", "presets", "suppression_lists", "projects", "schedules"):
        print(f"  {key.replace('_', ' '):<18} {report[key]}")
    for slug, result in report["organizations"].items():
        print(f"  organisations      {slug}: {result}")
    print(f"  runs               {report['runs']}")
    return 0


# --- projects, suppression, schedules -----------------------------------------------------------

@_service_call
def cmd_projects(args: argparse.Namespace) -> int:
    from ..services import projects

    engine, ws = _open(args)
    try:
        page = projects.list_projects(engine, ws, sort=args.sort, limit=500)
    finally:
        engine.dispose()
    if args.json:
        _print_json(page.items)
        return 0
    for p in page.items:
        retention = f", keeps {p['retention_days']} days" if p["retention_days"] else ""
        print(f"{p['id']:>4}  {p['name']:<36} {p['organizations']:>6} organisations  "
              f"{p['runs']:>3} runs{retention}")
    return 0


@_service_call
def cmd_suppression(args: argparse.Namespace) -> int:
    from ..services import suppression

    engine, ws = _open(args)
    try:
        if args.sup_cmd == "list":
            for item in suppression.list_lists(engine, ws, limit=500).items:
                print(f"{item['id']:>4}  {item['name']:<32} {item['mode']:<8} "
                      f"{item['entries']} entries")
            return 0
        if args.sup_cmd == "create":
            item = suppression.create_list(engine, ws, name=args.name, mode=args.mode)
            print(f"created list {item['id']}: {item['name']} ({item['mode']})")
            return 0
        listing = suppression.find_list(engine, ws, args.list)
        if listing is None and args.list.isdigit():
            listing = suppression.get_list(engine, ws, int(args.list))
        if listing is None:
            print(f"error: no suppression list {args.list!r}", file=sys.stderr)
            return 2
        path = Path(args.file)
        result = suppression.import_file(engine, ws, listing["id"], path.read_bytes(), path.name)
        print(f"{listing['name']}: read {result['read']}, added {result['added']}, "
              f"skipped {result['skipped']}")
        return 0
    finally:
        engine.dispose()


@_service_call
def cmd_schedules(args: argparse.Namespace) -> int:
    from ..services import schedules

    engine, ws = _open(args)
    try:
        if args.sched_cmd == "list":
            for item in schedules.list_schedules(engine, ws, limit=500).items:
                state = "on " if item["enabled"] else "off"
                print(f"{item['id']:>4}  {state} {item['cron']:<16} {item['timezone']:<16} "
                      f"{item['config_name']}  next {item['next_run_at']}"
                      + ("  (only new)" if item["only_new"] else ""))
            return 0
        if args.sched_cmd == "create":
            overrides = {k: v for k, v in (("depth", args.depth),
                                           ("time_budget_minutes", args.time_budget)) if v}
            item = schedules.create_schedule(engine, ws, saved_config_id=args.config,
                                             cron=args.cron, timezone=args.timezone,
                                             name=args.name or "", only_new=args.only_new,
                                             overrides=overrides)
            print(f"schedule {item['id']}: {item['cron']} ({item['timezone']}), next "
                  f"{item['next_run_at']}; a running worker queues each run")
            return 0
        if args.sched_cmd == "delete":
            schedules.delete_schedule(engine, ws, args.id)
            print(f"deleted schedule {args.id}")
            return 0
        run_id = schedules.run_now(engine, ws, args.id)
        print(f"queued run {run_id}")
        return 0
    finally:
        engine.dispose()


@_service_call
def cmd_purge(args: argparse.Namespace) -> int:
    from ..services import retention

    engine, ws = _open(args)
    try:
        report = retention.purge(engine, ws, dry_run=args.dry_run)
    finally:
        engine.dispose()
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb}: {report['events']} events, {report['seen_urls']} URL memos, "
          f"{report['log_files']} log files"
          + "".join(f", {n} organisations of {p}" for p, n in report["organizations"].items()))
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    import getpass

    from ..core import secrets

    if args.secret_cmd == "set":
        value = getpass.getpass(f"value for {args.name} (not echoed): ")
        try:
            secrets.set_secret(args.name, value)
        except (RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"stored {args.name} in the OS keychain")
        return 0
    if args.secret_cmd == "delete":
        print("deleted" if secrets.delete_secret(args.name) else "not in the keychain")
        return 0
    source = secrets.secret_source(args.name)
    print(f"{args.name}: {'configured (' + source + ')' if source else 'not configured'}")
    return 0 if source else 1


# --- saved configs, run history (extensions of `config` / `runs`) ------------------------------

@_service_call
def cmd_saved_config(args: argparse.Namespace) -> int:
    from ..services import configs

    engine, ws = _open(args)
    try:
        if args.config_cmd == "save":
            path = Path(args.file)
            saved = configs.import_config(engine, ws, path.read_bytes(), filename=path.name,
                                          name=args.name)
            print(f"saved config {saved['id']}: {saved['name']} v{saved['version']} "
                  f"({saved['versions']} version(s))")
            return 0
        if args.config_cmd == "list":
            for item in configs.list_configs(engine, ws, limit=500).items:
                print(f"{item['id']:>4}  {item['name']:<36} v{item['version']:<3} "
                      f"project {item['project']}")
            return 0
        if args.config_cmd == "versions":
            for v in configs.list_versions(engine, ws, args.id):
                print(f"v{v['version']:<3} {v['created_at']:%Y-%m-%d %H:%M}  {v['description']}")
            return 0
        if args.config_cmd == "show":
            _print_json(configs.get_config(engine, ws, args.id, args.version))
            return 0
        _, text = configs.export_config(engine, ws, args.id, version=args.version,
                                        fmt=args.format)
    finally:
        engine.dispose()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


@_service_call
def cmd_run_history(args: argparse.Namespace) -> int:
    from ..services import runs

    engine, ws = _open(args)
    try:
        if args.runs_cmd == "rerun":
            overrides = {k: v for k, v in (("depth", args.depth),
                                           ("time_budget_minutes", args.time_budget)) if v}
            run = runs.rerun(engine, ws, args.run_id, overrides)
            print(f"queued run {run['id']} (a re-run of {args.run_id}); start a worker to "
                  "execute it: python -m leadscraper worker")
            return 0
        if args.runs_cmd == "diff":
            result = runs.diff_runs(engine, ws, args.run_id, against=args.against,
                                    change=args.change, limit=500)
            if args.json:
                _print_json(result)
                return 0
            counts = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in result["counts"].items())
            print(f"run {args.run_id} against {result['against'] or '(nothing earlier)'}: "
                  f"{counts}")
            for item in result["items"][:args.limit]:
                print(f"  {item['change']:<16} {item['name']}  ({item['locality'] or '-'})")
            return 0
        page = runs.list_failures(engine, ws, args.run_id, source=args.source,
                                  reason=args.reason, limit=500)
    finally:
        engine.dispose()
    if args.json:
        _print_json(page.items)
        return 0
    print(f"{page.total} failure(s)")
    for f in page.items[:args.limit]:
        print(f"  {f['source']:<14} {f['reason']:<22} {f['url'] or f['query'] or ''}")
    return 0


# --- parsers ------------------------------------------------------------------------------------

def _db(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--db", help="database URL (default: LEADSCRAPER_DB_URL or per-user)")
    return parser


def add_parsers(sub) -> dict:
    """Register the Phase 3 commands; returns {command: handler}."""
    api = _db(sub.add_parser("api", help="serve the HTTP API (docs at /api/v1/docs)"))
    api.add_argument("--host", default="127.0.0.1",
                     help="interface (default 127.0.0.1; 0.0.0.0 in a container)")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("-v", "--verbose", action="store_true")
    api.add_argument("--openapi", metavar="FILE",
                     help="write the OpenAPI document to FILE and exit (client generation)")

    status = _db(sub.add_parser("status", help="health checks, active and queued runs"))
    status.add_argument("--json", action="store_true")

    export = _db(sub.add_parser("export", help="a run's or project's results as a file"))
    export.add_argument("--run", type=int)
    export.add_argument("--project", type=int)
    export.add_argument("--format", choices=["xlsx", "csv", "json"], default="xlsx")
    export.add_argument("--scope", choices=["run", "new", "project"])
    export.add_argument("--against", type=int, help="with --scope new: compare with this run")
    export.add_argument("-o", "--output")

    bundle = sub.add_parser("bundle", help="export | import a bundle (configs, projects, leads)")
    bundle_sub = bundle.add_subparsers(dest="bundle_cmd", required=True)
    out = _db(bundle_sub.add_parser("export", help="write a bundle zip (never secrets)"))
    out.add_argument("-o", "--output", required=True)
    out.add_argument("--leads", action="store_true", help="include organisations")
    out.add_argument("--history", action="store_true", help="include run history")
    out.add_argument("--project", type=int, action="append", help="only these projects")
    imp = _db(bundle_sub.add_parser("import", help="import a bundle zip (version-checked)"))
    imp.add_argument("file")
    imp.add_argument("--no-leads", action="store_true")
    imp.add_argument("--no-history", action="store_true")
    imp.add_argument("--json", action="store_true")

    projects = _db(sub.add_parser("projects", help="list projects"))
    projects.add_argument("--sort", default="name")
    projects.add_argument("--json", action="store_true")

    sup = sub.add_parser("suppression", help="suppression lists: list | create | import")
    sup_sub = sup.add_subparsers(dest="sup_cmd", required=True)
    _db(sup_sub.add_parser("list"))
    create = _db(sup_sub.add_parser("create"))
    create.add_argument("name")
    create.add_argument("--mode", choices=["exclude", "flag"], default="exclude")
    load = _db(sup_sub.add_parser("import", help="names / domains / phones from CSV or XLSX"))
    load.add_argument("list", help="list name or id")
    load.add_argument("file")

    sched = sub.add_parser("schedules", help="recurring runs: list | create | delete | run")
    sched_sub = sched.add_subparsers(dest="sched_cmd", required=True)
    _db(sched_sub.add_parser("list"))
    new = _db(sched_sub.add_parser("create"))
    new.add_argument("--config", type=int, required=True, help="saved config id")
    new.add_argument("--cron", required=True, help="e.g. '0 6 1 * *' or @weekly")
    new.add_argument("--timezone", default="UTC")
    new.add_argument("--name")
    new.add_argument("--only-new", action="store_true")
    new.add_argument("--depth", choices=["quick", "balanced", "deep"])
    new.add_argument("--time-budget", type=float)
    for name in ("delete", "run"):
        _db(sched_sub.add_parser(name)).add_argument("id", type=int)

    purge = _db(sub.add_parser("purge", help="apply the workspace's retention settings now"))
    purge.add_argument("--dry-run", action="store_true")

    secret = sub.add_parser("secrets", help="secrets in the OS keychain: set | delete | check")
    secret_sub = secret.add_subparsers(dest="secret_cmd", required=True)
    for name in ("set", "delete", "check"):
        secret_sub.add_parser(name).add_argument("name")

    return {"api": cmd_api, "status": cmd_status, "export": cmd_export, "bundle": cmd_bundle,
            "projects": cmd_projects, "suppression": cmd_suppression,
            "schedules": cmd_schedules, "purge": cmd_purge, "secrets": cmd_secrets}


def add_config_parsers(config_sub) -> None:
    """``config save | list | show | versions | export`` -- saved configs in the database."""
    save = _db(config_sub.add_parser("save", help="save a YAML/JSON (or legacy) config; an "
                                                  "existing name gets a new version"))
    save.add_argument("file")
    save.add_argument("--name")
    _db(config_sub.add_parser("list", help="saved configs"))
    show = _db(config_sub.add_parser("show", help="a saved config as JSON"))
    show.add_argument("id", type=int)
    show.add_argument("--version", type=int)
    _db(config_sub.add_parser("versions", help="a saved config's versions")).add_argument(
        "id", type=int)
    export = _db(config_sub.add_parser("export", help="a saved config as YAML/JSON"))
    export.add_argument("id", type=int)
    export.add_argument("--version", type=int)
    export.add_argument("--format", choices=["yaml", "json"], default="yaml")
    export.add_argument("-o", "--output")


def add_runs_parsers(runs_sub) -> None:
    """``runs rerun | diff | failures``."""
    rerun = _db(runs_sub.add_parser("rerun", help="queue a re-run (same config snapshot)"))
    rerun.add_argument("run_id", type=int)
    rerun.add_argument("--depth", choices=["quick", "balanced", "deep"])
    rerun.add_argument("--time-budget", type=float)
    diff = _db(runs_sub.add_parser("diff", help="new / updated / unchanged vs. another run"))
    diff.add_argument("run_id", type=int)
    diff.add_argument("--against", type=int, help="default: the previous run of the project")
    diff.add_argument("--change", choices=["new", "updated", "unchanged", "only_in_against"])
    diff.add_argument("--limit", type=int, default=50)
    diff.add_argument("--json", action="store_true")
    failures = _db(runs_sub.add_parser("failures", help="what a run could not fetch or parse"))
    failures.add_argument("run_id", type=int)
    failures.add_argument("--source")
    failures.add_argument("--reason")
    failures.add_argument("--limit", type=int, default=50)
    failures.add_argument("--json", action="store_true")


SAVED_CONFIG_COMMANDS = frozenset({"save", "list", "show", "versions", "export"})
RUN_HISTORY_COMMANDS = frozenset({"rerun", "diff", "failures"})
