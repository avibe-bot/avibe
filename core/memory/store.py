"""Dedicated SQLite state for the provider-independent Memory module."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from config import paths
from core.memory.observations import (
    AddAck,
    AddRejected,
    FlushRejected,
    FlushResult,
    FlushSucceeded,
    FlushUnknown,
)
from core.memory.types import (
    MemoryErrorCode,
    MemoryFailureLogEntry,
    ProviderSessionRef,
    is_memory_error_code,
)


MEMORY_STORE_FILENAME = "memory.sqlite"
MEMORY_STORE_DIRNAME = "memory"
MEMORY_STORE_SCHEMA_VERSION = 1
MAX_NONTERMINAL_QUEUE_ROWS = 500
MAX_MESSAGE_ATTEMPTS = 3
TERMINAL_TOMBSTONE_LIMIT = 100_000
TERMINAL_TOMBSTONE_RETENTION = timedelta(days=90)

_MEMORY_STORE_TABLES = frozenset(
    {
        "memory_meta",
        "memory_attachment_bundle",
        "memory_session_flush_state",
        "memory_capture_queue",
        "memory_flush_settlements",
    }
)
_MEMORY_STORE_REQUIRED_COLUMNS = {
    "memory_meta": frozenset(
        {
            "singleton",
            "processing_fault_kind",
            "processing_fault_since",
            "processing_alert_active",
        }
    ),
    "memory_attachment_bundle": frozenset({"bundle_id", "relative_path", "state"}),
    "memory_session_flush_state": frozenset(
        {
            "provider_session_ref",
            "open_generation",
            "target_generation",
            "operation_epoch",
        }
    ),
    "memory_capture_queue": frozenset(
        {
            "provider_session_ref",
            "generation",
            "attachment_bundle_id",
            "lease_token",
            "add_status",
        }
    ),
    "memory_flush_settlements": frozenset(
        {"provider_session_ref", "generation", "operation_token", "recovery_origin"}
    ),
}
_MEMORY_STORE_INDEXES = frozenset(
    {
        "ix_memory_capture_due",
        "ix_memory_capture_session_generation",
        "ix_memory_session_flush_due",
        "ix_memory_flush_settlements_recent",
    }
)
_MEMORY_STORE_TRIGGERS = frozenset(
    {
        "trg_memory_flush_settlements_immutable",
        "trg_memory_flush_settlements_no_delete",
    }
)


def memory_store_path() -> Path:
    """Return the dedicated Memory database under the effective Avibe state root."""

    return paths.get_state_dir() / MEMORY_STORE_DIRNAME / MEMORY_STORE_FILENAME


def utc_now_iso() -> str:
    """Return a lexically sortable UTC instant with millisecond precision."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _application_tables(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not str(row[0]).startswith("sqlite_")
    )


def _quote_sqlite_identifier(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _install_clean_schema(
    conn: sqlite3.Connection,
    schema_sql: str,
    application_tables: frozenset[str],
) -> None:
    drops = "\n".join(
        f"DROP TABLE {_quote_sqlite_identifier(table)};"
        for table in sorted(application_tables)
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            f"{drops}\n"
            f"{schema_sql}\n"
            f"PRAGMA user_version = {MEMORY_STORE_SCHEMA_VERSION};\n"
            "COMMIT;"
        )
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _verify_current_schema(conn: sqlite3.Connection) -> None:
    application_tables = _application_tables(conn)
    if application_tables != _MEMORY_STORE_TABLES:
        raise RuntimeError("Memory store schema is incomplete")

    for table, required_columns in _MEMORY_STORE_REQUIRED_COLUMNS.items():
        columns = frozenset(
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table)})")
        )
        if not required_columns.issubset(columns):
            raise RuntimeError("Memory store schema is incomplete")

    indexes = frozenset(
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        if row[0] is not None
    )
    triggers = frozenset(
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    )
    if not _MEMORY_STORE_INDEXES.issubset(indexes) or not _MEMORY_STORE_TRIGGERS.issubset(
        triggers
    ):
        raise RuntimeError("Memory store schema is incomplete")

    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise RuntimeError("Memory store failed integrity check")


def _absolute_path_without_resolve(value: Path | str) -> Path:
    """Make a lexical absolute path without following any filesystem links."""

    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _ensure_no_follow_directory_chain(directory: Path) -> None:
    """Create and validate each directory component before SQLite can open a file."""

    if not directory.is_absolute():
        raise OSError("Memory store path must be absolute")
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, mode=0o700)
            except FileExistsError:
                pass
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("Memory store path contains an unsafe directory component")


@dataclass(frozen=True)
class MemoryMeta:
    epoch: int
    clear_in_progress: bool
    scope_key: bytes
    provider_root_id: str
    last_provider_timestamp_ms: int
    missed_count: int
    last_success_at: str | None
    last_error: MemoryErrorCode | None
    last_error_at: str | None
    processing_fault_kind: Literal["credential", "engine"] | None
    processing_fault_since: str | None
    processing_alert_active: bool
    updated_at: str


@dataclass(frozen=True)
class QueueRow:
    source_message_digest: str
    epoch: int
    session_id: str
    provider_session_ref: ProviderSessionRef
    generation: int
    principal_id: str
    project_ref: str
    provenance: Literal["user_input", "agent"]
    payload_text: str | None
    occurred_at_ms: int
    provider_timestamp_ms: int
    state: Literal["pending", "processing", "delivered", "dead", "manual_required"]
    attempts: int
    next_retry_at: str | None
    lease_owner: str | None
    lease_at: str | None
    last_error: MemoryErrorCode | None
    created_at: str
    completed_at: str | None
    payload_attachments: str | None = None
    attachment_bundle_id: str | None = None
    lease_token: int = 0
    add_request_id: str | None = None
    add_status: Literal["accumulated", "extracted"] | None = None


@dataclass(frozen=True)
class QueueStats:
    pending: int = 0
    processing: int = 0
    dead: int = 0
    queue_plaintext_bytes: int = 0
    awaiting_receipt: int = 0
    succeeded: int = 0
    receipt_unknown: int = 0
    distill_failed: int = 0
    last_flush_observation: Literal["succeeded", "rejected", "unknown"] | None = None
    last_flush_status: Literal["extracted", "no_extraction"] | None = None
    last_flush_error_code: str | None = None
    last_flush_request_id: str | None = None
    last_flush_at: str | None = None


@dataclass(frozen=True)
class EnqueueResult:
    outcome: Literal["accepted", "duplicate", "queue_full", "clearing", "timestamp_invalid"]
    row: QueueRow | None = None


@dataclass(frozen=True)
class MessageFailureResult:
    state: Literal["pending", "dead"] | None
    attempts: int | None
    attachment_release_id: str | None = None


@dataclass(frozen=True)
class Delivered:
    """The provider accepted the row; scrub the payload and keep the receipt."""

    add_request_id: str | None = None
    add_status: Literal["accumulated", "extracted"] = "accumulated"


@dataclass(frozen=True)
class AmbiguousAdd:
    """The provider response cannot prove whether this add was accepted."""

    add_request_id: str | None = None
    error: MemoryErrorCode = "memory_provider_response_invalid"


@dataclass(frozen=True)
class SystemOutage:
    """Infrastructure failed, not this row. Release it without spending an attempt."""

    error: MemoryErrorCode


@dataclass(frozen=True)
class MessageFailure:
    """This row failed. Spend one attempt, then retry or scrub it terminally."""

    error: MemoryErrorCode
    retryable: bool = True


#: Every way a claimed row can leave the ``processing`` state.
DeliveryOutcome = Delivered | AddRejected | AmbiguousAdd | SystemOutage | MessageFailure


@dataclass(frozen=True)
class SettleResult:
    """What one settle transition did to the claimed row."""

    #: False when the fenced update matched no row — a lost or stolen lease.
    settled: bool
    state: Literal["delivered", "pending", "dead", "manual_required"] | None = None
    #: Attempts consumed so far; only a MessageFailure spends one.
    attempts: int | None = None
    attachment_release_id: str | None = None


@dataclass(frozen=True)
class AddSettleResult:
    """Exact result of settling one provider add acknowledgement."""

    settled: bool
    natural_boundary: bool = False
    attachment_release_id: str | None = None


@dataclass(frozen=True)
class SessionFlushState:
    """The minimal durable authority for one canonical provider session."""

    provider_session_ref: ProviderSessionRef
    epoch: int
    open_generation: int
    target_generation: int | None
    state: Literal["idle", "due", "in_flight", "manual_required"]
    first_unflushed_at: str | None
    last_add_ack_at: str | None
    confirmed_add_watermark_ms: int | None
    unflushed_count: int
    due_at: str | None
    next_attempt_at: str | None
    retry_count: int
    operation_epoch: int
    fence_token: str | None
    submission_started_at: str | None
    updated_at: str


@dataclass(frozen=True)
class FlushLease:
    """An exact generation fence used by every flush CAS."""

    provider_session_ref: ProviderSessionRef
    epoch: int
    generation: int
    operation_epoch: int
    fence_token: str


@dataclass(frozen=True)
class FlushSettleResult:
    settled: bool
    state: Literal["idle", "due", "manual_required"] | None = None


@dataclass(frozen=True)
class BootRecovery:
    """What one boot recovery found, in the order the store had to look."""

    reclaimed: int
    interrupted_flushes: int
    due_flushes: int = 0


class MemoryStore:
    """Own the small, durable Memory queue without exposing SQLite to callers."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._effective_home = _absolute_path_without_resolve(paths.get_vibe_remote_dir())
        requested_path = db_path if db_path is not None else memory_store_path()
        self.path = _absolute_path_without_resolve(requested_path)
        self._validate_store_confinement()
        self._prepare_private_directory()
        self._initialize()
        self._enforce_private_database_modes()

    def ensure_meta(self) -> MemoryMeta:
        """Create and return the singleton metadata row when Memory first opens."""

        with self._transaction() as conn:
            return self._ensure_meta_in_connection(conn)

    def principal_for_user_key(self, user_key: str) -> str:
        """Return the stable opaque provider principal for one platform identity."""

        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            return derive_principal_id(meta.scope_key, user_key)

    def project_for_workdir(self, workdir: str) -> str:
        """Return the stable opaque provider project for one normalized cwd."""

        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            return derive_project_id(meta.scope_key, workdir)

    def provider_session_ref(
        self,
        *,
        principal_id: str,
        project_ref: str,
        session_id: str,
    ) -> ProviderSessionRef:
        """Resolve trusted caller context into the current canonical provider identity."""

        if not is_principal_id(principal_id) or not is_project_id(project_ref):
            raise ValueError("invalid Memory scope")
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ValueError("invalid Memory session")
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            if meta.clear_in_progress:
                raise RuntimeError("Memory clear is in progress")
            return ProviderSessionRef(
                principal_id=principal_id,
                epoch=meta.epoch,
                project_ref=project_ref,
                session_id=_provider_session_ref(
                    meta.scope_key,
                    principal_id,
                    project_ref,
                    session_id,
                    meta.epoch,
                ),
            )

    def resolve_current_session_scope(self, session_id: str) -> tuple[str, str] | None:
        """Recover one unambiguous current-epoch scope for a raw session ID.

        Raw session IDs are never persisted.  Recompute each durable canonical
        reference with the store-owned key so only exact capture state can
        authorize recovery after a controller restart.
        """

        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            return None
        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.clear_in_progress:
                return None
            rows = conn.execute(
                """
                SELECT provider_session_ref
                FROM memory_session_flush_state
                WHERE epoch = ?
                """,
                (meta.epoch,),
            ).fetchall()

        matches: set[tuple[str, str]] = set()
        for row in rows:
            try:
                ref = ProviderSessionRef.deserialize(str(row["provider_session_ref"]))
            except (TypeError, ValueError):
                return None
            if (
                ref.epoch != meta.epoch
                or not is_principal_id(ref.principal_id)
                or not is_project_id(ref.project_ref)
            ):
                return None
            expected_session_id = _provider_session_ref(
                meta.scope_key,
                ref.principal_id,
                ref.project_ref,
                session_id,
                meta.epoch,
            )
            if hmac.compare_digest(ref.session_id, expected_session_id):
                matches.add((ref.principal_id, ref.project_ref))
                if len(matches) > 1:
                    return None
        return next(iter(matches)) if matches else None

    def get_meta(self) -> MemoryMeta | None:
        """Return the metadata row without creating Memory state."""

        with self._connection() as conn:
            row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        return _meta_from_row(row) if row is not None else None

    def clear_in_progress(self) -> bool:
        """Return whether a prior or active Clear all operation is unfinished."""

        meta = self.get_meta()
        return bool(meta and meta.clear_in_progress)

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
        principal_id: str,
        project_ref: str,
        provenance: Literal["user_input", "agent"],
        payload_text: str,
        payload_attachments: str | None = None,
        attachment_bundle_id: str | None = None,
        attachment_bundle_relative_path: str | None = None,
        attachment_file_count: int = 0,
        attachment_total_bytes: int = 0,
        occurred_at_ms: int,
        max_provider_timestamp_ms: int,
        nonterminal_limit: int = MAX_NONTERMINAL_QUEUE_ROWS,
    ) -> EnqueueResult:
        """Admit one validated capture in a single local queue transaction.

        Raw source identifiers are transformed only inside this transaction and
        never written to SQLite.
        """

        if (
            not is_principal_id(principal_id)
            or not is_project_id(project_ref)
            or provenance not in {"user_input", "agent"}
        ):
            raise ValueError("invalid Memory capture identity")

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
                    WHERE epoch = ?
                      AND state IN ('pending', 'processing', 'manual_required')
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

            session_id_ref = _provider_session_ref(
                meta.scope_key,
                principal_id,
                project_ref,
                session_id,
                meta.epoch,
            )
            provider_session_ref = ProviderSessionRef(
                principal_id=principal_id,
                epoch=meta.epoch,
                project_ref=project_ref,
                session_id=session_id_ref,
            )
            serialized_ref = provider_session_ref.serialize()
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_session_flush_state (
                    provider_session_ref, epoch, open_generation,
                    target_generation, state, first_unflushed_at,
                    last_add_ack_at, confirmed_add_watermark_ms,
                    unflushed_count, due_at, next_attempt_at, retry_count,
                    operation_epoch, fence_token, submission_started_at, updated_at
                ) VALUES (?, ?, 1, NULL, 'idle', NULL, NULL, NULL, 0,
                          NULL, NULL, 0, 0, NULL, NULL, ?)
                """,
                (serialized_ref, meta.epoch, now),
            )
            session_state = conn.execute(
                """
                SELECT epoch, open_generation
                FROM memory_session_flush_state
                WHERE provider_session_ref = ?
                """,
                (serialized_ref,),
            ).fetchone()
            if session_state is None or int(session_state["epoch"]) != meta.epoch:
                raise RuntimeError("Memory session authority is inconsistent")
            generation = int(session_state["open_generation"])

            if attachment_bundle_id is not None:
                if (
                    not _is_bundle_id(attachment_bundle_id)
                    or not isinstance(attachment_bundle_relative_path, str)
                    or not attachment_bundle_relative_path
                    or not 1 <= attachment_file_count <= 8
                    or attachment_total_bytes < 0
                ):
                    raise ValueError("invalid Memory attachment bundle")
                conn.execute(
                    """
                    INSERT INTO memory_attachment_bundle (
                        bundle_id, relative_path, state, file_count,
                        total_bytes, created_at, updated_at
                    ) VALUES (?, ?, 'pinned', ?, ?, ?, ?)
                    """,
                    (
                        attachment_bundle_id,
                        attachment_bundle_relative_path,
                        attachment_file_count,
                        attachment_total_bytes,
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE memory_meta
                SET last_provider_timestamp_ms = ?,
                    last_error = CASE
                        WHEN last_error IN ('memory_queue_full', 'memory_low_disk_space') THEN NULL
                        ELSE last_error
                    END,
                    last_error_at = CASE
                        WHEN last_error IN ('memory_queue_full', 'memory_low_disk_space') THEN NULL
                        ELSE last_error_at
                    END,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (provider_timestamp_ms, now),
            )
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, provider_session_ref, generation,
                    principal_id,
                    project_ref, provenance, payload_text,
                    payload_attachments, attachment_bundle_id,
                    occurred_at_ms, provider_timestamp_ms,
                    state, attempts,
                    next_retry_at, lease_owner, lease_at, lease_token, last_error,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0,
                          NULL, NULL, NULL, 0, NULL, ?, NULL)
                """,
                (
                    source_message_digest,
                    meta.epoch,
                    session_id_ref,
                    serialized_ref,
                    generation,
                    principal_id,
                    project_ref,
                    provenance,
                    payload_text,
                    payload_attachments,
                    attachment_bundle_id,
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
                    session_id=session_id_ref,
                    provider_session_ref=provider_session_ref,
                    generation=generation,
                    principal_id=principal_id,
                    project_ref=project_ref,
                    provenance=provenance,
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
                    payload_attachments=payload_attachments,
                    attachment_bundle_id=attachment_bundle_id,
                ),
            )

    def claim_due(self, *, lease_owner: str, now: str) -> QueueRow | None:
        """Fence one due pending row for a worker without holding a provider call transaction."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.clear_in_progress:
                return None
            row = conn.execute(
                """
                SELECT q.* FROM memory_capture_queue AS q
                JOIN memory_session_flush_state AS session
                  ON session.provider_session_ref = q.provider_session_ref
                WHERE q.epoch = ?
                  AND q.state = 'pending'
                  AND session.epoch = q.epoch
                  AND session.state = 'idle'
                  AND (q.next_retry_at IS NULL OR q.next_retry_at <= ?)
                ORDER BY q.created_at, q.source_message_digest
                LIMIT 1
                """,
                (meta.epoch, now),
            ).fetchone()
            if row is None:
                return None
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'processing', lease_owner = ?, lease_at = ?,
                    lease_token = lease_token + 1
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
            claimed["lease_token"] = int(row["lease_token"]) + 1
            return _queue_from_row(claimed)

    def claim_fenced_generation(
        self,
        lease: FlushLease,
        *,
        lease_owner: str,
        now: str,
    ) -> QueueRow | None:
        """Claim one target-generation add while holding an exact flush fence."""

        serialized_ref = lease.provider_session_ref.serialize()
        with self._transaction() as conn:
            authority = conn.execute(
                """
                SELECT 1 FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND state = 'due'
                """,
                (
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            ).fetchone()
            if authority is None:
                return None
            row = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE provider_session_ref = ? AND epoch = ? AND generation = ?
                  AND state = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at, source_message_digest
                LIMIT 1
                """,
                (serialized_ref, lease.epoch, lease.generation, now),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'processing', lease_owner = ?, lease_at = ?,
                    lease_token = lease_token + 1
                WHERE source_message_digest = ? AND epoch = ? AND state = 'pending'
                """,
                (lease_owner, now, row["source_message_digest"], lease.epoch),
            )
            if updated.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["state"] = "processing"
            claimed["lease_owner"] = lease_owner
            claimed["lease_at"] = now
            claimed["lease_token"] = int(row["lease_token"]) + 1
            return _queue_from_row(claimed)

    def return_claim_if_fenced(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
    ) -> bool:
        """Return a raced normal claim after a fence was acquired."""

        serialized_ref = row.provider_session_ref.serialize()
        with self._transaction() as conn:
            authority = conn.execute(
                """
                SELECT state FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                """,
                (serialized_ref, row.epoch),
            ).fetchone()
            if authority is None or authority["state"] == "idle":
                return False
            updated = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'pending', lease_owner = NULL, lease_at = NULL
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            )
            return updated.rowcount == 1

    def return_unsubmitted_claim(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
    ) -> bool:
        """Return an exact add claim that never crossed the provider boundary."""

        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'pending', lease_owner = NULL, lease_at = NULL
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            )
            return updated.rowcount == 1

    def claim_is_current(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
    ) -> bool:
        """Revalidate an exact add lease immediately before provider submission."""

        with self._connection() as conn:
            current = conn.execute(
                """
                SELECT 1 FROM memory_capture_queue
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            ).fetchone()
        return current is not None

    def settle(
        self,
        row: QueueRow,
        outcome: DeliveryOutcome,
        *,
        lease_owner: str,
        now: datetime,
    ) -> SettleResult:
        """Move one claimed row out of ``processing``, whatever happened to it.

        This is the only way a claim ends. Callers choose *what happened*; which
        columns move, whether an attempt is spent, and whether the payload is
        scrubbed are decided here. Keeping the three transitions behind one
        method is what makes "a claimed row is always settled" checkable in one
        place instead of remembered at every call site.
        """

        now_iso = _iso_from_datetime(now)
        if isinstance(outcome, Delivered):
            settled = self.settle_add_ack(
                row,
                AddAck(
                    request_id=outcome.add_request_id,
                    status=outcome.add_status,
                ),
                lease_owner=lease_owner,
                now=now,
            )
            return SettleResult(
                settled=settled.settled,
                state="delivered" if settled.settled else None,
                attachment_release_id=settled.attachment_release_id,
            )
        if isinstance(outcome, AmbiguousAdd):
            settled = self._settle_ambiguous_add(
                row,
                lease_owner=lease_owner,
                now=now_iso,
                add_request_id=outcome.add_request_id,
                error=outcome.error,
            )
            return SettleResult(
                settled=settled,
                state="manual_required" if settled else None,
            )
        if isinstance(outcome, SystemOutage):
            settled = self._return_system_failure(
                row,
                lease_owner=lease_owner,
                error=outcome.error,
                now=now_iso,
            )
            return SettleResult(settled=settled, state="pending" if settled else None)
        if isinstance(outcome, AddRejected):
            error: MemoryErrorCode = "memory_processing_failed"
            retryable = False
            rejection = outcome
        else:
            error = outcome.error
            retryable = outcome.retryable
            rejection = None
        failure = self._record_message_failure(
            row,
            lease_owner=lease_owner,
            error=error,
            retryable=retryable,
            now=now,
            rejection=rejection,
        )
        return SettleResult(
            settled=failure.state is not None,
            state=failure.state,
            attempts=failure.attempts,
            attachment_release_id=failure.attachment_release_id,
        )

    def recover_after_boot(self, *, lease_owner: str, clock: Callable[[], datetime]) -> BootRecovery:
        """Return the queue to a claimable state after an unclean shutdown.

        Processing add leases are ambiguous and become session-terminal.
        Submitted flushes are also ambiguous; unsubmitted durable fences remain
        due and can be resumed with their exact generation/token.
        """

        reclaimed = self._reclaim_processing(lease_owner=lease_owner)
        now_iso = _iso_from_datetime(clock())
        interrupted = self._recover_interrupted_session_flushes(now=now_iso)
        with self._connection() as conn:
            due = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_session_flush_state
                    WHERE state = 'due' AND submission_started_at IS NULL
                    """
                ).fetchone()[0]
            )
        return BootRecovery(
            reclaimed=reclaimed,
            interrupted_flushes=interrupted,
            due_flushes=due,
        )

    def _recover_interrupted_session_flushes(self, *, now: str) -> int:
        """Fence every flush that crossed the durable submission boundary."""

        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_session_flush_state
                WHERE state = 'in_flight'
                   OR (state = 'due' AND submission_started_at IS NOT NULL)
                """
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_flush_settlements (
                        provider_session_ref, epoch, generation, operation_kind,
                        operation_token, observation, request_id,
                        confirmed_watermark_ms, observed_at, error_code,
                        recovery_origin
                    ) VALUES (?, ?, ?, 'flush', ?, 'manual_required', NULL, ?, ?, ?, 'boot')
                    """,
                    (
                        row["provider_session_ref"],
                        row["epoch"],
                        row["target_generation"],
                        row["fence_token"],
                        row["confirmed_add_watermark_ms"],
                        now,
                        "memory_provider_response_invalid",
                    ),
                )
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET state = 'manual_required', next_attempt_at = NULL, updated_at = ?
                    WHERE provider_session_ref = ? AND operation_epoch = ?
                      AND fence_token = ?
                    """,
                    (
                        now,
                        row["provider_session_ref"],
                        row["operation_epoch"],
                        row["fence_token"],
                    ),
                )
            if rows:
                self._open_processing_fault_in_connection(conn, now=now)
            return len(rows)

    def settle_add_ack(
        self,
        row: QueueRow,
        ack: AddAck,
        *,
        lease_owner: str,
        now: datetime,
        idle_timeout: timedelta = timedelta(minutes=5),
        max_unflushed_age: timedelta = timedelta(minutes=30),
        message_bound: int = 100,
    ) -> AddSettleResult:
        """Settle an exact add lease and update its session generation atomically."""

        if ack.status not in {"accumulated", "extracted"}:
            raise ValueError("unsupported Memory add acknowledgement")
        now_iso = _iso_from_datetime(now)
        serialized_ref = row.provider_session_ref.serialize()
        with self._transaction() as conn:
            current = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            ).fetchone()
            authority = conn.execute(
                """
                SELECT * FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                """,
                (serialized_ref, row.epoch),
            ).fetchone()
            if current is None or authority is None:
                return AddSettleResult(settled=False)
            state = str(authority["state"])
            open_generation = int(authority["open_generation"])
            target_generation = (
                int(authority["target_generation"])
                if authority["target_generation"] is not None
                else None
            )
            valid_generation = (
                (state == "idle" and row.generation == open_generation)
                or (
                    state == "due"
                    and target_generation == row.generation
                    and authority["submission_started_at"] is None
                )
            )
            if not valid_generation:
                return AddSettleResult(settled=False)

            bundle_id = (
                str(current["attachment_bundle_id"])
                if current["attachment_bundle_id"] is not None
                else None
            )
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'delivered', payload_text = NULL, payload_attachments = NULL,
                    attachment_bundle_id = NULL, next_retry_at = NULL,
                    lease_owner = NULL, lease_at = NULL, last_error = NULL,
                    completed_at = ?, add_request_id = ?, add_status = ?
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    now_iso,
                    _bounded_opaque_text(ack.request_id),
                    ack.status,
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            )
            if result.rowcount != 1:
                return AddSettleResult(settled=False)
            if bundle_id is not None:
                conn.execute(
                    """
                    UPDATE memory_attachment_bundle
                    SET state = 'releasing', updated_at = ?
                    WHERE bundle_id = ? AND state = 'pinned'
                    """,
                    (now_iso, bundle_id),
                )

            previous_first = (
                str(authority["first_unflushed_at"])
                if authority["first_unflushed_at"] is not None
                else now_iso
            )
            confirmed_watermark = max(
                row.provider_timestamp_ms,
                int(authority["confirmed_add_watermark_ms"] or 0),
            )
            natural_boundary = ack.status == "extracted"
            if natural_boundary:
                operation_token = f"add:{row.source_message_digest}:{row.lease_token}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_flush_settlements (
                        provider_session_ref, epoch, generation, operation_kind,
                        operation_token, observation, request_id,
                        confirmed_watermark_ms, observed_at, error_code
                    ) VALUES (?, ?, ?, 'add', ?, 'settled', ?, ?, ?, NULL)
                    """,
                    (
                        serialized_ref,
                        row.epoch,
                        row.generation,
                        operation_token,
                        _bounded_opaque_text(ack.request_id),
                        confirmed_watermark,
                        now_iso,
                    ),
                )
                next_generation = (
                    open_generation if state == "due" else open_generation + 1
                )
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET generation = ?
                    WHERE provider_session_ref = ? AND epoch = ? AND generation = ?
                      AND state = 'pending'
                    """,
                    (next_generation, serialized_ref, row.epoch, row.generation),
                )
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET open_generation = ?, target_generation = NULL, state = 'idle',
                        first_unflushed_at = NULL, last_add_ack_at = ?,
                        confirmed_add_watermark_ms = ?, unflushed_count = 0,
                        due_at = NULL, next_attempt_at = NULL, retry_count = 0,
                        fence_token = NULL, submission_started_at = NULL,
                        updated_at = ?
                    WHERE provider_session_ref = ? AND epoch = ?
                    """,
                    (
                        next_generation,
                        now_iso,
                        confirmed_watermark,
                        now_iso,
                        serialized_ref,
                        row.epoch,
                    ),
                )
            else:
                unflushed_count = int(authority["unflushed_count"]) + 1
                idle_due = now + idle_timeout
                max_due = _datetime_from_iso(previous_first) + max_unflushed_age
                due = min(idle_due, max_due)
                if unflushed_count >= max(int(message_bound), 1):
                    due = now
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET first_unflushed_at = COALESCE(first_unflushed_at, ?),
                        last_add_ack_at = ?, confirmed_add_watermark_ms = ?,
                        unflushed_count = ?, due_at = ?, updated_at = ?
                    WHERE provider_session_ref = ? AND epoch = ?
                    """,
                    (
                        previous_first,
                        now_iso,
                        confirmed_watermark,
                        unflushed_count,
                        _iso_from_datetime(due),
                        now_iso,
                        serialized_ref,
                        row.epoch,
                    ),
                )
            conn.execute(
                """
                UPDATE memory_meta
                SET last_success_at = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (now_iso, now_iso),
            )
            self._compact_terminal_tombstones_in_connection(conn, now)
            return AddSettleResult(
                settled=True,
                natural_boundary=natural_boundary,
                attachment_release_id=bundle_id,
            )

    def _settle_ambiguous_add(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        now: str,
        add_request_id: str | None,
        error: MemoryErrorCode,
    ) -> bool:
        """Retain an uncertain add and fence its session against auto-replay."""

        safe_error = _closed_error_or(error, "memory_provider_response_invalid")
        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'manual_required', next_retry_at = NULL,
                    lease_owner = NULL, lease_at = NULL,
                    last_error = ?, add_request_id = ?, completed_at = ?
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    safe_error,
                    _bounded_opaque_text(add_request_id),
                    now,
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            )
            if result.rowcount != 1:
                return False
            serialized_ref = row.provider_session_ref.serialize()
            authority = conn.execute(
                """
                SELECT open_generation, operation_epoch
                FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                """,
                (serialized_ref, row.epoch),
            ).fetchone()
            if authority is None:
                raise RuntimeError("Memory session authority is missing")
            operation_epoch = int(authority["operation_epoch"]) + 1
            fence_token = f"add-{operation_epoch}-{secrets.token_hex(8)}"
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET open_generation = CASE
                        WHEN open_generation <= ? THEN ? + 1
                        ELSE open_generation
                    END,
                    target_generation = ?, state = 'manual_required',
                    operation_epoch = ?, fence_token = ?,
                    submission_started_at = ?, next_attempt_at = NULL,
                    updated_at = ?
                WHERE provider_session_ref = ? AND epoch = ?
                """,
                (
                    row.generation,
                    row.generation,
                    row.generation,
                    operation_epoch,
                    fence_token,
                    now,
                    now,
                    serialized_ref,
                    row.epoch,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_flush_settlements (
                    provider_session_ref, epoch, generation, operation_kind,
                    operation_token, observation, request_id,
                    confirmed_watermark_ms, observed_at, error_code
                ) VALUES (?, ?, ?, 'add', ?, 'manual_required', ?, NULL, ?, ?)
                """,
                (
                    serialized_ref,
                    row.epoch,
                    row.generation,
                    f"add:{row.source_message_digest}:{row.lease_token}",
                    _bounded_opaque_text(add_request_id),
                    now,
                    safe_error,
                ),
            )
            self._set_last_error_in_connection(conn, safe_error, now)
            self._open_processing_fault_in_connection(conn, now=now)
            return True

    def has_manual_required_fence(self) -> bool:
        """Return whether the active epoch contains a terminal session fence."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return False
            row = conn.execute(
                """
                SELECT 1 FROM memory_session_flush_state
                WHERE epoch = ? AND state = 'manual_required'
                LIMIT 1
                """,
                (meta.epoch,),
            ).fetchone()
        return row is not None

    def get_session_flush_state(
        self,
        provider_session_ref: ProviderSessionRef,
    ) -> SessionFlushState | None:
        """Read one canonical session authority for coordination and tests."""

        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_session_flush_state
                WHERE provider_session_ref = ?
                """,
                (provider_session_ref.serialize(),),
            ).fetchone()
        return _session_state_from_row(row) if row is not None else None

    def acquire_flush(
        self,
        *,
        now: str,
        provider_session_ref: ProviderSessionRef | None = None,
        force: bool = False,
    ) -> FlushLease | None:
        """Acquire or resume one due generation, persisting the fence first."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.clear_in_progress:
                return None
            if provider_session_ref is not None:
                serialized_ref = provider_session_ref.serialize()
                row = conn.execute(
                    """
                    SELECT * FROM memory_session_flush_state
                    WHERE provider_session_ref = ? AND epoch = ?
                    """,
                    (serialized_ref, meta.epoch),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM memory_session_flush_state
                    WHERE epoch = ? AND (
                        (state = 'due' AND submission_started_at IS NULL
                            AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                        OR
                        (state = 'idle' AND unflushed_count > 0
                            AND due_at IS NOT NULL AND due_at <= ?)
                    )
                    ORDER BY
                        CASE WHEN state = 'due' THEN 0 ELSE 1 END,
                        COALESCE(next_attempt_at, due_at), provider_session_ref
                    LIMIT 1
                    """,
                    (meta.epoch, now, now),
                ).fetchone()
                serialized_ref = str(row["provider_session_ref"]) if row is not None else ""
            if row is None:
                return None

            state = str(row["state"])
            if state == "due":
                if row["submission_started_at"] is not None:
                    return None
                next_attempt_at = row["next_attempt_at"]
                if next_attempt_at is not None and str(next_attempt_at) > now:
                    return None
                return _flush_lease_from_row(row)
            if state != "idle":
                return None

            if provider_session_ref is not None and force:
                pending = conn.execute(
                    """
                    SELECT 1 FROM memory_capture_queue
                    WHERE provider_session_ref = ? AND epoch = ?
                      AND generation = ? AND state IN ('pending', 'processing')
                    LIMIT 1
                    """,
                    (serialized_ref, meta.epoch, int(row["open_generation"])),
                ).fetchone()
                if int(row["unflushed_count"]) == 0 and pending is None:
                    return None
            elif not (
                int(row["unflushed_count"]) > 0
                and row["due_at"] is not None
                and str(row["due_at"]) <= now
            ):
                return None

            generation = int(row["open_generation"])
            operation_epoch = int(row["operation_epoch"]) + 1
            fence_token = f"flush-{operation_epoch}-{secrets.token_hex(16)}"
            updated = conn.execute(
                """
                UPDATE memory_session_flush_state
                SET open_generation = open_generation + 1,
                    target_generation = open_generation,
                    state = 'due', operation_epoch = ?, fence_token = ?,
                    next_attempt_at = NULL, retry_count = 0,
                    submission_started_at = NULL, updated_at = ?
                WHERE provider_session_ref = ? AND epoch = ?
                  AND state = 'idle' AND operation_epoch = ?
                """,
                (
                    operation_epoch,
                    fence_token,
                    now,
                    serialized_ref,
                    meta.epoch,
                    int(row["operation_epoch"]),
                ),
            )
            if updated.rowcount != 1:
                return None
            return FlushLease(
                provider_session_ref=ProviderSessionRef.deserialize(serialized_ref),
                epoch=meta.epoch,
                generation=generation,
                operation_epoch=operation_epoch,
                fence_token=fence_token,
            )

    def list_flush_candidates(self, *, now: str, limit: int = 16) -> tuple[ProviderSessionRef, ...]:
        """List independently acquirable session refs without changing state."""

        bounded_limit = max(1, min(int(limit), 64))
        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.clear_in_progress:
                return ()
            rows = conn.execute(
                """
                SELECT provider_session_ref
                FROM memory_session_flush_state
                WHERE epoch = ? AND (
                    (state = 'due' AND submission_started_at IS NULL
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR
                    (state = 'idle' AND unflushed_count > 0
                        AND due_at IS NOT NULL AND due_at <= ?)
                )
                ORDER BY
                    CASE WHEN state = 'due' THEN 0 ELSE 1 END,
                    COALESCE(next_attempt_at, due_at), provider_session_ref
                LIMIT ?
                """,
                (meta.epoch, now, now, bounded_limit),
            ).fetchall()
        return tuple(
            ProviderSessionRef.deserialize(str(row["provider_session_ref"]))
            for row in rows
        )

    def target_generation_counts(self, lease: FlushLease) -> tuple[int, int]:
        """Return pending/processing rows only after verifying the exact fence."""

        serialized_ref = lease.provider_session_ref.serialize()
        with self._connection() as conn:
            authority = conn.execute(
                """
                SELECT 1 FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND state = 'due'
                """,
                (
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            ).fetchone()
            if authority is None:
                return (0, 0)
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN state = 'processing' THEN 1 ELSE 0 END) AS processing
                FROM memory_capture_queue
                WHERE provider_session_ref = ? AND epoch = ? AND generation = ?
                """,
                (serialized_ref, lease.epoch, lease.generation),
            ).fetchone()
        return (int(row["pending"] or 0), int(row["processing"] or 0))

    def reclaim_fenced_generation_claims(self, lease: FlushLease) -> int:
        """Return pre-call raced claims while the coordinator owns the session lock."""

        serialized_ref = lease.provider_session_ref.serialize()
        with self._transaction() as conn:
            authority = conn.execute(
                """
                SELECT 1 FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND state = 'due'
                """,
                (
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            ).fetchone()
            if authority is None:
                return 0
            updated = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'pending', lease_owner = NULL, lease_at = NULL
                WHERE provider_session_ref = ? AND epoch = ? AND generation = ?
                  AND state = 'processing'
                """,
                (serialized_ref, lease.epoch, lease.generation),
            )
            return int(updated.rowcount)

    def mark_flush_submission_started(self, lease: FlushLease, *, now: str) -> bool:
        """Persist the ambiguity boundary only after the target generation drains."""

        serialized_ref = lease.provider_session_ref.serialize()
        with self._transaction() as conn:
            remaining = conn.execute(
                """
                SELECT 1 FROM memory_capture_queue
                WHERE provider_session_ref = ? AND epoch = ? AND generation = ?
                  AND state IN ('pending', 'processing')
                LIMIT 1
                """,
                (serialized_ref, lease.epoch, lease.generation),
            ).fetchone()
            if remaining is not None:
                return False
            updated = conn.execute(
                """
                UPDATE memory_session_flush_state
                SET state = 'in_flight', submission_started_at = ?, updated_at = ?
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND state = 'due'
                  AND submission_started_at IS NULL
                """,
                (
                    now,
                    now,
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            )
            return updated.rowcount == 1

    def return_unsubmitted_flush(self, lease: FlushLease, *, now: str) -> bool:
        """Return an exact marked flush whose provider coroutine never began."""

        serialized_ref = lease.provider_session_ref.serialize()
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE memory_session_flush_state
                SET state = 'due', submission_started_at = NULL, updated_at = ?
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND state = 'in_flight'
                  AND submission_started_at IS NOT NULL
                """,
                (
                    now,
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            )
            return updated.rowcount == 1

    def settle_flush(
        self,
        lease: FlushLease,
        result: FlushResult,
        *,
        now: str,
    ) -> FlushSettleResult:
        """Settle one submitted flush with an exact generation/token CAS."""

        if isinstance(result, FlushSucceeded):
            if result.status not in {"extracted", "no_extraction"} or not result.request_id:
                observation = "manual_required"
                request_id = result.request_id
                error_code = "memory_provider_response_invalid"
            else:
                observation = "settled"
                request_id = result.request_id
                error_code = None
        elif isinstance(result, FlushRejected):
            observation = "rejected"
            request_id = result.request_id
            error_code = result.error_code or "memory_processing_failed"
        elif isinstance(result, FlushUnknown):
            observation = "manual_required"
            request_id = None
            error_code = (
                "memory_provider_timeout"
                if result.reason == "timeout"
                else "memory_provider_response_invalid"
            )
        else:
            raise TypeError("unsupported flush result")

        serialized_ref = lease.provider_session_ref.serialize()
        with self._transaction() as conn:
            authority = conn.execute(
                """
                SELECT confirmed_add_watermark_ms FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND state = 'in_flight'
                  AND submission_started_at IS NOT NULL
                """,
                (
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            ).fetchone()
            if authority is None:
                return FlushSettleResult(settled=False)
            conn.execute(
                """
                INSERT INTO memory_flush_settlements (
                    provider_session_ref, epoch, generation, operation_kind,
                    operation_token, observation, request_id,
                    confirmed_watermark_ms, observed_at, error_code
                ) VALUES (?, ?, ?, 'flush', ?, ?, ?, ?, ?, ?)
                """,
                (
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.fence_token,
                    observation,
                    _bounded_opaque_text(request_id),
                    authority["confirmed_add_watermark_ms"],
                    now,
                    _bounded_opaque_text(error_code),
                ),
            )
            if observation == "manual_required":
                updated = conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET state = 'manual_required', next_attempt_at = NULL, updated_at = ?
                    WHERE provider_session_ref = ? AND epoch = ?
                      AND target_generation = ? AND operation_epoch = ?
                      AND fence_token = ? AND state = 'in_flight'
                    """,
                    (
                        now,
                        serialized_ref,
                        lease.epoch,
                        lease.generation,
                        lease.operation_epoch,
                        lease.fence_token,
                    ),
                )
                state: Literal["idle", "due", "manual_required"] = "manual_required"
            else:
                updated = conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET target_generation = NULL, state = 'idle',
                        first_unflushed_at = NULL, unflushed_count = 0,
                        due_at = NULL, next_attempt_at = NULL, retry_count = 0,
                        fence_token = NULL, submission_started_at = NULL,
                        updated_at = ?
                    WHERE provider_session_ref = ? AND epoch = ?
                      AND target_generation = ? AND operation_epoch = ?
                      AND fence_token = ? AND state = 'in_flight'
                    """,
                    (
                        now,
                        serialized_ref,
                        lease.epoch,
                        lease.generation,
                        lease.operation_epoch,
                        lease.fence_token,
                    ),
                )
                state = "idle"
            if updated.rowcount != 1:
                raise RuntimeError("Memory flush authority changed inside settlement")
            return FlushSettleResult(settled=True, state=state)

    def retry_unsubmitted_flush(
        self,
        lease: FlushLease,
        *,
        now: datetime,
        max_attempts: int = 3,
    ) -> FlushSettleResult:
        """Back off a failure proven before the submission marker."""

        now_iso = _iso_from_datetime(now)
        serialized_ref = lease.provider_session_ref.serialize()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT retry_count FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND epoch = ?
                  AND target_generation = ? AND operation_epoch = ?
                  AND fence_token = ? AND (
                      (state = 'due' AND submission_started_at IS NULL)
                      OR state = 'in_flight'
                  )
                """,
                (
                    serialized_ref,
                    lease.epoch,
                    lease.generation,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            ).fetchone()
            if row is None:
                return FlushSettleResult(settled=False)
            retries = int(row["retry_count"]) + 1
            if retries > max(int(max_attempts), 0):
                conn.execute(
                    """
                    INSERT INTO memory_flush_settlements (
                        provider_session_ref, epoch, generation, operation_kind,
                        operation_token, observation, request_id,
                        confirmed_watermark_ms, observed_at, error_code
                    ) VALUES (?, ?, ?, 'flush', ?, 'manual_required', NULL, NULL, ?, ?)
                    """,
                    (
                        serialized_ref,
                        lease.epoch,
                        lease.generation,
                        lease.fence_token,
                        now_iso,
                        "memory_sidecar_unavailable",
                    ),
                )
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET state = 'manual_required', retry_count = ?,
                        next_attempt_at = NULL, updated_at = ?
                    WHERE provider_session_ref = ? AND epoch = ?
                      AND operation_epoch = ? AND fence_token = ?
                      AND state IN ('due', 'in_flight')
                    """,
                    (
                        retries,
                        now_iso,
                        serialized_ref,
                        lease.epoch,
                        lease.operation_epoch,
                        lease.fence_token,
                    ),
                )
                return FlushSettleResult(settled=True, state="manual_required")
            retry_at = now + timedelta(seconds=2 ** (retries - 1))
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET state = 'due', retry_count = ?, next_attempt_at = ?,
                    submission_started_at = NULL, updated_at = ?
                WHERE provider_session_ref = ? AND epoch = ?
                  AND operation_epoch = ? AND fence_token = ?
                  AND state IN ('due', 'in_flight')
                """,
                (
                    retries,
                    _iso_from_datetime(retry_at),
                    now_iso,
                    serialized_ref,
                    lease.epoch,
                    lease.operation_epoch,
                    lease.fence_token,
                ),
            )
            return FlushSettleResult(settled=True, state="due")

    def _return_system_failure(
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
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    error,
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            )
            if result.rowcount != 1:
                return False
            self._set_last_error_in_connection(conn, error, now)
            return True

    def _record_message_failure(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        error: MemoryErrorCode,
        retryable: bool,
        now: datetime,
        rejection: AddRejected | None = None,
    ) -> MessageFailureResult:
        """Spend one message failure attempt, retrying or terminally scrubbing it."""

        error = _closed_error_or(error, "memory_processing_failed")
        now_iso = _iso_from_datetime(now)
        with self._transaction() as conn:
            current = conn.execute(
                """
                SELECT attempts, attachment_bundle_id FROM memory_capture_queue
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                    row.lease_token,
                ),
            ).fetchone()
            if current is None:
                return MessageFailureResult(state=None, attempts=None)
            attempts = int(current["attempts"]) + 1
            bundle_id = (
                str(current["attachment_bundle_id"])
                if current["attachment_bundle_id"] is not None
                else None
            )
            terminal = not retryable or attempts >= MAX_MESSAGE_ATTEMPTS
            if terminal:
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET state = 'dead', attempts = ?, payload_text = NULL,
                        payload_attachments = NULL, attachment_bundle_id = NULL,
                        next_retry_at = NULL, lease_owner = NULL, lease_at = NULL,
                        last_error = ?, add_request_id = ?, completed_at = ?
                    WHERE source_message_digest = ? AND epoch = ?
                      AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                    """,
                    (
                        attempts,
                        error,
                        _bounded_opaque_text(
                            rejection.request_id if rejection is not None else None
                        ),
                        now_iso,
                        row.source_message_digest,
                        row.epoch,
                        lease_owner,
                        row.lease_token,
                    ),
                )
                if bundle_id is not None:
                    conn.execute(
                        """
                        UPDATE memory_attachment_bundle
                        SET state = 'releasing', updated_at = ?
                        WHERE bundle_id = ? AND state = 'pinned'
                        """,
                        (now_iso, bundle_id),
                    )
                if rejection is not None:
                    conn.execute(
                        """
                        INSERT INTO memory_flush_settlements (
                            provider_session_ref, epoch, generation, operation_kind,
                            operation_token, observation, request_id,
                            confirmed_watermark_ms, observed_at, error_code
                        ) VALUES (?, ?, ?, 'add', ?, 'rejected', ?, NULL, ?, ?)
                        """,
                        (
                            row.provider_session_ref.serialize(),
                            row.epoch,
                            row.generation,
                            f"add:{row.source_message_digest}:{row.lease_token}",
                            _bounded_opaque_text(rejection.request_id),
                            now_iso,
                            _bounded_opaque_text(
                                rejection.error_code or "memory_processing_failed"
                            ),
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
                      AND state = 'processing' AND lease_owner = ? AND lease_token = ?
                    """,
                    (
                        attempts,
                        _iso_from_datetime(retry_at),
                        error,
                        row.source_message_digest,
                        row.epoch,
                        lease_owner,
                        row.lease_token,
                    ),
                )
                state = "pending"
            self._set_last_error_in_connection(conn, error, now_iso)
            return MessageFailureResult(
                state=state,
                attempts=attempts,
                attachment_release_id=bundle_id if terminal else None,
            )

    def _reclaim_processing(self, *, lease_owner: str) -> int:
        """Fence rows whose provider add outcome was ambiguous at boot."""

        now = utc_now_iso()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE state = 'processing'
                  AND (lease_owner IS NULL OR lease_owner != ?)
                """,
                (lease_owner,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET state = 'manual_required', lease_owner = NULL, lease_at = NULL,
                        next_retry_at = NULL,
                        last_error = 'memory_provider_response_invalid', completed_at = ?
                    WHERE source_message_digest = ? AND state = 'processing'
                    """,
                    (now, row["source_message_digest"]),
                )
                serialized_ref = str(row["provider_session_ref"])
                authority = conn.execute(
                    """
                    SELECT open_generation, operation_epoch
                    FROM memory_session_flush_state
                    WHERE provider_session_ref = ? AND epoch = ?
                    """,
                    (serialized_ref, row["epoch"]),
                ).fetchone()
                if authority is None:
                    continue
                operation_epoch = int(authority["operation_epoch"]) + 1
                fence_token = f"boot-add-{operation_epoch}-{secrets.token_hex(8)}"
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET open_generation = CASE
                            WHEN open_generation <= ? THEN ? + 1
                            ELSE open_generation
                        END,
                        target_generation = ?, state = 'manual_required',
                        operation_epoch = ?, fence_token = ?,
                        submission_started_at = ?, next_attempt_at = NULL,
                        updated_at = ?
                    WHERE provider_session_ref = ? AND epoch = ?
                    """,
                    (
                        row["generation"],
                        row["generation"],
                        row["generation"],
                        operation_epoch,
                        fence_token,
                        now,
                        now,
                        serialized_ref,
                        row["epoch"],
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_flush_settlements (
                        provider_session_ref, epoch, generation, operation_kind,
                        operation_token, observation, request_id,
                        confirmed_watermark_ms, observed_at, error_code,
                        recovery_origin
                    ) VALUES (?, ?, ?, 'add', ?, 'manual_required', NULL, NULL, ?, ?, 'boot')
                    """,
                    (
                        serialized_ref,
                        row["epoch"],
                        row["generation"],
                        f"add:{row['source_message_digest']}:{row['lease_token']}",
                        now,
                        "memory_provider_response_invalid",
                    ),
                )
            if rows:
                self._open_processing_fault_in_connection(conn, now=now)
            return len(rows)

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
                    SUM(CASE WHEN state = 'delivered' AND add_status = 'accumulated'
                        THEN 1 ELSE 0 END) AS awaiting_receipt,
                    SUM(CASE WHEN state = 'delivered' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN state = 'manual_required' THEN 1 ELSE 0 END)
                        AS receipt_unknown,
                    COALESCE(SUM(
                        CASE WHEN state IN ('pending', 'processing', 'manual_required')
                        THEN length(CAST(payload_text AS BLOB))
                           + length(CAST(COALESCE(payload_attachments, '') AS BLOB))
                        ELSE 0 END
                    ), 0) AS plaintext_bytes
                FROM memory_capture_queue
                WHERE epoch = ?
                """,
                (meta.epoch,),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT observation, error_code, request_id, observed_at
                FROM memory_flush_settlements
                WHERE epoch = ? AND operation_kind = 'flush'
                ORDER BY observed_at DESC, settlement_id DESC
                LIMIT 1
                """,
                (meta.epoch,),
            ).fetchone()
            rejected = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_flush_settlements
                    WHERE epoch = ? AND operation_kind = 'flush'
                      AND observation = 'rejected'
                    """,
                    (meta.epoch,),
                ).fetchone()[0]
            )
        return QueueStats(
            pending=int(row["pending"] or 0),
            processing=int(row["processing"] or 0),
            dead=int(row["dead"] or 0),
            queue_plaintext_bytes=int(row["plaintext_bytes"] or 0),
            awaiting_receipt=int(row["awaiting_receipt"] or 0),
            succeeded=int(row["succeeded"] or 0),
            receipt_unknown=int(row["receipt_unknown"] or 0),
            distill_failed=rejected,
            last_flush_observation=(
                {
                    "settled": "succeeded",
                    "rejected": "rejected",
                    "manual_required": "unknown",
                }.get(str(latest["observation"]))
                if latest is not None
                else None
            ),
            last_flush_status=None,
            last_flush_error_code=(
                str(latest["error_code"])
                if latest is not None and latest["error_code"] is not None
                else None
            ),
            last_flush_request_id=(
                str(latest["request_id"])
                if latest is not None and latest["request_id"] is not None
                else None
            ),
            last_flush_at=(
                str(latest["observed_at"])
                if latest is not None and latest["observed_at"] is not None
                else None
            ),
        )

    def failure_log(self, *, limit: int = 50) -> tuple[MemoryFailureLogEntry, ...]:
        """Return sanitized terminal delivery and provider observations."""

        bounded_limit = max(1, min(int(limit), 100))
        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return ()
            self._compact_terminal_tombstones_in_connection(conn, datetime.now(timezone.utc))
            rows = conn.execute(
                """
                SELECT kind, state, operation, generation,
                       occurred_at, error_code, request_id, attempts
                FROM (
                    SELECT
                        CASE
                            WHEN state = 'dead' THEN 'delivery_abandoned'
                            WHEN EXISTS (
                                SELECT 1
                                FROM memory_flush_settlements AS settlement
                                WHERE settlement.provider_session_ref =
                                        capture.provider_session_ref
                                  AND settlement.epoch = capture.epoch
                                  AND settlement.generation = capture.generation
                                  AND settlement.operation_kind = 'add'
                                  AND settlement.observation = 'manual_required'
                                  AND settlement.recovery_origin = 'boot'
                                  AND settlement.operation_token =
                                      'add:' || capture.source_message_digest || ':' ||
                                      capture.lease_token
                            ) THEN 'boot_recovery'
                            ELSE 'result_unknown'
                        END AS kind,
                        state,
                        'add' AS operation,
                        generation,
                        COALESCE(completed_at, created_at) AS occurred_at,
                        last_error AS error_code,
                        add_request_id AS request_id,
                        attempts,
                        source_message_digest AS sort_key
                    FROM memory_capture_queue AS capture
                    WHERE capture.epoch = ?
                      AND capture.state IN ('dead', 'manual_required')
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_flush_settlements AS settlement
                          WHERE settlement.provider_session_ref = capture.provider_session_ref
                            AND settlement.epoch = capture.epoch
                            AND settlement.generation = capture.generation
                            AND settlement.operation_kind = 'add'
                            AND settlement.observation = 'rejected'
                            AND settlement.operation_token =
                                'add:' || capture.source_message_digest || ':' || capture.lease_token
                      )

                    UNION ALL

                    SELECT
                        CASE
                            WHEN recovery_origin = 'boot' THEN 'boot_recovery'
                            WHEN observation = 'rejected' AND operation_kind = 'flush'
                                THEN 'distillation_rejected'
                            WHEN observation = 'rejected' THEN 'delivery_abandoned'
                            ELSE 'result_unknown'
                        END AS kind,
                        observation AS state,
                        operation_kind AS operation,
                        generation,
                        observed_at AS occurred_at,
                        error_code,
                        request_id,
                        0 AS attempts,
                        printf('%020d', settlement_id) AS sort_key
                    FROM memory_flush_settlements
                    WHERE epoch = ? AND (
                        (operation_kind = 'flush'
                            AND observation IN ('rejected', 'manual_required'))
                        OR (operation_kind = 'add' AND observation = 'rejected')
                    )
                )
                ORDER BY occurred_at DESC, sort_key DESC
                LIMIT ?
                """,
                (meta.epoch, meta.epoch, bounded_limit),
            ).fetchall()
        return tuple(
            MemoryFailureLogEntry(
                kind=str(row["kind"]),
                occurred_at=str(row["occurred_at"]),
                error_code=(str(row["error_code"]) if row["error_code"] is not None else None),
                request_id=(
                    str(row["request_id"])
                    if row["request_id"] is not None
                    else None
                ),
                attempts=int(row["attempts"]),
                state=str(row["state"]),
                operation=str(row["operation"]),
                generation=int(row["generation"]),
            )
            for row in rows
        )

    def has_provider_data_history(self) -> bool:
        """Whether the active epoch contains any queued or terminal Memory history."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return False
            row = conn.execute(
                """
                SELECT 1 FROM memory_capture_queue
                WHERE epoch = ?
                LIMIT 1
                """,
                (meta.epoch,),
            ).fetchone()
        return row is not None

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

    def attachment_bundle_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """Return referenced and releasing bundle IDs for filesystem reconciliation."""

        with self._connection() as conn:
            referenced_rows = conn.execute(
                """
                SELECT DISTINCT attachment_bundle_id
                FROM memory_capture_queue
                WHERE attachment_bundle_id IS NOT NULL
                """
            ).fetchall()
            releasing_rows = conn.execute(
                """
                SELECT bundle_id FROM memory_attachment_bundle
                WHERE state = 'releasing'
                """
            ).fetchall()
        return (
            frozenset(str(row[0]) for row in referenced_rows),
            frozenset(str(row[0]) for row in releasing_rows),
        )

    def finalize_attachment_release(self, bundle_id: str) -> bool:
        """Delete one releasing metadata row after its private files are gone."""

        if not _is_bundle_id(bundle_id):
            return False
        with self._transaction() as conn:
            referenced = conn.execute(
                """
                SELECT 1 FROM memory_capture_queue
                WHERE attachment_bundle_id = ? LIMIT 1
                """,
                (bundle_id,),
            ).fetchone()
            if referenced is not None:
                return False
            deleted = conn.execute(
                """
                DELETE FROM memory_attachment_bundle
                WHERE bundle_id = ? AND state = 'releasing'
                """,
                (bundle_id,),
            )
            return deleted.rowcount == 1

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
                    last_success_at = NULL, last_error = NULL, last_error_at = NULL,
                    processing_fault_kind = NULL, processing_fault_since = NULL,
                    processing_alert_active = 0, updated_at = ?
                WHERE singleton = 1
                """,
                (epoch, now),
            )
            return MemoryMeta(
                epoch=epoch,
                clear_in_progress=True,
                scope_key=meta.scope_key,
                provider_root_id=meta.provider_root_id,
                last_provider_timestamp_ms=meta.last_provider_timestamp_ms,
                missed_count=0,
                last_success_at=None,
                last_error=None,
                last_error_at=None,
                processing_fault_kind=None,
                processing_fault_since=None,
                processing_alert_active=False,
                updated_at=now,
            )

    def begin_clear_fence(self) -> MemoryMeta:
        """Fence admission without changing the pre-clear snapshot generation."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            conn.execute(
                """
                UPDATE memory_meta
                SET clear_in_progress = 1, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            refreshed = self._meta_in_connection(conn)
            if refreshed is None:
                raise RuntimeError("Memory metadata disappeared while fencing clear")
            return refreshed

    def release_clear_fence(self) -> MemoryMeta:
        """Release admission after an abort restored the pre-clear surfaces."""

        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta_in_connection(conn)
            conn.execute(
                """
                UPDATE memory_meta
                SET clear_in_progress = 0, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            refreshed = self._meta_in_connection(conn)
            if refreshed is None:
                raise RuntimeError("Memory metadata disappeared while releasing clear")
            return refreshed

    def finish_clear(self) -> MemoryMeta:
        """Delete all queue state and make the advanced epoch available again."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            conn.execute("DELETE FROM memory_capture_queue")
            conn.execute("DELETE FROM memory_flush_settlements")
            conn.execute("DELETE FROM memory_session_flush_state")
            conn.execute("DELETE FROM memory_attachment_bundle")
            conn.execute(
                """
                UPDATE memory_meta
                SET clear_in_progress = 0, last_error = NULL,
                    last_error_at = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            return MemoryMeta(
                epoch=meta.epoch,
                clear_in_progress=False,
                scope_key=meta.scope_key,
                provider_root_id=meta.provider_root_id,
                last_provider_timestamp_ms=meta.last_provider_timestamp_ms,
                missed_count=meta.missed_count,
                last_success_at=meta.last_success_at,
                last_error=None,
                last_error_at=None,
                processing_fault_kind=meta.processing_fault_kind,
                processing_fault_since=meta.processing_fault_since,
                processing_alert_active=meta.processing_alert_active,
                updated_at=now,
            )

    def reset_for_clear(self, *, target_epoch: int | None = None) -> MemoryMeta:
        """Clear SQLite state at an exact, replay-safe target epoch."""

        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            epoch = meta.epoch + 1 if target_epoch is None else target_epoch
            if (
                isinstance(epoch, bool)
                or not isinstance(epoch, int)
                or epoch < 0
                or meta.epoch not in {epoch - 1, epoch}
            ):
                raise ValueError("Memory clear target epoch does not match current state")
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE memory_meta
                SET clear_in_progress = 1, updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            conn.execute("DELETE FROM memory_capture_queue")
            conn.execute("DELETE FROM memory_flush_settlements")
            conn.execute("DELETE FROM memory_session_flush_state")
            conn.execute("DELETE FROM memory_attachment_bundle")
            conn.execute(
                """
                UPDATE memory_meta
                SET epoch = ?, clear_in_progress = 0,
                    last_provider_timestamp_ms = 0, missed_count = 0,
                    last_success_at = NULL, last_error = NULL, last_error_at = NULL,
                    processing_fault_kind = NULL, processing_fault_since = NULL,
                    processing_alert_active = 0, updated_at = ?
                WHERE singleton = 1
                """,
                (epoch, now),
            )
            refreshed = self._meta_in_connection(conn)
            if refreshed is None:
                raise RuntimeError("Memory metadata disappeared during clear")
            return refreshed

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

    def open_processing_fault(self, *, now: str) -> bool:
        """Persist one OPEN cycle and return whether it starts a new outage."""

        with self._transaction() as conn:
            return self._open_processing_fault_in_connection(conn, now=now)

    def _open_processing_fault_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
    ) -> bool:
        meta = self._ensure_meta_in_connection(conn)
        newly_open = meta.processing_fault_since is None
        conn.execute(
            """
            UPDATE memory_meta
            SET processing_fault_kind = CASE
                    WHEN processing_fault_since IS NULL THEN NULL
                    ELSE processing_fault_kind
                END,
                processing_fault_since = ?,
                last_error = 'memory_processing_failed', last_error_at = ?,
                updated_at = ?
            WHERE singleton = 1
            """,
            (now, now, now),
        )
        return newly_open

    def classify_processing_fault(self, kind: Literal["credential", "engine"]) -> bool:
        """Store display classification and report whether its alert is pending."""

        if kind not in {"credential", "engine"}:
            raise ValueError("invalid processing fault kind")
        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.processing_fault_since is None:
                return False
            should_alert = not meta.processing_alert_active
            conn.execute(
                """
                UPDATE memory_meta
                SET processing_fault_kind = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (kind, now),
            )
            return should_alert

    def mark_processing_alert_active(self) -> bool:
        """Persist that the current outage notification was delivered."""

        now = utc_now_iso()
        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_meta
                SET processing_alert_active = 1, updated_at = ?
                WHERE singleton = 1 AND processing_fault_since IS NOT NULL
                  AND processing_alert_active = 0
                """,
                (now,),
            )
            return bool(result.rowcount)

    def close_processing_fault(self, *, now: str) -> bool:
        """Close an active breaker without clearing unrelated persisted errors."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.processing_fault_since is None:
                return False
            conn.execute(
                """
                UPDATE memory_meta
                SET processing_fault_kind = NULL, processing_fault_since = NULL,
                    processing_alert_active = 0,
                    last_error = CASE
                        WHEN last_error = 'memory_processing_failed' THEN NULL
                        ELSE last_error
                    END,
                    last_error_at = CASE
                        WHEN last_error = 'memory_processing_failed' THEN NULL
                        ELSE last_error_at
                    END,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (now,),
            )
            return True

    def clear_system_outage_error(self) -> None:
        """Clear only the availability categories resolved by a fresh health probe."""

        now = utc_now_iso()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE memory_meta
                SET last_error = NULL, last_error_at = NULL, updated_at = ?
                WHERE singleton = 1
                  AND (
                    last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                    OR (last_error = 'memory_processing_failed' AND processing_fault_since IS NULL)
                  )
                """,
                (now,),
            )

    def clear_superseded_error(
        self,
        *,
        expected_error: MemoryErrorCode,
        expected_error_at: str,
    ) -> bool:
        """Atomically retire a legacy error superseded by a newer flush observation."""

        now = utc_now_iso()
        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_meta
                SET last_error = NULL, last_error_at = NULL, updated_at = ?
                WHERE singleton = 1
                  AND last_error = ? AND last_error_at = ?
                  AND (
                    last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                    OR (last_error = 'memory_processing_failed' AND processing_fault_since IS NULL)
                  )
                """,
                (now, expected_error, expected_error_at),
            )
            return bool(result.rowcount)

    def compact_terminal_tombstones(self, *, now: datetime | None = None) -> int:
        """Bound terminal digest retention by age and count without exposing payloads."""

        reference = now or datetime.now(timezone.utc)
        with self._transaction() as conn:
            return self._compact_terminal_tombstones_in_connection(conn, reference)

    def _initialize(self) -> None:
        schema_sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._connection() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            application_tables = _application_tables(conn)
            if version not in {0, MEMORY_STORE_SCHEMA_VERSION}:
                raise RuntimeError(f"Unsupported Memory store schema version: {version}")
            if version == 0:
                _install_clean_schema(conn, schema_sql, application_tables)
            _verify_current_schema(conn)

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
        scope_key = secrets.token_bytes(32)
        provider_root_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO memory_meta (
                singleton, epoch, clear_in_progress, scope_key,
                provider_root_id, last_provider_timestamp_ms, missed_count,
                last_success_at, last_error, last_error_at, updated_at
            ) VALUES (1, 0, 0, ?, ?, 0, 0, NULL, NULL, NULL, ?)
            """,
            (scope_key, provider_root_id, now),
        )
        return MemoryMeta(
            epoch=0,
            clear_in_progress=False,
            scope_key=scope_key,
            provider_root_id=provider_root_id,
            last_provider_timestamp_ms=0,
            missed_count=0,
            last_success_at=None,
            last_error=None,
            last_error_at=None,
            processing_fault_kind=None,
            processing_fault_since=None,
            processing_alert_active=False,
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
            SET last_error = ?, last_error_at = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (error, now if error is not None else None, now),
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
                last_error_at = CASE WHEN ? IS NOT NULL THEN ? ELSE last_error_at END,
                updated_at = ?
            WHERE singleton = 1
            """,
            (safe_error, safe_error, now, now),
        )

    def _prepare_private_directory(self) -> None:
        _ensure_no_follow_directory_chain(self._effective_home)
        _ensure_no_follow_directory_chain(self.path.parent)
        directory_info = os.lstat(self.path.parent)
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise OSError("Memory store directory must be an owned directory")
        os.chmod(self.path.parent, 0o700)
        if stat.S_IMODE(os.lstat(self.path.parent).st_mode) != 0o700:
            raise OSError("Memory store directory is not owner-only")
        try:
            database_info = os.lstat(self.path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(database_info.st_mode) or not stat.S_ISREG(database_info.st_mode):
            raise OSError("Memory database path must be a regular file")

    def _enforce_private_database_modes(self) -> None:
        self._enforce_private_file_mode(self.path, sidecar=False)
        self._enforce_private_file_mode(self.path.with_name(f"{self.path.name}-wal"), sidecar=True)
        self._enforce_private_file_mode(self.path.with_name(f"{self.path.name}-shm"), sidecar=True)

    def _enforce_private_file_mode(self, candidate: Path, *, sidecar: bool) -> None:
        """Hold one Memory database file at owner-only mode, or fail closed.

        SQLite creates and removes the WAL/shm sidecars itself, so a concurrent
        connection checkpointing mid-check can delete one between any two of
        these syscalls. This method runs on both entry to and exit from
        `_connection()`, so an unguarded ENOENT there surfaces a benign race as
        `memory_store_unavailable` on an otherwise successful capture or read.
        A sidecar therefore treats its own disappearance as the race it is;
        every other verification stays strict, and for the main database only
        the not-yet-created case is tolerated.
        """

        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("Memory database path must be a regular file")
        try:
            os.chmod(candidate, 0o600)
            mode = stat.S_IMODE(os.lstat(candidate).st_mode)
        except FileNotFoundError:
            if sidecar:
                return
            raise
        if mode != 0o600:
            raise OSError("Memory database is not owner-only")

    def _validate_store_confinement(self) -> None:
        try:
            self.path.relative_to(self._effective_home)
        except ValueError as error:
            raise OSError("Memory store path must stay within the effective Avibe home") from error
        if self.path == self._effective_home:
            raise OSError("Memory store path must name a database below the effective Avibe home")

    def _compact_terminal_tombstones_in_connection(
        self,
        conn: sqlite3.Connection,
        reference: datetime,
    ) -> int:
        cutoff = _iso_from_datetime(reference - TERMINAL_TOMBSTONE_RETENTION)
        prunable_session_refs = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT provider_session_ref
                FROM memory_capture_queue
                WHERE state IN ('delivered', 'dead')
                  AND completed_at IS NOT NULL
                  AND completed_at < ?
                """,
                (cutoff,),
            ).fetchall()
        }
        removed = conn.execute(
            """
            DELETE FROM memory_capture_queue
            WHERE state IN ('delivered', 'dead')
              AND completed_at IS NOT NULL
              AND completed_at < ?
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
            overflow_rows = conn.execute(
                """
                SELECT provider_session_ref FROM memory_capture_queue
                WHERE state IN ('delivered', 'dead')
                ORDER BY completed_at, source_message_digest
                LIMIT ?
                """,
                (overflow,),
            ).fetchall()
            prunable_session_refs.update(str(row[0]) for row in overflow_rows)
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
        if prunable_session_refs:
            conn.executemany(
                """
                DELETE FROM memory_session_flush_state AS session
                WHERE provider_session_ref = ?
                  AND state = 'idle'
                  AND unflushed_count = 0
                  AND first_unflushed_at IS NULL
                  AND due_at IS NULL
                  AND next_attempt_at IS NULL
                  AND target_generation IS NULL
                  AND fence_token IS NULL
                  AND submission_started_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_capture_queue AS capture
                      WHERE capture.provider_session_ref = session.provider_session_ref
                  )
                """,
                ((session_ref,) for session_ref in prunable_session_refs),
            )
        return int(removed)


def _meta_from_row(row: sqlite3.Row) -> MemoryMeta:
    error = _closed_error_or(row["last_error"], "memory_store_unavailable") if row["last_error"] is not None else None
    return MemoryMeta(
        epoch=int(row["epoch"]),
        clear_in_progress=bool(row["clear_in_progress"]),
        scope_key=bytes(row["scope_key"]),
        provider_root_id=str(row["provider_root_id"]),
        last_provider_timestamp_ms=int(row["last_provider_timestamp_ms"]),
        missed_count=int(row["missed_count"]),
        last_success_at=str(row["last_success_at"]) if row["last_success_at"] is not None else None,
        last_error=error,
        last_error_at=(
            str(row["last_error_at"])
            if row["last_error_at"] is not None
            else None
        ),
        processing_fault_kind=(
            str(row["processing_fault_kind"])
            if row["processing_fault_kind"] in {"credential", "engine"}
            else None
        ),
        processing_fault_since=(
            str(row["processing_fault_since"])
            if row["processing_fault_since"] is not None
            else None
        ),
        processing_alert_active=bool(row["processing_alert_active"]),
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
        provider_session_ref=ProviderSessionRef.deserialize(
            str(row["provider_session_ref"])
        ),
        generation=int(row["generation"]),
        principal_id=str(row["principal_id"]),
        project_ref=str(row["project_ref"]),
        provenance=str(row["provenance"]),
        payload_text=str(row["payload_text"]) if row["payload_text"] is not None else None,
        payload_attachments=(
            str(row["payload_attachments"])
            if row["payload_attachments"] is not None
            else None
        ),
        attachment_bundle_id=(
            str(row["attachment_bundle_id"])
            if row["attachment_bundle_id"] is not None
            else None
        ),
        occurred_at_ms=int(row["occurred_at_ms"]),
        provider_timestamp_ms=int(row["provider_timestamp_ms"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        next_retry_at=str(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_at=str(row["lease_at"]) if row["lease_at"] is not None else None,
        lease_token=int(row["lease_token"]),
        last_error=last_error,
        created_at=str(row["created_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        add_request_id=str(row["add_request_id"]) if row["add_request_id"] is not None else None,
        add_status=(
            str(row["add_status"])
            if row["add_status"] in {"accumulated", "extracted"}
            else None
        ),
    )


def _session_state_from_row(row: sqlite3.Row) -> SessionFlushState:
    return SessionFlushState(
        provider_session_ref=ProviderSessionRef.deserialize(str(row["provider_session_ref"])),
        epoch=int(row["epoch"]),
        open_generation=int(row["open_generation"]),
        target_generation=(
            int(row["target_generation"])
            if row["target_generation"] is not None
            else None
        ),
        state=str(row["state"]),
        first_unflushed_at=(
            str(row["first_unflushed_at"])
            if row["first_unflushed_at"] is not None
            else None
        ),
        last_add_ack_at=(
            str(row["last_add_ack_at"])
            if row["last_add_ack_at"] is not None
            else None
        ),
        confirmed_add_watermark_ms=(
            int(row["confirmed_add_watermark_ms"])
            if row["confirmed_add_watermark_ms"] is not None
            else None
        ),
        unflushed_count=int(row["unflushed_count"]),
        due_at=str(row["due_at"]) if row["due_at"] is not None else None,
        next_attempt_at=(
            str(row["next_attempt_at"])
            if row["next_attempt_at"] is not None
            else None
        ),
        retry_count=int(row["retry_count"]),
        operation_epoch=int(row["operation_epoch"]),
        fence_token=str(row["fence_token"]) if row["fence_token"] is not None else None,
        submission_started_at=(
            str(row["submission_started_at"])
            if row["submission_started_at"] is not None
            else None
        ),
        updated_at=str(row["updated_at"]),
    )


def _flush_lease_from_row(row: sqlite3.Row) -> FlushLease:
    target_generation = row["target_generation"]
    fence_token = row["fence_token"]
    if target_generation is None or fence_token is None:
        raise ValueError("invalid Memory flush authority")
    return FlushLease(
        provider_session_ref=ProviderSessionRef.deserialize(str(row["provider_session_ref"])),
        epoch=int(row["epoch"]),
        generation=int(target_generation),
        operation_epoch=int(row["operation_epoch"]),
        fence_token=str(fence_token),
    )


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _closed_error_or(value: object, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return value if is_memory_error_code(value) else fallback


def _bounded_opaque_text(value: str | None, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _is_bundle_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _keyed_digest(scope_key: bytes, value: str) -> str:
    return hmac.new(scope_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def derive_principal_id(scope_key: bytes, user_key: str) -> str:
    """Derive one stable provider-safe principal without retaining the user key."""

    if not isinstance(scope_key, bytes) or len(scope_key) < 16:
        raise ValueError("invalid Memory scope key")
    if not isinstance(user_key, str) or not user_key or user_key != user_key.strip():
        raise ValueError("invalid Memory user key")
    digest = hmac.new(scope_key, user_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"u-{digest[:32]}"


def derive_project_id(scope_key: bytes, workdir: str) -> str:
    """Derive one stable provider-safe project without retaining the cwd."""

    if not isinstance(scope_key, bytes) or len(scope_key) < 16:
        raise ValueError("invalid Memory scope key")
    if (
        not isinstance(workdir, str)
        or not workdir
        or workdir != workdir.strip()
        or not os.path.isabs(workdir)
        or os.path.abspath(os.path.expanduser(workdir)) != workdir
    ):
        raise ValueError("invalid Memory workdir")
    digest = hmac.new(scope_key, workdir.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"p-{digest[:32]}"


def is_principal_id(value: object) -> bool:
    """Return whether a value has the exact opaque Memory principal shape."""

    return (
        isinstance(value, str)
        and len(value) == 34
        and value.startswith("u-")
        and all(character in "0123456789abcdef" for character in value[2:])
    )


def is_project_id(value: object) -> bool:
    """Return whether a value has the exact opaque Memory project shape."""

    return (
        isinstance(value, str)
        and len(value) == 34
        and value.startswith("p-")
        and all(character in "0123456789abcdef" for character in value[2:])
    )


def _provider_session_ref(
    scope_key: bytes,
    principal_id: str,
    project_ref: str,
    session_id: str,
    epoch: int,
) -> str:
    return f"src--{_keyed_digest(scope_key, f'{principal_id}:{project_ref}:{session_id}')}--e{epoch}"
