"""Per-domain pacing and retry.

Two separate concerns, deliberately kept apart:
  * RateLimiter -- guarantees a randomised minimum gap between hits on the
    same host, so we never burst regardless of which source is calling.
  * with_retry  -- exponential backoff with jitter for transient failures.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class UnitCut(BaseException):
    """The engine cut the work unit this thread runs (it overran its estimate
    into time the budget had promised to other work).

    A BaseException, like KeyboardInterrupt, so the sources' own ``except
    Exception`` guards don't record it as a failed request: it ends the unit,
    which the engine records as ``partial`` (it runs again on resume).
    """


class RateLimiter:
    """Randomised per-key minimum interval between calls.

    Keys are usually hostnames, so two sources hitting google.com share a
    budget rather than each thinking they're being polite on their own.

    ``interrupt`` (set by the engine) is asked before every request and about
    once a second while pacing: when it says so, ``wait`` raises ``UnitCut``
    instead of sleeping on. A host under an escalating penalty can make one
    wait last minutes, and nothing else could stop the unit then.
    """

    def __init__(self, default_range: tuple[float, float] = (2.0, 5.0)):
        self._default = default_range
        self._ranges: dict[str, tuple[float, float]] = {}
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()
        # Escalating penalty applied after 429/403 responses, per key.
        self._penalty: dict[str, float] = {}
        self.interrupt: Callable[[], bool] | None = None

    def configure(self, key: str, min_delay: float, max_delay: float) -> None:
        """Set the delay range for `key`, keeping whichever is more cautious.

        Several sources share hosts -- five of them fall back to a search
        engine. Last-writer-wins would let a source with short delays silently
        undo the long ones another source set on google.com, so a shared key
        always ends up with the most conservative range anyone asked for.
        """
        if max_delay < min_delay:
            min_delay, max_delay = max_delay, min_delay
        with self._lock:
            existing = self._ranges.get(key)
            if existing is not None:
                min_delay = max(min_delay, existing[0])
                max_delay = max(max_delay, existing[1])
            self._ranges[key] = (min_delay, max_delay)

    def range_for(self, key: str) -> tuple[float, float]:
        with self._lock:
            return self._ranges.get(key, self._default)

    def wait(self, key: str) -> None:
        """Block until it's polite to hit `key` again (``UnitCut``: don't)."""
        interrupt = self.interrupt
        if interrupt is not None and interrupt():
            raise UnitCut(key)
        with self._lock:
            low, high = self._ranges.get(key, self._default)
            penalty = self._penalty.get(key, 0.0)
            gap = random.uniform(low, high) + penalty
            last = self._last_call.get(key)
            now = time.monotonic()
            sleep_for = 0.0 if last is None else max(0.0, (last + gap) - now)
            # Reserve the slot before releasing the lock so concurrent callers
            # queue behind us instead of all sleeping the same amount.
            self._last_call[key] = now + sleep_for
        if sleep_for > 0:
            log.debug("pacing %s: sleeping %.1fs", key, sleep_for)
            if interrupt is None:
                time.sleep(sleep_for)
                return
            # The slot stays reserved if we are cut: the next caller waits as
            # if this request had been made, which errs on the polite side.
            end = time.monotonic() + sleep_for
            while (left := end - time.monotonic()) > 0:
                time.sleep(min(left, 1.0))
                if interrupt():
                    raise UnitCut(key)

    def penalise(self, key: str, seconds: float = 30.0, cap: float = 300.0) -> None:
        """Slow down further after a block signal (429/403/CAPTCHA)."""
        with self._lock:
            current = self._penalty.get(key, 0.0)
            self._penalty[key] = min(cap, current + seconds)
        log.warning("rate-limit penalty on %s now +%.0fs", key, self._penalty[key])

    def relax(self, key: str, factor: float = 0.5) -> None:
        """Decay the penalty after a clean response."""
        with self._lock:
            current = self._penalty.get(key, 0.0)
            if current:
                self._penalty[key] = current * factor if current * factor > 1 else 0.0

    def penalty_for(self, key: str) -> float:
        with self._lock:
            return self._penalty.get(key, 0.0)


class BlockedError(RuntimeError):
    """Raised when a source actively blocks us (CAPTCHA, 429, login wall)."""


class PermanentError(RuntimeError):
    """A failure retrying cannot fix -- a 404, a 410, a dead domain."""


def with_retry(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base: float = 2.0,
    label: str = "request",
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (),
) -> T:
    """Run `func`, retrying with exponential backoff + jitter.

    Re-raises the final exception so callers decide whether to log a failure
    or continue -- this helper never swallows errors silently.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except give_up_on:
            raise
        except retry_on as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base * (2 ** (attempt - 1)) + random.uniform(0, base)
            log.info("%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                     label, attempt, attempts, exc, delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def jitter_sleep(low: float, high: float) -> None:
    """Plain randomised pause, for use between logical steps."""
    time.sleep(random.uniform(low, high))
