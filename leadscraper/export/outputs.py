"""Which files a run writes, and where (``output.*`` in the run config; Phase 4).

* ``output.excel`` (default on) -- the deliverable workbook, Overview + Records;
* ``output.json`` -- the same records as JSON;
* ``output.csv`` -- the run's legacy-layout CSV (``runtime.csv_export``) and its
  ``skipped.csv``, written as the run goes (the Mumbai and Nashik presets keep
  it on). The Records-layout CSV is an export (API / CLI), not a run file.

A compiled config without an ``output`` section (a bare dict, a pre-schema
YAML) keeps the behaviour it always had: the CSV is written, and so now is
the workbook.

Files go next to the CSV (``runtime.csv_export``'s folder: the project's own
folder for a new config), or to ``runtime.export_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: run-file format -> ``output`` key
RUN_FORMATS = (("xlsx", "excel"), ("json", "json"))


def _output(config: Any) -> dict[str, Any] | None:
    output = config.get("output") if config is not None else None
    return output if isinstance(output, dict) else None


def wants_csv(config: Any) -> bool:
    output = _output(config)
    return True if output is None else bool(output.get("csv"))


def run_formats(config: Any) -> list[str]:
    """The export formats a finished run writes as files."""
    output = _output(config)
    if output is None:
        return ["xlsx"]
    return [fmt for fmt, key in RUN_FORMATS if output.get(key, key == "excel")]


def output_dir(config: Any) -> Path:
    csv_dir = config.path_for("csv_export", "data/leads.csv").parent
    if config.get("runtime.export_dir"):
        return config.path_for("export_dir", str(csv_dir))
    return csv_dir
