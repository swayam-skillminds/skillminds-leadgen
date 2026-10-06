"""Configuration.

* ``schema``   -- the versioned pydantic run config (APP-BUILD-PROMPT A.8).
* ``importer`` -- legacy ``config.yaml`` files -> run configs / presets.
* ``compile``  -- run config -> ``Config``, the runtime view sources read.
* ``legacy``   -- ``Config`` itself: dotted-path access over a plain dict.
"""

from .legacy import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Category, Config, Locality

__all__ = ["Category", "Config", "Locality", "DEFAULT_CONFIG_PATH", "PROJECT_ROOT"]
