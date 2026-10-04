"""The ``write`` tool (C-7 section 3).

Ported from Pi ``packages/coding-agent/src/core/tools/write.ts`` (MIT, Copyright
(c) 2025 Mario Zechner); the description and result text are Pi's.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.agent_core.tools.args import ToolInputError, error_result, str_arg, text_result
from core.agent_core.tools.base import ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.paths import (
    KIND_REASON,
    NotRegularFile,
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
        pinned = ctx.pinned_target
        async with file_mutation_lock(absolute):
            # Checked before each step, never in the middle of one, so the lock is held until
            # the filesystem operation in progress has finished.
            if ctx.cancel.cancelled:
                return error_result("Operation aborted")
            try:
                if pinned is not None:
                    # Before any parent directory is created; write_bytes binds it again up to the rename.
                    await to_thread_joined(_require_pinned_path, absolute, pinned)
                refusal = await to_thread_joined(_prepare_target, absolute)
                if refusal:
                    return error_result(f"Cannot write {path}: {refusal}.")
                if ctx.cancel.cancelled:
                    return error_result("Operation aborted")
                await to_thread_joined(write_text, absolute, content, pinned)
            except NotPinned:
                return error_result(f"Cannot write {path}: it no longer resolves to the authorized location.")
            except NotReplaceable:
                return error_result(
                    f"Cannot write {path}: its directory is not writable, so the file cannot be replaced safely."
                )
            except FileChanged:
                return error_result(f"Cannot write {path}: it changed while it was being written; write it again.")
            except NotRegularFile as exc:
                return error_result(f"Cannot write {path}: {KIND_REASON[exc.kind]}.")
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


def write_text(path: str, content: str, pinned: Optional[str] = None) -> None:
    """UTF-8 without newline translation; ``content`` came through the one sanitizer for model text."""
    write_bytes(path, content.encode("utf-8"), pinned=pinned)


class FileChanged(Exception):
    """The file is no longer the one a read-modify-write read; nothing was written."""


class NotPinned(Exception):
    """The path no longer resolves to the target its caller authorized; nothing was written."""


def _require_pinned(resolved: str, pinned: Optional[str]) -> None:
    if pinned is not None and resolved != pinned:
        raise NotPinned()


def _require_pinned_path(path: str, pinned: str) -> None:
    _require_pinned(os.path.realpath(path), pinned)


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
    # Changes on every write and metadata change, and cannot be set back like mtime (rsync -t, tar).
    ctime_ns: int

    @classmethod
    def of(cls, path: str, st: os.stat_result) -> "FileIdentity":
        return cls(path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _require(expected: Optional[FileIdentity], path: str, st: os.stat_result) -> None:
    if expected is not None and FileIdentity.of(path, st) != expected:
        raise FileChanged()


def _check_publishable(
    path: str, target: str, expected: Optional[FileIdentity], pinned: Optional[str] = None
) -> None:
    """Right before the rename: ``path`` still names ``target``, which is absent or a writable regular file
    (for a read-modify-write, the one that was read), and is the ``pinned`` target when there is one."""
    current = os.path.realpath(path)
    _require_pinned(current, pinned)
    if current != target:
        raise FileChanged()
    try:
        st = os.stat(current)
    except FileNotFoundError:
        if expected is not None:
            raise FileChanged() from None
        return
    if not stat.S_ISREG(st.st_mode):
        raise NotRegularFile("directory" if stat.S_ISDIR(st.st_mode) else "other")
    if not os.access(current, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), current)
    _require(expected, current, st)


def write_bytes(
    path: str, data: bytes, expected: Optional[FileIdentity] = None, pinned: Optional[str] = None
) -> None:
    """Create or replace ``path`` only once ``data`` is completely on disk.

    A failed write (a full disk, a quota) leaves the original, or its absence, as it was. The temp
    file sits beside the real target (through symlinks). Replacing a file, it is private (0600) while
    it receives the new contents and takes the file's mode once they are fsynced; a new file's temp is
    created with the umask default it keeps, so the process umask is never read or changed. Then
    ``os.replace`` puts it in place.

    An existing file whose directory does not let a temp file be created is refused
    (``NotReplaceable``) rather than written in place, which a full disk could leave half-written.

    Right before the rename, the path must still name the same target, which must be absent or a
    writable regular file, as ``write`` first classified it; with ``expected``, it must also be the very
    file that was read. So a file swapped for a FIFO, a directory, or a read-only file, or a path
    retargeted to another file, is never written behind the result's back. POSIX has no
    compare-and-rename, so a change between that check and the rename is not seen. With ``pinned``
    (C-9's checkpoint turn), the path must also resolve to that authorized target, from the start
    through that check.
    """
    target = os.path.realpath(path)
    _require_pinned(target, pinned)
    try:
        mode: Optional[int] = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    # Short and not derived from the target's name, which may already be at NAME_MAX.
    tmp = os.path.join(os.path.dirname(target), f".avibe-{secrets.token_hex(6)}.tmp")
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
        _check_publishable(path, target, expected, pinned)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
