"""Which lead types exist.

Built-in lead types register themselves when first asked for; tests (and,
later, optional plugins) can register more at runtime. The run-config schema
validates ``lead_types.<key>`` against whatever is registered here, so a new
lead type needs no change to ``config/schema.py``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from .base import LeadType

_lock = threading.RLock()
_lead_types: dict[str, "LeadType"] = {}
_builtins_loaded = False


def _load_builtins() -> None:
    global _builtins_loaded
    with _lock:
        if _builtins_loaded:
            return
        _builtins_loaded = True
        from .local_business.plugin import LOCAL_BUSINESS

        _lead_types.setdefault(LOCAL_BUSINESS.key, LOCAL_BUSINESS)


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
