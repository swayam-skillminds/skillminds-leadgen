"""Time, injectable.

The scheduler, the time budget and the estimator all reason about seconds.
Production code reads a monotonic clock; tests hand in a ``FakeClock`` whose
time only moves when a fake source "spends" it, so a 15-minute budget run
takes milliseconds and is exactly reproducible.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""

    def wall(self) -> float:
        """Wall-clock epoch seconds (for sleep detection)."""

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """A clock that advances only when told to. Thread-safe."""

    def __init__(self, start: float = 1_000.0):
        self._now = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def wall(self) -> float:
        return self.now()

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += max(0.0, float(seconds))


SYSTEM_CLOCK = SystemClock()
