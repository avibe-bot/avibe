"""Shared file-target handling for bounded IM downloads."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


@contextmanager
def open_download_target(
    target_path: Path | str,
    *,
    target_fd: int | None = None,
    exclusive_path: bool = False,
) -> Iterator[BinaryIO]:
    """Open a path normally or write through a caller-owned anchored descriptor."""

    if target_fd is not None:
        duplicate = os.dup(target_fd)
        try:
            os.ftruncate(duplicate, 0)
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "wb") as file_obj:
                duplicate = -1
                yield file_obj
        finally:
            if duplicate >= 0:
                os.close(duplicate)
        return

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if exclusive_path:
        target.unlink(missing_ok=True)
    with target.open("xb" if exclusive_path else "wb") as file_obj:
        yield file_obj
