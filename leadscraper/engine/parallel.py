"""Parallel across hosts, never within one (REBUILD-PROMPT Part 2).

``RateLimiter`` already paces each host on its own, so running work against
*different* hosts at the same time is safe. This module adds the other half:
two pieces of work that share a host never run at the same moment. A Maps
unit and a Google SERP unit both hit google.com, so they take turns; a Maps
unit and a Justdial unit run side by side.

The dispatcher runs in the calling thread and owns all bookkeeping (``on_done``
is always called there), so checkpoints and events stay in order. Work runs
in daemon threads: a hard Ctrl-C ends the process without waiting for them.
With one worker everything runs inline, in plan order -- exactly the old
sequential behaviour.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import OrderedDict, deque
from typing import Any, Callable, Hashable, Iterable

log = logging.getLogger(__name__)


class _Exhausted:
    def __repr__(self) -> str:
        return "EXHAUSTED"


#: Returned by ``next_item`` when nothing is left at all.
EXHAUSTED = _Exhausted()

NextItem = Callable[[set[str]], Any]


def run_across_hosts(next_item: NextItem, hosts_of: Callable[[Any], Iterable[str]],
                     work: Callable[[Any], Any],
                     on_done: Callable[[Any, Any, BaseException | None], None], *,
                     workers: int = 1, should_stop: Callable[[], bool] = lambda: False,
                     poll_seconds: float = 0.25, thread_name: str = "work") -> None:
    """Run items until ``next_item`` is exhausted or ``should_stop()``.

    ``next_item(busy_hosts)`` returns an item whose hosts are all free, None
    when nothing can start until a host frees up, or ``EXHAUSTED``. A stop
    lets work in flight finish (graceful); nothing new starts.
    """
    if workers <= 1:
        while not should_stop():
            item = next_item(set())
            if item is EXHAUSTED or item is None:
                return
            try:
                result, error = work(item), None
            except Exception as exc:  # work() is expected to contain its own failures
                result, error = None, exc
            on_done(item, result, error)
        return

    tasks: queue.Queue = queue.Queue()
    results: queue.Queue = queue.Queue()

    def worker() -> None:
        while True:
            item = tasks.get()
            if item is EXHAUSTED:
                return
            try:
                results.put((item, work(item), None))
            except BaseException as exc:  # reported to the dispatcher, never lost
                results.put((item, None, exc))

    threads = [threading.Thread(target=worker, name=f"{thread_name}-{i + 1}", daemon=True)
               for i in range(workers)]
    for thread in threads:
        thread.start()

    busy: set[str] = set()
    held: dict[int, frozenset[str]] = {}
    in_flight = 0
    exhausted = False
    try:
        while True:
            while not exhausted and in_flight < workers and not should_stop():
                item = next_item(set(busy))
                if item is EXHAUSTED:
                    exhausted = True
                    break
                if item is None:
                    break
                hosts = frozenset(hosts_of(item))
                busy |= hosts
                held[id(item)] = hosts
                in_flight += 1
                tasks.put(item)
            if in_flight == 0:
                return
            try:
                item, result, error = results.get(timeout=poll_seconds)
            except queue.Empty:
                continue
            in_flight -= 1
            busy -= held.pop(id(item), frozenset())
            on_done(item, result, error)
    finally:
        for _ in threads:
            tasks.put(EXHAUSTED)


class HostQueue:
    """Items grouped by host, handed out round-robin to free hosts.

    For enrichment: hundreds of vendor sites are hundreds of hosts, so eight
    of them are fetched at once; every Instagram profile is one host, so they
    go one at a time behind Instagram's own delay.
    """

    def __init__(self, items: Iterable[Any], host_of: Callable[[Any], Hashable]):
        self._by_host: OrderedDict[Hashable, deque] = OrderedDict()
        self._host_of = host_of
        self.total = 0
        for item in items:
            self._by_host.setdefault(host_of(item), deque()).append(item)
            self.total += 1

    def next(self, busy: set[str]) -> Any:
        if not self._by_host:
            return EXHAUSTED
        for host in list(self._by_host):
            if str(host) in busy:
                continue
            pending = self._by_host[host]
            item = pending.popleft()
            if pending:
                self._by_host.move_to_end(host)   # round-robin across hosts
            else:
                del self._by_host[host]
            return item
        return None

    def hosts_of(self, item: Any) -> list[str]:
        return [str(self._host_of(item))]

    def remaining(self) -> int:
        return sum(len(pending) for pending in self._by_host.values())
