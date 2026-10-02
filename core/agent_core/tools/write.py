"""The ``write`` tool (C-7 section 3).

Ported from Pi ``packages/coding-agent/src/core/tools/write.ts`` (MIT, Copyright
(c) 2025 Mario Zechner); the description and result text are Pi's.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.agent_core.tools.args import ToolInputError, error_result, str_arg, text_result
from core.agent_core.tools.base import ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.paths import (
    KIND_REASON,
    file_mutation_lock,
    os_reason,
    resolve_to_cwd,
    target_kind,
    to_thread_joined,
)

WRITE_DESCRIPTION = (
    "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates "
    "parent directories."
)

WRITE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
        "content": {"type": "string", "description": "Content to write to the file"},
    },
}


class WriteTool:
    def __init__(self) -> None:
        self._spec = ToolSpec(name="write", description=WRITE_DESCRIPTION, input_schema=WRITE_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = str_arg(arguments, "path")
            content = str_arg(arguments, "content")
            absolute = resolve_to_cwd(path, ctx.cwd)
        except ToolInputError as exc:
            return error_result(str(exc))
        async with file_mutation_lock(absolute):
            # Checked before each step, never in the middle of one, so the lock is held until
            # the filesystem operation in progress has finished.
            if ctx.cancel.cancelled:
                return error_result("Operation aborted")
            try:
                refusal = await to_thread_joined(_prepare_target, absolute)
                if refusal:
                    return error_result(f"Cannot write {path}: {refusal}.")
                if ctx.cancel.cancelled:
                    return error_result("Operation aborted")
                await to_thread_joined(write_text, absolute, content)
            except NotReplaceable:
                return error_result(
                    f"Cannot write {path}: its directory is not writable, so the file cannot be replaced safely."
                )
            except OSError as exc:
                return error_result(f"Cannot write {path}: {os_reason(exc)}.")
        return text_result(f"Successfully wrote to {path}")


def _prepare_target(absolute: str) -> Optional[str]:
    """Why ``write`` will not replace what ``absolute`` names, or ``None`` once it may.

    The target is what the path names after symlinks: only a writable regular file is replaced; a
    missing target gets its parent directories (also behind a dangling symlink); anything else
    (a directory, FIFO, socket, device, or a file this user cannot write) is left as it is.
    """
    target = os.path.realpath(absolute)
    try:
        kind = target_kind(target)
    except FileNotFoundError:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return None
    if kind != "regular":
        return KIND_REASON[kind]
    # A rename checks the directory, not the file, so a read-only file must be refused here.
    if not os.access(target, os.W_OK):
        return "permission denied"
    return None


def write_text(path: str, content: str) -> None:
    """UTF-8 without newline translation; ``content`` came through the one sanitizer for model text."""
    write_bytes(path, content.encode("utf-8"))


class FileChanged(Exception):
    """The file is no longer the one a read-modify-write read; nothing was written."""


class NotReplaceable(Exception):
    """An existing file in a directory no temp file can be created in; nothing was written.

    Writing it in place could leave it half-written (ENOSPC, a quota), so it is refused.
    """


@dataclass(frozen=True)
class FileIdentity:
    """The file a read-modify-write read, so its result is published only over that same file."""

    path: str  # the realpath that was read
    dev: int
    ino: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, path: str, st: os.stat_result) -> "FileIdentity":
        return cls(path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _require(expected: Optional[FileIdentity], path: str, st: os.stat_result) -> None:
    if expected is not None and FileIdentity.of(path, st) != expected:
        raise FileChanged()


def write_bytes(path: str, data: bytes, expected: Optional[FileIdentity] = None) -> None:
    """Create or replace ``path`` only once ``data`` is completely on disk.

    A failed write (a full disk, a quota) leaves the original, or its absence, as it was. The temp
    file sits beside the real target (through symlinks). Replacing a file, it is private (0600) while
    it receives the new contents and takes the file's mode once they are fsynced; a new file's temp is
    created with the umask default it keeps, so the process umask is never read or changed. Then
    ``os.replace`` puts it in place.

    An existing file whose directory does not let a temp file be created is refused
    (``NotReplaceable``) rather than written in place, which a full disk could leave half-written.

    With ``expected``, the file must still be the one that was read, checked once right before the
    rename. POSIX has no compare-and-rename, so a change between that check and the rename is not seen.
    """
    target = os.path.realpath(path)
    try:
        mode: Optional[int] = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    tmp = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if mode is not None else 0o666)
    except PermissionError:
        if mode is None:
            raise
        raise NotReplaceable() from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
        if expected is not None:
            current = os.path.realpath(path)
            try:
                _require(expected, current, os.stat(current))
            except FileNotFoundError:
                raise FileChanged() from None
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
