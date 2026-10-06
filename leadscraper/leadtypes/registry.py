"""Which lead types exist.

Built-in lead types register themselves when first asked for; tests (and,
later, optional plugins) can register more at runtime. The run-config schema
validates ``lead_types.<key>`` against whatever is registered here, so a new
lead type needs no change to ``config/schema.py``.

``LEADSCRAPER_PLUGINS`` (comma-separated module paths) loads extra lead types
in every process -- a worker started separately included. Each module exposes
``LEAD_TYPES`` (a list) or ``LEAD_TYPE``.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from .base import LeadType

_lock = threading.RLock()
_lead_types: dict[str, "LeadType"] = {}
_builtins_loaded = False
PLUGINS_ENV = "LEADSCRAPER_PLUGINS"


def _load_builtins() -> None:
    global _builtins_loaded
    with _lock:
        if _builtins_loaded:
            return
        _builtins_loaded = True
        from .local_business.plugin import LOCAL_BUSINESS

        _lead_types.setdefault(LOCAL_BUSINESS.key, LOCAL_BUSINESS)
        for module_name in filter(None, (m.strip() for m in
                                         os.environ.get(PLUGINS_ENV, "").split(","))):
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # a broken plugin never stops the built-ins
                logging.getLogger(__name__).error("lead-type plugin %s failed to load: %s",
                                                  module_name, exc)
                continue
            for lead_type in getattr(module, "LEAD_TYPES", None) or \
                    [getattr(module, "LEAD_TYPE", None)]:
                if lead_type is not None:
                    _lead_types.setdefault(lead_type.key, lead_type)


def register(lead_type: "LeadType") -> None:
    _load_builtins()
    with _lock:
        _lead_types[lead_type.key] = lead_type


def unregister(key: str) -> None:
    with _lock:
        _lead_types.pop(key, None)


def all_lead_types() -> list["LeadType"]:
    _load_builtins()
    with _lock:
        return list(_lead_types.values())


def get(key: str) -> "LeadType | None":
    _load_builtins()
    with _lock:
        return _lead_types.get(key)


def config_model(key: str) -> type[BaseModel] | None:
    lead_type = get(key)
    return lead_type.config_model if lead_type else None
