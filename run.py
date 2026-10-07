#!/usr/bin/env python3
"""Compatibility entry point: ``python run.py [flags]`` == ``python -m leadscraper run [flags]``.

Every flag it ever took still works. With no ``--config`` it runs config.yaml
(the Mumbai event-vendor run); ``--config config.nashik.yaml`` the Nashik venue
run; ``--config presets/<name>.yaml`` any run config. See
``python run.py --help`` and ``python -m leadscraper --help``.
"""

from __future__ import annotations

import sys

from leadscraper.cli.run_cmd import main

if __name__ == "__main__":
    sys.exit(main(prog="run.py"))
