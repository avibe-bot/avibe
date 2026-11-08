"""Stdlib-only release downloads for build/CI scripts (not runtime mirror policy)."""

from __future__ import annotations

import errno
import http.client
import logging
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")
_MAX_ATTEMPTS = 4
_REQUEST_TIMEOUT = 10.0
_RETRY_WINDOW = 45.0


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (ConnectionResetError, TimeoutError, http.client.RemoteDisconnected,
                               http.client.IncompleteRead)) or (
        isinstance(reason, OSError) and reason.errno in {errno.ECONNRESET, errno.ETIMEDOUT}
    )


def _with_retry(operation: Callable[[float], _T]) -> _T:
    # Four stalled attempts plus 1/2/4s backoff and <=0.5s jitter each fit in
    # roughly a minute. The window bounds retries, not healthy large transfers;
    # urllib's timeout is a socket inactivity limit, not a transfer deadline.
    deadline = time.monotonic() + _RETRY_WINDOW
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return operation(min(_REQUEST_TIMEOUT, max(0.001, deadline - time.monotonic())))
        except Exception as exc:
            # HTTPError owns a response stream even when urlopen never returns.
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if not _retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = 2.0**attempt + random.uniform(0.0, 0.5)
            if time.monotonic() + delay >= deadline:
                raise
            detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            logging.getLogger(__name__).warning(
                "Release download attempt %d/%d failed (%s); retrying in %.1fs",
                attempt + 1, _MAX_ATTEMPTS, detail, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def read_bytes(
    request: str | urllib.request.Request,
    *,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    """Read fresh bytes on each transient retry; validation belongs to callers."""
    resolved_opener = opener or urllib.request.urlopen

    def read(timeout: float) -> bytes:
        with resolved_opener(request, timeout=timeout) as response:
            return response.read()

    return _with_retry(read)


def download_to_path(
    request: str | urllib.request.Request,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> None:
    """Stream each attempt from byte zero, never retaining an incomplete file."""
    def download(timeout: float) -> None:
        started = False
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
                started = True
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if max_bytes is not None and downloaded > max_bytes:
                        raise ValueError("release asset exceeds its size limit")
                    output.write(chunk)
        except Exception:
            if started:
                destination.unlink(missing_ok=True)
            raise

    _with_retry(download)
