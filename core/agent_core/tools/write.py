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
from core.agent_core.tools import windows_files
from core.agent_core.tools.paths import (
    KIND_REASON,
    NotRegularFile,
    file_mutation_lock,
    os_reason,
    resolve_to_cwd,
    target_kind,
    to_thread_joined,
)

_WINDOWS = os.name == "nt"

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
    if not may_write(target):
        return "permission denied"
    return None


def may_write(path: str) -> bool:
    """Whether this user may write the existing file ``path``: by its mode on POSIX, and on Windows by its
    ACL, which ``os.access`` does not consult there (it sees only the read-only bit)."""
    return windows_files.may_write(path) if _WINDOWS else os.access(path, os.W_OK)


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
    # Changes on every write and metadata change, and cannot be set back like mtime (rsync -t, tar).
    ctime_ns: int

    @classmethod
    def of(cls, path: str, fd: int) -> "FileIdentity":
        """The identity of the open file ``fd``, which ``path`` names.

        Windows' ``stat`` does not report a change time on every Python (3.10 and 3.11 give the creation time
        as ``st_ctime``), so there it is read from the file itself.
        """
        st = os.fstat(fd)
        ctime_ns = windows_files.change_time_ns(fd) if _WINDOWS else st.st_ctime_ns
        return cls(path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, ctime_ns)

    def matches(self, path: str, st: os.stat_result) -> bool:
        """Whether ``path``, as ``stat`` saw it, is still this file; on Windows it is opened to read its change time."""
        if not _WINDOWS:
            return FileIdentity(path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) == self
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            return FileIdentity.of(path, fd) == self
        finally:
            os.close(fd)


def _check_publishable(path: str, target: str, expected: Optional[FileIdentity]) -> bool:
    """Right before the rename: ``path`` still names ``target``, which is absent or a writable regular file
    (for a read-modify-write, the one that was read). Returns whether it exists."""
    current = os.path.realpath(path)
    if current != target:
        raise FileChanged()
    try:
        st = os.stat(current)
    except FileNotFoundError:
        if expected is not None:
            raise FileChanged() from None
        return False
    if not stat.S_ISREG(st.st_mode):
        raise NotRegularFile("directory" if stat.S_ISDIR(st.st_mode) else "other")
    if not may_write(current):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), current)
    if expected is not None and not expected.matches(current, st):
        raise FileChanged()
    return True


def write_bytes(path: str, data: bytes, expected: Optional[FileIdentity] = None) -> None:
    """Create or replace ``path`` only once ``data`` is completely on disk.

    A failed write (a full disk, a quota) leaves the original, or its absence, as it was. The temp
    file sits beside the real target (through symlinks). Replacing a file, it is private (0600) while
    it receives the new contents and takes the file's mode once they are fsynced; a new file's temp is
    created with the umask default it keeps, so the process umask is never read or changed. Then
    ``os.replace`` puts it in place. On Windows the private temp file has an owner-only DACL and then
    the file's own, and an existing file is replaced with ``ReplaceFileW``, which keeps the rest of
    what is the file's: its encryption, compression, named streams, and creation time
    (``windows_files``).

    An existing file whose directory does not let a temp file be created is refused
    (``NotReplaceable``) rather than written in place, which a full disk could leave half-written.

    Right before the rename, the path must still name the same target, which must be absent or a
    writable regular file, as ``write`` first classified it; with ``expected``, it must also be the very
    file that was read. So a file swapped for a FIFO, a directory, or a read-only file, or a path
    retargeted to another file, is never written behind the result's back. POSIX has no
    compare-and-rename, so a change between that check and the rename is not seen.
    """
    target = os.path.realpath(path)
    try:
        mode: Optional[int] = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    # Short and not derived from the target's name, which may already be at NAME_MAX.
    tmp = os.path.join(os.path.dirname(target), f".avibe-{secrets.token_hex(6)}.tmp")
    try:
        if _WINDOWS and mode is not None:
            fd = windows_files.create_private(tmp)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            fd = os.open(tmp, flags, 0o600 if mode is not None else 0o666)
    except PermissionError:
        if mode is None:
            raise
        raise NotReplaceable() from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if mode is not None and not _WINDOWS:
                os.fchmod(handle.fileno(), mode)
        if _check_publishable(path, target, expected) and _WINDOWS:
            windows_files.copy_dacl(target, tmp)  # private until now; the file's own from here
            windows_files.replace_existing(tmp, target)
        else:
            os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
