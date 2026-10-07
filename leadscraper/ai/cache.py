"""The response cache: a run never pays twice for the same call (Phase 5.3).

Keyed by provider kind + model + schema + prompt hash. The pre-Phase-5 cache
(``ai_cache.sqlite3``, keyed by model + prompt only) was filled by the Claude
backends, so for those kinds the old key is still read: the Mumbai and Nashik
prompts are byte-identical, and their cached answers stay valid.

Only answers that passed schema validation are stored.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

#: Kinds whose answers the legacy (model + prompt) key may hold.
_LEGACY_KINDS = ("claude_code_cli", "anthropic", "legacy")


class ResponseCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_cache ("
            " key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        self._conn.commit()
        self.hits = 0

    @staticmethod
    def legacy_key(system: str, user: str, model: str) -> str:
        return hashlib.sha256(f"{model}\x00{system}\x00{user}".encode()).hexdigest()[:48]

    @staticmethod
    def key_for(kind: str, model: str, system: str, user: str, schema_name: str = "") -> str:
        blob = f"v2\x00{kind}\x00{model}\x00{schema_name}\x00{system}\x00{user}"
        return hashlib.sha256(blob.encode()).hexdigest()[:48]

    def lookup(self, kind: str, model: str, system: str, user: str,
               schema_name: str = "") -> str | None:
        keys = [self.key_for(kind, model, system, user, schema_name)]
        if kind in _LEGACY_KINDS:
            keys.append(self.legacy_key(system, user, model))
        for key in keys:
            value = self.get(key)
            if value is not None:
                return value
        return None

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT response FROM ai_cache WHERE key = ?",
                                     (key,)).fetchone()
        if row:
            self.hits += 1
            return row[0]
        return None

    def put(self, key: str, response: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_cache (key, response, created_at) VALUES (?, ?, ?)",
                (key, response, time.time()))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
