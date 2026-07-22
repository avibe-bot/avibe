"""Dedicated SQLite state for the provider-independent Memory module."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from config import paths
from core.memory.types import MemoryErrorCode, is_memory_error_code


MEMORY_STORE_FILENAME = "memory.sqlite"
MEMORY_STORE_DIRNAME = "memory"
MAX_NONTERMINAL_QUEUE_ROWS = 500
MAX_MESSAGE_ATTEMPTS = 3
TERMINAL_TOMBSTONE_LIMIT = 100_000
TERMINAL_TOMBSTONE_RETENTION = timedelta(days=90)


def memory_store_path() -> Path:
    """Return the dedicated Memory database under the effective Avibe state root."""

    return paths.get_state_dir() / MEMORY_STORE_DIRNAME / MEMORY_STORE_FILENAME


def utc_now_iso() -> str:
    """Return a lexically sortable UTC instant with millisecond precision."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class MemoryMeta:
    epoch: int
    clear_in_progress: bool
    principal_id: str
    scope_key: bytes
    provider_root_id: str
    last_provider_timestamp_ms: int
    missed_count: int
    last_success_at: str | None
    last_error: MemoryErrorCode | None
    updated_at: str


@dataclass(frozen=True)
class QueueRow:
    source_message_digest: str
    epoch: int
    session_id: str
    payload_text: str | None
    occurred_at_ms: int
    provider_timestamp_ms: int
    state: Literal["pending", "processing", "delivered", "dead"]
    attempts: int
    next_retry_at: str | None
    lease_owner: str | None
    lease_at: str | None
    last_error: MemoryErrorCode | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class QueueStats:
    pending: int = 0
    processing: int = 0
    dead: int = 0
    queue_plaintext_bytes: int = 0


@dataclass(frozen=True)
class EnqueueResult:
    outcome: Literal["accepted", "duplicate", "queue_full", "clearing", "timestamp_invalid"]
    row: QueueRow | None = None


@dataclass(frozen=True)
class MessageFailureResult:
    state: Literal["pending", "dead"] | None
    attempts: int | None


class MemoryStore:
    """Own the small, durable Memory queue without exposing SQLite to callers."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.path = (db_path or memory_store_path()).expanduser().resolve()
        self._prepare_private_directory()
        self._initialize()
        self._enforce_private_database_modes()

    def ensure_meta(self) -> MemoryMeta:
        """Create and return the singleton metadata row when Memory first opens."""

        with self._transaction() as conn:
            return self._ensure_meta_in_connection(conn)

    def get_meta(self) -> MemoryMeta | None:
        """Return the metadata row without creating Memory state."""

        with self._connection() as conn:
            row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        return _meta_from_row(row) if row is not None else None

    def clear_in_progress(self) -> bool:
        """Return whether a prior or active Clear all operation is unfinished."""

        meta = self.get_meta()
        return bool(meta and meta.clear_in_progress)

    def increment_missed(self) -> None:
        """Record one validation or capacity rejection without retaining input."""

        self.record_capture_skip(None)

    def record_capture_skip(self, error: MemoryErrorCode | None) -> None:
        """Record a closed admission skip without retaining rejected input."""

        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta_in_connection(conn)
            self._record_capture_skip_in_connection(conn, error, now)

    def enqueue_request(
        self,
        *,
        source_message_id: str,
        session_id: str,
        payload_text: str,
        occurred_at_ms: int,
        max_provider_timestamp_ms: int,
        nonterminal_limit: int = MAX_NONTERMINAL_QUEUE_ROWS,
    ) -> EnqueueResult:
        """Admit one validated capture in a single local queue transaction.

        Raw source identifiers are transformed only inside this transaction and
        never written to SQLite.  This is the capture-path entry point; the
        lower-level ``enqueue_capture`` remains for focused store maintenance
        tests that already hold keyed identifiers.
        """

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            if meta.clear_in_progress:
                return EnqueueResult(outcome="clearing")

            source_message_digest = _keyed_digest(meta.scope_key, source_message_id)
            existing = conn.execute(
                "SELECT * FROM memory_capture_queue WHERE source_message_digest = ?",
                (source_message_digest,),
            ).fetchone()
            if existing is not None:
                return EnqueueResult(outcome="duplicate", row=_queue_from_row(existing))

            pending_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_capture_queue
                    WHERE epoch = ? AND state IN ('pending', 'processing')
                    """,
                    (meta.epoch,),
                ).fetchone()[0]
            )
            if pending_count >= nonterminal_limit:
                self._record_capture_skip_in_connection(conn, "memory_queue_full", now)
                return EnqueueResult(outcome="queue_full")

            provider_timestamp_ms = max(occurred_at_ms, meta.last_provider_timestamp_ms + 1)
            if provider_timestamp_ms > max_provider_timestamp_ms:
                self._record_capture_skip_in_connection(conn, None, now)
                return EnqueueResult(outcome="timestamp_invalid")

            session_ref = _provider_session_ref(meta.scope_key, session_id, meta.epoch)
            conn.execute(
                """
                UPDATE memory_meta
                SET last_provider_timestamp_ms = ?,
                    last_error = CASE
                        WHEN last_error IN ('memory_queue_full', 'memory_low_disk_space') THEN NULL
                        ELSE last_error
                    END,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (provider_timestamp_ms, now),
            )
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, attempts,
                    next_retry_at, lease_owner, lease_at, last_error,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    source_message_digest,
                    meta.epoch,
                    session_ref,
                    payload_text,
                    occurred_at_ms,
                    provider_timestamp_ms,
                    now,
                ),
            )
            return EnqueueResult(
                outcome="accepted",
                row=QueueRow(
                    source_message_digest=source_message_digest,
                    epoch=meta.epoch,
                    session_id=session_ref,
                    payload_text=payload_text,
                    occurred_at_ms=occurred_at_ms,
                    provider_timestamp_ms=provider_timestamp_ms,
                    state="pending",
                    attempts=0,
                    next_retry_at=None,
                    lease_owner=None,
                    lease_at=None,
                    last_error=None,
                    created_at=now,
                    completed_at=None,
                ),
            )

    def enqueue_capture(
        self,
        *,
        source_message_digest: str,
        session_ref: str,
        payload_text: str,
        occurred_at_ms: int,
        max_provider_timestamp_ms: int,
        nonterminal_limit: int = MAX_NONTERMINAL_QUEUE_ROWS,
    ) -> EnqueueResult:
        """Atomically deduplicate, allocate a provider timestamp, and queue a capture."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            existing = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE source_message_digest = ?
                """,
                (source_message_digest,),
            ).fetchone()
            if existing is not None:
                return EnqueueResult(outcome="duplicate", row=_queue_from_row(existing))
            if meta.clear_in_progress:
                return EnqueueResult(outcome="clearing")

            pending_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_capture_queue
                    WHERE epoch = ? AND state IN ('pending', 'processing')
                    """,
                    (meta.epoch,),
                ).fetchone()[0]
            )
            if pending_count >= nonterminal_limit:
                return EnqueueResult(outcome="queue_full")

            provider_timestamp_ms = max(occurred_at_ms, meta.last_provider_timestamp_ms + 1)
            if provider_timestamp_ms > max_provider_timestamp_ms:
                return EnqueueResult(outcome="timestamp_invalid")

            conn.execute(
                """
                UPDATE memory_meta
                SET last_provider_timestamp_ms = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (provider_timestamp_ms, now),
            )
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, attempts,
                    next_retry_at, lease_owner, lease_at, last_error,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    source_message_digest,
                    meta.epoch,
                    session_ref,
                    payload_text,
                    occurred_at_ms,
                    provider_timestamp_ms,
                    now,
                ),
            )
            row = QueueRow(
                source_message_digest=source_message_digest,
                epoch=meta.epoch,
                session_id=session_ref,
                payload_text=payload_text,
                occurred_at_ms=occurred_at_ms,
                provider_timestamp_ms=provider_timestamp_ms,
                state="pending",
                attempts=0,
                next_retry_at=None,
                lease_owner=None,
                lease_at=None,
                last_error=None,
                created_at=now,
                completed_at=None,
            )
            return EnqueueResult(outcome="accepted", row=row)

    def claim_due(self, *, lease_owner: str, now: str) -> QueueRow | None:
        """Fence one due pending row for a worker without holding a provider call transaction."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.clear_in_progress:
                return None
            row = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE epoch = ?
                  AND state = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at, source_message_digest
                LIMIT 1
                """,
                (meta.epoch, now),
            ).fetchone()
            if row is None:
                return None
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'processing', lease_owner = ?, lease_at = ?
                WHERE source_message_digest = ? AND epoch = ? AND state = 'pending'
                """,
                (lease_owner, now, row["source_message_digest"], meta.epoch),
            )
            if result.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["state"] = "processing"
            claimed["lease_owner"] = lease_owner
            claimed["lease_at"] = now
            return _queue_from_row(claimed)

    def mark_delivered(self, row: QueueRow, *, lease_owner: str, now: str) -> bool:
        """Finalize a fenced provider success and scrub the source payload."""

        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'delivered', payload_text = NULL, next_retry_at = NULL,
                    lease_owner = NULL, lease_at = NULL, last_error = NULL,
                    completed_at = ?
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ?
                """,
                (now, row.source_message_digest, row.epoch, lease_owner),
            )
            if result.rowcount != 1:
                return False
            conn.execute(
                """
                UPDATE memory_meta
                SET last_success_at = ?, last_error = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (now, now),
            )
            self._compact_terminal_tombstones_in_connection(conn, _datetime_from_iso(now))
            return True

    def return_system_failure(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        error: MemoryErrorCode,
        now: str,
    ) -> bool:
        """Release a claimed row after a global outage without consuming attempts."""

        error = _closed_error_or(error, "memory_sidecar_unavailable")
        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'pending', next_retry_at = NULL,
                    lease_owner = NULL, lease_at = NULL, last_error = ?
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ?
                """,
                (error, row.source_message_digest, row.epoch, lease_owner),
            )
            if result.rowcount != 1:
                return False
            self._set_last_error_in_connection(conn, error, now)
            return True

    def record_message_failure(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        error: MemoryErrorCode,
        retryable: bool,
        now: datetime,
    ) -> MessageFailureResult:
        """Spend one message failure attempt, retrying or terminally scrubbing it."""

        error = _closed_error_or(error, "memory_processing_failed")
        now_iso = _iso_from_datetime(now)
        with self._transaction() as conn:
            current = conn.execute(
                """
                SELECT attempts FROM memory_capture_queue
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ?
                """,
                (row.source_message_digest, row.epoch, lease_owner),
            ).fetchone()
            if current is None:
                return MessageFailureResult(state=None, attempts=None)
            attempts = int(current["attempts"]) + 1
            terminal = not retryable or attempts >= MAX_MESSAGE_ATTEMPTS
            if terminal:
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET state = 'dead', attempts = ?, payload_text = NULL,
                        next_retry_at = NULL, lease_owner = NULL, lease_at = NULL,
                        last_error = ?, completed_at = ?
                    WHERE source_message_digest = ? AND epoch = ?
                      AND state = 'processing' AND lease_owner = ?
                    """,
                    (
                        attempts,
                        error,
                        now_iso,
                        row.source_message_digest,
                        row.epoch,
                        lease_owner,
                    ),
                )
                state: Literal["pending", "dead"] = "dead"
                self._compact_terminal_tombstones_in_connection(conn, now)
            else:
                retry_at = now + (timedelta(seconds=30) if attempts == 1 else timedelta(minutes=2))
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET state = 'pending', attempts = ?, next_retry_at = ?,
                        lease_owner = NULL, lease_at = NULL, last_error = ?
                    WHERE source_message_digest = ? AND epoch = ?
                      AND state = 'processing' AND lease_owner = ?
                    """,
                    (
                        attempts,
                        _iso_from_datetime(retry_at),
                        error,
                        row.source_message_digest,
                        row.epoch,
                        lease_owner,
                    ),
                )
                state = "pending"
            self._set_last_error_in_connection(conn, error, now_iso)
            return MessageFailureResult(state=state, attempts=attempts)

    def reclaim_processing(self, *, lease_owner: str) -> int:
        """Return rows leased by prior boots to pending for at-least-once delivery."""

        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'pending', lease_owner = NULL, lease_at = NULL,
                    next_retry_at = NULL
                WHERE state = 'processing'
                  AND (lease_owner IS NULL OR lease_owner != ?)
                """,
                (lease_owner,),
            )
            return int(result.rowcount)

    def queue_stats(self) -> QueueStats:
        """Return aggregate counts and retained plaintext bytes for the active epoch."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return QueueStats()
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN state = 'processing' THEN 1 ELSE 0 END) AS processing,
                    SUM(CASE WHEN state = 'dead' THEN 1 ELSE 0 END) AS dead,
                    COALESCE(SUM(
                        CASE WHEN state IN ('pending', 'processing')
                        THEN length(CAST(payload_text AS BLOB)) ELSE 0 END
                    ), 0) AS plaintext_bytes
                FROM memory_capture_queue
                WHERE epoch = ?
                """,
                (meta.epoch,),
            ).fetchone()
        return QueueStats(
            pending=int(row["pending"] or 0),
            processing=int(row["processing"] or 0),
            dead=int(row["dead"] or 0),
            queue_plaintext_bytes=int(row["plaintext_bytes"] or 0),
        )

    def get_queue_row(self, source_message_digest: str) -> QueueRow | None:
        """Return one queue row for worker and focused store tests."""

        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_capture_queue WHERE source_message_digest = ?",
                (source_message_digest,),
            ).fetchone()
        return _queue_from_row(row) if row is not None else None

    def list_queue_rows(self) -> tuple[QueueRow, ...]:
        """Return queue rows in deterministic order for internal maintenance and tests."""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_capture_queue ORDER BY created_at, source_message_digest"
            ).fetchall()
        return tuple(_queue_from_row(row) for row in rows)

    def begin_clear(self) -> MemoryMeta:
        """Persist the clear-recovery marker and advance the epoch exactly once."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            if meta.clear_in_progress:
                return meta
            epoch = meta.epoch + 1
            conn.execute(
                """
                UPDATE memory_meta
                SET epoch = ?, clear_in_progress = 1, missed_count = 0,
                    last_success_at = NULL, last_error = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (epoch, now),
            )
            return MemoryMeta(
                epoch=epoch,
                clear_in_progress=True,
                principal_id=meta.principal_id,
                scope_key=meta.scope_key,
                provider_root_id=meta.provider_root_id,
                last_provider_timestamp_ms=meta.last_provider_timestamp_ms,
                missed_count=0,
                last_success_at=None,
                last_error=None,
                updated_at=now,
            )

    def finish_clear(self) -> MemoryMeta:
        """Delete all queue state and make the advanced epoch available again."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            conn.execute("DELETE FROM memory_capture_queue")
            conn.execute(
                """
                UPDATE memory_meta
                SET clear_in_progress = 0, last_error = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            return MemoryMeta(
                epoch=meta.epoch,
                clear_in_progress=False,
                principal_id=meta.principal_id,
                scope_key=meta.scope_key,
                provider_root_id=meta.provider_root_id,
                last_provider_timestamp_ms=meta.last_provider_timestamp_ms,
                missed_count=meta.missed_count,
                last_success_at=meta.last_success_at,
                last_error=None,
                updated_at=now,
            )

    def set_last_error(self, error: MemoryErrorCode | None) -> None:
        """Persist a closed error category without retaining exception details."""

        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta_in_connection(conn)
            self._set_last_error_in_connection(
                conn,
                _closed_error_or(error, "memory_store_unavailable") if error is not None else None,
                now,
            )

    def compact_terminal_tombstones(self, *, now: datetime | None = None) -> int:
        """Bound terminal digest retention by age and count without exposing payloads."""

        reference = now or datetime.now(UTC)
        with self._transaction() as conn:
            return self._compact_terminal_tombstones_in_connection(conn, reference)

    def _initialize(self) -> None:
        migration_path = Path(__file__).with_name("migrations") / "0001_initial.sql"
        with self._connection() as conn:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version == 0:
                conn.executescript(migration_path.read_text(encoding="utf-8"))
                conn.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            self._enforce_private_database_modes()
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            self._enforce_private_database_modes()
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _ensure_meta_in_connection(self, conn: sqlite3.Connection) -> MemoryMeta:
        meta = self._meta_in_connection(conn)
        if meta is not None:
            return meta
        now = utc_now_iso()
        principal_id = str(uuid.uuid4())
        scope_key = secrets.token_bytes(32)
        provider_root_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO memory_meta (
                singleton, epoch, clear_in_progress, principal_id, scope_key,
                provider_root_id, last_provider_timestamp_ms, missed_count,
                last_success_at, last_error, updated_at
            ) VALUES (1, 0, 0, ?, ?, ?, 0, 0, NULL, NULL, ?)
            """,
            (principal_id, scope_key, provider_root_id, now),
        )
        return MemoryMeta(
            epoch=0,
            clear_in_progress=False,
            principal_id=principal_id,
            scope_key=scope_key,
            provider_root_id=provider_root_id,
            last_provider_timestamp_ms=0,
            missed_count=0,
            last_success_at=None,
            last_error=None,
            updated_at=now,
        )

    def _meta_in_connection(self, conn: sqlite3.Connection) -> MemoryMeta | None:
        row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        return _meta_from_row(row) if row is not None else None

    def _set_last_error_in_connection(
        self,
        conn: sqlite3.Connection,
        error: MemoryErrorCode | None,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE memory_meta
            SET last_error = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (error, now),
        )

    def _record_capture_skip_in_connection(
        self,
        conn: sqlite3.Connection,
        error: MemoryErrorCode | None,
        now: str,
    ) -> None:
        """Increment missed work and retain at most a validated closed category."""

        safe_error = _closed_error_or(error, "memory_invalid_input") if error is not None else None
        conn.execute(
            """
            UPDATE memory_meta
            SET missed_count = missed_count + 1,
                last_error = COALESCE(?, last_error),
                updated_at = ?
            WHERE singleton = 1
            """,
            (safe_error, now),
        )

    def _prepare_private_directory(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_info = os.lstat(self.path.parent)
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise OSError("Memory store directory must be an owned directory")
        os.chmod(self.path.parent, 0o700)
        if stat.S_IMODE(os.stat(self.path.parent).st_mode) != 0o700:
            raise OSError("Memory store directory is not owner-only")
        try:
            database_info = os.lstat(self.path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(database_info.st_mode) or not stat.S_ISREG(database_info.st_mode):
            raise OSError("Memory database path must be a regular file")

    def _enforce_private_database_modes(self) -> None:
        for candidate in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            try:
                info = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OSError("Memory database path must be a regular file")
            os.chmod(candidate, 0o600)
            if stat.S_IMODE(os.stat(candidate).st_mode) != 0o600:
                raise OSError("Memory database is not owner-only")

    def _compact_terminal_tombstones_in_connection(
        self,
        conn: sqlite3.Connection,
        reference: datetime,
    ) -> int:
        cutoff = _iso_from_datetime(reference - TERMINAL_TOMBSTONE_RETENTION)
        removed = conn.execute(
            """
            DELETE FROM memory_capture_queue
            WHERE state IN ('delivered', 'dead')
              AND completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoff,),
        ).rowcount
        terminal_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_capture_queue WHERE state IN ('delivered', 'dead')"
            ).fetchone()[0]
        )
        overflow = max(terminal_count - TERMINAL_TOMBSTONE_LIMIT, 0)
        if overflow:
            removed += conn.execute(
                """
                DELETE FROM memory_capture_queue
                WHERE source_message_digest IN (
                    SELECT source_message_digest FROM memory_capture_queue
                    WHERE state IN ('delivered', 'dead')
                    ORDER BY completed_at, source_message_digest
                    LIMIT ?
                )
                """,
                (overflow,),
            ).rowcount
        return int(removed)


def _meta_from_row(row: sqlite3.Row) -> MemoryMeta:
    error = _closed_error_or(row["last_error"], "memory_store_unavailable") if row["last_error"] is not None else None
    return MemoryMeta(
        epoch=int(row["epoch"]),
        clear_in_progress=bool(row["clear_in_progress"]),
        principal_id=str(row["principal_id"]),
        scope_key=bytes(row["scope_key"]),
        provider_root_id=str(row["provider_root_id"]),
        last_provider_timestamp_ms=int(row["last_provider_timestamp_ms"]),
        missed_count=int(row["missed_count"]),
        last_success_at=str(row["last_success_at"]) if row["last_success_at"] is not None else None,
        last_error=error,
        updated_at=str(row["updated_at"]),
    )


def _queue_from_row(row: sqlite3.Row | dict[str, object]) -> QueueRow:
    last_error = (
        _closed_error_or(row["last_error"], "memory_store_unavailable")
        if row["last_error"] is not None
        else None
    )
    return QueueRow(
        source_message_digest=str(row["source_message_digest"]),
        epoch=int(row["epoch"]),
        session_id=str(row["session_id"]),
        payload_text=str(row["payload_text"]) if row["payload_text"] is not None else None,
        occurred_at_ms=int(row["occurred_at_ms"]),
        provider_timestamp_ms=int(row["provider_timestamp_ms"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        next_retry_at=str(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_at=str(row["lease_at"]) if row["lease_at"] is not None else None,
        last_error=last_error,
        created_at=str(row["created_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
    )


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _closed_error_or(value: object, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return value if is_memory_error_code(value) else fallback


def _keyed_digest(scope_key: bytes, value: str) -> str:
    return hmac.new(scope_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _provider_session_ref(scope_key: bytes, session_id: str, epoch: int) -> str:
    return f"src--{_keyed_digest(scope_key, session_id)}--e{epoch}"
