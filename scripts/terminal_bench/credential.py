"""Ephemeral benchmark API-key file transport, not a key-custody service.

Harbor Docker exec env values appear in host argv. Instead upload one 0600 file
into a fresh 0700 per-trial directory, validate metadata, then read+unlink before
starting Avibe. Secret bytes never occur in this helper's argv or printed output.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

LIMIT = 65536


@contextmanager
def _directory(path: Path):
    if path.name != "provider.key" or path.parent.name != "credential":
        raise ValueError("unexpected credential input path")
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise ValueError("credential directory must be private and owned by the runner")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _file(path: Path, directory: int):
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1 or not 0 < info.st_size <= LIMIT):
            raise ValueError("credential input must be one owned private regular bounded file")
        yield fd, info
    finally:
        os.close(fd)


def metadata(path: Path) -> dict:
    with _directory(path) as directory:
        with _file(path, directory) as (_fd, info):
            return {"regular": True, "mode": "0600", "directory_mode": "0700", "size": info.st_size}


def consume(path: Path) -> str:
    with _directory(path) as directory:
        try:
            with _file(path, directory) as (fd, _info):
                with os.fdopen(os.dup(fd), "rb") as reader:
                    data = reader.read(LIMIT + 1)
                if len(data) > LIMIT:
                    raise ValueError("credential input grew beyond its bound")
                value = data.decode("utf-8").strip()
                if not value:
                    raise ValueError("empty credential input")
                return value
        finally:
            # A rejected symlink is removed as a link, never followed. This
            # fixed name sits in the freshly claimed per-trial private directory.
            try:
                os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass


def clean(path: Path) -> dict:
    if not path.parent.exists() and not path.parent.is_symlink():
        return {"removed": True}
    with _directory(path) as directory:
        try:
            os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            pass
    path.parent.rmdir()  # Refuse unknown files rather than delete recursively.
    return {"removed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["metadata", "clean"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(metadata(args.path) if args.operation == "metadata" else clean(args.path)))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
