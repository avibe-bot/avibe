"""Path resolution and per-file mutation serialization for the file tools.

Ported from Pi ``packages/coding-agent/src/core/tools/path-utils.ts``,
``file-mutation-queue.ts``, and ``src/utils/paths.ts`` (MIT, Copyright (c) 2025
Mario Zechner).
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

from core.agent_core.tools.args import ToolInputError

_UNICODE_SPACES = re.compile("[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
_NARROW_NO_BREAK_SPACE = "\u202f"
_AM_PM = re.compile(r" (AM|PM)\.", re.IGNORECASE)


def expand_path(path: str) -> str:
    """Normalize a model-supplied path: Unicode spaces, a leading ``@``, ``~``, ``file://``."""
    value = _UNICODE_SPACES.sub(" ", path)
    if value.startswith("@"):
        value = value[1:]
    if value == "~" or value.startswith("~/"):
        value = os.path.expanduser(value)
    if value.startswith("file://"):
        value = unquote(urlparse(value).path)
    return value


def resolve_to_cwd(path: str, cwd: str) -> str:
    """The absolute path; raises ``ToolInputError`` for a path no file can have (a NUL byte, possibly from ``%00``)."""
    expanded = expand_path(path)
    if "\x00" in expanded:
        raise ToolInputError(f"Invalid path: {path!r} contains a NUL byte")
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(cwd, expanded))


def resolve_read_path(path: str, cwd: str) -> str:
    """Like ``resolve_to_cwd``, also trying the spellings macOS uses in screenshot names."""
    resolved = resolve_to_cwd(path, cwd)
    if os.path.exists(resolved):
        return resolved
    am_pm = _AM_PM.sub(lambda m: f"{_NARROW_NO_BREAK_SPACE}{m.group(1)}.", resolved)
    nfd = unicodedata.normalize("NFD", resolved)
    curly = resolved.replace("'", "\u2019")
    nfd_curly = nfd.replace("'", "\u2019")
    for candidate in (am_pm, nfd, curly, nfd_curly):
        if candidate != resolved and os.path.exists(candidate):
            return candidate
    return resolved


class _PathLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_locks: dict[str, _PathLock] = {}


@asynccontextmanager
async def file_mutation_lock(path: str) -> AsyncIterator[None]:
    """Serialize ``write`` and ``edit`` on one file, keyed by its canonical path.

    Different files proceed in parallel. The key follows symlinks, so two names
    for one file share a lock.
    """
    key = os.path.realpath(path)
    entry = _locks.get(key)
    if entry is None:
        entry = _locks[key] = _PathLock()
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:
            del _locks[key]
