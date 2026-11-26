"""One unresolved, process-local measured pair per route/model. No completed success is admission state."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from weakref import WeakValueDictionary

from core.agent_core.ai import thread_read_loss


class Lease:
    """The stream, then single-use parent, then Agent commit candidate may own the same pair."""

    def __init__(self, live: Callable[[], object]):
        self._live = live
        self._lock = threading.Lock()

    def live(self) -> bool:
        with self._lock:
            return bool(self._live())

    def handoff(self, successor: Callable[[], object]) -> None:
        with self._lock:
            if self._live():
                self._live = successor


@dataclass(frozen=True)
class Trial:
    reads: int
    lease: Lease


# Registry entries disappear with the last owner; no sweep, capacity eviction, or persisted IDs.
_pending: WeakValueDictionary[bytes, Lease] = WeakValueDictionary()
_lock = threading.Lock()


def reserve(route: bytes, reads: int, preview: bool, *, lease: Lease | None = None) -> tuple[Trial | None, str]:
    with _lock:
        if reason := thread_read_loss.stop_reason(route):
            return None, reason
        current = _pending.get(route)
        if current and current.live():
            return None, "bootstrap_pending"
        trial = Trial(reads, lease or Lease(lambda: True))
        if not preview:
            _pending[route] = trial.lease
        return trial, "bootstrap_trial"


def owns(route: bytes, trial: Trial) -> bool:
    with _lock:
        return _pending.get(route) is trial.lease and trial.lease.live() and not thread_read_loss.stop_reason(route)


def complete(route: bytes, trial: Trial) -> None:
    with _lock:
        if _pending.get(route) is trial.lease:
            del _pending[route]


def measured_loss(route: bytes) -> None:
    with _lock:
        # A stop vetoes this route's admission without discarding any unresolved stream or commit.
        thread_read_loss.record(route)
