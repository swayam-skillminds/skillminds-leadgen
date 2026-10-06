"""Keep the machine awake while a run is active (local mode; Phase 2.6).

``KeepAwake`` holds one ``wakepy`` "keep running" lock for as long as at
least one run is active in this process, and releases it when the last one
ends. It works on macOS (``caffeinate``-style assertions), Windows
(``SetThreadExecutionState``) and Linux (D-Bus inhibitors, where a session bus
exists). Failure to get the lock is logged, never fatal.

``SleepWatch`` notices that the machine slept anyway: a heartbeat that should
tick every few seconds sees the wall clock jump. The run then re-validates its
browsers and connections and does not count the gap against its time budget.

Honest limits (also in the README):

* A sleeping machine runs nothing. The lock only *prevents idle sleep*.
* On macOS, closing the lid on battery power forces sleep regardless of any
  lock; so does an explicit "Sleep" from the menu on every OS.
* For truly unattended runs use server mode (Phase 3): an always-on machine.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import ExitStack
from typing import Callable

log = logging.getLogger(__name__)

ENV_TOGGLE = "LEADSCRAPER_KEEP_AWAKE"


def enabled_by_default() -> bool:
    return os.environ.get(ENV_TOGGLE, "1").strip().lower() not in ("0", "false", "no", "off")


class KeepAwake:
    """A reference-counted keep-awake lock: acquired by the first active run,
    released by the last."""

    def __init__(self, enabled: bool | None = None,
                 factory: Callable[[], object] | None = None):
        self._enabled = enabled
        self._factory = factory
        self._count = 0
        self._stack: ExitStack | None = None
        self._lock = threading.Lock()
        #: True while the OS actually granted the lock.
        self.active = False
        self.method: str | None = None

    @property
    def enabled(self) -> bool:
        """On unless switched off here or by LEADSCRAPER_KEEP_AWAKE=0."""
        return enabled_by_default() if self._enabled is None else self._enabled

    @enabled.setter
    def enabled(self, value: bool | None) -> None:
        self._enabled = value

    def _mode(self):
        if self._factory is not None:
            return self._factory()
        from wakepy import keep

        return keep.running(on_fail="pass")

    def acquire(self) -> bool:
        with self._lock:
            self._count += 1
            if self._count > 1 or not self.enabled:
                return self.active
            stack = ExitStack()
            try:
                mode = stack.enter_context(self._mode())
            except Exception as exc:
                stack.close()
                log.warning("keep-awake unavailable (%s); the machine may sleep mid-run", exc)
                return False
            self._stack = stack
            self.active = bool(getattr(mode, "active", True))
            method = getattr(mode, "active_method", None) or getattr(mode, "method", None)
            self.method = str(getattr(method, "name", method) or "") or None
            if self.active:
                log.info("keep-awake on (%s): the machine won't idle-sleep while a run is active",
                         self.method or "wakepy")
            else:
                log.warning("keep-awake requested but no method worked on this system; "
                            "the machine may sleep mid-run (see docs/engine.md)")
            return self.active

    def release(self) -> None:
        with self._lock:
            if self._count == 0:
                return
            self._count -= 1
            if self._count > 0 or self._stack is None:
                return
            stack, self._stack = self._stack, None
            try:
                stack.close()
            except Exception as exc:
                log.debug("keep-awake release failed (ignored): %s", exc)
            if self.active:
                log.info("keep-awake off: no run is active")
            self.active = False

    @property
    def holders(self) -> int:
        with self._lock:
            return self._count

    def __enter__(self) -> "KeepAwake":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


#: The process-wide lock (worker and CLI runs share it).
KEEP_AWAKE = KeepAwake()


class SleepWatch:
    """Detects that the machine slept between two ticks.

    A tick is expected every `interval` seconds. If the wall clock moved
    `threshold` seconds more than that, the process was suspended -- the
    machine slept (or the process was stopped). Monotonic time is compared
    too: on macOS and Linux it stops during sleep, on Windows it keeps
    counting, so the part of the gap the budget's clock saw is returned
    separately (to discount it).
    """

    def __init__(self, clock, *, interval: float, threshold: float = 30.0):
        self.clock = clock
        self.interval = interval
        self.threshold = threshold
        self._wall = clock.wall()
        self._mono = clock.now()

    def tick(self) -> tuple[float, float] | None:
        """(gap seconds, of which the monotonic clock counted) or None."""
        wall, mono = self.clock.wall(), self.clock.now()
        wall_delta, mono_delta = wall - self._wall, mono - self._mono
        self._wall, self._mono = wall, mono
        gap = wall_delta - self.interval
        if gap < self.threshold:
            return None
        counted = max(0.0, mono_delta - self.interval)
        return gap, counted
