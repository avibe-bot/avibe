"""Failed model request artifacts: the exact bytes a model request sent, kept when it did not succeed.

``drive_sse_stream`` records every model request it sends while a capture scope is active
(``capture_model_requests``); outside one it records nothing, so the library stays hermetic by default.

Each attempt is one directory under the store root:

- ``request.bin``: ``httpx.Request.content`` exactly as sent, never re-serialized.
- ``meta.json``: typed facts only. These are the hop (protocol, provider, model, Source), the URL without its query
  or userinfo, allowlisted headers, the body's size and sha256, the Session and Turn, the outcome, the HTTP status,
  and the error kind. Never a credential, never an error message: a provider's message can echo the request.
- ``response.bin``: the raw response bytes as read, when there were any. An HTTP error body is kept from its start
  up to ``ERROR_BODY_LIMIT``. A stream keeps its first ``STREAM_HEAD_BYTES`` and last ``STREAM_TAIL_BYTES``, so a
  terminal error event survives. ``meta.json`` states any truncation.

Lifecycle:
- The pending attempt is written before the request is sent.
- A semantically successful attempt is deleted: a ``Done``, or an Anthropic ``pause_turn`` sub-request that ended
  normally.
- Every other ending is kept, marked failed or cancelled.
- An attempt whose consumer closed it, or whose task was cancelled, before any ending is marked ``unknown`` by a
  shielded write its driver awaits; one cancelled before it was sent is marked ``unknown`` (``not_sent``).
- An attempt its process never got to mark (a crash, or a second cancellation during that write) stays ``pending``
  on disk. It reads as ``unknown`` once its owner process is gone, or, in the owner, once it is no longer active.
  It is never treated as a success.

Files are ``0600`` in ``0700`` directories and created with ``O_EXCL | O_NOFOLLOW``. All filesystem work runs on one
private worker thread, never on the event loop. A failure to record never changes the request's outcome; it is
logged without payload.

Retention defaults to 7 days and 256 MiB. Expired or over-quota attempts are removed oldest first. An attempt still
active in a live process is never removed.
"""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_RETENTION_S = 7 * 24 * 60 * 60
DEFAULT_QUOTA_BYTES = 256 * 1024 * 1024
#: An HTTP error body is kept whole up to this size; anything after it is cut and the cut stated.
ERROR_BODY_LIMIT = 4 * 1024 * 1024
#: A stream (HTTP 2xx) keeps its start and its end; the middle of a long one is dropped and the drop stated.
STREAM_HEAD_BYTES = 64 * 1024
STREAM_TAIL_BYTES = 1024 * 1024
#: An interrupted write leaves a ``.partial`` directory; one this old belongs to no live write.
_PARTIAL_GRACE_S = 60 * 60

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
        #: What stopped a read made only for the capture (an exception type, or ``cancelled``).
        self.read_error: str | None = None
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

    @property
    def full(self) -> bool:
        """An error body past its limit: reading more would only be dropped."""

        return not self._streaming and self.total > ERROR_BODY_LIMIT

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        room = self.head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk and self._streaming:
            self._tail.append(bytes(chunk))
            self._tail_size += len(chunk)
            while self._tail and self._tail_size - len(self._tail[0]) >= STREAM_TAIL_BYTES:
                self._tail_size -= len(self._tail.popleft())

    def finish(self) -> None:
        self.read_to_end = True

    def interrupted(self, reason: str) -> None:
        self.read_error = reason

    def _tail_bytes(self) -> bytes:
        return b"".join(self._tail)[-STREAM_TAIL_BYTES:]

    def body(self) -> bytes:
        return bytes(self._head) + self._tail_bytes()

    def summary(self) -> dict[str, Any]:
        tail = min(self._tail_size, STREAM_TAIL_BYTES)
        captured = len(self._head) + tail
        omitted = self.total - captured
        summary: dict[str, Any] = {
            "status": self.status,
            "headers": dict(self.headers),
            "read_bytes": self.total,
            "read_to_end": self.read_to_end,
            "read_error": self.read_error,
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
        self._pruned_once = False

    # -- paths -------------------------------------------------------------------------------------------------

    def _ensure_root(self) -> Path:
        try:
            info = os.lstat(self.root)
        except FileNotFoundError:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            info = os.lstat(self.root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactStoreError(f"{self.root} is not a private directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(self.root, 0o700)
        return self.root

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
        if not self._pruned_once:
            # What earlier processes left behind is pruned once, before this process adds to it.
            self._pruned_once = True
            self.prune()
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

        with _ACTIVE_LOCK:
            _ACTIVE.discard(artifact_id)
        self._remove(self.path(artifact_id))

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
        with _ACTIVE_LOCK:
            _ACTIVE.discard(artifact_id)
        self.prune()

    def abandon(self, artifact_id: str, kind: str = UNKNOWN) -> None:
        """The attempt ended with no known outcome: its consumer closed it or its task was cancelled (``unknown``),
        or it was cancelled before it was sent (``not_sent``). It is no longer active, and every reader, in any
        process, sees ``unknown``."""

        with _ACTIVE_LOCK:
            _ACTIVE.discard(artifact_id)
        directory = self.path(artifact_id)
        meta = self.meta(artifact_id)
        meta["state"] = UNKNOWN
        meta["outcome"] = {"kind": kind, "error_kind": None, "http_status": None, "exception_type": None}
        meta["finished_at"] = _now()
        _replace_private(directory / META_FILE, _encode(meta))

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
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.is_symlink() or not destination.is_dir():
            raise ArtifactStoreError(f"{destination} is not a directory")
        target = destination / artifact_id
        os.mkdir(target, 0o700)
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


async def _off_loop(function: Callable[..., _T], *args: Any) -> _T:
    return await asyncio.get_running_loop().run_in_executor(_worker(), function, *args)


class RequestAttempt:
    """One sent request's artifact, driven by ``drive_sse_stream``. Every step is best effort: a failure to record
    is logged without payload and never reaches the request's outcome."""

    def __init__(self, store: RequestArtifactStore, artifact_id: str) -> None:
        self.store = store
        self.id = artifact_id
        self.capture = ResponseCapture()
        self.exception_type: str | None = None
        self.ended = False

    async def succeeded(self) -> None:
        if self.ended:
            return
        self.ended = True
        try:
            await _off_loop(self.store.succeed, self.id)
        except Exception as exc:
            _record_failure("remove a successful request", exc)

    async def failed(self, *, kind: str, error_kind: str | None = None, status: int | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        outcome: dict[str, Any] = {
            "state": CANCELLED if kind == "cancelled" else FAILED,
            "outcome": {
                "kind": kind,
                "error_kind": error_kind,
                "http_status": status if status is not None else self.capture.status,
                "exception_type": self.exception_type,
            },
        }
        try:
            await _off_loop(self.store.fail, self.id, outcome, self.capture)
        except Exception as exc:
            _record_failure("keep a failed request", exc)

    def release(self) -> None:
        """No longer active in this process, at once and without I/O: retention may treat it as ended."""

        with _ACTIVE_LOCK:
            _ACTIVE.discard(self.id)

    async def abandon(self) -> None:
        """No outcome is known: mark the durable entry ``unknown`` for every reader. The write is shielded and owned
        by the caller's await; if that await is cancelled again, the write already queued still completes."""

        if self.ended:
            return
        self.ended = True
        self.release()
        try:
            await asyncio.shield(_off_loop(self.store.abandon, self.id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _record_failure("mark an interrupted request", exc)


async def begin_attempt(request: Any, *, endpoint: Any, protocol: str) -> RequestAttempt | None:
    """Record ``request`` (an ``httpx.Request``) as pending before it is sent, when a capture scope is active."""

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
    written = asyncio.get_running_loop().run_in_executor(_worker(), scope.store.begin, meta, body)
    try:
        artifact_id = await asyncio.shield(written)
    except asyncio.CancelledError:
        # Cancelled while the write ran: it may still finish, and what it wrote was never sent. Release it as soon
        # as it does, then mark it for every reader before the cancellation goes on.
        written.add_done_callback(_release_unsent)
        await _settle_unsent(scope.store, written)
        raise
    except Exception as exc:
        _record_failure("record a pending request", exc)
        return None
    return RequestAttempt(scope.store, artifact_id)


def _release_unsent(written: asyncio.Future[str]) -> None:
    if not written.cancelled() and written.exception() is None:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(written.result())


async def _settle_unsent(store: RequestArtifactStore, written: asyncio.Future[str]) -> None:
    try:
        artifact_id = await asyncio.shield(written)
        await asyncio.shield(_off_loop(store.abandon, artifact_id, "not_sent"))
    except asyncio.CancelledError:
        # Cancelled again: the entry is released in memory (``_release_unsent``) and stays pending on disk until
        # its owner exits, when every reader sees it as unknown.
        pass
    except Exception as exc:
        _record_failure("mark an unsent request", exc)


def _record_failure(action: str, exc: BaseException) -> None:
    logger.warning("Could not %s artifact: %s", action, type(exc).__name__)
