"""``python -m leadscraper <command>`` -- the command line over the same core
the future API uses (nothing business-critical lives here).

Commands:
  run              scrape / enrich / export (same flags as ``python run.py``)
  migrate-legacy   import an original SQLite lead store (a copy) into a project
  config           import-legacy | validate | schema
  presets          list the shipped presets
  db               upgrade | info
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
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        return _cmd_run(argv[1:])
    args = build_parser().parse_args(argv)
    handlers = {"migrate-legacy": _cmd_migrate, "config": _cmd_config,
                "presets": _cmd_presets, "db": _cmd_db}
    if args.command not in handlers:
        build_parser().print_help()
        return 2
    return handlers[args.command](args)
