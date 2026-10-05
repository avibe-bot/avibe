"""The checkpoint turn's scratch files, through one directory descriptor (C-9 ``context.md`` section 6).

Scratch is flat: every file is a single name directly inside the Session's
scratch root. The root is opened once, when the checkpoint turn starts
(``O_DIRECTORY | O_NOFOLLOW``, then ``fstat``), and every read, temporary
file, and publication is relative to that descriptor, never to a pathname.
Renaming the root or swapping it, or a name in it, for a symlink after it was
opened therefore cannot redirect anything: a name is never followed, and the
descriptor keeps naming the directory that was opened.

``write`` and ``edit`` keep the tools' names, arguments, and result texts
(C-7); ``edit`` reuses ``edit_diff`` for matching, BOM, and line endings.
Where the platform cannot open and rename relative to a directory descriptor
(Windows), no scratch root is opened and the policy denies scratch writes.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from typing import Any, Mapping, Optional

from core.agent_core.tools.args import ToolInputError, error_result, str_arg, text_result
from core.agent_core.tools.base import ToolResult
from core.agent_core.tools.edit import MAX_EDIT_BYTES, _edits_arg, edit_summary, prepare_edit_arguments
from core.agent_core.tools.edit_diff import EditError, ResultTooLarge, apply_edits
from core.agent_core.tools.text import decode_file, encode_file


def supported() -> bool:
    """Whether this platform can open and rename relative to a directory descriptor.

    CPython lists ``renameat`` support under ``os.rename``; ``os.replace`` is the same call.
    """
    return os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


def valid_name(name: str) -> bool:
    """A single file name: no separator, and neither ``.`` nor ``..``."""
    separators = {os.sep} | ({os.altsep} if os.altsep else set())
    return bool(name) and name not in {".", ".."} and not any(separator in name for separator in separators)


class ScratchRoot:
    """The opened scratch root of one checkpoint turn; ``close`` releases the descriptor."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @classmethod
    def open(cls, path: str) -> Optional["ScratchRoot"]:
        """Create and open the root; None when the platform cannot, or it is not a real directory."""
        if not supported():
            return None
        try:
            os.makedirs(path, exist_ok=True)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return None
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return cls(fd)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)

    # --- the tools -------------------------------------------------------------------------

    def write(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        path = arguments.get("path")
        try:
            content = str_arg(arguments, "content")
        except ToolInputError as exc:
            return error_result(str(exc))
        try:
            refusal = self._replaceable(name)
            if refusal:
                return error_result(f"Cannot write {path}: {refusal}.")
            self._publish(name, content.encode("utf-8"))
        except OSError as exc:
            return error_result(f"Cannot write {path}: {os.strerror(exc.errno) if exc.errno else exc}.")
        return text_result(f"Successfully wrote to {path}")

    def edit(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            arguments = prepare_edit_arguments(arguments)
            path = str_arg(arguments, "path")
            edits = _edits_arg(arguments)
        except ToolInputError as exc:
            return error_result(str(exc))
        try:
            raw, identity = self._read(name)
            if raw is None:
                return error_result(f"Could not edit file: {path}. It is not a regular file.")
            if len(raw) > MAX_EDIT_BYTES:
                return error_result(f"File {path} is over the edit limit. Write it again instead.")
            new_text, _, _, counts = apply_edits(decode_file(raw), edits, path, max_result_chars=MAX_EDIT_BYTES)
            self._publish(name, encode_file(new_text), expected=identity)
        except EditError as exc:
            return error_result(str(exc))
        except ResultTooLarge:
            return error_result(f"File {path} would be over the edit limit after this edit.")
        except _Changed:
            return error_result(f"Could not edit file: {path}. It changed while the edit was being applied; read it again.")
        except OSError as exc:
            return error_result(f"Could not edit file: {path}. {os.strerror(exc.errno) if exc.errno else exc}.")
        return text_result(edit_summary(path, edits, counts))

    # --- descriptor-relative steps ------------------------------------------------------------

    def _lstat(self, name: str) -> Optional[os.stat_result]:
        try:
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _replaceable(self, name: str) -> Optional[str]:
        info = self._lstat(name)
        if info is None or stat.S_ISREG(info.st_mode):
            return None
        return "it is a directory" if stat.S_ISDIR(info.st_mode) else "it is not a regular file"

    def _read(self, name: str) -> tuple[Optional[bytes], Optional[tuple[int, int, int]]]:
        """The bytes of a regular file, never through a symlink, and its identity; None for anything else."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # the name is a symlink: never followed
                return None, None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, None
            chunks, size = [], 0
            while size <= MAX_EDIT_BYTES:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return b"".join(chunks), (info.st_ino, info.st_size, info.st_mtime_ns)
        finally:
            os.close(fd)

    def _publish(self, name: str, data: bytes, *, expected: Optional[tuple[int, int, int]] = None) -> None:
        """A private temp file beside ``name`` in the root, fsynced, then renamed over it; all by descriptor."""
        tmp = f".avibe-{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if expected is not None:
                info = self._lstat(name)
                if info is None or (info.st_ino, info.st_size, info.st_mtime_ns) != expected:
                    raise _Changed()
            os.replace(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self._fd)
            raise


class _Changed(Exception):
    """The file is no longer the one an edit read; nothing was written."""
