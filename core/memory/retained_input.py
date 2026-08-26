"""Aggregate admission for unbounded caller-owned Memory inputs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any


MAX_RETAINED_INPUT_BYTES = 64 * 1024 * 1024
MAX_RETAINED_INPUT_RESERVATIONS = 32
_JSON_BODY_SPOOL_CHUNK_BYTES = 64 * 1024
_JSON_STRING_CHUNK_CHARS = 8 * 1024
_JSON_BODY_PROCESS_THRESHOLD_BYTES = 64 * 1024
_JSON_BODY_SPOOL_FREE_BYTES = 512 * 1024 * 1024
_JSON_BODY_PARSE_TIMEOUT_SECONDS = 30.0
_JSON_BODY_SPOOL_LOCK = threading.Lock()


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
    # Content-Length is only a declaration. Reserve the small initial body
    # footprint, then grow admission as bytes are actually retained so a slow
    # upload cannot monopolize the aggregate budget before sending its body.
    reserved_bytes = _json_body_residency(0)
    reservation = budget.reserve(reserved_bytes)
    if reservation is None:
        raise RetainedInputRejected

    try:
        spool = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix="avibe-memory-request-",
            suffix=".json",
            delete=False,
        )
    except BaseException:
        reservation.release()
        raise
    path = spool.name
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
            view = memoryview(chunk)
            for offset in range(0, len(view), _JSON_BODY_SPOOL_CHUNK_BYTES):
                await asyncio.to_thread(
                    _write_json_body_spool_chunk,
                    spool,
                    view[offset : offset + _JSON_BODY_SPOOL_CHUNK_BYTES],
                )
        await asyncio.to_thread(spool.flush)
        await asyncio.to_thread(spool.close)
        value = await _parse_json_body_spool(path, byte_count=received)
        return (value if isinstance(value, dict) else None), reservation
    except BaseException:
        reservation.release()
        raise
    finally:
        if not spool.closed:
            spool.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _write_json_body_spool_chunk(spool: Any, chunk: memoryview) -> None:
    with _JSON_BODY_SPOOL_LOCK:
        free_bytes = shutil.disk_usage(tempfile.gettempdir()).free
        if free_bytes - len(chunk) < _JSON_BODY_SPOOL_FREE_BYTES:
            raise OSError(
                errno.ENOSPC,
                "insufficient temporary storage for Memory request body",
            )
        written = spool.write(chunk)
        if written != len(chunk):
            raise OSError(errno.EIO, "short Memory request spool write")
        spool.flush()


def _load_json_body_spool(path: str) -> Any:
    with open(path, "rb") as spool:
        return json.load(spool)


def _terminate_json_body_pool(
    pool: concurrent.futures.ProcessPoolExecutor,
) -> None:
    processes = tuple((getattr(pool, "_processes", None) or {}).values())
    for process in processes:
        process.terminate()
    for process in processes:
        process.join()
    pool.shutdown(wait=True, cancel_futures=True)


async def _parse_json_body_spool(path: str, *, byte_count: int) -> Any:
    if byte_count <= _JSON_BODY_PROCESS_THRESHOLD_BYTES:
        return await asyncio.to_thread(_load_json_body_spool, path)
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    )
    terminated = False
    try:
        future = asyncio.get_running_loop().run_in_executor(
            pool,
            _load_json_body_spool,
            path,
        )
        return await asyncio.wait_for(
            future,
            timeout=_JSON_BODY_PARSE_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        terminated = True
        await asyncio.to_thread(_terminate_json_body_pool, pool)
        raise
    finally:
        if not terminated:
            await asyncio.to_thread(
                pool.shutdown,
                wait=True,
                cancel_futures=True,
            )


def _json_scalar_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def iter_json_bytes(value: object) -> Iterator[bytes]:
    """Serialize JSON values incrementally without a payload-sized byte buffer."""

    stack: list[tuple[str, object]] = [("value", value)]
    while stack:
        action, current = stack.pop()
        if action == "emit":
            if not isinstance(current, bytes):
                raise TypeError("invalid JSON stream token")
            yield current
            continue
        if action == "string":
            text = current
            if not isinstance(text, str):
                raise TypeError("JSON object keys must be strings")
            yield b'"'
            for offset in range(0, len(text), _JSON_STRING_CHUNK_CHARS):
                encoded = json.dumps(
                    text[offset : offset + _JSON_STRING_CHUNK_CHARS],
                    ensure_ascii=False,
                )[1:-1].encode("utf-8")
                if encoded:
                    yield encoded
            yield b'"'
            continue
        if action == "mapping":
            iterator, first = current  # type: ignore[misc]
            try:
                key, item = next(iterator)
            except StopIteration:
                yield b"}"
                continue
            if not first:
                yield b","
            stack.append(("mapping", (iterator, False)))
            stack.append(("value", item))
            stack.append(("emit", b":"))
            stack.append(("string", key))
            continue
        if action == "sequence":
            iterator, first = current  # type: ignore[misc]
            try:
                item = next(iterator)
            except StopIteration:
                yield b"]"
                continue
            if not first:
                yield b","
            stack.append(("sequence", (iterator, False)))
            stack.append(("value", item))
            continue
        if isinstance(current, str):
            stack.append(("string", current))
        elif current is None or isinstance(current, (bool, int, float)):
            yield _json_scalar_bytes(current)
        elif isinstance(current, Mapping):
            yield b"{"
            stack.append(("mapping", (iter(current.items()), True)))
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            yield b"["
            stack.append(("sequence", (iter(current), True)))
        else:
            raise TypeError(f"unsupported JSON value: {type(current).__name__}")


async def stream_json_bytes(value: object) -> AsyncIterator[bytes]:
    for chunk in iter_json_bytes(value):
        yield chunk
        await asyncio.sleep(0)
