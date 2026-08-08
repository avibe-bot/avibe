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
from urllib.parse import quote

from config import paths
from core.memory.observations import FlushRejected, FlushResult, FlushSucceeded, FlushUnknown
from core.memory.types import (
    MemoryErrorCode,
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
TERMINAL_TOMBSTONE_LIMIT = 100_000
TERMINAL_TOMBSTONE_RETENTION = timedelta(days=90)
MEMORY_SCHEMA_VERSION = 2
MEMORY_LEGACY_SCHEMA_VERSION = 1


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
    principal_id: str
    project_ref: str
    provenance: Literal["user_input", "agent"]
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
    payload_attachments: str | None = None
    add_request_id: str | None = None
    flush_observation: Literal["not_attempted", "in_flight", "succeeded", "rejected", "unknown"] | None = None
    flush_status: Literal["extracted", "no_extraction"] | None = None
    flush_error_code: str | None = None
    flush_request_id: str | None = None
    flush_observed_at: str | None = None
    provider_session_ref: ProviderSessionRef | None = None
    app: str | None = None
    target_generation: int | None = None
    target_watermark_ms: int | None = None


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
    provider_session_ref: ProviderSessionRef | None = None
    target_generation: int | None = None
    target_watermark_ms: int | None = None


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
class SystemOutage:
    """Infrastructure failed, not this row. Release it without spending an attempt."""

    error: MemoryErrorCode


@dataclass(frozen=True)
class MessageFailure:
    """This row failed. Spend one attempt, then retry or scrub it terminally."""

    error: MemoryErrorCode
    retryable: bool = True


#: Every way a claimed row can leave the ``processing`` state.
DeliveryOutcome = Delivered | SystemOutage | MessageFailure


@dataclass(frozen=True)
class SettleResult:
    """What one settle transition did to the claimed row."""

    #: False when the fenced update matched no row — a lost or stolen lease.
    settled: bool
    state: Literal["delivered", "pending", "dead"] | None = None
    #: Attempts consumed so far; only a MessageFailure spends one.
    attempts: int | None = None
    #: True when the provider's add acknowledgement already completed extraction.
    flush_complete: bool = False


@dataclass(frozen=True)
class BootRecovery:
    """What one boot recovery found, in the order the store had to look."""

    reclaimed: int
    interrupted_flushes: int
    not_attempted_sessions: tuple[tuple[str, str], ...]


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

    def get_session_flush_state(
        self,
        provider_session_ref: ProviderSessionRef,
    ) -> MemorySessionState | None:
        """Read the durable coordinator state for one canonical session."""

        key = _provider_session_key(provider_session_ref)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_session_flush_state
                WHERE provider_session_ref = ?
                """,
                (key,),
            ).fetchone()
        return _session_state_from_row(row) if row is not None else None

    # The shorter name is useful to internal coordinators while the explicit
    # name documents that this is per-session state rather than global status.
    get_flush_state = get_session_flush_state

    def ensure_session_flush_state(
        self,
        provider_session_ref: ProviderSessionRef,
        *,
        first_unflushed_at: str | None = None,
    ) -> MemorySessionState:
        """Create coordinator state without changing the active flush policy."""

        now = utc_now_iso()
        with self._transaction() as conn:
            return self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
                first_unflushed_at=first_unflushed_at,
            )

    def list_session_flush_states(
        self,
        *,
        epoch: int | None = None,
    ) -> tuple[MemorySessionState, ...]:
        """List durable session state in deterministic coordinator order."""

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

    def list_flush_settlements(
        self,
        provider_session_ref: ProviderSessionRef | None = None,
        *,
        generation: int | None = None,
    ) -> tuple[MemorySettlementRecord, ...]:
        """Read append-only settlement history without provider payloads."""

        clauses: list[str] = []
        values: list[object] = []
        if provider_session_ref is not None:
            clauses.append("provider_session_ref = ?")
            values.append(_provider_session_key(provider_session_ref))
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
        """Persist one idempotent settlement record and its state projection."""

        with self._transaction() as conn:
            return self._record_settlement_in_connection(conn, record)

    record_flush_settlement = record_settlement

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
        app: str | None = None,
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
                duplicate = _queue_from_row(existing)
                duplicate_ref = duplicate.provider_session_ref or _provider_ref_from_queue_row(duplicate)
                state_row = conn.execute(
                    """
                    SELECT generation FROM memory_session_flush_state
                    WHERE provider_session_ref = ?
                    """,
                    (duplicate_ref.serialize(),),
                ).fetchone()
                if state_row is None:
                    # A row without a pinned target is a legacy repair case. Do
                    # not recreate an old age boundary for ordinary duplicates.
                    state = self._ensure_session_state_in_connection(
                        conn,
                        duplicate_ref,
                        now=now,
                        first_unflushed_at=None,
                    )
                    fallback_generation = state.generation
                else:
                    fallback_generation = int(state_row["generation"])
                return EnqueueResult(
                    outcome="duplicate",
                    row=duplicate,
                    provider_session_ref=duplicate_ref,
                    target_generation=(
                        duplicate.target_generation
                        if duplicate.target_generation is not None
                        else fallback_generation
                    ),
                    target_watermark_ms=(
                        duplicate.target_watermark_ms
                        if duplicate.target_watermark_ms is not None
                        else duplicate.provider_timestamp_ms
                    ),
                )

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

            session_ref = _provider_session_ref(
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
                session_id=session_ref,
            )
            state = self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
                first_unflushed_at=now,
                advance_if_settled=True,
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
                    target_generation, target_watermark_ms,
                    principal_id, project_ref, provenance, app, payload_text,
                    payload_attachments,
                    occurred_at_ms, provider_timestamp_ms, state, attempts,
                    next_retry_at, lease_owner, lease_at, last_error,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    source_message_digest,
                    meta.epoch,
                    session_ref,
                    provider_session_ref.serialize(),
                    state.generation,
                    provider_timestamp_ms,
                    principal_id,
                    project_ref,
                    provenance,
                    _bounded_opaque_text(app),
                    payload_text,
                    payload_attachments,
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
                    provider_session_ref=provider_session_ref,
                    app=_bounded_opaque_text(app),
                    target_generation=state.generation,
                    target_watermark_ms=provider_timestamp_ms,
                ),
                provider_session_ref=provider_session_ref,
                target_generation=state.generation,
                target_watermark_ms=provider_timestamp_ms,
            )

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
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memory_session_flush_state AS session_state
                      WHERE session_state.principal_id = memory_capture_queue.principal_id
                        AND session_state.epoch = memory_capture_queue.epoch
                        AND session_state.project_ref = memory_capture_queue.project_ref
                        AND session_state.session_id = memory_capture_queue.session_id
                        AND session_state.flush_state = 'manual_required'
                  )
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
            return SettleResult(
                settled=settled,
                state="delivered" if settled else None,
                flush_complete=settled and outcome.add_status == "extracted",
            )
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

        naturally_extracted = add_status == "extracted"
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
                    "succeeded" if naturally_extracted else "not_attempted",
                    "extracted" if naturally_extracted else None,
                    now if naturally_extracted else None,
                    row.source_message_digest,
                    row.epoch,
                    lease_owner,
                ),
            )
            if result.rowcount != 1:
                return False
            provider_session_ref = row.provider_session_ref or _provider_ref_from_queue_row(row)
            state = self._ensure_session_state_in_connection(
                conn,
                provider_session_ref,
                now=now,
                first_unflushed_at=row.created_at,
            )
            watermark_after = state.watermark
            if naturally_extracted:
                watermark_after = max(watermark_after, row.provider_timestamp_ms)
            conn.execute(
                """
                UPDATE memory_session_flush_state
                    SET last_add_ack_at = ?,
                    watermark = CASE
                        WHEN ? = 'extracted' THEN MAX(watermark, ?)
                        ELSE watermark
                    END,
                    updated_at = ?
                WHERE provider_session_ref = ?
                """,
                (
                    now,
                    add_status,
                    row.provider_timestamp_ms,
                    now,
                    provider_session_ref.serialize(),
                ),
            )
            self._record_settlement_in_connection(
                conn,
                MemorySettlementRecord(
                    provider_session_ref=provider_session_ref,
                    generation=(
                        row.target_generation
                        if row.target_generation is not None
                        else state.generation
                    ),
                    fence_epoch=state.fence_epoch,
                    operation_id=(
                        (
                            "add-" + _bounded_opaque_text(add_request_id)
                            if _bounded_opaque_text(add_request_id) is not None
                            else f"add-{row.source_message_digest}"
                        )
                    ),
                    operation_kind="add",
                    outcome="succeeded",
                    observed_at=now,
                    last_known_state=add_status,
                    request_id=_bounded_opaque_text(add_request_id),
                    watermark_before=state.watermark,
                    watermark_after=watermark_after,
                    settled_at=now,
                    confirmed_watermark_ms=(
                        watermark_after if add_status == "extracted" else None
                    ),
                    flush_state=("settled" if add_status == "extracted" else None),
                    source=(
                        "natural_boundary" if add_status == "extracted" else "add"
                    ),
                    settlement_id=f"add-{row.source_message_digest}",
                ),
            )
            self._compact_terminal_tombstones_in_connection(conn, _datetime_from_iso(now))
            return True

    def mark_flush_in_flight(self, session_id: str, project_ref: str) -> int:
        """Freeze the delivered rows consumed by one imminent session flush."""

        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return 0
            targets = conn.execute(
                """
                SELECT DISTINCT provider_session_ref, principal_id, session_id,
                                project_ref, target_generation,
                                MIN(created_at) AS first_unflushed_at
                FROM memory_capture_queue
                WHERE epoch = ? AND session_id = ? AND project_ref = ?
                  AND state = 'delivered'
                  AND flush_observation = 'not_attempted'
                GROUP BY provider_session_ref, principal_id, session_id,
                         project_ref, target_generation
                """,
                (meta.epoch, session_id, project_ref),
            ).fetchall()
            marked = 0
            for target in targets:
                provider_session_ref = _provider_ref_from_values(
                    principal_id=str(target["principal_id"]),
                    epoch=meta.epoch,
                    project_ref=str(target["project_ref"]),
                    session_id=str(target["session_id"]),
                )
                target_generation = int(target["target_generation"] or 0)
                state = self._ensure_session_state_in_connection(
                    conn,
                    provider_session_ref,
                    now=now,
                    first_unflushed_at=str(target["first_unflushed_at"]),
                )
                if state.flush_state == "manual_required":
                    continue
                result = conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET flush_observation = 'in_flight', flush_status = NULL,
                        flush_error_code = NULL, flush_request_id = NULL,
                        flush_observed_at = NULL,
                        provider_session_ref = ?
                    WHERE epoch = ? AND session_id = ? AND project_ref = ?
                      AND principal_id = ? AND state = 'delivered'
                      AND flush_observation = 'not_attempted'
                      AND target_generation = ?
                    """,
                    (
                        provider_session_ref.serialize(),
                        meta.epoch,
                        session_id,
                        project_ref,
                        provider_session_ref.principal_id,
                        target_generation,
                    ),
                )
                if result.rowcount:
                    marked += int(result.rowcount)
                    conn.execute(
                        """
                        UPDATE memory_session_flush_state
                        SET flush_state = 'in_flight',
                            fence_epoch = fence_epoch + 1,
                            fence_owner = 'memory-worker',
                            fence_acquired_at = ?,
                            due_at = NULL,
                            updated_at = ?
                        WHERE provider_session_ref = ?
                        """,
                        (now, now, provider_session_ref.serialize()),
                    )
            return marked

    def record_flush_verdict(
        self,
        session_id: str,
        project_ref: str,
        result: FlushResult,
        *,
        now: str,
    ) -> int:
        """Persist one closed provider verdict for exactly its in-flight group."""

        if isinstance(result, FlushSucceeded):
            observation = "succeeded"
            status = result.status if result.status in {"extracted", "no_extraction"} else None
            error_code = None
            request_id = result.request_id
        elif isinstance(result, FlushRejected):
            observation = "rejected"
            status = None
            error_code = result.error_code
            request_id = result.request_id
        elif isinstance(result, FlushUnknown):
            observation = "unknown"
            status = None
            error_code = None
            request_id = None
        else:
            raise TypeError("unsupported flush result")

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return 0
            targets = conn.execute(
                """
                SELECT DISTINCT provider_session_ref, principal_id, session_id,
                                project_ref, target_generation
                FROM memory_capture_queue
                WHERE epoch = ? AND session_id = ? AND project_ref = ?
                  AND state = 'delivered' AND flush_observation = 'in_flight'
                """,
                (meta.epoch, session_id, project_ref),
            ).fetchall()
            updated_count = 0
            for target in targets:
                provider_session_ref = _provider_ref_from_values(
                    principal_id=str(target["principal_id"]),
                    epoch=meta.epoch,
                    project_ref=str(target["project_ref"]),
                    session_id=str(target["session_id"]),
                )
                target_generation = int(target["target_generation"] or 0)
                updated = conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET flush_observation = ?, flush_status = ?, flush_error_code = ?,
                        flush_request_id = ?, flush_observed_at = ?,
                        provider_session_ref = ?
                    WHERE epoch = ? AND session_id = ? AND project_ref = ?
                      AND principal_id = ? AND state = 'delivered'
                      AND flush_observation = 'in_flight'
                      AND target_generation = ?
                    """,
                    (
                        observation,
                        status,
                        _bounded_opaque_text(error_code),
                        _bounded_opaque_text(request_id),
                        now,
                        provider_session_ref.serialize(),
                        meta.epoch,
                        session_id,
                        project_ref,
                        provider_session_ref.principal_id,
                        target_generation,
                    ),
                )
                if not updated.rowcount:
                    continue
                updated_count += int(updated.rowcount)
                state = self._ensure_session_state_in_connection(
                    conn,
                    provider_session_ref,
                    now=now,
                    first_unflushed_at=None,
                )
                watermark_after = state.watermark
                if observation == "succeeded":
                    max_timestamp = conn.execute(
                        """
                        SELECT MAX(provider_timestamp_ms)
                        FROM memory_capture_queue
                        WHERE provider_session_ref = ? AND epoch = ?
                          AND target_generation = ? AND state = 'delivered'
                        """,
                        (
                            provider_session_ref.serialize(),
                            meta.epoch,
                            target_generation,
                        ),
                    ).fetchone()[0]
                    watermark_after = max(watermark_after, int(max_timestamp or 0))
                settlement = MemorySettlementRecord(
                    provider_session_ref=provider_session_ref,
                    generation=target_generation,
                    fence_epoch=state.fence_epoch,
                    operation_id=(
                        (
                            "flush-" + _bounded_opaque_text(request_id)
                            if _bounded_opaque_text(request_id) is not None
                            else f"legacy-flush-{uuid.uuid4().hex}"
                        )
                    ),
                    operation_kind="flush",
                    outcome=(
                        "succeeded"
                        if observation == "succeeded"
                        else "rejected"
                        if observation == "rejected"
                        else "manual_required"
                    ),
                    observed_at=now,
                    last_known_state=state.flush_state,
                    last_observed_outcome=(
                        "unknown" if observation == "unknown" else None
                    ),
                    request_id=_bounded_opaque_text(request_id),
                    error_code=_bounded_opaque_text(error_code),
                    watermark_before=state.watermark,
                    watermark_after=watermark_after,
                    settled_at=now,
                    confirmed_watermark_ms=(
                        watermark_after if observation == "succeeded" else None
                    ),
                    flush_state=("settled" if observation == "succeeded" else None),
                    source="flush",
                )
                self._record_settlement_in_connection(conn, settlement)

            if updated_count:
                conn.execute(
                    """
                    UPDATE memory_meta
                    SET last_success_at = CASE
                            WHEN ? = 'succeeded' THEN ?
                            ELSE last_success_at
                        END,
                        last_error = CASE
                            WHEN last_error IN ('memory_sidecar_unavailable', 'memory_provider_timeout')
                                THEN NULL
                            WHEN last_error = 'memory_processing_failed'
                                 AND processing_fault_since IS NULL
                                THEN NULL
                            ELSE last_error
                        END,
                        last_error_at = CASE
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
            return updated_count

    def _recover_in_flight_flushes(self, *, now: str) -> int:
        """Turn activation-interrupted flush attempts into terminal unknowns."""

        with self._transaction() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return 0
            targets = conn.execute(
                """
                SELECT DISTINCT provider_session_ref, principal_id, session_id,
                                project_ref, target_generation
                FROM memory_capture_queue
                WHERE epoch = ? AND state = 'delivered'
                  AND flush_observation = 'in_flight'
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
            for target in targets:
                provider_session_ref = _provider_ref_from_values(
                    principal_id=str(target["principal_id"]),
                    epoch=meta.epoch,
                    project_ref=str(target["project_ref"]),
                    session_id=str(target["session_id"]),
                )
                target_generation = int(target["target_generation"] or 0)
                state = self._ensure_session_state_in_connection(
                    conn,
                    provider_session_ref,
                    now=now,
                    first_unflushed_at=None,
                )
                self._record_settlement_in_connection(
                    conn,
                    MemorySettlementRecord(
                        provider_session_ref=provider_session_ref,
                        generation=target_generation,
                        fence_epoch=state.fence_epoch,
                        operation_id=f"recovery-flush-{uuid.uuid4().hex}",
                        operation_kind="flush",
                        outcome="manual_required",
                        observed_at=now,
                        last_known_state=state.flush_state,
                        last_observed_outcome="in_flight",
                    ),
                )
            return int(result.rowcount)

    def _list_not_attempted_sessions(self) -> tuple[tuple[str, str], ...]:
        """Return active sessions whose acknowledged buffer still needs a flush."""

        with self._connection() as conn:
            meta = self._meta_in_connection(conn)
            if meta is None:
                return ()
            rows = conn.execute(
                """
                SELECT session_id, project_ref, MIN(completed_at) AS first_completed_at
                FROM memory_capture_queue
                WHERE epoch = ? AND state = 'delivered'
                  AND flush_observation = 'not_attempted'
                GROUP BY session_id, project_ref
                ORDER BY first_completed_at, session_id, project_ref
                """,
                (meta.epoch,),
            ).fetchall()
        return tuple((str(row["session_id"]), str(row["project_ref"])) for row in rows)

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
        schema = Path(__file__).with_name("schema.sql")
        version = self._read_schema_version_without_mutation()
        if version > MEMORY_SCHEMA_VERSION:
            raise OSError("Memory store schema is newer than this Avibe build")
        with self._connection() as conn:
            conn.executescript(schema.read_text(encoding="utf-8"))
            conn.execute("BEGIN IMMEDIATE")
            try:
                if version < MEMORY_LEGACY_SCHEMA_VERSION:
                    # Stores created before versioning are the v1 baseline.  Do
                    # not reinterpret their queue rows as a newer protocol.
                    version = MEMORY_LEGACY_SCHEMA_VERSION
                if version < MEMORY_SCHEMA_VERSION:
                    self._migrate_v1_to_v2(conn)
                conn.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _read_schema_version_without_mutation(self) -> int:
        """Read the schema marker without changing SQLite connection state."""

        if not self.path.exists():
            return 0
        database_uri = f"file:{quote(self.path.as_posix(), safe='/')}?mode=ro"
        conn = sqlite3.connect(database_uri, uri=True, timeout=5.0)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

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

    def _ensure_session_state_in_connection(
        self,
        conn: sqlite3.Connection,
        provider_session_ref: ProviderSessionRef,
        *,
        now: str,
        first_unflushed_at: str | None,
        advance_if_settled: bool = False,
    ) -> MemorySessionState:
        key = provider_session_ref.serialize()
        row = conn.execute(
            """
            SELECT * FROM memory_session_flush_state
            WHERE provider_session_ref = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO memory_session_flush_state (
                    provider_session_ref, principal_id, epoch, project_ref,
                    session_id, generation, first_unflushed_at, last_add_ack_at,
                    due_at, next_attempt_at, flush_state, watermark, fence_epoch,
                    fence_owner, fence_acquired_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL,
                          'not_due', 0, 0, NULL, NULL, ?)
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
        elif (
            advance_if_settled
            and first_unflushed_at is not None
            and row["flush_state"] in {"settled", "settled_with_caveat"}
        ):
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET generation = generation + 1,
                    first_unflushed_at = ?, flush_state = 'not_due',
                    due_at = NULL, next_attempt_at = NULL,
                    fence_epoch = fence_epoch + 1,
                    fence_owner = NULL, fence_acquired_at = NULL,
                    updated_at = ?
                WHERE provider_session_ref = ?
                """,
                (first_unflushed_at, now, key),
            )
        elif (
            first_unflushed_at is not None
            and row["first_unflushed_at"] is None
            and row["flush_state"] != "manual_required"
        ):
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET first_unflushed_at = ?,
                    flush_state = CASE
                        WHEN flush_state IN ('settled', 'settled_with_caveat')
                        THEN 'not_due'
                        ELSE flush_state
                    END,
                    due_at = CASE
                        WHEN flush_state IN ('settled', 'settled_with_caveat')
                        THEN NULL
                        ELSE due_at
                    END,
                    updated_at = ?
                WHERE provider_session_ref = ?
                """,
                (first_unflushed_at, now, key),
            )
        refreshed = conn.execute(
            """
            SELECT * FROM memory_session_flush_state
            WHERE provider_session_ref = ?
            """,
            (key,),
        ).fetchone()
        if refreshed is None:
            raise OSError("Memory session state could not be created")
        return _session_state_from_row(refreshed)

    def _record_settlement_in_connection(
        self,
        conn: sqlite3.Connection,
        record: MemorySettlementRecord,
    ) -> bool:
        provider_session_ref = record.provider_session_ref
        state = self._ensure_session_state_in_connection(
            conn,
            provider_session_ref,
            now=record.observed_at,
            first_unflushed_at=None,
        )
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO memory_flush_settlements (
                settlement_id, provider_session_ref, generation, fence_epoch,
                operation_id, operation_kind, outcome, last_known_state,
                last_observed_outcome, request_id, error_code,
                watermark_before, watermark_after, actor, decision,
                evidence_ref, observed_at, settled_at, confirmed_watermark_ms,
                flush_state, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.settlement_id,
                provider_session_ref.serialize(),
                record.generation,
                record.fence_epoch,
                _bounded_opaque_text(record.operation_id) or record.operation_id,
                record.operation_kind,
                record.outcome,
                _bounded_opaque_text(record.last_known_state),
                record.last_observed_outcome,
                _bounded_opaque_text(record.request_id),
                _bounded_opaque_text(record.error_code),
                record.watermark_before,
                record.watermark_after,
                _bounded_opaque_text(record.actor),
                _bounded_opaque_text(record.decision),
                _bounded_opaque_text(record.evidence_ref),
                record.observed_at,
                record.settled_at or record.observed_at,
                (
                    record.confirmed_watermark_ms
                    if record.confirmed_watermark_ms is not None
                    else record.watermark_after
                ),
                record.flush_state or _settlement_flush_state(record),
                record.source,
            ),
        )
        if inserted.rowcount != 1:
            return False

        if (
            record.operation_kind == "add"
            and record.source == "natural_boundary"
            and record.outcome in {"succeeded", "committed"}
        ):
            conn.execute(
                """
                UPDATE memory_meta
                SET last_success_at = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (record.observed_at, record.observed_at),
            )

        projection_allowed = (
            state.generation == record.generation
            and (
                record.operation_kind == "add"
                or state.fence_epoch == record.fence_epoch
            )
            and (
                state.flush_state != "manual_required"
                or record.outcome in {"unknown", "manual_required"}
            )
        )
        if not projection_allowed:
            return True

        watermark_after = (
            max(
                state.watermark,
                record.confirmed_watermark_ms
                if record.confirmed_watermark_ms is not None
                else record.watermark_after,
            )
            if record.confirmed_watermark_ms is not None or record.watermark_after is not None
            else state.watermark
        )
        if record.outcome in {"unknown", "manual_required"}:
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET flush_state = 'manual_required',
                    fence_epoch = MAX(fence_epoch, ?),
                    fence_owner = COALESCE(fence_owner, 'manual-required'),
                    fence_acquired_at = COALESCE(fence_acquired_at, ?),
                    watermark = ?, updated_at = ?
                WHERE provider_session_ref = ? AND generation = ?
                """,
                (
                    record.fence_epoch,
                    record.observed_at,
                    watermark_after,
                    record.observed_at,
                    provider_session_ref.serialize(),
                    record.generation,
                ),
            )
        elif record.operation_kind == "add":
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET last_add_ack_at = ?, watermark = ?, updated_at = ?
                WHERE provider_session_ref = ? AND generation = ?
                """,
                (
                    record.observed_at,
                    watermark_after,
                    record.observed_at,
                    provider_session_ref.serialize(),
                    record.generation,
                ),
            )
            if record.source == "natural_boundary" or record.flush_state == "settled":
                remaining = self._first_unsettled_generation_created_at(
                    conn,
                    provider_session_ref,
                    record.generation,
                )
                if remaining is None:
                    conn.execute(
                        """
                        UPDATE memory_session_flush_state
                        SET flush_state = 'settled', due_at = NULL,
                            next_attempt_at = NULL, first_unflushed_at = NULL,
                            watermark = ?, updated_at = ?
                        WHERE provider_session_ref = ? AND generation = ?
                        """,
                        (
                            watermark_after,
                            record.observed_at,
                            provider_session_ref.serialize(),
                            record.generation,
                        ),
                    )
                else:
                    # Keep the generation live while an older pinned row is
                    # still queued; settling it would strand that receipt.
                    conn.execute(
                        """
                        UPDATE memory_session_flush_state
                        SET flush_state = 'not_due', due_at = NULL,
                            next_attempt_at = NULL, first_unflushed_at = ?,
                            watermark = ?, updated_at = ?
                        WHERE provider_session_ref = ? AND generation = ?
                        """,
                        (
                            remaining,
                            watermark_after,
                            record.observed_at,
                            provider_session_ref.serialize(),
                            record.generation,
                        ),
                    )
        elif record.operation_kind == "flush" and record.outcome in {"succeeded", "committed"}:
            if record.source == "migration":
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET flush_state = 'settled', due_at = NULL,
                        next_attempt_at = NULL, watermark = ?, updated_at = ?
                    WHERE provider_session_ref = ? AND generation = ?
                    """,
                    (
                        watermark_after,
                        record.observed_at,
                        provider_session_ref.serialize(),
                        record.generation,
                    ),
                )
                return True
            remaining = self._first_unsettled_generation_created_at(
                conn,
                provider_session_ref,
                record.generation,
            )
            if remaining is None:
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET generation = generation + 1,
                        first_unflushed_at = NULL, flush_state = 'not_due',
                        due_at = NULL, next_attempt_at = NULL,
                        fence_epoch = fence_epoch + 1,
                        fence_owner = NULL, fence_acquired_at = NULL,
                        watermark = ?, updated_at = ?
                    WHERE provider_session_ref = ? AND generation = ?
                    """,
                    (
                        watermark_after,
                        record.observed_at,
                        provider_session_ref.serialize(),
                        record.generation,
                    ),
                )
            else:
                # Keep the receipt's original generation while older rows are
                # still queued; advancing here would strand their pinned target.
                conn.execute(
                    """
                    UPDATE memory_session_flush_state
                    SET first_unflushed_at = ?, flush_state = 'not_due',
                        due_at = NULL, next_attempt_at = NULL,
                        fence_epoch = fence_epoch + 1,
                        fence_owner = NULL, fence_acquired_at = NULL,
                        watermark = ?, updated_at = ?
                    WHERE provider_session_ref = ? AND generation = ?
                    """,
                    (
                        str(remaining),
                        watermark_after,
                        record.observed_at,
                        provider_session_ref.serialize(),
                        record.generation,
                    ),
                )
        elif record.outcome in {"not_committed", "rejected"}:
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET flush_state = 'due', due_at = ?, next_attempt_at = ?,
                    fence_owner = NULL, fence_acquired_at = NULL,
                    watermark = ?, updated_at = ?
                WHERE provider_session_ref = ? AND generation = ?
                """,
                (
                    record.observed_at,
                    record.observed_at,
                    watermark_after,
                    record.observed_at,
                    provider_session_ref.serialize(),
                    record.generation,
                ),
            )
        elif record.outcome == "settled_with_caveat":
            conn.execute(
                """
                UPDATE memory_session_flush_state
                SET flush_state = 'settled_with_caveat', due_at = NULL,
                    next_attempt_at = NULL, fence_owner = NULL,
                    fence_acquired_at = NULL, watermark = ?, updated_at = ?
                WHERE provider_session_ref = ? AND generation = ?
                """,
                (
                    watermark_after,
                    record.observed_at,
                    provider_session_ref.serialize(),
                    record.generation,
                ),
            )
        return True

    def _first_unsettled_generation_created_at(
        self,
        conn: sqlite3.Connection,
        provider_session_ref: ProviderSessionRef,
        generation: int,
    ) -> str | None:
        row = conn.execute(
            """
            SELECT MIN(created_at)
            FROM memory_capture_queue
            WHERE provider_session_ref = ? AND target_generation = ?
              AND (
                  state IN ('pending', 'processing')
                  OR (
                      state = 'delivered'
                      AND COALESCE(flush_observation, 'not_attempted') != 'succeeded'
                  )
              )
            """,
            (provider_session_ref.serialize(), generation),
        ).fetchone()
        return str(row[0]) if row[0] is not None else None

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """Add coordinator state without rewriting legacy delivery outcomes."""

        _add_column_if_missing(conn, "memory_capture_queue", "provider_session_ref", "TEXT")
        _add_column_if_missing(
            conn,
            "memory_capture_queue",
            "target_generation",
            "INTEGER",
        )
        _add_column_if_missing(
            conn,
            "memory_capture_queue",
            "target_watermark_ms",
            "INTEGER",
        )
        _add_column_if_missing(conn, "memory_capture_queue", "app", "TEXT")
        for column, definition in (
            ("settled_at", "TEXT"),
            ("confirmed_watermark_ms", "INTEGER"),
            ("flush_state", "TEXT"),
            ("source", "TEXT"),
        ):
            _add_column_if_missing(
                conn,
                "memory_flush_settlements",
                column,
                definition,
            )
        rows = conn.execute(
            """
            SELECT source_message_digest, provider_session_ref, epoch, session_id,
                   target_generation, target_watermark_ms,
                   principal_id, project_ref, state, created_at, completed_at,
                   provider_timestamp_ms, flush_observation, flush_observed_at
            FROM memory_capture_queue
            ORDER BY created_at, source_message_digest
            """
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        refs: dict[str, ProviderSessionRef] = {}
        for row in rows:
            provider_session_ref = _provider_ref_from_legacy_row(row)
            key = provider_session_ref.serialize()
            if row["provider_session_ref"] != key:
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET provider_session_ref = ?
                    WHERE source_message_digest = ?
                    """,
                    (key, row["source_message_digest"]),
                )
            if row["target_generation"] is None or row["target_watermark_ms"] is None:
                conn.execute(
                    """
                    UPDATE memory_capture_queue
                    SET target_generation = COALESCE(target_generation, 0),
                        target_watermark_ms = COALESCE(target_watermark_ms, ?)
                    WHERE source_message_digest = ?
                    """,
                    (
                        int(row["provider_timestamp_ms"]),
                        row["source_message_digest"],
                    ),
                )
            grouped.setdefault(key, []).append(row)
            refs[key] = provider_session_ref

        for key, group in grouped.items():
            provider_session_ref = refs[key]
            active_rows = [
                row
                for row in group
                if row["state"] in {"pending", "processing", "delivered"}
            ]
            ambiguous = any(
                row["flush_observation"] in {"in_flight", "unknown"}
                for row in group
            )
            needs_flush = any(
                row["state"] == "delivered"
                and row["flush_observation"] in {None, "not_attempted"}
                for row in group
            )
            succeeded = [
                row for row in group if row["flush_observation"] == "succeeded"
            ]
            rejected = any(row["flush_observation"] == "rejected" for row in group)
            first_unflushed_at = min(
                (str(row["created_at"]) for row in active_rows),
                default=None,
            )
            last_add_ack_at = max(
                (str(row["completed_at"]) for row in group if row["completed_at"]),
                default=None,
            )
            watermark = max(
                (int(row["provider_timestamp_ms"]) for row in succeeded),
                default=0,
            )
            state = "manual_required" if ambiguous else (
                "due" if needs_flush or rejected else "settled" if succeeded else "not_due"
            )
            now = utc_now_iso()
            fence_epoch = 1 if ambiguous else 0
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_session_flush_state (
                    provider_session_ref, principal_id, epoch, project_ref,
                    session_id, generation, first_unflushed_at, last_add_ack_at,
                    due_at, next_attempt_at, flush_state, watermark, fence_epoch,
                    fence_owner, fence_acquired_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    provider_session_ref.principal_id,
                    provider_session_ref.epoch,
                    provider_session_ref.project_ref,
                    provider_session_ref.session_id,
                    first_unflushed_at,
                    last_add_ack_at,
                    now if state == "due" else None,
                    now if state == "due" else None,
                    state,
                    watermark,
                    fence_epoch,
                    "memory-migration" if ambiguous else None,
                    now if ambiguous else None,
                    now,
                ),
            )
            observations: list[tuple[str, str]] = []
            for observation in {"succeeded", "rejected", "unknown", "in_flight"}:
                observed_at = max(
                    (
                        str(row["flush_observed_at"] or row["completed_at"] or row["created_at"])
                        for row in group
                        if row["flush_observation"] == observation
                    ),
                    default=now,
                )
                if any(row["flush_observation"] == observation for row in group):
                    observations.append((observed_at, observation))
            observations.sort(key=lambda item: (item[0], item[1]))
            for observed_at, observation in observations:
                outcome = (
                    "manual_required"
                    if observation in {"unknown", "in_flight"}
                    else observation
                )
                operation_id = "migration-v2-" + hashlib.sha256(
                    f"{key}:{observation}".encode("utf-8")
                ).hexdigest()[:32]
                settlement = MemorySettlementRecord(
                    provider_session_ref=provider_session_ref,
                    generation=0,
                    fence_epoch=fence_epoch,
                    operation_id=operation_id,
                    operation_kind="flush",
                    outcome=outcome,  # type: ignore[arg-type]
                    observed_at=observed_at,
                    last_known_state=state,
                    last_observed_outcome=observation,  # type: ignore[arg-type]
                    request_id=None,
                    watermark_before=0,
                    watermark_after=watermark,
                    settled_at=observed_at,
                    confirmed_watermark_ms=(
                        watermark if outcome == "succeeded" else None
                    ),
                    flush_state=(
                        "settled"
                        if outcome == "succeeded"
                        else "due"
                        if outcome == "rejected"
                        else "manual_required"
                    ),
                    source="migration",
                    settlement_id=operation_id,
                )
                self._record_settlement_in_connection(conn, settlement)

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
        removed = conn.execute(
            """
            DELETE FROM memory_capture_queue
            WHERE state IN ('delivered', 'dead')
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
                    SELECT source_message_digest FROM memory_capture_queue
                    WHERE state IN ('delivered', 'dead')
                    ORDER BY COALESCE(flush_observed_at, completed_at), source_message_digest
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
        provider_session_ref=(
            _deserialize_provider_session_ref(row["provider_session_ref"])
            if _row_has_key(row, "provider_session_ref")
            and row["provider_session_ref"] is not None
            else _provider_ref_from_values(
                principal_id=str(row["principal_id"]),
                epoch=int(row["epoch"]),
                project_ref=str(row["project_ref"]),
                session_id=str(row["session_id"]),
            )
        ),
        app=(
            str(row["app"])
            if _row_has_key(row, "app") and row["app"] is not None
            else None
        ),
        target_generation=(
            int(row["target_generation"])
            if _row_has_key(row, "target_generation")
            and row["target_generation"] is not None
            else None
        ),
        target_watermark_ms=(
            int(row["target_watermark_ms"])
            if _row_has_key(row, "target_watermark_ms")
            and row["target_watermark_ms"] is not None
            else None
        ),
    )


def _session_state_from_row(row: sqlite3.Row) -> MemorySessionState:
    return MemorySessionState(
        provider_session_ref=_deserialize_provider_session_ref(row["provider_session_ref"]),
        generation=int(row["generation"]),
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
        due_at=str(row["due_at"]) if row["due_at"] is not None else None,
        next_attempt_at=(
            str(row["next_attempt_at"])
            if row["next_attempt_at"] is not None
            else None
        ),
        flush_state=str(row["flush_state"]),
        watermark=int(row["watermark"]),
        fence_epoch=int(row["fence_epoch"]),
        fence_owner=(str(row["fence_owner"]) if row["fence_owner"] is not None else None),
        fence_acquired_at=(
            str(row["fence_acquired_at"])
            if row["fence_acquired_at"] is not None
            else None
        ),
        updated_at=str(row["updated_at"]),
    )


def _settlement_from_row(row: sqlite3.Row) -> MemorySettlementRecord:
    return MemorySettlementRecord(
        provider_session_ref=_deserialize_provider_session_ref(row["provider_session_ref"]),
        generation=int(row["generation"]),
        fence_epoch=int(row["fence_epoch"]),
        operation_id=str(row["operation_id"]),
        operation_kind=str(row["operation_kind"]),
        outcome=str(row["outcome"]),
        observed_at=str(row["observed_at"]),
        last_known_state=(
            str(row["last_known_state"])
            if row["last_known_state"] is not None
            else None
        ),
        last_observed_outcome=(
            str(row["last_observed_outcome"])
            if row["last_observed_outcome"] is not None
            else None
        ),
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        error_code=(
            str(row["error_code"]) if row["error_code"] is not None else None
        ),
        watermark_before=(
            int(row["watermark_before"])
            if row["watermark_before"] is not None
            else None
        ),
        watermark_after=(
            int(row["watermark_after"])
            if row["watermark_after"] is not None
            else None
        ),
        actor=str(row["actor"]) if row["actor"] is not None else None,
        decision=str(row["decision"]) if row["decision"] is not None else None,
        evidence_ref=(
            str(row["evidence_ref"]) if row["evidence_ref"] is not None else None
        ),
        settled_at=(
            str(row["settled_at"])
            if row["settled_at"] is not None
            else str(row["observed_at"])
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
        settlement_id=str(row["settlement_id"]),
    )


def _iso_from_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _row_has_key(row: sqlite3.Row | dict[str, object], key: str) -> bool:
    if isinstance(row, sqlite3.Row):
        return key in row.keys()
    return key in row


def _deserialize_provider_session_ref(value: object) -> ProviderSessionRef:
    if not isinstance(value, str):
        raise OSError("Memory queue contains no provider session reference")
    try:
        return ProviderSessionRef.deserialize(value)
    except ValueError as error:
        raise OSError("Memory queue contains an invalid provider session reference") from error


def _provider_session_key(provider_session_ref: ProviderSessionRef) -> str:
    if not isinstance(provider_session_ref, ProviderSessionRef):
        raise TypeError("expected ProviderSessionRef")
    return provider_session_ref.serialize()


def _settlement_flush_state(record: MemorySettlementRecord) -> str | None:
    if record.flush_state is not None:
        return record.flush_state
    if record.outcome in {"unknown", "manual_required"}:
        return "manual_required"
    if record.outcome in {"succeeded", "committed"} and record.operation_kind == "flush":
        return "settled"
    if record.outcome in {"rejected", "not_committed"}:
        return "due"
    return None


def _provider_ref_from_values(
    *,
    principal_id: str,
    epoch: int,
    project_ref: str,
    session_id: str,
) -> ProviderSessionRef:
    return ProviderSessionRef(
        principal_id=principal_id,
        epoch=epoch,
        project_ref=project_ref,
        session_id=session_id,
    )


def _provider_ref_from_queue_row(row: QueueRow) -> ProviderSessionRef:
    return _provider_ref_from_values(
        principal_id=row.principal_id,
        epoch=row.epoch,
        project_ref=row.project_ref,
        session_id=row.session_id,
    )


def _provider_ref_from_legacy_row(row: sqlite3.Row) -> ProviderSessionRef:
    raw = row["provider_session_ref"]
    if raw is not None:
        try:
            candidate = ProviderSessionRef.deserialize(str(raw))
            if candidate.as_tuple() == (
                str(row["principal_id"]),
                int(row["epoch"]),
                str(row["project_ref"]),
                str(row["session_id"]),
            ):
                return candidate
        except ValueError:
            # A partially written migration must not make the old queue
            # unreadable.  Rebuild the reference from the old stable columns.
            pass
    return _provider_ref_from_values(
        principal_id=str(row["principal_id"]),
        epoch=int(row["epoch"]),
        project_ref=str(row["project_ref"]),
        session_id=str(row["session_id"]),
    )


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _closed_error_or(value: object, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return value if is_memory_error_code(value) else fallback


def _bounded_opaque_text(value: str | None, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str) or not value.strip():
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


def provider_session_ref(
    scope_key: bytes,
    principal_id: str,
    project_ref: str,
    session_id: str,
    epoch: int,
) -> ProviderSessionRef:
    """Build the canonical structured reference used by later coordinators."""

    return ProviderSessionRef(
        principal_id=principal_id,
        epoch=epoch,
        project_ref=project_ref,
        session_id=_provider_session_ref(
            scope_key,
            principal_id,
            project_ref,
            session_id,
            epoch,
        ),
    )
