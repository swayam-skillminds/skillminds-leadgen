#!/usr/bin/env python3
"""Write the sample workbooks in docs/samples/ from fixture data (never real leads).

Each scenario of ``tests/workbook_fixtures.py`` -- local_business only, a
three-lead-type mix, venues (the hospitality vertical), an empty run, Phase 6's
digital_audit + hiring, its funding_news + decision_makers, Phase 7's tenders +
registrations and Phase 8's tech_stack + app_store -- is
built into a throwaway database and exported exactly as a run's workbook is.
``tests/test_workbook.py`` pins the same workbooks' structure, and
``test_samples_are_current`` fails when a sample here is out of date.

    .venv/bin/python tools/build_sample_workbooks.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "samples"
NAMES = {"local_business": "sample-local-business.xlsx", "mix": "sample-three-lead-types.xlsx",
         "venues": "sample-venues.xlsx", "empty": "sample-empty-run.xlsx",
         "audit_hiring": "sample-audit-and-hiring.xlsx",
         "news_people": "sample-news-and-people.xlsx",
         "tenders_registrations": "sample-tenders-and-registrations.xlsx",
         "techstack_appstore": "sample-tech-stack-and-app-store.xlsx"}


def main() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        # Never the real per-user database: everything lives in the scratch folder.
        os.environ["LEADSCRAPER_DATA_DIR"] = scratch
        sys.path.insert(0, str(ROOT / "tests"))
        sys.path.insert(0, str(ROOT))
        import workbook_fixtures as wf
        from leadscraper.db.engine import open_database
        from leadscraper.services import exports
        from leadscraper.services.workspace import default_workspace

        OUT.mkdir(parents=True, exist_ok=True)
        for scenario in wf.SCENARIOS:
            engine = open_database(f"sqlite:///{Path(scratch) / scenario}.sqlite3")
            try:
                run_id = wf.build(engine, scenario)
                _, content, table = exports.export(engine, default_workspace(engine), "xlsx",
                                                   run_id=run_id, generated_at=wf.FIXED_NOW)
            finally:
                engine.dispose()
                wf.unregister_demo_lead_types()
            path = OUT / NAMES[scenario]
            path.write_bytes(content)
            print(f"wrote {path.relative_to(ROOT)}  ({len(table.records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
