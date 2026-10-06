"""One stop path for every way a run ends early.

Pause, cancel, the time-budget deadline, Ctrl-C in the CLI and a worker
shutting down all land here, and they all mean the same thing to the code
doing the work: *don't start anything new; let what is in flight finish;
then flush*. Only the final status differs:

=============  =====================  ===========================
request        set by                 run ends as
=============  =====================  ===========================
``pause``      API/CLI, Ctrl-C        ``paused`` (resumable)
``cancel``     API/CLI                ``cancelled``
``shutdown``   the worker stopping    ``queued`` again (resumed by
                                      the next worker)
deadline       the time budget        ``completed`` (budget spent;
                                      skipped units reported)
=============  =====================  ===========================

A second Ctrl-C is the only hard stop (``KeyboardInterrupt``): the unit in
flight is then lost, and the run's checkpoint says so.

The budget is measured in *active* seconds: time spent paused does not
count, nor does a detected machine sleep (``discount``), and a resumed run
continues from the seconds it had already used.
"""

from __future__ import annotations

import threading
from typing import Callable

from .clock import SYSTEM_CLOCK, Clock

STOP_REQUESTS = ("pause", "cancel", "shutdown")

#: Final run status for each stop request.
STATUS_FOR_REQUEST = {"pause": "paused", "cancel": "cancelled", "shutdown": "queued"}


class RunControl:
    def __init__(self, *, budget_seconds: float | None = None, tail_seconds: float = 90.0,
                 clock: Clock | None = None, used_seconds: float = 0.0):
        self.clock = clock or SYSTEM_CLOCK
        self.budget_seconds = budget_seconds
        #: Reserved for flush, scoring and export; never spent on work.
        self.tail_seconds = max(0.0, float(tail_seconds)) if budget_seconds else 0.0
        self._started = self.clock.now()
        self._used_before = float(used_seconds)
        self._discounted = 0.0
        self._request: str | None = None
        self._deadline_hit = False
        # Re-entrant: a signal handler runs on the main thread, possibly while
        # that thread is already inside this lock.
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._listeners: list[Callable[[str], None]] = []

    # --- requests -----------------------------------------------------------------

    def request(self, what: str) -> bool:
        """Ask the run to stop gracefully; True if this changed the request.

        The first request wins, except that a cancel overrides an earlier
        pause or shutdown: the user's "stop for good" must not end as a
        resumable run.
        """
        if what not in STOP_REQUESTS:
            raise ValueError(f"unknown stop request {what!r}")
        with self._lock:
            if self._request is not None and not (what == "cancel"
                                                  and self._request != "cancel"):
                return False
            self._request = what
            listeners = list(self._listeners)
        self._wake.set()
        for listener in listeners:
            try:
                listener(what)
            except Exception:
                pass
        return True

    def on_request(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    @property
    def requested(self) -> str | None:
        with self._lock:
            return self._request

    @property
    def stop_requested(self) -> bool:
        """Nothing new should start: a request, or the deadline."""
        return self.requested is not None or self.deadline_reached

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`, waking early on a stop request. True if one came."""
        return self._wake.wait(seconds)

    # --- the budget -----------------------------------------------------------------

    def elapsed(self) -> float:
        """Active seconds spent on this run, across resumes."""
        with self._lock:
            discounted = self._discounted
        return self._used_before + max(0.0, self.clock.now() - self._started - discounted)

    def discount(self, seconds: float) -> None:
        """Don't count `seconds` (a detected machine sleep) against the budget."""
        with self._lock:
            self._discounted += max(0.0, float(seconds))

    @property
    def budgeted(self) -> bool:
        return self.budget_seconds is not None

    def remaining(self) -> float:
        """Seconds of budget left for work (the tail excluded); inf without a budget."""
        if self.budget_seconds is None:
            return float("inf")
        return self.budget_seconds - self.tail_seconds - self.elapsed()

    def fits(self, expected_seconds: float, *, reserve: float = 0.0) -> bool:
        """Can work expected to take this long start now and finish inside the budget?"""
        return expected_seconds <= self.remaining() - reserve

    @property
    def deadline_reached(self) -> bool:
        if self.budget_seconds is None:
            return False
        if not self._deadline_hit and self.remaining() <= 0:
            self._deadline_hit = True
        return self._deadline_hit

    def final_status(self, default: str = "completed") -> str:
        request = self.requested
        return STATUS_FOR_REQUEST.get(request, default) if request else default
