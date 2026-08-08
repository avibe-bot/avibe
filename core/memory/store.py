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
    FlushPreSubmission,
    FlushRejected,
    FlushResult,
    FlushSucceeded,
    FlushUnknown,
)
from core.memory.types import (
    MemoryErrorCode,
    MemoryFlushToken,
    MemoryFailureLogEntry,
    MemorySessionState,
    MemorySettlementRecord,
    ProviderSessionRef,
    is_memory_error_code,
)


MEMORY_STORE_FILENAME = "memory.sqlite"
MEMORY_STORE_DIRNAME = "memory"
MAX_NONTERMINAL_QUEUE_ROWS = 500
MAX_MESSAGE_ATTEMPTS = 3
MAX_FLUSH_RETRY_ATTEMPTS = 3
FLUSH_RETRY_BACKOFF_SECONDS = (30, 120)
TERMINAL_TOMBSTONE_LIMIT = 100_000
TERMINAL_TOMBSTONE_RETENTION = timedelta(days=90)


def memory_store_path() -> Path:
    """Return the dedicated Memory database under the effective Avibe state root."""

    return paths.get_state_dir() / MEMORY_STORE_DIRNAME / MEMORY_STORE_FILENAME


def utc_now_iso() -> str:
    """Return a lexically sortable UTC instant with millisecond precision."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    principal_id: str
    project_ref: str
    provenance: Literal["user_input", "agent"]
    payload_text: str | None
    occurred_at_ms: int
    provider_timestamp_ms: int
    flush_generation: int
    state: Literal["pending", "processing", "delivered", "dead"]
    attempts: int
    next_retry_at: str | None
    lease_owner: str | None
    lease_at: str | None
    last_error: MemoryErrorCode | None
    created_at: str
    completed_at: str | None
    payload_attachments: str | None = None
    add_request_id: str | None = None
    flush_observation: Literal["not_attempted", "in_flight", "succeeded", "rejected", "unknown"] | None = None
    flush_status: Literal["extracted", "no_extraction"] | None = None
    flush_error_code: str | None = None
    flush_request_id: str | None = None
    flush_observed_at: str | None = None


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
    outcome: Literal[
        "accepted",
        "duplicate",
        "queue_full",
        "clearing",
        "timestamp_invalid",
        "manual_required",
    ]
    row: QueueRow | None = None


@dataclass(frozen=True)
class MessageFailureResult:
    state: Literal["pending", "dead"] | None
    attempts: int | None


@dataclass(frozen=True)
class Delivered:
    """The provider accepted the row; scrub the payload and keep the receipt."""

    add_request_id: str | None = None
    add_status: Literal["accumulated", "extracted"] | None = None


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
DeliveryOutcome = Delivered | AmbiguousAdd | SystemOutage | MessageFailure


@dataclass(frozen=True)
class SettleResult:
    """What one settle transition did to the claimed row."""

    #: False when the fenced update matched no row — a lost or stolen lease.
    settled: bool
    state: Literal["delivered", "pending", "dead", "manual_required"] | None = None
    #: Attempts consumed so far; only a MessageFailure spends one.
    attempts: int | None = None


@dataclass(frozen=True)
class BootRecovery:
    """What one boot recovery found, in the order the store had to look."""

    reclaimed: int
    interrupted_flushes: int
    not_attempted_sessions: tuple[ProviderSessionRef, ...]


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
            manual_fence = conn.execute(
                """
                SELECT 1 FROM memory_session_flush_state
                WHERE provider_session_ref = ?
                  AND flush_state = 'manual_required'
                """,
                (provider_session_ref.serialize(),),
            ).fetchone()
            if manual_fence is not None:
                self._record_capture_skip_in_connection(
                    conn,
                    "memory_provider_response_invalid",
                    now,
                )
                return EnqueueResult(outcome="manual_required")

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

            session_state = self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
                first_unflushed_at=now,
            )
            flush_generation = session_state.generation + int(
                session_state.flush_state in {"due", "in_flight"}
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
                    source_message_digest, epoch, session_id, provider_session_ref,
                    principal_id,
                    project_ref, provenance, payload_text,
                    payload_attachments,
                    occurred_at_ms, provider_timestamp_ms, flush_generation,
                    state, attempts,
                    next_retry_at, lease_owner, lease_at, last_error,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    source_message_digest,
                    meta.epoch,
                    session_id_ref,
                    provider_session_ref.serialize(),
                    principal_id,
                    project_ref,
                    provenance,
                    payload_text,
                    payload_attachments,
                    occurred_at_ms,
                    provider_timestamp_ms,
                    flush_generation,
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
                    principal_id=principal_id,
                    project_ref=project_ref,
                    provenance=provenance,
                    payload_text=payload_text,
                    occurred_at_ms=occurred_at_ms,
                    provider_timestamp_ms=provider_timestamp_ms,
                    flush_generation=flush_generation,
                    state="pending",
                    attempts=0,
                    next_retry_at=None,
                    lease_owner=None,
                    lease_at=None,
                    last_error=None,
                    created_at=now,
                    completed_at=None,
                    payload_attachments=payload_attachments,
                ),
            )

    def ensure_session_flush_state(
        self,
        provider_session_ref: ProviderSessionRef,
        *,
        first_unflushed_at: str | None = None,
    ) -> MemorySessionState:
        """Create coordinator scaffolding without changing flush policy."""

        now = utc_now_iso()
        with self._transaction() as conn:
            return self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
                first_unflushed_at=first_unflushed_at,
            )

    def get_session_flush_state(
        self,
        provider_session_ref: ProviderSessionRef,
    ) -> MemorySessionState | None:
        """Read one canonical session's durable coordination state."""

        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_session_flush_state WHERE provider_session_ref = ?",
                (provider_session_ref.serialize(),),
            ).fetchone()
        return _session_state_from_row(row) if row is not None else None

    def has_manual_required_fence(self) -> bool:
        """Return whether the active epoch contains a terminal manual fence."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return False
            row = conn.execute(
                """
                SELECT 1 FROM memory_session_flush_state
                WHERE epoch = ? AND flush_state = 'manual_required'
                LIMIT 1
                """,
                (meta.epoch,),
            ).fetchone()
        return row is not None

    def list_session_flush_states(
        self,
        *,
        epoch: int | None = None,
    ) -> tuple[MemorySessionState, ...]:
        """List durable session state in deterministic order."""

        with self._connection() as conn:
            if epoch is None:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_session_flush_state
                    ORDER BY epoch, provider_session_ref
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_session_flush_state
                    WHERE epoch = ?
                    ORDER BY provider_session_ref
                    """,
                    (epoch,),
                ).fetchall()
        return tuple(_session_state_from_row(row) for row in rows)

    def list_due_flush_sessions(self, *, now: str) -> tuple[ProviderSessionRef, ...]:
        """Return due sessions whose rejected generation can be retried now."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return ()
            rows = conn.execute(
                """
                SELECT s.provider_session_ref
                FROM memory_session_flush_state AS s
                WHERE s.epoch = ?
                  AND s.flush_state = 'due'
                  AND (s.due_at IS NULL OR s.due_at <= ?)
                  AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= ?)
                  AND EXISTS (
                      SELECT 1
                      FROM memory_capture_queue AS q
                      WHERE q.epoch = s.epoch
                        AND q.provider_session_ref = s.provider_session_ref
                        AND q.flush_generation = s.generation
                        AND q.state = 'delivered'
                        AND q.flush_observation = 'rejected'
                  )
                ORDER BY COALESCE(s.next_attempt_at, s.due_at, s.updated_at),
                         s.provider_session_ref
                """,
                (meta.epoch, now, now),
            ).fetchall()
        return tuple(ProviderSessionRef.deserialize(str(row["provider_session_ref"])) for row in rows)

    def list_flush_settlements(
        self,
        provider_session_ref: ProviderSessionRef | None = None,
        *,
        generation: int | None = None,
    ) -> tuple[MemorySettlementRecord, ...]:
        """Read append-only settlement evidence without projecting it."""

        clauses: list[str] = []
        values: list[object] = []
        if provider_session_ref is not None:
            clauses.append("provider_session_ref = ?")
            values.append(provider_session_ref.serialize())
        if generation is not None:
            clauses.append("generation = ?")
            values.append(generation)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_flush_settlements
                {where}
                ORDER BY observed_at, settlement_id
                """,
                tuple(values),
            ).fetchall()
        return tuple(_settlement_from_row(row) for row in rows)

    def record_settlement(self, record: MemorySettlementRecord) -> bool:
        """Persist one idempotent settlement record without projecting state."""

        with self._transaction() as conn:
            self._ensure_session_state_in_connection(
                conn,
                record.provider_session_ref,
                now=record.observed_at,
            )
            return self._record_settlement_in_connection(conn, record)

    def claim_due(self, *, lease_owner: str, now: str) -> QueueRow | None:
        """Fence one due pending row for a worker without holding a provider call transaction."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or meta.clear_in_progress:
                return None
            row = conn.execute(
                """
                SELECT q.* FROM memory_capture_queue AS q
                WHERE q.epoch = ?
                  AND q.state = 'pending'
                  AND (q.next_retry_at IS NULL OR q.next_retry_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memory_session_flush_state AS s
                      WHERE s.provider_session_ref = q.provider_session_ref
                        AND s.flush_state IN ('due', 'in_flight', 'manual_required')
                  )
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
            settled = self._mark_delivered(
                row,
                lease_owner=lease_owner,
                now=now_iso,
                add_request_id=outcome.add_request_id,
                add_status=outcome.add_status,
            )
            return SettleResult(settled=settled, state="delivered" if settled else None)
        if isinstance(outcome, AmbiguousAdd):
            settled = self._settle_ambiguous_add(
                row,
                lease_owner=lease_owner,
                now=now_iso,
                add_request_id=outcome.add_request_id,
                error=outcome.error,
            )
            return SettleResult(settled=settled, state="manual_required" if settled else None)
        if isinstance(outcome, SystemOutage):
            settled = self._return_system_failure(
                row,
                lease_owner=lease_owner,
                error=outcome.error,
                now=now_iso,
            )
            return SettleResult(settled=settled, state="pending" if settled else None)
        failure = self._record_message_failure(
            row,
            lease_owner=lease_owner,
            error=outcome.error,
            retryable=outcome.retryable,
            now=now,
        )
        return SettleResult(
            settled=failure.state is not None,
            state=failure.state,
            attempts=failure.attempts,
        )

    def recover_after_boot(self, *, lease_owner: str, clock: Callable[[], datetime]) -> BootRecovery:
        """Return the queue to a claimable state after an unclean shutdown.

        The three steps are ordered: stale leases must be reclaimed before
        interrupted flushes are resolved, and sessions still awaiting a first
        flush can only be listed once both have settled. That ordering is an
        invariant of this method, not something a caller has to reproduce.

        `clock` rather than an instant for the same reason. Reclamation can
        block for seconds on SQLite contention, and the timestamp it precedes is
        what dates the interrupted flushes. Sampling before reclamation would
        backdate them by however long the contention lasted, which can reorder
        "most recent flush"; sampling here keeps that impossible to get wrong
        from a call site.
        """

        reclaimed = self._reclaim_processing(lease_owner=lease_owner)
        now_iso = _iso_from_datetime(clock())
        interrupted = self._recover_in_flight_flushes(now=now_iso)
        return BootRecovery(
            reclaimed=reclaimed,
            interrupted_flushes=interrupted,
            not_attempted_sessions=self._list_not_attempted_sessions(),
        )

    def _mark_delivered(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        now: str,
        add_request_id: str | None = None,
        add_status: Literal["accumulated", "extracted"] | None = None,
    ) -> bool:
        """Finalize a fenced provider success and scrub the source payload."""

        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'delivered', payload_text = NULL, payload_attachments = NULL,
                    next_retry_at = NULL,
                    lease_owner = NULL, lease_at = NULL, last_error = NULL,
                    completed_at = ?, add_request_id = ?,
                    flush_observation = ?, flush_status = ?,
                    flush_error_code = NULL, flush_request_id = NULL,
                    flush_observed_at = ?
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ?
                """,
                (
                    now,
                    _bounded_opaque_text(add_request_id),
                    "succeeded" if add_status == "extracted" else "not_attempted",
                    "extracted" if add_status == "extracted" else None,
                    now if add_status == "extracted" else None,
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                ),
            )
            if result.rowcount != 1:
                return False
            provider_session_ref = row.provider_session_ref
            state = self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
            )
            watermark_after = state.watermark
            if add_status == "extracted":
                watermark_after = max(watermark_after, row.provider_timestamp_ms)
                operation_id = f"add-{row.source_message_digest}"
                self._record_settlement_in_connection(
                    conn,
                    MemorySettlementRecord(
                        provider_session_ref=provider_session_ref,
                        generation=row.flush_generation,
                        fence_epoch=state.fence_epoch,
                        operation_id=operation_id,
                        operation_kind="add",
                        outcome="succeeded",
                        observed_at=now,
                        last_known_state="delivered",
                        last_observed_outcome="succeeded",
                        request_id=_bounded_opaque_text(add_request_id),
                        watermark_before=state.watermark,
                        watermark_after=watermark_after,
                        confirmed_watermark_ms=watermark_after,
                        flush_state="settled",
                        source="add",
                    ),
                )
                if state.flush_state != "in_flight":
                    remaining = conn.execute(
                        """
                        SELECT MIN(created_at) AS first_unflushed_at
                        FROM memory_capture_queue
                        WHERE epoch = ? AND provider_session_ref = ?
                          AND source_message_digest != ?
                          AND (
                              state IN ('pending', 'processing')
                              OR (state = 'delivered' AND flush_observation = 'not_attempted')
                          )
                        """,
                        (row.epoch, provider_session_ref.serialize(), row.source_message_digest),
                    ).fetchone()
                    first_unflushed_at = (
                        str(remaining["first_unflushed_at"])
                        if remaining["first_unflushed_at"] is not None
                        else None
                    )
                    conn.execute(
                        """
                        UPDATE memory_session_flush_state
                        SET generation = MAX(generation, ? + 1), first_unflushed_at = ?,
                            last_add_ack_at = ?, watermark = ?,
                            due_at = NULL, next_attempt_at = NULL,
                            flush_state = 'not_due', fence_operation_id = NULL,
                            fence_owner = NULL, fence_acquired_at = NULL, updated_at = ?
                        WHERE provider_session_ref = ?
                        """,
                        (
                            row.flush_generation,
                            first_unflushed_at,
                            now,
                            watermark_after,
                            now,
                            provider_session_ref.serialize(),
                        ),
                    )
            else:
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET flush_generation = ?
                    WHERE source_message_digest = ? AND epoch = ?
                      AND state = 'delivered'
                    """,
                    (state.generation, row.source_message_digest, row.epoch),
                )
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET last_add_ack_at = ?, watermark = ?, updated_at = ?
                    WHERE provider_session_ref = ?
                    """,
                    (now, watermark_after, now, provider_session_ref.serialize()),
                )
            if add_status == "extracted":
                conn.execute(
                    """
                    UPDATE memory_meta
                    SET last_success_at = ?,
                        last_error = CASE
                            WHEN EXISTS (
                                SELECT 1 FROM memory_session_flush_state
                                WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                                  AND flush_state = 'manual_required'
                            ) THEN last_error
                            WHEN last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                                THEN NULL
                            WHEN last_error = 'memory_processing_failed'
                                 AND processing_fault_since IS NULL
                                THEN NULL
                            ELSE last_error
                        END,
                        last_error_at = CASE
                            WHEN EXISTS (
                                SELECT 1 FROM memory_session_flush_state
                                WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                                  AND flush_state = 'manual_required'
                            ) THEN last_error_at
                            WHEN last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                                THEN NULL
                            WHEN last_error = 'memory_processing_failed'
                                 AND processing_fault_since IS NULL
                                THEN NULL
                            ELSE last_error_at
                        END,
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (now, now),
                )
            self._compact_terminal_tombstones_in_connection(conn, _datetime_from_iso(now))
            return True

    def _settle_ambiguous_add(
        self,
        row: QueueRow,
        *,
        lease_owner: str,
        now: str,
        add_request_id: str | None,
        error: MemoryErrorCode,
    ) -> bool:
        """Retain an uncertain add and fence its session against replay."""

        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET state = 'pending', next_retry_at = NULL,
                    lease_owner = NULL, lease_at = NULL,
                    last_error = ?,
                    add_request_id = ?
                WHERE source_message_digest = ? AND epoch = ?
                  AND state = 'processing' AND lease_owner = ?
                """,
                (
                    _closed_error_or(error, "memory_provider_response_invalid"),
                    _bounded_opaque_text(add_request_id),
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                ),
            )
            if result.rowcount != 1:
                return False
            provider_session_ref = row.provider_session_ref
            state = self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
            )
            fence_epoch = state.fence_epoch + 1
            self._mark_manual_required_in_connection(
                conn,
                MemorySettlementRecord(
                    provider_session_ref=provider_session_ref,
                    generation=state.generation,
                    fence_epoch=fence_epoch,
                    operation_id=f"manual-add-{row.source_message_digest}",
                    operation_kind="add",
                    outcome="manual_required",
                    observed_at=now,
                    last_known_state="pending",
                    last_observed_outcome="manual_required",
                    request_id=_bounded_opaque_text(add_request_id),
                    error_code=_closed_error_or(error, "memory_provider_response_invalid"),
                    flush_state="manual_required",
                    source="add",
                ),
                now=now,
            )
            self._set_last_error_in_connection(
                conn,
                _closed_error_or(error, "memory_provider_response_invalid"),
                now,
            )
            return True

    def _mark_manual_required_in_connection(
        self,
        conn: sqlite3.Connection,
        record: MemorySettlementRecord,
        *,
        now: str,
    ) -> None:
        """Fence one session and append its durable ambiguous-outcome verdict."""

        key = record.provider_session_ref.serialize()
        conn.execute(
            """
            UPDATE memory_session_flush_state
                SET fence_epoch = ?, fence_operation_id = ?, fence_owner = 'manual-required',
                    fence_acquired_at = ?, flush_state = 'manual_required',
                    due_at = NULL, next_attempt_at = NULL, flush_retry_count = 0,
                    updated_at = ?
            WHERE provider_session_ref = ?
            """,
            (record.fence_epoch, record.operation_id, now, now, key),
        )
        self._record_settlement_in_connection(conn, record)

    def mark_flush_in_flight(
        self,
        provider_session_ref: ProviderSessionRef,
    ) -> MemoryFlushToken | None:
        """Freeze one canonical session and return its exact flush token."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None or provider_session_ref.epoch != meta.epoch:
                return None
            state = self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
            )
            if state.flush_state in {"in_flight", "manual_required"}:
                return None
            if state.flush_state == "due" and state.next_attempt_at is not None and state.next_attempt_at > now:
                return None
            if conn.execute(
                """
                SELECT 1 FROM memory_capture_queue
                WHERE epoch = ? AND provider_session_ref = ? AND state = 'processing'
                LIMIT 1
                """,
                (meta.epoch, provider_session_ref.serialize()),
            ).fetchone() is not None:
                return None
            flush_observation = "rejected" if state.flush_state == "due" else "not_attempted"
            candidate = conn.execute(
                """
                SELECT 1 FROM memory_capture_queue
                WHERE epoch = ? AND provider_session_ref = ?
                  AND flush_generation = ?
                  AND state = 'delivered' AND flush_observation = ?
                LIMIT 1
                """,
                (meta.epoch, provider_session_ref.serialize(), state.generation, flush_observation),
            ).fetchone()
            if candidate is None:
                return None
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET flush_observation = 'in_flight', flush_status = NULL,
                    flush_error_code = NULL, flush_request_id = NULL,
                    flush_observed_at = NULL
                WHERE epoch = ? AND provider_session_ref = ?
                  AND flush_generation = ?
                  AND state = 'delivered'
                  AND flush_observation = ?
                """,
                (meta.epoch, provider_session_ref.serialize(), state.generation, flush_observation),
            )
            if not result.rowcount:
                return None
            conn.execute(
                """
                UPDATE memory_capture_queue
                SET flush_generation = ?
                WHERE epoch = ? AND provider_session_ref = ?
                  AND state = 'pending' AND flush_generation <= ?
                """,
                (
                    state.generation + 1,
                    meta.epoch,
                    provider_session_ref.serialize(),
                    state.generation,
                ),
            )
            fence_epoch = state.fence_epoch + 1
            operation_id = _flush_operation_id(
                provider_session_ref,
                state.generation,
                fence_epoch,
            )
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET fence_epoch = ?, fence_operation_id = ?, fence_owner = 'flush',
                    fence_acquired_at = ?, flush_state = 'in_flight',
                    due_at = NULL, next_attempt_at = NULL, updated_at = ?
                WHERE provider_session_ref = ?
                """,
                (fence_epoch, operation_id, now, now, provider_session_ref.serialize()),
            )
            return MemoryFlushToken(
                provider_session_ref=provider_session_ref,
                generation=state.generation,
                fence_epoch=fence_epoch,
                operation_id=operation_id,
            )

    def record_flush_verdict(
        self,
        token: MemoryFlushToken,
        result: FlushResult,
        *,
        now: str,
    ) -> int:
        """Persist one closed provider verdict for exactly its in-flight group."""

        valid_success = isinstance(result, FlushSucceeded) and result.status in {
            "extracted",
            "no_extraction",
        }
        retryable_rejection = (
            isinstance(result, FlushRejected) and result.retryable
        ) or isinstance(result, FlushPreSubmission)
        if valid_success:
            observation = "succeeded"
            status = result.status
            error_code = None
            request_id = result.request_id
        elif isinstance(result, FlushRejected):
            observation = "rejected"
            status = None
            error_code = result.error_code
            request_id = result.request_id
        elif isinstance(result, FlushPreSubmission):
            observation = "rejected"
            status = None
            error_code = (
                "memory_provider_timeout"
                if result.reason == "timeout"
                else "memory_sidecar_unavailable"
            )
            request_id = None
        elif isinstance(result, FlushSucceeded):
            observation = "unknown"
            status = None
            error_code = None
            request_id = result.request_id
        elif isinstance(result, FlushUnknown):
            observation = "unknown"
            status = None
            error_code = None
            request_id = None
        else:
            raise TypeError("unsupported flush result")

        with self._transaction() as conn:
            state_row = conn.execute(
                """
                SELECT * FROM memory_session_flush_state
                WHERE provider_session_ref = ? AND generation = ?
                  AND fence_epoch = ? AND fence_operation_id = ?
                  AND flush_state = 'in_flight'
                """,
                (
                    token.provider_session_ref.serialize(),
                    token.generation,
                    token.fence_epoch,
                    token.operation_id,
                ),
            ).fetchone()
            if state_row is None:
                return 0
            provider_session_ref = token.provider_session_ref
            state = _session_state_from_row(state_row)
            candidate = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE epoch = ? AND provider_session_ref = ?
                  AND flush_generation = ?
                  AND state = 'delivered' AND flush_observation = 'in_flight'
                ORDER BY completed_at, source_message_digest
                LIMIT 1
                """,
                (provider_session_ref.epoch, provider_session_ref.serialize(), token.generation),
            ).fetchone()
            if candidate is None:
                return 0
            if not valid_success and not isinstance(
                result, (FlushRejected, FlushPreSubmission)
            ):
                observation = "unknown"
                status = None
            updated = conn.execute(
                """
                UPDATE memory_capture_queue
                SET flush_observation = ?, flush_status = ?, flush_error_code = ?,
                    flush_request_id = ?, flush_observed_at = ?
                WHERE epoch = ? AND provider_session_ref = ?
                  AND flush_generation = ?
                  AND state = 'delivered'
                  AND flush_observation = 'in_flight'
                """,
                (
                    observation,
                    status,
                    _bounded_opaque_text(error_code),
                    _bounded_opaque_text(request_id),
                    now,
                    provider_session_ref.epoch,
                    provider_session_ref.serialize(),
                    token.generation,
                ),
            )
            if updated.rowcount:
                group = conn.execute(
                    """
                    SELECT MAX(provider_timestamp_ms) AS watermark
                    FROM memory_capture_queue
                    WHERE epoch = ? AND provider_session_ref = ?
                      AND flush_generation = ?
                      AND state = 'delivered'
                      AND flush_observation = ?
                    """,
                    (
                        provider_session_ref.epoch,
                        provider_session_ref.serialize(),
                        token.generation,
                        observation,
                    ),
                ).fetchone()
                watermark = int(group["watermark"] or 0)
                flush_retry_count = (
                    state.flush_retry_count + 1 if retryable_rejection else 0
                )
                retry_exhausted = (
                    retryable_rejection
                    and flush_retry_count >= MAX_FLUSH_RETRY_ATTEMPTS
                )
                settlement_outcome: Literal[
                    "succeeded", "rejected", "unknown", "manual_required"
                ] = (
                    "succeeded"
                    if observation == "succeeded"
                    else "rejected"
                    if observation == "rejected" and not retry_exhausted
                    else "manual_required"
                )
                settlement = MemorySettlementRecord(
                    provider_session_ref=provider_session_ref,
                    generation=state.generation,
                    fence_epoch=state.fence_epoch,
                    operation_id=token.operation_id,
                    operation_kind="flush",
                    outcome=settlement_outcome,
                    observed_at=now,
                    last_known_state="delivered",
                    last_observed_outcome=(
                        observation
                        if observation in {"succeeded", "rejected", "unknown"}
                        else "unknown"
                    ),
                    request_id=_bounded_opaque_text(request_id),
                    error_code=_bounded_opaque_text(error_code),
                    watermark_before=state.watermark,
                    watermark_after=(
                        watermark
                        if observation in {"succeeded", "unknown"}
                        else state.watermark
                    ),
                    confirmed_watermark_ms=watermark if observation == "succeeded" else None,
                    flush_state=(
                        "settled"
                        if observation == "succeeded"
                        else "due"
                        if observation == "rejected" and retryable_rejection and not retry_exhausted
                        else "not_due"
                        if observation == "rejected"
                        else "manual_required"
                    ),
                    source="flush",
                    settled_at=now,
                )
                if settlement_outcome == "manual_required":
                    self._mark_manual_required_in_connection(
                        conn,
                        settlement,
                        now=now,
                    )
                    if retry_exhausted:
                        self._set_last_error_in_connection(
                            conn,
                            "memory_processing_failed",
                            now,
                        )
                else:
                    self._record_settlement_in_connection(conn, settlement)
                    remaining = conn.execute(
                        """
                        SELECT MIN(created_at) AS first_unflushed_at
                        FROM memory_capture_queue
                        WHERE epoch = ? AND provider_session_ref = ?
                          AND (
                              state IN ('pending', 'processing')
                              OR (state = 'delivered' AND flush_observation = 'not_attempted')
                          )
                        """,
                        (provider_session_ref.epoch, provider_session_ref.serialize()),
                    ).fetchone()
                    first_unflushed_at = (
                        str(remaining["first_unflushed_at"])
                        if remaining["first_unflushed_at"] is not None
                        else None
                    )
                    conn.execute(
                        """
                        UPDATE memory_session_flush_state
                        SET generation = generation + CASE WHEN ? = 'succeeded' THEN 1 ELSE 0 END,
                            first_unflushed_at = CASE
                                WHEN ? = 'succeeded' THEN ?
                                ELSE first_unflushed_at
                            END,
                            fence_epoch = ?, fence_operation_id = NULL, fence_owner = NULL,
                            fence_acquired_at = NULL, flush_state = ?,
                            due_at = ?, next_attempt_at = ?, flush_retry_count = ?,
                            watermark = MAX(watermark, ?),
                            updated_at = ?
                        WHERE provider_session_ref = ?
                        """,
                        (
                            settlement_outcome,
                            settlement_outcome,
                            first_unflushed_at,
                            state.fence_epoch,
                            "due"
                            if settlement_outcome == "rejected" and retryable_rejection
                            else "not_due"
                            if settlement_outcome in {"succeeded", "rejected"}
                            else "manual_required",
                            now
                            if settlement_outcome == "rejected" and retryable_rejection
                            else None,
                            _next_flush_retry_at(now, flush_retry_count)
                            if settlement_outcome == "rejected" and retryable_rejection
                            else None,
                            flush_retry_count,
                            watermark if settlement_outcome == "succeeded" else state.watermark,
                            now,
                            provider_session_ref.serialize(),
                        ),
                    )
                conn.execute(
                    """
                    UPDATE memory_meta
                    SET last_success_at = CASE
                            WHEN ? = 'succeeded' THEN ?
                            ELSE last_success_at
                        END,
                        last_error = CASE
                            WHEN EXISTS (
                                SELECT 1 FROM memory_session_flush_state
                                WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                                  AND flush_state = 'manual_required'
                            ) THEN last_error
                            WHEN last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                                THEN NULL
                            WHEN last_error = 'memory_processing_failed'
                                 AND processing_fault_since IS NULL
                                THEN NULL
                            ELSE last_error
                        END,
                        last_error_at = CASE
                            WHEN EXISTS (
                                SELECT 1 FROM memory_session_flush_state
                                WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                                  AND flush_state = 'manual_required'
                            ) THEN last_error_at
                            WHEN last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                                THEN NULL
                            WHEN last_error = 'memory_processing_failed'
                                 AND processing_fault_since IS NULL
                                THEN NULL
                            ELSE last_error_at
                        END,
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (observation, now, now),
                )
            return int(updated.rowcount)

    def _recover_in_flight_flushes(self, *, now: str) -> int:
        """Turn activation-interrupted flush attempts into terminal unknowns."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return 0
            candidates = conn.execute(
                """
                SELECT * FROM memory_capture_queue
                WHERE epoch = ? AND state = 'delivered'
                  AND flush_observation = 'in_flight'
                ORDER BY provider_session_ref, source_message_digest
                """,
                (meta.epoch,),
            ).fetchall()
            result = conn.execute(
                """
                UPDATE memory_capture_queue
                SET flush_observation = 'unknown', flush_status = NULL,
                    flush_error_code = NULL, flush_request_id = NULL,
                    flush_observed_at = ?
                WHERE epoch = ? AND state = 'delivered'
                  AND flush_observation = 'in_flight'
                """,
                (now, meta.epoch),
            )
            grouped: dict[tuple[str, int], list[sqlite3.Row]] = {}
            for candidate in candidates:
                key = (_provider_ref_from_row(candidate).serialize(), int(candidate["flush_generation"]))
                grouped.setdefault(key, []).append(candidate)
            for (key, generation), group in grouped.items():
                provider_session_ref = ProviderSessionRef.deserialize(key)
                state = self._ensure_session_state_in_connection(
                    conn,
                    provider_session_ref,
                    now=now,
                )
                fence_epoch = state.fence_epoch
                watermark = max(int(row["provider_timestamp_ms"]) for row in group)
                self._mark_manual_required_in_connection(
                    conn,
                    MemorySettlementRecord(
                        provider_session_ref=provider_session_ref,
                        generation=generation,
                        fence_epoch=fence_epoch,
                        operation_id=state.fence_operation_id
                        if state.generation == generation and state.fence_operation_id is not None
                        else _flush_operation_id(provider_session_ref, generation, fence_epoch),
                        operation_kind="flush",
                        outcome="manual_required",
                        observed_at=now,
                        last_known_state="delivered",
                        last_observed_outcome="in_flight",
                        watermark_after=watermark,
                        flush_state="manual_required",
                        source="flush",
                    ),
                    now=now,
                )
            return int(result.rowcount)

    def _list_not_attempted_sessions(self) -> tuple[ProviderSessionRef, ...]:
        """Return active sessions whose acknowledged buffer still needs a flush."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return ()
            rows = conn.execute(
                """
                SELECT provider_session_ref, MIN(completed_at) AS first_completed_at
                FROM memory_capture_queue
                WHERE epoch = ? AND state = 'delivered'
                  AND flush_observation = 'not_attempted'
                GROUP BY provider_session_ref
                ORDER BY first_completed_at, provider_session_ref
                """,
                (meta.epoch,),
            ).fetchall()
        return tuple(ProviderSessionRef.deserialize(str(row["provider_session_ref"])) for row in rows)

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
                  AND state = 'processing' AND lease_owner = ?
                """,
                (error, row.source_message_digest, row.epoch, lease_owner),
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
                        payload_attachments = NULL,
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
                provider_session_ref = _provider_ref_from_row(row)
                state = self._ensure_session_state_in_connection(
                    conn,
                    provider_session_ref,
                    now=now,
                )
                error = "memory_provider_response_invalid"
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET state = 'pending', lease_owner = NULL, lease_at = NULL,
                        next_retry_at = NULL, last_error = ?
                    WHERE source_message_digest = ? AND epoch = ?
                      AND state = 'processing'
                    """,
                    (error, row["source_message_digest"], row["epoch"]),
                )
                self._mark_manual_required_in_connection(
                    conn,
                    MemorySettlementRecord(
                        provider_session_ref=provider_session_ref,
                        generation=state.generation,
                        fence_epoch=state.fence_epoch + 1,
                        operation_id=f"recovered-add-{row['source_message_digest']}",
                        operation_kind="add",
                        outcome="manual_required",
                        observed_at=now,
                        last_known_state="processing",
                        last_observed_outcome="in_flight",
                        error_code=error,
                        flush_state="manual_required",
                        source="add",
                    ),
                    now=now,
                )
                self._set_last_error_in_connection(conn, error, now)
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
                    SUM(CASE WHEN state = 'delivered' AND flush_observation IN
                        ('not_attempted', 'in_flight') THEN 1 ELSE 0 END) AS awaiting_receipt,
                    SUM(CASE WHEN state = 'delivered' AND flush_observation = 'succeeded'
                        THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN state = 'delivered' AND
                        (flush_observation = 'unknown' OR flush_observation IS NULL)
                        THEN 1 ELSE 0 END) AS receipt_unknown,
                    SUM(CASE WHEN state = 'delivered' AND flush_observation = 'rejected'
                        THEN 1 ELSE 0 END) AS distill_failed,
                    COALESCE(SUM(
                        CASE WHEN state IN ('pending', 'processing')
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
                SELECT flush_observation, flush_status, flush_error_code,
                       flush_request_id, flush_observed_at
                FROM memory_capture_queue
                WHERE epoch = ? AND state = 'delivered'
                  AND (flush_observation IN ('succeeded', 'rejected', 'unknown')
                       OR flush_observation IS NULL)
                ORDER BY COALESCE(flush_observed_at, completed_at, created_at) DESC,
                         source_message_digest DESC
                LIMIT 1
                """,
                (meta.epoch,),
            ).fetchone()
        return QueueStats(
            pending=int(row["pending"] or 0),
            processing=int(row["processing"] or 0),
            dead=int(row["dead"] or 0),
            queue_plaintext_bytes=int(row["plaintext_bytes"] or 0),
            awaiting_receipt=int(row["awaiting_receipt"] or 0),
            succeeded=int(row["succeeded"] or 0),
            receipt_unknown=int(row["receipt_unknown"] or 0),
            distill_failed=int(row["distill_failed"] or 0),
            last_flush_observation=(
                (
                    str(latest["flush_observation"])
                    if latest["flush_observation"] is not None
                    else "unknown"
                )
                if latest is not None else None
            ),
            last_flush_status=(
                str(latest["flush_status"])
                if latest is not None and latest["flush_status"] is not None
                else None
            ),
            last_flush_error_code=(
                str(latest["flush_error_code"])
                if latest is not None and latest["flush_error_code"] is not None
                else None
            ),
            last_flush_request_id=(
                str(latest["flush_request_id"])
                if latest is not None and latest["flush_request_id"] is not None
                else None
            ),
            last_flush_at=(
                str(latest["flush_observed_at"])
                if latest is not None and latest["flush_observed_at"] is not None
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
                SELECT kind, occurred_at, error_code, request_id, attempts
                FROM (
                    SELECT
                        'delivery_abandoned' AS kind,
                        COALESCE(completed_at, created_at) AS occurred_at,
                        last_error AS error_code,
                        add_request_id AS request_id,
                        attempts,
                        source_message_digest AS sort_key
                    FROM memory_capture_queue
                    WHERE epoch = ? AND state = 'dead'

                    UNION ALL

                    SELECT
                        CASE
                            WHEN flush_observation = 'rejected' THEN 'distillation_rejected'
                            ELSE 'result_unknown'
                        END AS kind,
                        COALESCE(flush_observed_at, completed_at, created_at) AS occurred_at,
                        flush_error_code AS error_code,
                        flush_request_id AS request_id,
                        MAX(attempts) AS attempts,
                        MIN(source_message_digest) AS sort_key
                    FROM memory_capture_queue
                    WHERE epoch = ? AND state = 'delivered' AND (
                        flush_observation IN ('rejected', 'unknown')
                        OR flush_observation IS NULL
                    )
                    GROUP BY session_id, flush_observation, occurred_at,
                             flush_error_code, flush_request_id
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

    def finish_clear(self) -> MemoryMeta:
        """Delete all queue state and make the advanced epoch available again."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta_in_connection(conn)
            conn.execute("DELETE FROM memory_capture_queue")
            conn.execute("DELETE FROM memory_session_flush_state")
            conn.execute("DELETE FROM memory_flush_settlements")
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
                        WHEN last_error = 'memory_processing_failed'
                             AND NOT EXISTS (
                                 SELECT 1 FROM memory_session_flush_state
                                 WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                                   AND flush_state = 'manual_required'
                             ) THEN NULL
                        ELSE last_error
                    END,
                    last_error_at = CASE
                        WHEN last_error = 'memory_processing_failed'
                             AND NOT EXISTS (
                                 SELECT 1 FROM memory_session_flush_state
                                 WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                                   AND flush_state = 'manual_required'
                             ) THEN NULL
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
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_session_flush_state
                      WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                        AND flush_state = 'manual_required'
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
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_session_flush_state
                      WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                        AND flush_state = 'manual_required'
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
        schema = Path(__file__).with_name("schema.sql")
        with self._connection() as conn:
            conn.executescript(schema.read_text(encoding="utf-8"))

    def _ensure_session_state_in_connection(
        self,
        conn: sqlite3.Connection,
        provider_session_ref: ProviderSessionRef,
        *,
        now: str,
        first_unflushed_at: str | None = None,
    ) -> MemorySessionState:
        key = provider_session_ref.serialize()
        existing = conn.execute(
            "SELECT * FROM memory_session_flush_state WHERE provider_session_ref = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            state = _session_state_from_row(existing)
            if first_unflushed_at is not None and state.first_unflushed_at is None:
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET first_unflushed_at = ?, updated_at = ?
                    WHERE provider_session_ref = ?
                    """,
                    (first_unflushed_at, now, key),
                )
                refreshed = conn.execute(
                    "SELECT * FROM memory_session_flush_state WHERE provider_session_ref = ?",
                    (key,),
                ).fetchone()
                return _session_state_from_row(refreshed)
            return state
        conn.execute(
            """
            INSERT INTO memory_session_flush_state (
                provider_session_ref, principal_id, epoch, project_ref, session_id,
                generation, first_unflushed_at, last_add_ack_at, due_at,
                next_attempt_at, flush_retry_count, flush_state, watermark, fence_epoch,
                fence_operation_id, fence_owner, fence_acquired_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, 0, 'not_due', 0, 0, NULL, NULL, NULL, ?)
            """,
            (
                key,
                provider_session_ref.principal_id,
                provider_session_ref.epoch,
                provider_session_ref.project_ref,
                provider_session_ref.session_id,
                first_unflushed_at,
                now,
            ),
        )
        return MemorySessionState(
            provider_session_ref=provider_session_ref,
            first_unflushed_at=first_unflushed_at,
            flush_retry_count=0,
            updated_at=now,
        )

    def _record_settlement_in_connection(
        self,
        conn: sqlite3.Connection,
        record: MemorySettlementRecord,
    ) -> bool:
        observed = record.observed_at
        settled = record.settled_at or observed
        result = conn.execute(
            """
            INSERT OR IGNORE INTO memory_flush_settlements (
                settlement_id, provider_session_ref, generation, fence_epoch,
                operation_id, operation_kind, outcome, last_known_state,
                last_observed_outcome, request_id, error_code, watermark_before,
                watermark_after, confirmed_watermark_ms, flush_state, source,
                observed_at, settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.settlement_id,
                record.provider_session_ref.serialize(),
                record.generation,
                record.fence_epoch,
                record.operation_id,
                record.operation_kind,
                record.outcome,
                record.last_known_state,
                record.last_observed_outcome,
                _bounded_opaque_text(record.request_id),
                _bounded_opaque_text(record.error_code),
                record.watermark_before,
                record.watermark_after,
                record.confirmed_watermark_ms,
                record.flush_state,
                record.source,
                observed,
                settled,
            ),
        )
        return bool(result.rowcount)

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
                last_error = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM memory_session_flush_state
                        WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                          AND flush_state = 'manual_required'
                    ) THEN last_error
                    ELSE COALESCE(?, last_error)
                END,
                last_error_at = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM memory_session_flush_state
                        WHERE epoch = (SELECT epoch FROM memory_meta WHERE singleton = 1)
                          AND flush_state = 'manual_required'
                    ) THEN last_error_at
                    WHEN ? IS NOT NULL THEN ?
                    ELSE last_error_at
                END,
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
        removed = conn.execute(
            """
            DELETE FROM memory_capture_queue
            WHERE state IN ('delivered', 'dead')
              AND (flush_observation IS NULL OR flush_observation != 'in_flight')
              AND NOT EXISTS (
                  SELECT 1 FROM memory_session_flush_state AS s
                  WHERE s.epoch = memory_capture_queue.epoch
                    AND s.provider_session_ref = memory_capture_queue.provider_session_ref
                    AND s.flush_state = 'due'
                    AND s.generation = memory_capture_queue.flush_generation
              )
              AND COALESCE(flush_observed_at, completed_at) IS NOT NULL
              AND COALESCE(flush_observed_at, completed_at) < ?
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
                    SELECT q.source_message_digest FROM memory_capture_queue AS q
                    WHERE q.state IN ('delivered', 'dead')
                      AND (q.flush_observation IS NULL OR q.flush_observation != 'in_flight')
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_session_flush_state AS s
                          WHERE s.epoch = q.epoch
                            AND s.provider_session_ref = q.provider_session_ref
                            AND s.flush_state = 'due'
                            AND s.generation = q.flush_generation
                      )
                      ORDER BY COALESCE(q.flush_observed_at, q.completed_at), q.source_message_digest
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
        provider_session_ref=_provider_ref_from_row(row),
        principal_id=str(row["principal_id"]),
        project_ref=str(row["project_ref"]),
        provenance=str(row["provenance"]),
        payload_text=str(row["payload_text"]) if row["payload_text"] is not None else None,
        payload_attachments=(
            str(row["payload_attachments"])
            if row["payload_attachments"] is not None
            else None
        ),
        occurred_at_ms=int(row["occurred_at_ms"]),
        provider_timestamp_ms=int(row["provider_timestamp_ms"]),
        flush_generation=int(row["flush_generation"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        next_retry_at=str(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_at=str(row["lease_at"]) if row["lease_at"] is not None else None,
        last_error=last_error,
        created_at=str(row["created_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        add_request_id=str(row["add_request_id"]) if row["add_request_id"] is not None else None,
        flush_observation=(
            str(row["flush_observation"])
            if row["flush_observation"] in {"not_attempted", "in_flight", "succeeded", "rejected", "unknown"}
            else None
        ),
        flush_status=(
            str(row["flush_status"])
            if row["flush_status"] in {"extracted", "no_extraction"}
            else None
        ),
        flush_error_code=(
            str(row["flush_error_code"])
            if row["flush_error_code"] is not None
            else None
        ),
        flush_request_id=(
            str(row["flush_request_id"])
            if row["flush_request_id"] is not None
            else None
        ),
        flush_observed_at=(
            str(row["flush_observed_at"])
            if row["flush_observed_at"] is not None
            else None
        ),
    )


def _provider_ref_from_row(row: sqlite3.Row | dict[str, object]) -> ProviderSessionRef:
    return ProviderSessionRef.deserialize(str(row["provider_session_ref"]))


def _session_state_from_row(row: sqlite3.Row) -> MemorySessionState:
    return MemorySessionState(
        provider_session_ref=ProviderSessionRef.deserialize(str(row["provider_session_ref"])),
        generation=int(row["generation"]),
        first_unflushed_at=(
            str(row["first_unflushed_at"]) if row["first_unflushed_at"] is not None else None
        ),
        last_add_ack_at=(
            str(row["last_add_ack_at"]) if row["last_add_ack_at"] is not None else None
        ),
        due_at=str(row["due_at"]) if row["due_at"] is not None else None,
        next_attempt_at=(
            str(row["next_attempt_at"]) if row["next_attempt_at"] is not None else None
        ),
        flush_retry_count=int(row["flush_retry_count"]),
        flush_state=(
            str(row["flush_state"])
            if row["flush_state"] in {"not_due", "due", "in_flight", "manual_required"}
            else "not_due"
        ),
        watermark=int(row["watermark"]),
        fence_epoch=int(row["fence_epoch"]),
        fence_operation_id=(
            str(row["fence_operation_id"]) if row["fence_operation_id"] is not None else None
        ),
        fence_owner=str(row["fence_owner"]) if row["fence_owner"] is not None else None,
        fence_acquired_at=(
            str(row["fence_acquired_at"]) if row["fence_acquired_at"] is not None else None
        ),
        updated_at=str(row["updated_at"]),
    )


def _settlement_from_row(row: sqlite3.Row) -> MemorySettlementRecord:
    return MemorySettlementRecord(
        provider_session_ref=ProviderSessionRef.deserialize(str(row["provider_session_ref"])),
        generation=int(row["generation"]),
        fence_epoch=int(row["fence_epoch"]),
        operation_id=str(row["operation_id"]),
        operation_kind=str(row["operation_kind"]),
        outcome=str(row["outcome"]),
        observed_at=str(row["observed_at"]),
        last_known_state=(
            str(row["last_known_state"]) if row["last_known_state"] is not None else None
        ),
        last_observed_outcome=(
            str(row["last_observed_outcome"])
            if row["last_observed_outcome"] is not None
            else None
        ),
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        watermark_before=(
            int(row["watermark_before"]) if row["watermark_before"] is not None else None
        ),
        watermark_after=(
            int(row["watermark_after"]) if row["watermark_after"] is not None else None
        ),
        confirmed_watermark_ms=(
            int(row["confirmed_watermark_ms"])
            if row["confirmed_watermark_ms"] is not None
            else None
        ),
        flush_state=(
            str(row["flush_state"]) if row["flush_state"] is not None else None
        ),
        source=str(row["source"]) if row["source"] is not None else None,
        settled_at=str(row["settled_at"]),
        settlement_id=str(row["settlement_id"]),
    )


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _next_flush_retry_at(now: str, retry_count: int) -> str:
    """Return a bounded backoff instant for one retryable flush rejection."""

    index = min(max(retry_count - 1, 0), len(FLUSH_RETRY_BACKOFF_SECONDS) - 1)
    return _iso_from_datetime(
        _datetime_from_iso(now) + timedelta(seconds=FLUSH_RETRY_BACKOFF_SECONDS[index])
    )


def _closed_error_or(value: object, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return value if is_memory_error_code(value) else fallback


def _bounded_opaque_text(value: str | None, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


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


def _flush_operation_id(
    provider_session_ref: ProviderSessionRef,
    generation: int,
    fence_epoch: int,
) -> str:
    digest = hashlib.sha256(
        f"{provider_session_ref.serialize()}:{generation}:{fence_epoch}".encode("utf-8")
    ).hexdigest()
    return f"flush-{digest[:32]}"
