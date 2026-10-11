"""Path resolution and per-file mutation serialization for the file tools.

Ported from Pi ``packages/coding-agent/src/core/tools/path-utils.ts``,
``file-mutation-queue.ts``, and ``src/utils/paths.ts`` (MIT, Copyright (c) 2025
Mario Zechner).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import ipaddress
import os
import re
import stat
import unicodedata
from contextlib import asynccontextmanager
from concurrent.futures import Executor
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional, TypeVar
from urllib.parse import unquote

from core.agent_core.tools.args import ToolInputError

T = TypeVar("T")

_UNICODE_SPACES = re.compile("[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
_NARROW_NO_BREAK_SPACE = "\u202f"
_AM_PM = re.compile(r" (AM|PM)\.", re.IGNORECASE)
_WINDOWS = os.name == "nt"
_ENCODED_SEPARATOR = re.compile("%2f", re.IGNORECASE)
_ENCODED_SEPARATOR_WINDOWS = re.compile("%2f|%5c", re.IGNORECASE)
_MALFORMED_ESCAPE = re.compile("%(?![0-9a-fA-F]{2})")
_C0_OR_SPACE = "".join(map(chr, range(0x21)))
_TAB_OR_NEWLINE = dict.fromkeys(map(ord, "\t\n\r"))
_QUERY_OR_FRAGMENT = re.compile("[?#]")
_DRIVE = re.compile(r"[A-Za-z][:|]\Z")
_NORMALIZED_DRIVE = re.compile(r"[A-Za-z]:\Z")
_FORBIDDEN_HOST = re.compile(r"[\x00-\x20#%/:<>?@\[\\\]^|\x7f]")
_IDNA_JOINERS = re.compile("[\u1806\u200c\u200d]")
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]*)\Z")
_SINGLE_DOT = {".", "%2e"}
_DOUBLE_DOT = {"..", ".%2e", "%2e.", "%2e%2e"}


def expand_path(path: str) -> str:
    """Normalize a model-supplied path: Unicode spaces, a leading ``@``, ``~``, ``file://``."""
    value = _UNICODE_SPACES.sub(" ", path)
    if value.startswith("@"):
        value = value[1:]
    if value == "~" or value.startswith("~/"):
        value = os.path.expanduser(value)
    if value.startswith("file://"):
        value = _file_url_to_path(value)
    return value


def _file_url_to_path(url: str) -> str:
    """Node's ``fileURLToPath``, which Pi uses, after the WHATWG URL parser's steps for a file URL.

    The parser removes C0 controls and spaces at either end and every tab and newline, reads a
    backslash as ``/``, drops the query and fragment, percent-decodes and lowercases the host, takes
    ``file://C:/`` as a path, writes a drive ``C|`` as ``C:``, and resolves ``.`` and ``..`` segments
    (``%2e`` too) without climbing above a drive. An empty or ``localhost`` host names this machine.
    Any other host is a UNC share on Windows and an error elsewhere, so a URL can never name a local
    file it did not mean. Encoded separators are refused, as Node refuses them.
    """
    rest = url.strip(_C0_OR_SPACE).translate(_TAB_OR_NEWLINE).replace("\\", "/")[len("file://") :]
    rest = _QUERY_OR_FRAGMENT.split(rest, maxsplit=1)[0]
    authority, slash, path = rest.partition("/")
    if _DRIVE.match(authority):
        authority, path = "", f"{authority}/{path}" if slash else authority
    host = _url_host(authority)
    pathname = _url_pathname(path)
    if _WINDOWS:
        if _ENCODED_SEPARATOR_WINDOWS.search(pathname):
            raise ToolInputError("Invalid path: a file URL must not include encoded \\ or / characters")
        local = _decode_url_path(pathname).replace("/", "\\")
        if host:
            return f"\\\\{host}{local}"
        if len(local) >= 3 and local[1].isascii() and local[1].isalpha() and local[2] == ":":
            return local[1:]
        raise ToolInputError("Invalid path: a file URL must be absolute")
    if host:
        raise ToolInputError("Invalid path: file URL host must be empty or localhost")
    if _ENCODED_SEPARATOR.search(pathname):
        raise ToolInputError("Invalid path: a file URL must not include encoded / characters")
    return _decode_url_path(pathname)


def _url_host(authority: str) -> str:
    """The WHATWG host of a file URL, lowercased, and empty for ``localhost``.

    Only hosts whose WHATWG form is the text itself are kept: ASCII names, dotted-quad IPv4, and
    IPv6 in its compressed form. A host that needs IDNA processing or address parsing to reach
    Node's form is refused rather than guessed (Python has IDNA 2003, Node UTS #46), unless it maps
    to ``localhost``, so a URL never names a share or a file Node would not.
    """
    if not authority:
        return ""
    try:
        host = unquote(authority, errors="strict")
    except UnicodeError:
        raise _invalid_host() from None
    if host.startswith("[") and host.endswith("]"):
        try:
            canonical = ipaddress.IPv6Address(host[1:-1]).compressed
        except ValueError:
            raise _invalid_host() from None
        if "." in host or canonical != host[1:-1].lower():
            raise _invalid_host()
        return f"[{canonical}]"
    if not host.isascii() or "xn--" in host.lower():
        # Node fails a zero-width joiner or non-joiner where Python's IDNA quietly drops it.
        if not _IDNA_JOINERS.search(host):
            with contextlib.suppress(UnicodeError):
                if host.encode("idna").decode("ascii").lower() == "localhost":
                    return ""
        raise _invalid_host()
    host = host.lower()
    if _FORBIDDEN_HOST.search(host):
        raise _invalid_host()
    labels = host.split(".")
    last = labels[-1] or (labels[-2] if len(labels) > 1 else "")
    if _NUMERIC_LABEL.match(last):
        # WHATWG parses it as IPv4 and prints it as a dotted quad; only that form is the text itself.
        try:
            if str(ipaddress.IPv4Address(host)) != host:
                raise ValueError(host)
        except ValueError:
            raise _invalid_host() from None
    return "" if host == "localhost" else host


def _invalid_host() -> ToolInputError:
    return ToolInputError("Invalid path: a file URL must have a valid host")


def _url_pathname(path: str) -> str:
    """The WHATWG path of a file URL (still percent-encoded), from the text after the authority's ``/``."""
    segments: list[str] = []
    parts = path.split("/")
    for index, segment in enumerate(parts):
        last = index == len(parts) - 1
        lowered = segment.lower()
        if lowered in _DOUBLE_DOT:
            if segments and not (len(segments) == 1 and _NORMALIZED_DRIVE.match(segments[0])):
                segments.pop()
            if last:
                segments.append("")
        elif lowered in _SINGLE_DOT:
            if last:
                segments.append("")
        else:
            if not segments and _DRIVE.match(segment):
                segment = segment[0] + ":"
            segments.append(segment)
    return "/" + "/".join(segments)


def _decode_url_path(path: str) -> str:
    """``decodeURIComponent``: a malformed escape or bytes that are not UTF-8 are an error, never a guess."""
    if _MALFORMED_ESCAPE.search(path):
        raise ToolInputError("Invalid path: a file URL must use valid percent-encoded UTF-8")
    try:
        return unquote(path, errors="strict")
    except UnicodeDecodeError:
        raise ToolInputError("Invalid path: a file URL must use valid percent-encoded UTF-8") from None


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


FileKind = Literal["regular", "directory", "other"]

#: Why a tool refuses a file that exists but is not a regular file.
KIND_REASON = {"directory": "it is a directory", "other": "it is not a regular file"}


def target_kind(path: str) -> FileKind:
    """What ``path`` names, following symlinks. ``stat`` never opens the file, so a FIFO cannot block it.

    Raises ``OSError`` as ``stat`` does: missing or dangling (ENOENT), a loop (ELOOP), a file used as a
    directory (ENOTDIR), no search permission (EACCES).
    """
    mode = os.stat(path).st_mode
    if stat.S_ISREG(mode):
        return "regular"
    return "directory" if stat.S_ISDIR(mode) else "other"


class NotRegularFile(OSError):
    """The descriptor names a directory or a special file; ``kind`` says which."""

    def __init__(self, kind: FileKind) -> None:
        super().__init__(KIND_REASON[kind])
        self.kind = kind


def open_regular(path: str, flags: int = os.O_RDONLY) -> int:
    """Open a model-named file once and return the descriptor, which is a regular file.

    ``O_NONBLOCK`` keeps a FIFO swapped in after an earlier check from blocking the open, and the
    kind is taken from the descriptor (``fstat``), so sizes and contents come from the same file.
    Raises ``NotRegularFile`` or the ``OSError`` of the open.
    """
    fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise NotRegularFile("directory" if stat.S_ISDIR(mode) else "other")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_at_most(fd: int, limit: int) -> bytes:
    """Up to ``limit + 1`` bytes from ``fd``, so a caller can tell the file is over ``limit``."""
    chunks, remaining = [], limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def os_reason(error: OSError) -> str:
    """``No such file or directory`` → ``no such file or directory``, for ``Cannot … {path}: {reason}.``"""
    text = error.strerror or str(error) or type(error).__name__
    return text[:1].lower() + text[1:]


def errno_name(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, "EIO")


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


async def to_thread_joined(func: Callable[..., T], /, *args: Any) -> T:
    """``asyncio.to_thread`` for work inside ``file_mutation_lock``: a cancel waits for the thread.

    Cancelling an await does not stop its worker thread, so without the wait a write could land after
    the lock was released and overwrite the next writer's result. The cancel is re-raised once the
    thread is done, however often the caller is cancelled and even when the thread raised.
    """
    return await run_joined(None, func, *args)


async def run_joined(executor: Optional[Executor], func: Callable[..., T], /, *args: Any) -> T:
    """``func(*args)`` on ``executor`` (``None``: asyncio's default); a cancel waits for it (see above)."""
    return await run_to_end(asyncio.get_running_loop().run_in_executor(executor, functools.partial(func, *args)))


async def run_to_end(awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` to its end even if the caller is cancelled meanwhile, then re-raise the cancel.

    For state transitions that must not stop half-way once begun (a worker thread, a decision, a kill).
    """
    worker = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                pass
        if not worker.cancelled():
            worker.exception()  # retrieved; the caller's cancel is what propagates
        raise
