from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

BIND_ATTEMPT_FREE_FAILURES = 3
BIND_ATTEMPT_BACKOFF_BASE_SECONDS = 30.0
BIND_ATTEMPT_BACKOFF_MAX_SECONDS = 15 * 60.0
BIND_ATTEMPT_LOCK_FAILURES = 10
BIND_ATTEMPT_LOCK_SECONDS = 60 * 60.0


@dataclass(frozen=True)
class BindAttemptDecision:
    allowed: bool
    retry_after_seconds: int = 0


@dataclass
class _BindAttemptState:
    failures: int = 0
    locked_until: float = 0.0


class BindAttemptLimiter:
    """In-process bind-code failure limiter keyed by platform, user, and DM channel."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        free_failures: int = BIND_ATTEMPT_FREE_FAILURES,
        backoff_base_seconds: float = BIND_ATTEMPT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = BIND_ATTEMPT_BACKOFF_MAX_SECONDS,
        lock_failures: int = BIND_ATTEMPT_LOCK_FAILURES,
        lock_seconds: float = BIND_ATTEMPT_LOCK_SECONDS,
    ) -> None:
        self._clock = clock
        self._free_failures = free_failures
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._lock_failures = lock_failures
        self._lock_seconds = lock_seconds
        self._states: dict[tuple[str, str, str], _BindAttemptState] = {}
        self._lock = threading.Lock()

    def check(self, *, platform: str, user_id: str, channel_id: str) -> BindAttemptDecision:
        key = self._key(platform=platform, user_id=user_id, channel_id=channel_id)
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if state is None or state.locked_until <= now:
                return BindAttemptDecision(allowed=True)
            return BindAttemptDecision(
                allowed=False,
                retry_after_seconds=max(1, int(math.ceil(state.locked_until - now))),
            )

    def record_failure(self, *, platform: str, user_id: str, channel_id: str) -> BindAttemptDecision:
        key = self._key(platform=platform, user_id=user_id, channel_id=channel_id)
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(key, _BindAttemptState())
            state.failures += 1
            delay = self._delay_for_failure_count(state.failures)
            if delay > 0:
                state.locked_until = max(state.locked_until, now + delay)
                return BindAttemptDecision(
                    allowed=False,
                    retry_after_seconds=max(1, int(math.ceil(state.locked_until - now))),
                )
            return BindAttemptDecision(allowed=True)

    def reset(self, *, platform: str, user_id: str, channel_id: str) -> None:
        key = self._key(platform=platform, user_id=user_id, channel_id=channel_id)
        with self._lock:
            self._states.pop(key, None)

    def _delay_for_failure_count(self, failures: int) -> float:
        if failures >= self._lock_failures:
            return self._lock_seconds
        if failures <= self._free_failures:
            return 0.0
        exponent = failures - self._free_failures - 1
        return min(self._backoff_max_seconds, self._backoff_base_seconds * (2**exponent))

    @staticmethod
    def _key(*, platform: str, user_id: str, channel_id: str) -> tuple[str, str, str]:
        return (str(platform or ""), str(user_id or ""), str(channel_id or ""))
