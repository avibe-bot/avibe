"""Size-bounded, regular-file-only reads for native menu discovery."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def read_native_file(path: Path, limit: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("not a regular native config file")
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversize native config")
    return raw
