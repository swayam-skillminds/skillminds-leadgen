"""Freeze the real-data characterisation baseline.

    python tests/characterisation/freeze.py                 # copy live DBs + snapshot
    python tests/characterisation/freeze.py --snapshot-only # re-snapshot the existing baseline

Copies data/leads.sqlite3 and data/nashik/venues.sqlite3 (read-only, via the
sqlite backup API) into data/characterisation/, then writes the committed
snapshots golden/real_<profile>.json. An existing baseline is never replaced
without --force: Phase 1 is measured against it.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from snapshot_lib import (  # noqa: E402
    GOLDEN_DIR, PROFILES, PROJECT_ROOT, baseline_dir, copy_db, real_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--snapshot-only", action="store_true",
                        help="keep the frozen DBs; only regenerate golden/real_*.json")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing frozen baseline")
    args = parser.parse_args(argv)

    target_dir = baseline_dir()
    for profile, (_, live_rel, frozen_name) in PROFILES.items():
        frozen = target_dir / frozen_name
        if not args.snapshot_only:
            live = PROJECT_ROOT / live_rel
            if not live.exists():
                print(f"[{profile}] no live DB at {live}; skipped")
                continue
            if frozen.exists() and not args.force:
                print(f"[{profile}] baseline already frozen at {frozen} (use --force to replace)")
            else:
                copy_db(live, frozen)
                print(f"[{profile}] froze {live} -> {frozen}")
        if not frozen.exists():
            print(f"[{profile}] no baseline at {frozen}; nothing to snapshot")
            continue
        with tempfile.TemporaryDirectory() as scratch:
            snapshot = real_snapshot(profile, frozen, Path(scratch))
        out = GOLDEN_DIR / f"real_{profile}.json"
        out.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        dedup = snapshot["dedup"]
        print(f"[{profile}] snapshot -> {out.relative_to(PROJECT_ROOT)}: "
              f"{snapshot['export']['lead_count']} leads exported; replay stores "
              f"{dedup['stored']} of {dedup['replayed']} "
              f"({len(dedup['merged_clusters'])} merged clusters, "
              f"{len(dedup['rejected_ids'])} rejected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
