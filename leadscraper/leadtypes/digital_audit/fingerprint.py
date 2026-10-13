"""The fingerprint rules moved to ``sources/web/fingerprint.py`` (Phase 8: ``tech_stack``
reads them too, and lead types never import each other). Re-exported for this lead type."""

from ...sources.web.fingerprint import (RULES_PATH, Detected, detect, rules,  # noqa: F401
                                        version_tuple)
