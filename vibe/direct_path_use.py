"""How much of a remote session's traffic arrives over the direct path.

The clients can count the connections their own listener carried, and report
that with the rendezvous request (``direct_rendezvous.log_path_use``). They
cannot count what never reaches their listener: a page that stays on a
connection it made before the direct route existed sends everything through
the tunnel, and its client sees nothing of it. The instance sees every
request. So it counts, per remote session and per minute, how many requests
whose session it accepted arrived through the direct ingress and how many any
other way, and logs one line for each session and minute that had any.

``other`` is "not through the direct ingress", and no more than that. The
instance knows a request of the direct ingress by its own listener; for the
rest it checks the host name the request carries, not where the request came
from. In practice that is the tunnel, but the number is not a verified count
of tunnel traffic.

Counts only. A session is named in the log by a small number given in the
order sessions were first seen since this process started; nothing of its
identity is logged or kept beyond the key the number stands for. The number
is a pseudonym, not anonymity: lines with the same number are the same
session for as long as its key is kept, and a session whose key was dropped
for a newer one is numbered anew. WebSockets are not counted: a stream is one
request here, as it is for the page.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 256
_MAX_SESSIONS_PER_MINUTE = 64


class IngressCounts:
    """Requests by arrival path, per session and minute. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # Session key -> its number in the log. The key never leaves this map.
        self._numbers: OrderedDict[Any, int] = OrderedDict()
        self._next_number = 1
        # Minute -> session number -> [role, direct, other].
        self._minutes: dict[int, dict[int, list[Any]]] = {}

    def note(self, session: Any, *, direct: bool, owner: bool) -> None:
        """Count one request of an accepted remote session, and log the minutes that are over."""
        with self._lock:
            # The clock is read under the lock: a minute that a drain found
            # over cannot then be counted in again and logged a second time.
            minute = int(self._clock() // 60)
            lines = self._drain_locked(minute)
            number = self._numbers.get(session)
            if number is None:
                number = self._next_number
                self._next_number += 1
                self._numbers[session] = number
                if len(self._numbers) > _MAX_SESSIONS:
                    self._numbers.popitem(last=False)
            else:
                self._numbers.move_to_end(session)
            sessions = self._minutes.setdefault(minute, {})
            # Past the bound, the rest of the minute's sessions share one line.
            if number not in sessions and len(sessions) >= _MAX_SESSIONS_PER_MINUTE:
                number = 0
            counts = sessions.setdefault(number, ["owner" if owner else "member", 0, 0])
            if number == 0:
                counts[0] = "several"
            counts[1 if direct else 2] += 1
        _log(lines)

    def flush(self) -> None:
        """Log every minute that is over; for a caller that runs when no request does."""
        with self._lock:
            lines = self._drain_locked(int(self._clock() // 60))
        _log(lines)

    def _drain_locked(self, current: int) -> list[tuple[int, str, int, int, int]]:
        # A minute before the current one is over: nothing more is counted in it.
        lines = []
        for minute in [minute for minute in self._minutes if minute < current]:
            for number, (role, direct, other) in sorted(self._minutes.pop(minute).items()):
                lines.append((number, role, current - minute, direct, other))
        return lines


def _log(lines: list[tuple[int, str, int, int, int]]) -> None:
    for number, role, minutes_ago, direct, other in lines:
        logger.info(
            "direct path_use ingress session=%d role=%s minutes_ago=%d direct=%d other=%d",
            number, role, minutes_ago, direct, other,
        )


counts = IngressCounts()
