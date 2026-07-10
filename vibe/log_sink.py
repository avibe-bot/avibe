from __future__ import annotations

import argparse
import os
import stat
import sys
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
    lock = MigrationFileLock(path.with_name(f".{path.name}.sink.lock"), timeout_seconds=30.0)
    try:
        with lock:
            log_file = _open_regular_log(path)
            if log_file is None:
                raise OSError(f"runtime log path is not a regular file: {path}")
            with log_file:
                _compact_log(log_file, max_bytes=max_bytes, retain_bytes=retain_bytes)
                while chunk := source.read(chunk_bytes):
                    if len(chunk) >= max_bytes:
                        log_file.seek(0)
                        log_file.write(chunk[-max_bytes:])
                        log_file.truncate()
                        continue
                    log_file.seek(0, os.SEEK_END)
                    threshold = max_bytes - len(chunk)
                    if log_file.tell() > threshold:
                        _compact_log(log_file, max_bytes=threshold, retain_bytes=retain_bytes)
                    log_file.seek(0, os.SEEK_END)
                    log_file.write(chunk)
        return True
    except (MigrationLockTimeout, OSError):
        while source.read(chunk_bytes):
            pass
        return False


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
