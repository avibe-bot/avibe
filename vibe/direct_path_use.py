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

Optional page-reported reasons and return holds are separate from those counts.
A session is named in the log by a small number given in the
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
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 256
_MAX_SESSIONS_PER_MINUTE = 64

_PAGE_WORDS = frozenset({
    "no_path", "on_path", "not_ready", "verdict", "spacing", "cap", "quiet_request",
    "quiet_socket", "quiet_frame", "not_ours", "asked", "idle",
})
_MAX_PAGE_LENGTH = max(map(len, _PAGE_WORDS)) + len(";hold=1000")


def page_sample(fields: list[str]) -> tuple[str, int | None] | None:
    """Decode exactly one X-Avibe-Page field; a bad hold invalidates its word too."""
    if len(fields) != 1 or len(fields[0]) > _MAX_PAGE_LENGTH:
        return None
    word, sep, ms = fields[0].partition(";hold=")
    if word not in _PAGE_WORDS:
        return None
    if not sep:
        return word, None
    if not (1 <= len(ms) <= 4 and ms.isascii() and ms.isdecimal()):
        return None
    hold = int(ms)
    return (word, hold) if hold <= 1000 and str(hold) == ms else None


@dataclass(slots=True)
class _PageMinute:
    last: str
    seen: set[str] = field(default_factory=set)
    holds: int = 0
    max_ms: int = 0

    def note(self, word: str, hold: int | None) -> None:
        self.last = word
        self.seen.add(word)
        if hold is not None:
            self.holds += 1
            self.max_ms = max(self.max_ms, hold)

    def suffix(self) -> str:
        suffix = f" page_last={self.last} page_seen={','.join(sorted(self.seen))} hold_count={self.holds}"
        return suffix + (f" hold_max_ms={self.max_ms}" if self.holds else "")


@dataclass(slots=True)
class _Minute:
    role: str
    direct: int = 0
    other: int = 0
    page: _PageMinute | None = None


class IngressCounts:
    """Requests by arrival path, per session and minute. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # Session key -> its number in the log. The key never leaves this map.
        self._numbers: OrderedDict[Any, int] = OrderedDict()
        self._next_number = 1
        self._minutes: dict[int, dict[int, _Minute]] = {}

    def note(self, session: Any, *, direct: bool, owner: bool, page: tuple[str, int | None] | None = None) -> None:
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
            counts = sessions.get(number)
            if counts is None:
                role = ("owner" if owner else "member") if number else "several"
                counts = sessions[number] = _Minute(role)
            if direct:
                counts.direct += 1
            else:
                counts.other += 1
            if number and page is not None:
                if counts.page is None:
                    counts.page = _PageMinute(page[0])
                counts.page.note(*page)
        _log(lines)

    def flush(self) -> None:
        """Log every minute that is over; for a caller that runs when no request does."""
        with self._lock:
            lines = self._drain_locked(int(self._clock() // 60))
        _log(lines)

    def _drain_locked(self, current: int) -> list[tuple[int, int, _Minute]]:
        # A minute before the current one is over: nothing more is counted in it.
        lines = []
        for minute in [minute for minute in self._minutes if minute < current]:
            for number, counts in sorted(self._minutes.pop(minute).items()):
                lines.append((number, current - minute, counts))
        return lines


def _log(lines: list[tuple[int, int, _Minute]]) -> None:
    for number, minutes_ago, counts in lines:
        logger.info(
            "direct path_use ingress session=%d role=%s minutes_ago=%d direct=%d other=%d%s",
            number, counts.role, minutes_ago, counts.direct, counts.other, counts.page.suffix() if counts.page else "",
        )


counts = IngressCounts()
