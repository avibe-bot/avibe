"""Keep private mobile files outside browser file, media and Show capabilities."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from config import paths


def is_mobile_private_path(value: str | Path, *, include_ancestors: bool = False) -> bool:
    """Compare resolved paths, including shipped home aliases and encoded spellings.

    Ancestor checks protect recursive copy/move/export operations; listing an
    ordinary parent remains allowed. Local CLI access does not use this policy.
    """
    roots = (
        paths.get_state_dir() / "mobile",
        Path.home() / ".avibe" / "state" / "mobile",
        Path.home() / ".vibe_remote" / "state" / "mobile",
    )
    raw = os.fspath(value)
    try:
        for _ in range(8):
            decoded = unquote(raw)
            if decoded == raw:
                break
            raw = decoded
        target = str(Path(raw).expanduser().resolve(strict=False)).rstrip("/\\").casefold()
        for root in roots:
            private = str(root.resolve(strict=False)).rstrip("/\\").casefold()
            if target == private or target.startswith(private + os.sep):
                return True
            if include_ancestors and (not target or private.startswith(target + os.sep)):
                return True
    except (OSError, RuntimeError, ValueError):
        # An unresolvable browser path cannot safely expose private state.
        return True
    return False
