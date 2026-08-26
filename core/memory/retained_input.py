"""Aggregate admission for unbounded caller-owned Memory inputs."""

from __future__ import annotations

import json
import sys
import threading
from typing import Any


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

    def resize(self, size: int) -> bool:
        """Grow this reservation without losing single-oversize admission."""

        if not self._active:
            return False
        resized = self._budget._resize(self._size, size)
        if resized:
            self._size = size
        return resized


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
            projected = self._retained_bytes + size
            if self._reservations >= self._max_reservations or (
                projected > self._max_bytes and self._reservations > 0
            ):
                return None
            self._retained_bytes += size
            self._reservations += 1
        return RetainedInputReservation(self, size)

    def _resize(self, current_size: int, size: int) -> bool:
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < current_size
        ):
            return False
        with self._lock:
            projected = self._retained_bytes - current_size + size
            if projected > self._max_bytes and self._reservations > 1:
                return False
            self._retained_bytes = projected
        return True

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


class RetainedInputRejected(Exception):
    """The process cannot retain another request body concurrently."""


def _json_body_residency(byte_count: int) -> int:
    # Raw ASGI chunks, the joined body, decoded strings, and the next transport
    # serialization can coexist while a Web request crosses into the controller.
    return max(1, byte_count) * 8


async def read_json_object_admitted(
    request: Any,
    budget: RetainedInputBudget,
) -> tuple[dict[str, Any] | None, RetainedInputReservation]:
    """Read one JSON object while admission covers every retained body copy."""

    content_length = request.headers.get("content-length")
    if content_length is None:
        expected_bytes = 0
    else:
        try:
            expected_bytes = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if expected_bytes < 0:
            raise ValueError("invalid Content-Length")
    reserved_bytes = _json_body_residency(expected_bytes)
    reservation = budget.reserve(reserved_bytes)
    if reservation is None:
        raise RetainedInputRejected

    chunks: list[bytes] = []
    received = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            retained_bytes = max(reserved_bytes, _json_body_residency(received))
            if not reservation.resize(retained_bytes):
                raise RetainedInputRejected
            reserved_bytes = retained_bytes
            chunks.append(bytes(chunk))
        body = b"".join(chunks)
        value = json.loads(body)
        return (value if isinstance(value, dict) else None), reservation
    except BaseException:
        reservation.release()
        raise
