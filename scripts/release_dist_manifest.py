#!/usr/bin/env python3
"""Print the exact manifest of a flat release dist directory, or refuse it.

Every entry must be a regular file directly in the directory (no symlinks, no
subdirectories, and the directory itself not a symlink). The output lists each
name with its sha256, sorted, so two manifests compare equal only when the
publishable set is byte-for-byte the same, with nothing added or removed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path


def manifest(directory: Path) -> str:
    if stat.S_ISLNK(os.lstat(directory).st_mode) or not directory.is_dir():
        raise SystemExit(f"{directory} must be a real directory")
    lines = []
    for name in sorted(os.listdir(directory)):
        path = directory / name
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise SystemExit(f"{path} is not a regular file")
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(manifest(Path(sys.argv[1] if len(sys.argv) > 1 else "dist")))
