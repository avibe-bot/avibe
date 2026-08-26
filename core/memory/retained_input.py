"""Aggregate admission for unbounded caller-owned Memory inputs."""

from __future__ import annotations

import sys
import threading


MAX_RETAINED_INPUT_BYTES = 64 * 1024 * 1024
MAX_RETAINED_INPUT_RESERVATIONS = 32


class RetainedInputReservation:
    """One idempotently releasable aggregate-input reservation."""

    def __init__(self, budget: "RetainedInputBudget", size: int) -> None:
        self._budget = budget
        self._size = size
        self._active = True

    def release(self) -> None:
        if not self._active:
            return
        self._active = False
        self._budget._release(self._size)


class RetainedInputBudget:
    """Bound concurrent retained bytes while allowing one arbitrarily large input."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_RETAINED_INPUT_BYTES,
        max_reservations: int = MAX_RETAINED_INPUT_RESERVATIONS,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("invalid retained input budget")
        if (
            isinstance(max_reservations, bool)
            or not isinstance(max_reservations, int)
            or max_reservations <= 0
        ):
            raise ValueError("invalid retained input reservation count")
        self._max_bytes = max_bytes
        self._max_reservations = max_reservations
        self._retained_bytes = 0
        self._reservations = 0
        self._lock = threading.Lock()

    def reserve(self, size: int) -> RetainedInputReservation | None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return None
        with self._lock:
            if self._reservations >= self._max_reservations or (
                self._retained_bytes and self._retained_bytes + size > self._max_bytes
            ):
                return None
            self._retained_bytes += size
            self._reservations += 1
        return RetainedInputReservation(self, size)

    def _release(self, size: int) -> None:
        with self._lock:
            self._retained_bytes = max(0, self._retained_bytes - size)
            self._reservations = max(0, self._reservations - 1)

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes


def estimate_text_residency(value: object, *, copies: int) -> int:
    """Conservatively estimate live text plus normalization/encoding copies."""

    if not isinstance(value, str) or isinstance(copies, bool) or copies <= 0:
        return 0
    per_copy = max(sys.getsizeof(value), sys.getsizeof("") + len(value) * 4)
    return per_copy * copies
