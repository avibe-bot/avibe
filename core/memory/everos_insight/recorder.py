from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from uuid import uuid4
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, TypeAlias
from core.memory.secret_scrubber import REDACTED as _REDACTED
from core.memory.secret_scrubber import scrub_text as _canonical_scrub_text

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    required_no_follow_flag,
    strict_file_create_flags,
)

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
ProviderKind: TypeAlias = Literal["llm", "multimodal_llm", "embedding"]

_ATTACHMENT_OMITTED = "[ATTACHMENT_OMITTED]"
_LLM_MESSAGE_BYTES = 16 * 1024
_LLM_PAYLOAD_BYTES = 64 * 1024
_MULTIMODAL_STRING_BYTES = 4 * 1024
_EMBEDDING_INPUT_BYTES = 2 * 1024
_EMBEDDING_INPUT_COUNT = 16
_ERROR_BYTES = 4 * 1024
_IDENTITY_BYTES = 256
_LABEL_BYTES = 128
_MODEL_BYTES = 1024
_PROVENANCE_BYTES = 1024
_MD_PATH_BYTES = 2048
_MAX_ROW_ENCODED_BYTES = 320 * 1024
_QUEUE_CAPACITY = 256
_WRITE_BATCH_SIZE = 32
_MAX_CONSECUTIVE_WRITER_FAILURES = 20
_RETENTION_AGE_MS = 14 * 24 * 60 * 60 * 1000
_RETENTION_ROWS = 5_000
_SOFT_STORAGE_BYTES = 128 * 1024 * 1024
_VACUUM_STEPS = 256
_MAX_VACUUM_PASSES = 4
_WRITER_BUSY_TIMEOUT_MS = 100
_SQLITE_PROGRESS_OPCODES = 1_000
_MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60.0
_MAINTENANCE_BATCHES = 64
_CLOSE_INTERRUPT_GRACE_SECONDS = 0.1

_SECRET_KEY_SUFFIXES = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "secretkey",
    "token",
)
_ATTACHMENT_KEYS = frozenset(
    {
        "attachment",
        "attachments",
        "audio",
        "b64json",
        "bytes",
        "file",
        "filedata",
        "image",
        "imageurl",
    }
)
_ATTACHMENT_PART_TYPES = frozenset(
    {
        "attachment",
        "audio",
        "document",
        "file",
        "image",
        "image_url",
        "input_audio",
        "input_file",
        "input_image",
    }
)


class _CallLogIncompatibleError(RuntimeError):
    """The existing database requires Clear before this version can use it."""


@dataclass(frozen=True, slots=True)
class ProviderCallInput:
    id: str
    started_at_ms: int
    duration_ms: int
    kind: ProviderKind
    stage: str
    status: str
    request: JsonValue
    response: JsonValue | None = None
    model: str | None = None
    error: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    request_id: str | None = None
    strategy_name: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    memcell_id: str | None = None
    app_id: str | None = None
    project_id: str | None = None
    owner_id: str | None = None
    md_path: str | None = None
    entry_id: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    dropped_before: int = 0


@dataclass(frozen=True, slots=True)
class ProviderCallRow:
    id: str
    started_at_ms: int
    duration_ms: int
    kind: str
    stage: str
    model: str | None
    status: str
    error: str | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    request_json: str
    response_json: str | None
    request_bytes: int
    response_bytes: int | None
    request_id: str | None
    strategy_name: str | None
    run_id: str | None
    attempt: int | None
    memcell_id: str | None
    app_id: str | None
    project_id: str | None
    owner_id: str | None
    md_path: str | None
    entry_id: str | None
    parent_type: str | None
    parent_id: str | None
    dropped_before: int


class RecorderHandle:
    """Own one nonblocking provider-call queue and its SQLite writer thread."""

    def __init__(
        self,
        db_path: Path,
        *,
        provider_base_urls: Sequence[str] = (),
        exact_redaction_values: Sequence[str] = (),
    ) -> None:
        self._db_path = Path(db_path)
        self._provider_base_urls = tuple(provider_base_urls)
        self._exact_redaction_values = tuple(exact_redaction_values)
        self._condition = threading.Condition()
        self._queue: deque[ProviderCallRow] = deque()
        self._pending_dropped = 0
        self._thread: threading.Thread | None = None
        self._started = False
        self._running = False
        self._closing = False
        self._close_deadline: float | None = None
        self._close_requested = threading.Event()
        self._health_state = "disabled"
        self._health_reason: str | None = None
        self._consecutive_writer_failures = 0
        self._consecutive_maintenance_failures = 0
        self._batches_since_maintenance = 0
        self._last_maintenance_monotonic: float | None = None
        self._writer_connection: sqlite3.Connection | None = None
        self._join_tasks: set[asyncio.Task[None]] = set()

    @property
    def health(self) -> dict[str, str | None]:
        with self._condition:
            return {"state": self._health_state, "reason": self._health_reason}

    def start(self) -> None:
        try:
            with self._condition:
                if self._started:
                    return
                self._started = True
                self._running = True
                self._set_health_locked("active", None)
                thread = threading.Thread(target=self._writer_main, name="memory-call-log", daemon=True)
                self._thread = thread
                thread.start()
        except Exception:
            with self._condition:
                self._running = False
                self._set_health_locked("degraded", "writer_failures")

    def submit(self, call: ProviderCallInput) -> None:
        try:
            with self._condition:
                if not self._running or self._closing or self._health_state == "disabled":
                    return
            try:
                row = normalize_provider_call(
                    call,
                    provider_base_urls=self._provider_base_urls,
                    exact_redaction_values=self._exact_redaction_values,
                )
            except Exception:
                with self._condition:
                    if self._running and not self._closing:
                        self._pending_dropped += 1
                        self._set_health_locked("degraded", "serialization_failed")
                return
            with self._condition:
                if not self._running or self._closing or self._health_state == "disabled":
                    return
                if len(self._queue) == _QUEUE_CAPACITY:
                    dropped = self._queue.popleft()
                    self._carry_drop_locked(1 + dropped.dropped_before)
                if self._pending_dropped:
                    row = replace(row, dropped_before=row.dropped_before + self._pending_dropped)
                    self._pending_dropped = 0
                self._queue.append(row)
                self._condition.notify()
        except Exception:
            return

    async def close(self, timeout: float = 1.0) -> None:
        budget = max(0.0, float(timeout))
        # sqlite3_interrupt() does not reliably cancel an in-flight busy wait.
        busy_grace = _WRITER_BUSY_TIMEOUT_MS / 1000.0
        interrupt_grace = min(_CLOSE_INTERRUPT_GRACE_SECONDS + busy_grace, budget)
        graceful_budget = max(0.0, budget - interrupt_grace)
        try:
            with self._condition:
                if not self._started:
                    return
                if not self._closing:
                    self._closing = True
                    self._close_deadline = time.monotonic() + graceful_budget
                    self._close_requested.set()
                    self._condition.notify_all()
                thread = self._thread
            if thread is not None:
                join_task = asyncio.create_task(asyncio.to_thread(thread.join))
                self._join_tasks.add(join_task)
                join_task.add_done_callback(self._join_finished)
                try:
                    if graceful_budget:
                        await asyncio.wait_for(
                            asyncio.shield(join_task),
                            timeout=graceful_budget,
                        )
                    elif join_task.done():
                        await join_task
                    else:
                        raise TimeoutError
                except TimeoutError:
                    self._interrupt_writer()
                    if interrupt_grace:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(join_task),
                                timeout=interrupt_grace,
                            )
                        except TimeoutError:
                            with self._condition:
                                self._set_health_locked("degraded", "writer_failures")
                except asyncio.CancelledError:
                    self._expire_close_deadline()
                    self._interrupt_writer()
                    await asyncio.shield(join_task)
                    raise
        except asyncio.CancelledError:
            raise
        except Exception:
            with self._condition:
                self._set_health_locked("degraded", "writer_failures")

    def _writer_main(self) -> None:
        try:
            with _database_connection(self._db_path) as conn:
                with self._condition:
                    self._writer_connection = conn
                try:
                    conn.set_progress_handler(
                        lambda: int(self._past_close_deadline()),
                        _SQLITE_PROGRESS_OPCODES,
                    )
                    _initialize_schema(conn)
                    if self._perform_maintenance(conn):
                        self._writer_loop(conn)
                finally:
                    conn.set_progress_handler(None, 0)
        except Exception as error:
            reason = "call_log_corrupt" if _is_corruption(error) else "writer_failures"
            with self._condition:
                self._set_health_locked("degraded", reason)
        finally:
            with self._condition:
                self._writer_connection = None
                self._queue.clear()
                self._pending_dropped = 0
                self._running = False
                if self._closing and self._health_reason not in {"call_log_corrupt", "writer_failures"}:
                    self._set_health_locked("disabled", None)
                self._condition.notify_all()

    def _writer_loop(self, conn: sqlite3.Connection) -> None:
        while True:
            batch = self._next_batch()
            if batch is None:
                return
            if not batch:
                if not self._perform_maintenance(conn):
                    return
                continue
            if not self._persist_batch(conn, batch):
                return
            self._record_writer_success()
            self._batches_since_maintenance += 1
            if (
                not self._closing
                and self._maintenance_due()
                and not self._perform_maintenance(conn)
            ):
                return

    def _next_batch(self) -> list[ProviderCallRow] | None:
        with self._condition:
            while not self._queue:
                if self._closing:
                    return None
                maintenance_wait = self._seconds_until_maintenance()
                if maintenance_wait <= 0:
                    return []
                self._condition.wait(timeout=maintenance_wait)
            if self._past_close_deadline_locked():
                self._queue.clear()
                self._pending_dropped = 0
                return None
            return [self._queue.popleft() for _ in range(min(_WRITE_BATCH_SIZE, len(self._queue)))]

    def _persist_batch(self, conn: sqlite3.Connection, rows: list[ProviderCallRow]) -> bool:
        while True:
            if self._past_close_deadline():
                return False
            try:
                committed = _write_batch(conn, rows, self._past_close_deadline)
            except Exception as error:
                if self._past_close_deadline():
                    return False
                if _is_corruption(error):
                    with self._condition:
                        self._set_health_locked("degraded", "call_log_corrupt")
                    return False
                if not self._record_writer_failure():
                    return False
                continue
            if not committed:
                return False
            return True

    def _record_writer_failure(self) -> bool:
        with self._condition:
            self._consecutive_writer_failures += 1
            if self._consecutive_writer_failures >= _MAX_CONSECUTIVE_WRITER_FAILURES:
                self._set_health_locked("disabled", "writer_failures")
                self._queue.clear()
                self._pending_dropped = 0
                return False
            self._set_health_locked("degraded", "writer_failures")
            return True

    def _record_writer_success(self) -> None:
        with self._condition:
            self._consecutive_writer_failures = 0
            if self._consecutive_maintenance_failures == 0:
                self._set_health_locked("active", None)

    def _record_maintenance_failure(self) -> bool:
        with self._condition:
            self._consecutive_maintenance_failures += 1
            if self._consecutive_maintenance_failures >= _MAX_CONSECUTIVE_WRITER_FAILURES:
                self._set_health_locked("disabled", "writer_failures")
                self._queue.clear()
                self._pending_dropped = 0
                return False
            self._set_health_locked("degraded", "writer_failures")
            return True

    def _perform_maintenance(self, conn: sqlite3.Connection) -> bool:
        self._last_maintenance_monotonic = time.monotonic()
        self._batches_since_maintenance = 0
        try:
            completed = _maintain_storage(
                conn,
                self._db_path,
                should_abort=self._past_close_deadline,
            )
        except Exception as error:
            if _is_corruption(error):
                with self._condition:
                    self._set_health_locked("degraded", "call_log_corrupt")
                return False
            if self._past_close_deadline():
                return False
            return self._record_maintenance_failure()
        if not completed:
            return False
        with self._condition:
            self._consecutive_maintenance_failures = 0
            if self._consecutive_writer_failures == 0:
                self._set_health_locked("active", None)
        return True

    def _maintenance_due(self) -> bool:
        if self._batches_since_maintenance >= _MAINTENANCE_BATCHES:
            return True
        last = self._last_maintenance_monotonic
        return last is None or time.monotonic() - last >= _MAINTENANCE_INTERVAL_SECONDS

    def _seconds_until_maintenance(self) -> float:
        last = self._last_maintenance_monotonic
        if last is None:
            return 0.0
        return max(0.0, _MAINTENANCE_INTERVAL_SECONDS - (time.monotonic() - last))

    def _carry_drop_locked(self, count: int) -> None:
        if self._queue:
            first = self._queue[0]
            self._queue[0] = replace(first, dropped_before=first.dropped_before + count)
        else:
            self._pending_dropped += count

    def _interrupt_writer(self) -> None:
        with self._condition:
            conn = self._writer_connection
        if conn is not None:
            try:
                conn.interrupt()
            except Exception:
                pass

    def _expire_close_deadline(self) -> None:
        with self._condition:
            self._close_deadline = time.monotonic()

    def _join_finished(self, task: asyncio.Task[None]) -> None:
        self._join_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _past_close_deadline(self) -> bool:
        with self._condition:
            return self._past_close_deadline_locked()

    def _past_close_deadline_locked(self) -> bool:
        return self._closing and self._close_deadline is not None and time.monotonic() >= self._close_deadline

    def _set_health_locked(self, state: str, reason: str | None) -> None:
        self._health_state = state
        self._health_reason = reason
def normalize_provider_call(
    call: ProviderCallInput,
    *,
    provider_base_urls: Sequence[str] = (),
    exact_redaction_values: Sequence[str] = (),
) -> ProviderCallRow:
    """Turn primitive provider data into one bounded, storage-ready row."""

    _validate_call(call)
    base_urls = tuple(url.rstrip("/") for url in provider_base_urls if url)
    exact_values = tuple(
        sorted({value for value in exact_redaction_values if value}, key=len, reverse=True)
    )
    raw_request = call.request
    raw_response = call.response
    if call.kind == "multimodal_llm":
        raw_request = _sanitize_multimodal(raw_request, bound_strings=False)
        if raw_response is not None:
            raw_response = _sanitize_multimodal(raw_response, bound_strings=False)
    request = _scrub_json(raw_request, base_urls=base_urls, exact_values=exact_values)
    response = (
        _scrub_json(raw_response, base_urls=base_urls, exact_values=exact_values)
        if raw_response is not None
        else None
    )
    request_bytes = _json_size(request)
    response_bytes = _json_size(response) if response is not None else None

    if call.kind == "embedding":
        request = _embedding_request(request)
        response = _embedding_response(response)
    else:
        if call.kind == "multimodal_llm":
            request = _sanitize_multimodal(request)
            response = _sanitize_multimodal(response)
        request = _llm_request(request)
        response = _bounded_json(response, _LLM_PAYLOAD_BYTES) if response is not None else None

    scrub_provider = lambda value: _scrub_optional_text(
        value,
        base_urls=base_urls,
        exact_values=exact_values,
    )
    scrub_internal = lambda value: _scrub_optional_text(
        value,
        base_urls=base_urls,
        exact_values=(),
    )
    row = ProviderCallRow(
        id=_required_bounded_text(scrub_internal(call.id), _IDENTITY_BYTES, "id"),
        started_at_ms=call.started_at_ms,
        duration_ms=call.duration_ms,
        kind=call.kind,
        stage=_required_bounded_text(scrub_internal(call.stage), _LABEL_BYTES, "stage"),
        model=_bounded_text(scrub_provider(call.model), _MODEL_BYTES),
        status=_required_bounded_text(scrub_internal(call.status), _LABEL_BYTES, "status"),
        error=_bounded_text(scrub_provider(call.error), _ERROR_BYTES),
        finish_reason=_bounded_text(scrub_provider(call.finish_reason), _LABEL_BYTES),
        prompt_tokens=call.prompt_tokens,
        completion_tokens=call.completion_tokens,
        request_json=_serialize_payload(request, _LLM_PAYLOAD_BYTES),
        response_json=_serialize_payload(response, _LLM_PAYLOAD_BYTES) if response is not None else None,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        request_id=_bounded_provenance(scrub_internal(call.request_id)),
        strategy_name=_bounded_text(scrub_internal(call.strategy_name), _PROVENANCE_BYTES),
        run_id=_bounded_provenance(scrub_internal(call.run_id)),
        attempt=call.attempt,
        memcell_id=_bounded_provenance(scrub_internal(call.memcell_id)),
        app_id=_bounded_provenance(scrub_internal(call.app_id)),
        project_id=_bounded_provenance(scrub_internal(call.project_id)),
        owner_id=_bounded_provenance(scrub_internal(call.owner_id)),
        md_path=_bounded_provenance(scrub_internal(call.md_path), _MD_PATH_BYTES),
        entry_id=_bounded_provenance(scrub_internal(call.entry_id)),
        parent_type=_bounded_provenance(scrub_internal(call.parent_type)),
        parent_id=_bounded_provenance(scrub_internal(call.parent_id)),
        dropped_before=call.dropped_before,
    )
    if _json_size(asdict(row)) > _MAX_ROW_ENCODED_BYTES:
        raise ValueError("normalized provider call exceeds the encoded row budget")
    return row


def initialize_call_log(db_path: Path) -> None:
    """Initialize the v1 call-log schema without retaining a connection."""

    db_path = Path(db_path)
    with _database_connection(db_path) as conn:
        _initialize_schema(conn)


def record_preflight_call(
    db_path: Path,
    *,
    kind: ProviderKind,
    model: str | None,
    request: dict,
    response: dict | None,
    status: str,
    error: str | None = None,
    provider_base_urls: Sequence[str] = (),
    exact_redaction_values: Sequence[str] = (),
) -> None:
    """Record a parent-side bounded preflight call with its distinct stage."""
    call = ProviderCallInput(
        id=uuid4().hex,
        started_at_ms=int(time.time() * 1000),
        duration_ms=0,
        kind=kind,
        stage="processing_preflight",
        status=status,
        request=request,
        response=response,
        model=model,
        error=error,
    )
    row = normalize_provider_call(call, provider_base_urls=provider_base_urls, exact_redaction_values=exact_redaction_values)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_directory_chain(db_path.parent)
    os.chmod(db_path.parent, 0o700)
    with _database_connection(db_path) as conn:
        _initialize_schema(conn)
        _write_batch(conn, [row], lambda: False)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
            CREATE TABLE IF NOT EXISTS provider_call (
                id TEXT PRIMARY KEY NOT NULL,
                started_at_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                kind TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL,
                error TEXT,
                finish_reason TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                request_json TEXT NOT NULL,
                response_json TEXT,
                request_bytes INTEGER NOT NULL,
                response_bytes INTEGER,
                request_id TEXT,
                strategy_name TEXT,
                run_id TEXT,
                attempt INTEGER,
                memcell_id TEXT,
                app_id TEXT,
                project_id TEXT,
                owner_id TEXT,
                md_path TEXT,
                entry_id TEXT,
                parent_type TEXT,
                parent_id TEXT,
                dropped_before INTEGER NOT NULL DEFAULT 0
            ) STRICT;
            CREATE INDEX IF NOT EXISTS provider_call_request_id_idx
                ON provider_call(request_id);
            CREATE INDEX IF NOT EXISTS provider_call_run_id_idx
                ON provider_call(run_id);
            CREATE INDEX IF NOT EXISTS provider_call_memcell_id_idx
                ON provider_call(memcell_id);
            CREATE INDEX IF NOT EXISTS provider_call_started_at_idx
                ON provider_call(started_at_ms DESC);
            CREATE INDEX IF NOT EXISTS provider_call_parent_idx
                ON provider_call(parent_type, parent_id);
            PRAGMA user_version = 1;
        """
    )


def maintain_call_log(db_path: Path, *, now_ms: int | None = None) -> str | None:
    """Prune an existing call log, returning a stable failure reason."""

    db_path = Path(db_path)
    try:
        os.lstat(db_path)
    except FileNotFoundError:
        return None
    try:
        with _database_connection(db_path) as conn:
            _initialize_schema(conn)
            _maintain_storage(conn, db_path, now_ms=now_ms)
    except Exception as error:
        return "call_log_corrupt" if _is_corruption(error) else "writer_failures"
    return None


def clear_call_log(db_path: Path) -> None:
    """Remove only the verified SQLite files owned by one call log."""

    db_path = Path(db_path)
    directory = db_path.parent
    try:
        os.lstat(directory)
    except FileNotFoundError:
        return
    _validate_directory_chain(directory)
    directory_info = os.lstat(directory)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise OSError("Call-log directory must be private and owned")
    candidates = (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
        db_path.with_name(f"{db_path.name}-journal"),
    )
    _validate_private_database_files(db_path)
    for candidate in candidates:
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
    try:
        directory.rmdir()
    except OSError:
        pass


def _write_batch(
    conn: sqlite3.Connection,
    rows: list[ProviderCallRow],
    should_abort: Callable[[], bool],
) -> bool:
    parameters = [asdict(row) for row in rows]
    columns = tuple(parameters[0])
    column_sql = ", ".join(columns)
    placeholder_sql = ", ".join(f":{column}" for column in columns)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            f"INSERT INTO provider_call ({column_sql}) VALUES ({placeholder_sql}) ON CONFLICT(id) DO NOTHING",
            parameters,
        )
        if should_abort():
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _maintain_storage(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    now_ms: int | None = None,
    should_abort: Callable[[], bool] = lambda: False,
) -> bool:
    if should_abort():
        return False
    reference_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cutoff_ms = reference_ms - _RETENTION_AGE_MS
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM provider_call WHERE started_at_ms < ?", (cutoff_ms,))
        conn.execute(
            "DELETE FROM provider_call WHERE id IN ("
            "SELECT id FROM provider_call "
            "ORDER BY started_at_ms DESC, id DESC LIMIT -1 OFFSET ?)",
            (_RETENTION_ROWS,),
        )
        if should_abort():
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    if should_abort():
        return False
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    previous_size = _call_log_size_bytes(db_path)
    for _ in range(_MAX_VACUUM_PASSES):
        if should_abort():
            return False
        if previous_size <= _SOFT_STORAGE_BYTES:
            break
        conn.execute(f"PRAGMA incremental_vacuum({_VACUUM_STEPS})")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        current_size = _call_log_size_bytes(db_path)
        if current_size >= previous_size:
            break
        previous_size = current_size
    return True


def _call_log_size_bytes(db_path: Path) -> int:
    total = 0
    for candidate in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ):
        try:
            total += os.lstat(candidate).st_size
        except FileNotFoundError:
            continue
    return total


def _is_corruption(error: BaseException) -> bool:
    if isinstance(error, _CallLogIncompatibleError):
        return True
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return True
    message = str(error).casefold()
    return any(
        marker in message
        for marker in ("database disk image is malformed", "file is not a database", "database corruption")
    )


@contextmanager
def _database_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    _prepare_private_database_path(db_path)
    conn = sqlite3.connect(db_path, timeout=1.0, isolation_level=None)
    try:
        _validate_private_database_files(db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise _CallLogIncompatibleError(f"Unsupported call-log schema version: {version}")
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            raise RuntimeError("Call-log database does not use incremental auto-vacuum")
        conn.execute("PRAGMA journal_mode = WAL")
        _validate_private_database_files(db_path)
        conn.execute(f"PRAGMA busy_timeout = {_WRITER_BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()
        _validate_private_database_files(db_path)


def _validate_call(call: ProviderCallInput) -> None:
    for name in ("id", "stage", "status"):
        value = getattr(call, name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
    if call.kind not in {"llm", "multimodal_llm", "embedding"}:
        raise ValueError("unsupported provider call kind")
    for name in ("started_at_ms", "duration_ms", "dropped_before"):
        value = getattr(call, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    for name in ("prompt_tokens", "completion_tokens", "attempt"):
        value = getattr(call, name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None")
    for name in (
        "model",
        "error",
        "finish_reason",
        "request_id",
        "strategy_name",
        "run_id",
        "memcell_id",
        "app_id",
        "project_id",
        "owner_id",
        "md_path",
        "entry_id",
        "parent_type",
        "parent_id",
    ):
        value = getattr(call, name)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{name} must be a string or None")
    _validate_json(call.request)
    if call.response is not None:
        _validate_json(call.response)


def _validate_json(value: JsonValue, *, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("provider JSON exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("provider JSON contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item, depth=depth + 1)
        return
    raise TypeError("provider payload must contain only JSON-compatible primitives")


def _scrub_json(
    value: JsonValue,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...] = (),
) -> JsonValue:
    if isinstance(value, str):
        return _scrub_text(value, base_urls=base_urls, exact_values=exact_values)
    if isinstance(value, list):
        return [
            _scrub_json(item, base_urls=base_urls, exact_values=exact_values)
            for item in value
        ]
    if isinstance(value, dict):
        scrubbed: dict[str, JsonValue] = {}
        for key, item in value.items():
            clean_key = _scrub_text(
                key,
                base_urls=base_urls,
                exact_values=exact_values,
            )
            if _is_secret_key(key):
                scrubbed[clean_key] = _REDACTED
            else:
                scrubbed[clean_key] = _scrub_json(
                    item,
                    base_urls=base_urls,
                    exact_values=exact_values,
                )
        return scrubbed
    return value


def _scrub_optional_text(
    value: str | None,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> str | None:
    return (
        _scrub_text(value, base_urls=base_urls, exact_values=exact_values)
        if value is not None
        else None
    )


def _scrub_text(
    value: str,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...] = (),
) -> str:
    return _canonical_scrub_text(
        value,
        base_urls=base_urls,
        exact_values=exact_values,
    )


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _is_secret_key(value: str) -> bool:
    return _normalized_key(value).endswith(_SECRET_KEY_SUFFIXES)


def _llm_request(value: JsonValue) -> JsonValue:
    if not isinstance(value, dict):
        return _bounded_json(value, _LLM_PAYLOAD_BYTES)
    result = dict(value)
    messages = result.get("messages")
    if isinstance(messages, list):
        bounded_messages = [_bounded_json(message, _LLM_MESSAGE_BYTES) for message in messages]
        result["messages"] = bounded_messages
    response_format = result.get("response_format")
    if isinstance(response_format, dict):
        result["response_format"] = _response_schema_name(response_format)
    if _json_size(result) <= _LLM_PAYLOAD_BYTES:
        return result
    if isinstance(messages, list) and len(messages) > 2:
        result["messages"] = [
            _bounded_json(messages[0], _LLM_MESSAGE_BYTES),
            {"omitted_messages": len(messages) - 2},
            _bounded_json(messages[-1], _LLM_MESSAGE_BYTES),
        ]
    return _bounded_json(result, _LLM_PAYLOAD_BYTES)


def _response_schema_name(response_format: dict[str, JsonValue]) -> JsonValue:
    name = response_format.get("name")
    if isinstance(name, str):
        return {"name": name}
    schema = response_format.get("json_schema")
    if isinstance(schema, dict) and isinstance(schema.get("name"), str):
        return {"name": schema["name"]}
    return {"name": None}


def _sanitize_multimodal(
    value: JsonValue,
    *,
    bound_strings: bool = True,
) -> JsonValue:
    if isinstance(value, str):
        return _bounded_json(value, _MULTIMODAL_STRING_BYTES) if bound_strings else value
    if isinstance(value, list):
        return [
            _sanitize_multimodal(item, bound_strings=bound_strings)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    part_type = value.get("type")
    if isinstance(part_type, str) and part_type.casefold() in _ATTACHMENT_PART_TYPES:
        return {"type": part_type, "attachment": _ATTACHMENT_OMITTED}
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if _normalized_key(key) in _ATTACHMENT_KEYS:
            result[key] = _ATTACHMENT_OMITTED
        else:
            result[key] = _sanitize_multimodal(
                item,
                bound_strings=bound_strings,
            )
    return result


def _embedding_request(value: JsonValue) -> JsonValue:
    source = value if isinstance(value, dict) else {"input": value}
    raw_inputs = source.get("input", source.get("inputs", []))
    inputs = raw_inputs if isinstance(raw_inputs, list) else [raw_inputs]
    declared_count = source.get("input_count")
    input_count = (
        declared_count
        if isinstance(declared_count, int)
        and not isinstance(declared_count, bool)
        and declared_count >= len(inputs)
        else len(inputs)
    )
    excerpts: list[JsonValue] = []
    for item in inputs[:_EMBEDDING_INPUT_COUNT]:
        if isinstance(item, str):
            excerpts.append(_excerpt(item, _EMBEDDING_INPUT_BYTES))
        else:
            excerpts.append({"omitted_input": True})
    result: dict[str, JsonValue] = {
        "model": source.get("model") if isinstance(source.get("model"), str) else None,
        "dimensions": source.get("dimensions") if isinstance(source.get("dimensions"), int) else None,
        "input_count": input_count,
        "inputs": excerpts,
    }
    if input_count > len(excerpts):
        result["omitted_inputs"] = input_count - len(excerpts)
    return result


def _embedding_response(value: JsonValue | None) -> JsonValue | None:
    if value is None:
        return None
    source = value if isinstance(value, dict) else {}
    vectors = source.get("vectors", source.get("data", []))
    vector_count = source.get("vector_count")
    if not isinstance(vector_count, int) or isinstance(vector_count, bool) or vector_count < 0:
        vector_count = len(vectors) if isinstance(vectors, list) else 0
    dimension = source.get("dimension")
    if not isinstance(dimension, int) and isinstance(vectors, list) and vectors:
        first = vectors[0]
        if isinstance(first, list):
            dimension = len(first)
        elif isinstance(first, dict) and isinstance(first.get("embedding"), list):
            dimension = len(first["embedding"])
    usage = source.get("usage")
    return {
        "vector_count": vector_count,
        "dimension": dimension if isinstance(dimension, int) else None,
        "usage": _embedding_usage(usage),
    }


def _embedding_usage(value: JsonValue | None) -> JsonValue:
    if not isinstance(value, dict):
        return None
    allowed = {
        "cached_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "total_tokens",
    }
    usage: dict[str, JsonValue] = {}
    for key in sorted(allowed):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if item >= 0 and (isinstance(item, int) or math.isfinite(item)):
                usage[key] = item
    return usage or None


def _bounded_json(value: JsonValue, limit: int) -> JsonValue:
    size = _json_size(value)
    if size <= limit:
        return value
    if isinstance(value, str):
        return _excerpt(value, limit)
    if isinstance(value, dict) and value:
        per_value_limit = max(64, (limit - _json_size({key: None for key in value})) // len(value))
        bounded = {key: _bounded_json(item, per_value_limit) for key, item in value.items()}
        if _json_size(bounded) <= limit:
            return bounded
    if isinstance(value, list) and value:
        if len(value) == 1:
            bounded_list = [_bounded_json(value[0], max(64, limit - 2))]
        else:
            middle: list[JsonValue] = [{"omitted_items": len(value) - 2}] if len(value) > 2 else []
            middle_bytes = sum(_json_size(item) + 1 for item in middle)
            item_limit = max(64, (limit - middle_bytes - 3) // 2)
            bounded_list = [
                _bounded_json(value[0], item_limit),
                *middle,
                _bounded_json(value[-1], item_limit),
            ]
        if _json_size(bounded_list) <= limit:
            return bounded_list
    return {"omitted_bytes": size}


def _excerpt(value: str, limit: int) -> JsonValue:
    encoded = value.encode("utf-8")
    if _json_size(value) <= limit:
        return value
    best: JsonValue = {"omitted_bytes": len(encoded)}
    lower = 0
    upper = len(encoded)
    while lower <= upper:
        midpoint = (lower + upper) // 2
        excerpt = encoded[:midpoint].decode("utf-8", errors="ignore")
        omitted = len(encoded) - len(excerpt.encode("utf-8"))
        candidate: JsonValue = {"excerpt": excerpt, "omitted_bytes": omitted}
        if _json_size(candidate) <= limit:
            best = candidate
            lower = midpoint + 1
        else:
            upper = midpoint - 1
    return best


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if _json_size(value) <= limit:
        return value
    best = f"[omitted_bytes={len(encoded)}]"
    lower = 0
    upper = len(encoded)
    while lower <= upper:
        midpoint = (lower + upper) // 2
        prefix = encoded[:midpoint].decode("utf-8", errors="ignore")
        omitted = len(encoded) - len(prefix.encode("utf-8"))
        candidate = f"{prefix} [omitted_bytes={omitted}]"
        if _json_size(candidate) <= limit:
            best = candidate
            lower = midpoint + 1
        else:
            upper = midpoint - 1
    return best


def _required_bounded_text(value: str | None, limit: int, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} must be present")
    if _json_size(value) > limit:
        raise ValueError(f"{name} exceeds its encoded byte budget")
    return value


def _bounded_provenance(value: str | None, limit: int = _PROVENANCE_BYTES) -> str | None:
    if value is None or _json_size(value) <= limit:
        return value
    return None


def _serialize_payload(value: JsonValue, limit: int) -> str:
    bounded = _bounded_json(value, limit)
    serialized = _encode_json(bounded)
    if len(serialized.encode("utf-8")) > limit:
        raise ValueError("normalized payload exceeds its encoded field budget")
    return serialized


def _json_size(value: JsonValue) -> int:
    return len(_encode_json(value).encode("utf-8"))


def _encode_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prepare_private_database_path(db_path: Path) -> None:
    try:
        required_no_follow_flag()
    except ConfinedFilesystemError as error:
        raise OSError(
            "Call-log persistence requires no-follow filesystem support"
        ) from error
    if not db_path.is_absolute() or ".." in db_path.parts:
        raise OSError("Call-log database path must be a lexical absolute path")
    _validate_directory_chain(db_path.parent)
    directory_info = os.lstat(db_path.parent)
    if directory_info.st_uid != os.getuid():
        raise OSError("Call-log directory must be owned by the current user")
    if stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise OSError("Call-log directory must have mode 0700")

    try:
        os.lstat(db_path)
    except FileNotFoundError:
        try:
            flags = strict_file_create_flags()
        except ConfinedFilesystemError as error:  # pragma: no cover - guarded above
            raise OSError("Call-log persistence is unavailable") from error
        fd = os.open(db_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("Call-log database must be a regular owned file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError("Call-log database must have mode 0600")
        finally:
            os.close(fd)
    _validate_private_database_files(db_path)


def _validate_directory_chain(directory: Path) -> None:
    for candidate in (*reversed(directory.parents), directory):
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("Call-log parent chain must contain only directories")


def _validate_private_database_files(db_path: Path) -> None:
    for candidate in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
        db_path.with_name(f"{db_path.name}-journal"),
    ):
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("Call-log database path must be a regular file")
        if info.st_uid != os.getuid():
            raise OSError("Call-log database must be owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("Call-log database must have mode 0600")
