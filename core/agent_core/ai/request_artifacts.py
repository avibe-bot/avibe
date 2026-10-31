"""Failed model request artifacts: the exact bytes a model request sent, kept when it did not succeed.

``drive_sse_stream`` records every model request it sends while a capture scope is active
(``capture_model_requests``); outside one it records nothing, so the library stays hermetic by default.

Each attempt is one directory under the store root:

- ``request.bin``: ``httpx.Request.content`` exactly as sent, never re-serialized.
- ``meta.json``: typed facts only. These are the hop (protocol, provider, model, Source), the URL without its query
  or userinfo, allowlisted headers, the body's size and sha256, the Session and Turn, the state and outcome, the
  HTTP status, and the error kind. Never a credential, never an error message: a provider's message can echo the
  request.
- ``response.bin``: the raw response bytes the transport read anyway, never a read of its own. An HTTP error body
  is what classification read (its first 64 KiB); a stream keeps its first ``STREAM_HEAD_BYTES`` and last
  ``STREAM_TAIL_BYTES``, so a terminal error event survives. ``meta.json`` says whether the body was read to its
  end and what was cut.

Lifecycle. ``_ACTIVE`` holds the attempts this process may still send or stream; retention never removes one.
- ``begin`` writes the pending attempt (and adds it to ``_ACTIVE``) before the request is sent. The send waits for
  that write at most ``ARTIFACT_WAIT_S`` and never past a Stop: a Stop sends nothing (``cancelled`` / ``not_sent``),
  and a stalled write is given up and the request sent untracked (``unknown`` / ``untracked``), settled when the
  write lands.
- Every ending goes through one path, ``RequestAttempt._queue``: in one synchronous step the attempt leaves
  ``_ACTIVE`` and the write of its final state is queued on the worker, so no later cancellation can skip it. The
  write runs to its end whoever waits for it, its outcome retrieved by a done-callback. The driver waits for a
  terminal's write at most ``ARTIFACT_WAIT_S`` and never past a Stop; on a closed or cancelled stream it does not
  wait at all. Endings:
  - a semantically successful request is deleted: a ``Done``, or an Anthropic ``pause_turn`` sub-request that
    ended normally;
  - one that ended in an error is kept as ``failed``, or ``cancelled`` when the turn was stopped;
  - one stopped before it was sent is kept as ``cancelled`` with outcome ``not_sent``;
  - one whose consumer closed it, or whose task was cancelled, before any ending is ``unknown``.
- An attempt whose final state was never written (the process died first) stays ``pending`` on disk. It reads
  ``unknown`` once its owner process is gone, or, in the owner, once it is no
  longer active. It is never treated as a success.

The store root and every export directory are made owner-private by ``core.private_fs`` (on Windows, a protected
DACL inherited by everything created inside). Files are created with ``O_EXCL | O_NOFOLLOW``. All filesystem work
runs on one private worker thread, never on the event loop. A failure to record never changes the request's
outcome; it is logged without payload.

Retention defaults to 7 days and 256 MiB. It runs at the store's own writes, never on a timer: after every kept
ending, and at any other write once ``MAINTENANCE_INTERVAL_S`` has passed. Each run removes expired attempts, then
the oldest finished ones while over the quota (strictly: one larger than the quota goes too). An active attempt is
never removed.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from time import monotonic as _monotonic
from collections import deque
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlsplit, urlunsplit

import psutil

from core.private_fs import ensure_private_directory

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_RETENTION_S = 7 * 24 * 60 * 60
DEFAULT_QUOTA_BYTES = 256 * 1024 * 1024
#: An HTTP error body is kept as far as it was read, up to this size; anything after it is cut and the cut stated.
ERROR_BODY_LIMIT = 1024 * 1024
#: A stream (HTTP 2xx) keeps its start and its end; the middle of a long one is dropped and the drop stated.
STREAM_HEAD_BYTES = 64 * 1024
STREAM_TAIL_BYTES = 1024 * 1024
#: An interrupted write leaves a ``.partial`` directory; one this old belongs to no live write.
_PARTIAL_GRACE_S = 60 * 60
#: Retention runs at the store's own writes, never on a timer: after every kept ending, and at any other write once
#: this long has passed since the last run. An attempt that expires is removed at the first write after that.
MAINTENANCE_INTERVAL_S = 10 * 60
#: The longest a request, its terminal, or its cleanup waits for an artifact write. A write still running then goes
#: on in the worker (its outcome still retrieved); the request does not wait for diagnostics any longer.
ARTIFACT_WAIT_S = 10.0

REQUEST_FILE = "request.bin"
RESPONSE_FILE = "response.bin"
META_FILE = "meta.json"
_FILES = (REQUEST_FILE, RESPONSE_FILE, META_FILE)

ARTIFACT_ID = re.compile(r"^mr_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}$")

#: Headers that name the request's shape and route, never a credential.
_REQUEST_HEADERS = frozenset({
    "accept",
    "anthropic-beta",
    "anthropic-version",
    "content-length",
    "content-type",
    "user-agent",
    "x-avibe-pin-model",
    "x-avibe-pin-source",
})
#: Headers that correlate a response across hops or describe its body.
_RESPONSE_HEADERS = frozenset({
    "content-type",
    "request-id",
    "retry-after",
    "x-avibe-served-hop",
    "x-request-id",
    "x-should-retry",
})

PENDING = "pending"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

_T = TypeVar("_T")

#: Attempts this process has sent and not yet ended, across every store object. Only these of its own pending
#: attempts are active; retention never removes an active attempt.
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


class ArtifactStoreError(RuntimeError):
    """The store cannot be used safely: a path that is not a private directory, or an unknown artifact."""


@dataclass(frozen=True)
class CaptureScope:
    """Where the requests sent in this context are recorded, and the Session and Turn they belong to."""

    store: "RequestArtifactStore"
    session_id: str | None = None
    turn_id: str | None = None
    version: str | None = None


_SCOPE: ContextVar[CaptureScope | None] = ContextVar("avibe_model_request_capture", default=None)


@contextmanager
def capture_model_requests(
    store: "RequestArtifactStore",
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    version: str | None = None,
) -> Iterator[None]:
    """Record every model request sent in this context, and in the tasks it starts, to ``store``."""

    token = _SCOPE.set(CaptureScope(store, session_id, turn_id, version))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return f"mr_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(6)}"


def safe_url(url: str) -> str:
    """The URL without userinfo, query, or fragment: a key some providers take as a query parameter stays out."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def allowed_headers(headers: Mapping[str, str], allowed: frozenset[str]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items() if name.lower() in allowed}


def _process_start(pid: int) -> float | None:
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError, ValueError):
        return None


def _owner_alive(owner: Any) -> bool:
    if not isinstance(owner, Mapping):
        return False
    pid, started = owner.get("pid"), owner.get("started")
    if not isinstance(pid, int) or pid <= 0:
        return False
    current = _process_start(pid)
    if current is None:
        return False
    # A reused pid belongs to a process started later; the clock's precision differs by platform.
    return not isinstance(started, (int, float)) or abs(current - float(started)) < 1.0


class ResponseCapture:
    """The raw response bytes as read, bounded: a whole error body up to its limit, or a stream's head and tail."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.total = 0
        self.read_to_end = False
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self._streaming = False

    def response(self, status: int, headers: Mapping[str, str]) -> None:
        self.status = status
        self.headers = allowed_headers(headers, _RESPONSE_HEADERS)
        self._streaming = 200 <= status < 300

    @property
    def head_limit(self) -> int:
        return STREAM_HEAD_BYTES if self._streaming else ERROR_BODY_LIMIT

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        room = self.head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk and self._streaming:
            if len(chunk) >= STREAM_TAIL_BYTES:
                # One chunk fills the tail alone: only its end is kept, never the chunk itself.
                self._tail.clear()
                self._tail.append(bytes(chunk[-STREAM_TAIL_BYTES:]))
                self._tail_size = STREAM_TAIL_BYTES
                return
            self._tail.append(bytes(chunk))
            self._tail_size += len(chunk)
            # Held strictly within the bound: whole chunks drop first, then the start of the oldest one left.
            while self._tail_size > STREAM_TAIL_BYTES:
                excess = self._tail_size - STREAM_TAIL_BYTES
                first = self._tail[0]
                if len(first) <= excess:
                    self._tail.popleft()
                    self._tail_size -= len(first)
                else:
                    self._tail[0] = first[excess:]
                    self._tail_size -= excess

    def finish(self) -> None:
        self.read_to_end = True


    def _tail_bytes(self) -> bytes:
        return b"".join(self._tail)

    def body(self) -> bytes:
        return bytes(self._head) + self._tail_bytes()

    def summary(self) -> dict[str, Any]:
        tail = self._tail_size
        captured = len(self._head) + tail
        omitted = self.total - captured
        summary: dict[str, Any] = {
            "status": self.status,
            "headers": dict(self.headers),
            "read_bytes": self.total,
            "read_to_end": self.read_to_end,
            "captured_bytes": captured,
            "truncated": omitted > 0,
        }
        if omitted > 0:
            # A stream drops its middle; an error body drops its end (and was not read past its limit).
            summary["truncation"] = "middle" if tail else "end"
            summary["omitted_bytes"] = omitted
            summary["head_bytes"] = len(self._head)
        return summary


class RequestArtifactStore:
    """The private directory of request artifacts. Every method is blocking: callers run them off the event loop."""

    def __init__(
        self,
        root: Path,
        *,
        retention_s: float = DEFAULT_RETENTION_S,
        quota_bytes: int = DEFAULT_QUOTA_BYTES,
    ) -> None:
        self.root = Path(root)
        self.retention_s = retention_s
        self.quota_bytes = quota_bytes
        self._ready = False
        self._retained_at: float | None = None

    # -- paths -------------------------------------------------------------------------------------------------

    def _ensure_root(self) -> Path:
        """The root, owner-private (``core.private_fs``): created so, or repaired, once per store object. A link or
        a non-directory in its place is refused."""

        if not self._ready:
            ensure_private_directory(self.root)
            if self.root.is_symlink():
                raise ArtifactStoreError(f"{self.root} is not a private directory")
            self._ready = True
            # What earlier processes left behind is pruned before this one adds to it.
            self._retain(force=True)
        return self.root

    def _retain(self, *, force: bool) -> None:
        """Apply retention at a write: always after a kept ending, otherwise once per ``MAINTENANCE_INTERVAL_S``."""

        now = _monotonic()
        if not force and self._retained_at is not None and now - self._retained_at < MAINTENANCE_INTERVAL_S:
            return
        self._retained_at = now
        self.prune()

    def path(self, artifact_id: str) -> Path:
        if not ARTIFACT_ID.match(artifact_id):
            raise ArtifactStoreError(f"invalid artifact id: {artifact_id!r}")
        directory = self.root / artifact_id
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            raise ArtifactStoreError(f"artifact {artifact_id} not found") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactStoreError(f"artifact {artifact_id} is not a private directory")
        return directory

    # -- writes ------------------------------------------------------------------------------------------------

    def begin(self, meta: dict[str, Any], body: bytes) -> str:
        """Write a pending attempt and return its id; it is active in this process until ``end`` or ``abandon``."""

        root = self._ensure_root()
        self._retain(force=False)
        artifact_id = _new_id()
        partial = root / f".{artifact_id}.partial"
        os.mkdir(partial, 0o700)
        _write_private(partial / REQUEST_FILE, body)
        _write_private(partial / META_FILE, _encode({**meta, "id": artifact_id}))
        os.rename(partial, root / artifact_id)
        with _ACTIVE_LOCK:
            _ACTIVE.add(artifact_id)
        return artifact_id

    def succeed(self, artifact_id: str) -> None:
        """A semantically successful attempt is not kept."""

        self._remove(self.path(artifact_id))
        self._retain(force=False)

    def fail(self, artifact_id: str, outcome: Mapping[str, Any], capture: ResponseCapture | None) -> None:
        """Keep the attempt with its outcome and the response read, then apply retention."""

        directory = self.path(artifact_id)
        meta = self.meta(artifact_id)
        if capture is not None and capture.total:
            _write_private(directory / RESPONSE_FILE, capture.body())
        meta.update(outcome)
        meta["finished_at"] = _now()
        if capture is not None:
            meta["response"] = capture.summary()
        _replace_private(directory / META_FILE, _encode(meta))
        self._retain(force=True)

    def close(self, artifact_id: str, *, state: str, kind: str) -> None:
        """An attempt that ended with no response to keep: stopped before it was sent (``cancelled``,
        ``not_sent``), or closed with no known outcome (``unknown``). Every reader, in any process, sees it."""

        directory = self.path(artifact_id)
        meta = self.meta(artifact_id)
        meta["state"] = state
        meta["outcome"] = {"kind": kind, "error_kind": None, "http_status": None, "exception_type": None}
        meta["finished_at"] = _now()
        _replace_private(directory / META_FILE, _encode(meta))
        self._retain(force=True)

    # -- reads -------------------------------------------------------------------------------------------------

    def meta(self, artifact_id: str) -> dict[str, Any]:
        path = self.path(artifact_id) / META_FILE
        data = json.loads(_read_private(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise ArtifactStoreError(f"artifact {artifact_id} has no readable metadata")
        return data

    def state(self, meta: Mapping[str, Any]) -> str:
        """The attempt's state as a reader should see it: a pending one nobody is still sending is ``unknown``."""

        state = meta.get("state")
        if state != PENDING:
            return str(state)
        return PENDING if self._is_active(meta) else UNKNOWN

    def _is_active(self, meta: Mapping[str, Any]) -> bool:
        owner = meta.get("owner")
        if isinstance(owner, Mapping) and owner.get("pid") == os.getpid():
            with _ACTIVE_LOCK:
                return meta.get("id") in _ACTIVE
        return _owner_alive(owner)

    def entries(self) -> list[dict[str, Any]]:
        """Every readable attempt, newest first, with its effective state and size on disk."""

        try:
            root_info = os.lstat(self.root)
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ArtifactStoreError(f"{self.root} is not a private directory")
        found: list[dict[str, Any]] = []
        with os.scandir(self.root) as listing:
            for entry in listing:
                if not ARTIFACT_ID.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    meta = self.meta(entry.name)
                except (OSError, ValueError, ArtifactStoreError):
                    continue
                meta["state"] = self.state(meta)
                meta["size_bytes"] = _size(Path(entry.path))
                found.append(meta)
        found.sort(key=lambda meta: str(meta.get("created_at") or ""), reverse=True)
        return found

    # -- retention ---------------------------------------------------------------------------------------------

    def prune(self) -> None:
        if not self.root.is_dir() or self.root.is_symlink():
            return
        now = time.time()
        with os.scandir(self.root) as listing:
            for entry in listing:
                if entry.name.endswith(".partial") and entry.is_dir(follow_symlinks=False):
                    try:
                        if now - entry.stat(follow_symlinks=False).st_mtime > _PARTIAL_GRACE_S:
                            self._remove(Path(entry.path))
                    except OSError:
                        continue
        entries = self.entries()
        cutoff = now - self.retention_s
        kept: list[dict[str, Any]] = []
        for meta in entries:
            if meta["state"] != PENDING and _timestamp(meta.get("created_at")) < cutoff:
                self._remove_quietly(meta)
            else:
                kept.append(meta)
        total = sum(int(meta["size_bytes"]) for meta in kept)
        for meta in reversed(kept):  # oldest first
            if total <= self.quota_bytes:
                break
            if meta["state"] == PENDING:
                continue
            self._remove_quietly(meta)
            total -= int(meta["size_bytes"])

    def _remove_quietly(self, meta: Mapping[str, Any]) -> None:
        try:
            self._remove(self.path(str(meta.get("id"))))
        except (OSError, ArtifactStoreError):
            pass

    @staticmethod
    def _remove(directory: Path) -> None:
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactStoreError(f"{directory} is not a private directory")
        shutil.rmtree(directory)

    # -- export ------------------------------------------------------------------------------------------------

    def export(self, artifact_id: str, destination: Path) -> Path:
        """Copy one attempt's files into ``destination/<id>/``: a new private directory under a directory the
        caller chose. Only this store's files are read; an existing or linked target is refused."""

        source = self.path(artifact_id)
        destination = Path(destination).expanduser()
        if destination.is_symlink():
            raise ArtifactStoreError(f"{destination} is a symbolic link")
        destination.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or not destination.is_dir():
            raise ArtifactStoreError(f"{destination} is not a directory")
        target = destination / artifact_id
        if os.path.lexists(target):
            raise FileExistsError(f"{target} already exists")
        # Owner-private from its creation (on Windows a protected, inherited DACL): the files below start private.
        ensure_private_directory(target)
        if target.is_symlink() or any(target.iterdir()):
            raise ArtifactStoreError(f"{target} is not a new private directory")
        for name in _FILES:
            path = source / name
            try:
                data = _read_private(path)
            except FileNotFoundError:
                continue
            _write_private(target / name, data)
        return target


def _encode(meta: Mapping[str, Any]) -> bytes:
    return (json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _size(directory: Path) -> int:
    total = 0
    for name in _FILES:
        try:
            total += os.lstat(directory / name).st_size
        except OSError:
            continue
    return total


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC | _BINARY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _replace_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    _write_private(temporary, data)
    os.replace(temporary, path)


def _read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _BINARY)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


# -- the attempt the transport drives ---------------------------------------------------------------------------

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _worker() -> ThreadPoolExecutor:
    """One private thread: artifact writes keep their order and never run on the event loop."""

    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="avibe-request-artifacts")
        return _executor


def _submit(function: Callable[..., _T], *args: Any) -> asyncio.Future[_T]:
    """Queue ``function`` on the artifact worker; the caller owns the returned future."""

    return asyncio.get_running_loop().run_in_executor(_worker(), functools.partial(function, *args))


class RequestAttempt:
    """One sent request's artifact, driven by ``drive_sse_stream``. Every ending is queued synchronously and returns
    the write's future; how long the request waits for it (never past a Stop) is the driver's decision. Every step
    is best effort: a failure to record is logged without payload and never reaches the request's outcome."""

    def __init__(self, store: RequestArtifactStore, artifact_id: str) -> None:
        self.store = store
        self.id = artifact_id
        self.capture = ResponseCapture()
        self.exception_type: str | None = None
        self.ended = False

    def succeeded(self) -> asyncio.Future[Any] | None:
        return self._queue("remove a successful request", self.store.succeed, self.id)

    def failed(
        self, *, kind: str, error_kind: str | None = None, status: int | None = None
    ) -> asyncio.Future[Any] | None:
        outcome: dict[str, Any] = {
            "state": CANCELLED if kind == "cancelled" else FAILED,
            "outcome": {
                "kind": kind,
                "error_kind": error_kind,
                "http_status": status if status is not None else self.capture.status,
                "exception_type": self.exception_type,
            },
        }
        return self._queue("keep a failed request", self.store.fail, self.id, outcome, self.capture)

    def not_sent(self) -> asyncio.Future[Any] | None:
        """Stopped before it was sent: the request never went out."""

        return self._queue(
            "keep an unsent request", functools.partial(self.store.close, state=CANCELLED, kind="not_sent"), self.id
        )

    def abandon(self) -> asyncio.Future[Any] | None:
        """Closed with no known outcome: its consumer stopped reading, or its task was cancelled."""

        return self._queue(
            "mark an interrupted request", functools.partial(self.store.close, state=UNKNOWN, kind=UNKNOWN), self.id
        )

    def _queue(self, action: str, write: Callable[..., Any], *args: Any) -> asyncio.Future[Any] | None:
        """The one way an attempt ends: out of ``_ACTIVE`` and its final state queued on the worker, in one
        synchronous step, so no later cancellation can skip it. The write runs to its end whoever waits for it; its
        outcome is retrieved by a done-callback and a failure logged, never raised into the request."""

        if self.ended:
            return None
        self.ended = True
        _release(self.id)
        written = _submit(write, *args)
        written.add_done_callback(functools.partial(_log_write_failure, action))
        return written


class PendingBegin:
    """A pending write queued before a send. The driver waits for it (never past a Stop or ``ARTIFACT_WAIT_S``),
    then takes the attempt it wrote, or gives it up: cancelled before the send (``cancelled`` / ``not_sent``), or
    sent without it because it stalled (``unknown`` / ``untracked``)."""

    def __init__(self, store: RequestArtifactStore, ticket: "_PendingWrite", written: asyncio.Future[str]) -> None:
        self.store = store
        self.written = written
        self._ticket = ticket

    def attempt(self) -> RequestAttempt | None:
        written = self.written
        if not written.done() or written.cancelled() or written.exception() is not None:
            return None
        return RequestAttempt(self.store, written.result())

    def give_up(self, *, sent: bool) -> None:
        if sent:
            self._ticket.give_up(self.store, state=UNKNOWN, kind="untracked")
        else:
            self._ticket.give_up(self.store, state=CANCELLED, kind="not_sent")


def begin_attempt(request: Any, *, endpoint: Any, protocol: str) -> PendingBegin | None:
    """Queue ``request`` (an ``httpx.Request``) as a pending attempt, when a capture scope is active. The caller
    waits for ``PendingBegin.written`` before it sends."""

    scope = _SCOPE.get()
    if scope is None:
        return None
    try:
        body = bytes(request.content)
        meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "state": PENDING,
            "created_at": _now(),
            "owner": {"pid": os.getpid(), "started": _process_start(os.getpid())},
            "session_id": scope.session_id,
            "turn_id": scope.turn_id,
            "avibe_version": scope.version,
            "request": {
                "method": request.method,
                "url": safe_url(str(request.url)),
                "protocol": protocol,
                "provider": getattr(endpoint, "provider", None),
                "model": getattr(endpoint, "model_id", None),
                "source_id": getattr(endpoint, "source_id", None),
                "headers": allowed_headers(request.headers, _REQUEST_HEADERS),
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            },
        }
    except Exception as exc:
        _record_failure("record a pending request", exc)
        return None
    ticket = _PendingWrite()
    written = _submit(ticket.write, scope.store, meta, body)
    written.add_done_callback(functools.partial(_log_write_failure, "record a pending request"))
    return PendingBegin(scope.store, ticket, written)


class _PendingWrite:
    """A pending write that its request may give up: whichever comes second, the write landing or the giving up,
    settles it on the worker and takes it out of ``_ACTIVE``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._given_up: tuple[str, str] | None = None
        self._artifact_id: str | None = None

    def write(self, store: RequestArtifactStore, meta: dict[str, Any], body: bytes) -> str:
        artifact_id = store.begin(meta, body)
        with self._lock:
            self._artifact_id = artifact_id
            given_up = self._given_up
        if given_up is not None:
            _release(artifact_id)
            store.close(artifact_id, state=given_up[0], kind=given_up[1])
        return artifact_id

    def give_up(self, store: RequestArtifactStore, *, state: str, kind: str) -> None:
        with self._lock:
            if self._given_up is not None:
                return
            self._given_up = (state, kind)
            landed = self._artifact_id
        if landed is None:
            return  # the write settles it when it lands
        _release(landed)
        settled = _submit(functools.partial(store.close, state=state, kind=kind), landed)
        settled.add_done_callback(functools.partial(_log_write_failure, "settle a pending request"))


def _release(artifact_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(artifact_id)


def _log_write_failure(action: str, written: Any) -> None:
    """Retrieve a worker write's outcome, so none goes unobserved, and log a failure without payload."""

    if written.cancelled():
        return
    exc = written.exception()
    if exc is not None:
        _record_failure(action, exc)


def note_slow_artifact_write() -> None:
    logger.warning("An artifact write is still running after %ss; not waiting for it", ARTIFACT_WAIT_S)


def _record_failure(action: str, exc: BaseException) -> None:
    logger.warning("Could not %s artifact: %s", action, type(exc).__name__)
