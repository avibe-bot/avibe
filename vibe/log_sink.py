from __future__ import annotations

import argparse
import os
import stat
import sys
import threading
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


def _drain(source: BinaryIO, chunk_bytes: int) -> None:
    while source.read(chunk_bytes):
        pass


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
    pending = bytearray()
    source_eof = False
    try:
        read_chunk = getattr(source, "read1", source.read)
        while not lock_ready.is_set():
            chunk = read_chunk(chunk_bytes)
            if not chunk:
                source_eof = True
                break
            pending.extend(chunk)
            if len(pending) > max_bytes:
                del pending[:-max_bytes]
        if source_eof and not lock_ready.is_set():
            lock_ready.wait(timeout=31.0)
        if not lock_acquired:
            if not source_eof:
                _drain(source, chunk_bytes)
            return False

        log_file = _open_regular_log(path)
        if log_file is None:
            raise OSError(f"runtime log path is not a regular file: {path}")
        with log_file:
            _compact_log(log_file, max_bytes=max_bytes, retain_bytes=retain_bytes)
            if pending:
                _write_bounded_chunk(
                    log_file,
                    bytes(pending),
                    max_bytes=max_bytes,
                    retain_bytes=retain_bytes,
                )
            if not source_eof:
                while chunk := source.read(chunk_bytes):
                    _write_bounded_chunk(
                        log_file,
                        chunk,
                        max_bytes=max_bytes,
                        retain_bytes=retain_bytes,
                    )
        return True
    except OSError:
        if not source_eof:
            _drain(source, chunk_bytes)
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
