from __future__ import annotations

import argparse
import os
import stat
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO

from storage.lock import MigrationFileLock, MigrationLockTimeout


RUNTIME_LOG_MAX_BYTES = 10 * 1024 * 1024
RUNTIME_LOG_RETAIN_BYTES = 5 * 1024 * 1024
RUNTIME_LOG_TRUNCATION_MARKER = b"[avibe: older runtime output truncated]\n"
_COPY_CHUNK_BYTES = 64 * 1024


def _open_regular_log(path: Path) -> BinaryIO | None:
    try:
        if path.is_symlink():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return None
        return os.fdopen(descriptor, "r+b", buffering=0)
    except OSError:
        return None


def _compact_log(log_file: BinaryIO, *, max_bytes: int, retain_bytes: int) -> None:
    marker = RUNTIME_LOG_TRUNCATION_MARKER[:max_bytes]
    retained_limit = min(max(0, retain_bytes), max(0, max_bytes - len(marker)))
    log_file.seek(0, os.SEEK_END)
    size = log_file.tell()
    if size <= max_bytes:
        return
    log_file.seek(max(0, size - retained_limit))
    tail = log_file.read(retained_limit)
    if size > retained_limit:
        first_newline = tail.find(b"\n")
        if first_newline >= 0:
            tail = tail[first_newline + 1 :]
    log_file.seek(0)
    log_file.write(marker)
    log_file.write(tail)
    log_file.truncate()


def _write_bounded_chunk(
    log_file: BinaryIO,
    chunk: bytes,
    *,
    max_bytes: int,
    retain_bytes: int,
) -> None:
    if len(chunk) >= max_bytes:
        marker = RUNTIME_LOG_TRUNCATION_MARKER
        bounded = (
            marker + chunk[-(max_bytes - len(marker)) :]
            if max_bytes > len(marker)
            else chunk[-max_bytes:]
        )
        log_file.seek(0)
        log_file.write(bounded)
        log_file.truncate()
        return
    log_file.seek(0, os.SEEK_END)
    threshold = max_bytes - len(chunk)
    if log_file.tell() > threshold:
        _compact_log(log_file, max_bytes=threshold, retain_bytes=retain_bytes)
    log_file.seek(0, os.SEEK_END)
    log_file.write(chunk)


def _append_bounded_chunk(
    chunks: deque[bytes],
    chunk: bytes,
    *,
    buffered_bytes: int,
    max_bytes: int,
) -> int:
    if len(chunk) >= max_bytes:
        chunks.clear()
        chunks.append(chunk[-max_bytes:])
        return max_bytes

    chunks.append(chunk)
    buffered_bytes += len(chunk)
    overflow = buffered_bytes - max_bytes
    while overflow > 0:
        oldest = chunks[0]
        if len(oldest) <= overflow:
            chunks.popleft()
            buffered_bytes -= len(oldest)
            overflow -= len(oldest)
            continue
        chunks[0] = oldest[overflow:]
        buffered_bytes -= overflow
        overflow = 0
    return buffered_bytes


def copy_bounded_log(
    source: BinaryIO,
    path: Path,
    *,
    max_bytes: int = RUNTIME_LOG_MAX_BYTES,
    retain_bytes: int = RUNTIME_LOG_RETAIN_BYTES,
    chunk_bytes: int = _COPY_CHUNK_BYTES,
) -> bool:
    """Drain one subprocess stream into a bounded, live-tail-friendly file."""

    max_bytes = max(1, max_bytes)
    chunk_bytes = max(1, chunk_bytes)
    lock = MigrationFileLock(path.with_name(f".{path.name}.sink.lock"), timeout_seconds=0.25)
    lock_ready = threading.Event()
    lock_stop = threading.Event()
    lock_acquired = False
    pending: deque[bytes] = deque()
    pending_bytes = 0
    source_eof = False
    source_failed = False
    pending_ready = threading.Condition()

    def _acquire_lock() -> None:
        nonlocal lock_acquired
        while not lock_stop.is_set():
            try:
                lock.acquire()
                lock_acquired = True
                break
            except MigrationLockTimeout:
                continue
            except OSError:
                break
        lock_ready.set()

    lock_thread = threading.Thread(target=_acquire_lock, name=f"log-sink-lock-{path.name}", daemon=True)
    lock_thread.start()

    def _read_source() -> None:
        nonlocal pending_bytes, source_eof, source_failed
        read_chunk = getattr(source, "read1", None) or source.read
        try:
            while chunk := read_chunk(chunk_bytes):
                with pending_ready:
                    pending_bytes = _append_bounded_chunk(
                        pending,
                        chunk,
                        buffered_bytes=pending_bytes,
                        max_bytes=max_bytes,
                    )
                    pending_ready.notify()
        except OSError:
            source_failed = True
        finally:
            with pending_ready:
                source_eof = True
                pending_ready.notify_all()

    reader_thread = threading.Thread(target=_read_source, name=f"log-sink-reader-{path.name}", daemon=True)
    reader_thread.start()
    try:
        eof_wait_deadline: float | None = None
        while not lock_ready.wait(timeout=0.05):
            with pending_ready:
                reached_eof = source_eof
            if not reached_eof:
                continue
            if eof_wait_deadline is None:
                eof_wait_deadline = time.monotonic() + 31.0
            elif time.monotonic() >= eof_wait_deadline:
                break
        if not lock_acquired:
            reader_thread.join()
            return False

        log_file = _open_regular_log(path)
        if log_file is None:
            raise OSError(f"runtime log path is not a regular file: {path}")
        with log_file:
            _compact_log(log_file, max_bytes=max_bytes, retain_bytes=retain_bytes)
            while True:
                with pending_ready:
                    while not pending and not source_eof:
                        pending_ready.wait()
                    chunk = b"".join(pending)
                    pending.clear()
                    pending_bytes = 0
                    finished = source_eof and not chunk
                if chunk:
                    _write_bounded_chunk(
                        log_file,
                        chunk,
                        max_bytes=max_bytes,
                        retain_bytes=retain_bytes,
                    )
                if finished:
                    break
        reader_thread.join()
        return not source_failed
    except OSError:
        reader_thread.join()
        return False
    finally:
        lock_stop.set()
        lock_thread.join(timeout=1.0)
        if lock_acquired:
            lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-bytes", type=int, default=RUNTIME_LOG_MAX_BYTES)
    parser.add_argument("--retain-bytes", type=int, default=RUNTIME_LOG_RETAIN_BYTES)
    args = parser.parse_args(argv)
    copied = copy_bounded_log(
        sys.stdin.buffer,
        args.path,
        max_bytes=max(1, args.max_bytes),
        retain_bytes=max(0, args.retain_bytes),
    )
    return 0 if copied else 1


if __name__ == "__main__":
    raise SystemExit(main())
