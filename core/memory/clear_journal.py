"""Durable state machine for crash-recoverable Memory clear operations.

This module records intent and progress only.  It never deletes or restores a
Memory surface; the runtime must perform those actions under its maintenance
fence and then acknowledge them with the narrow CAS methods below.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal, Mapping, Sequence

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    PrivateSqliteDatabase,
)
from core.memory.snapshot import (
    MemorySnapshot,
    _TerminalSnapshotPermit,
    _issue_terminal_snapshot_permit,
    _issue_preparing_snapshot_discard_permit,
    _PreparingSnapshotDiscardPermit,
    _preparing_snapshot_discard_succeeded,
    _revoke_preparing_snapshot_discard_permit,
)
from core.memory.types import CLOSED_MEMORY_ERROR_CODES, is_memory_error_code


ClearOperationState = Literal[
    "preparing",
    "prepared",
    "deleting",
    "recovery_needed",
    "completed",
    "aborted",
]
ClearSurfaceName = Literal["queue", "provider", "call_log", "attachments"]
ClearSurfaceState = Literal["pending", "snapshotted", "deleted", "restored"]
ClearResolution = Literal["resume", "abort"]

_OPEN_STATES = frozenset({"preparing", "prepared", "deleting", "recovery_needed"})
_TERMINAL_STATES = frozenset({"completed", "aborted"})
_SURFACE_NAMES = ("queue", "provider", "call_log", "attachments")
_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_VERSION = 1


def _validated_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid effective-home-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid effective-home-relative path")
    if path.as_posix() != value:
        raise ValueError("effective-home-relative path must be canonical")
    return value


class MemoryClearJournalError(RuntimeError):
    """Base class for a refused or failed journal operation."""


class ClearOperationConflict(MemoryClearJournalError):
    """A second clear was attempted while another operation is open."""


class ClearOperationNotFound(MemoryClearJournalError):
    """The requested clear operation does not exist."""


class ClearOperationCASMismatch(MemoryClearJournalError):
    """The caller no longer owns the journal revision or execution claim."""


class ClearTransitionError(MemoryClearJournalError):
    """The requested state transition violates clear ordering."""


class ClearBackupBlocked(MemoryClearJournalError):
    """An ordinary backup was attempted while a clear operation was open."""

    def __init__(self, operation_id: str, state: ClearOperationState) -> None:
        super().__init__(f"Memory backup is blocked by open clear {operation_id!r} ({state})")
        self.operation_id = operation_id
        self.state = state


@dataclass(frozen=True, slots=True)
class ClearSurfaceSpec:
    """One fixed logical surface and its effective-home-relative path."""

    name: ClearSurfaceName
    relative_path: str

    def __post_init__(self) -> None:
        if self.name not in _SURFACE_NAMES:
            raise ValueError("unsupported Memory clear surface")
        object.__setattr__(self, "relative_path", _validated_relative_path(self.relative_path))


DEFAULT_CLEAR_SURFACES: tuple[ClearSurfaceSpec, ...] = (
    ClearSurfaceSpec("queue", "state/memory/memory.sqlite"),
    ClearSurfaceSpec("provider", "memory/everos-root"),
    ClearSurfaceSpec("call_log", "memory/call-log/call-log.db"),
    ClearSurfaceSpec("attachments", "memory/attachments"),
)


@dataclass(frozen=True, slots=True)
class ClearOperation:
    operation_id: str
    operator_ref: str
    state: ClearOperationState
    recovery_from_state: Literal["preparing", "prepared", "deleting"] | None
    resolution: ClearResolution | None
    started_at: str
    updated_at: str
    terminal_at: str | None
    pre_epoch: int
    target_epoch: int
    snapshot_path: str | None
    manifest_sha256: str | None
    destructive_started: bool
    closed_error: str | None
    revision: int
    execution_token: str | None


@dataclass(frozen=True, slots=True)
class ClearSurface:
    operation_id: str
    surface: ClearSurfaceName
    relative_path: str
    relative_snapshot_path: str | None
    present: bool | None
    pre_clear_digest: str | None
    snapshot_digest: str | None
    state: ClearSurfaceState
    updated_at: str


@dataclass(frozen=True, slots=True)
class ClearEvent:
    event_id: int
    operation_id: str
    event: str
    actor_ref: str
    surface: ClearSurfaceName | None
    occurred_at: str
    closed_error: str | None
    resulting_revision: int


class MemoryClearJournal:
    """Separate SQLite journal for one-at-a-time Memory clear recovery."""

    def __init__(
        self,
        effective_home: Path | str,
        *,
        database_path: Path | str = "state/memory/clear-journal.sqlite",
        surfaces: Sequence[ClearSurfaceSpec] = DEFAULT_CLEAR_SURFACES,
    ) -> None:
        self._effective_home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
        database_value = Path(database_path)
        if database_value.is_absolute():
            self._database_path = Path(os.path.abspath(database_value))
            _relative_to_home(self._database_path, self._effective_home)
        else:
            relative = _validated_relative_path(database_value.as_posix())
            self._database_path = self._effective_home / relative
        self._database = PrivateSqliteDatabase(self._effective_home, self._database_path)
        self._surfaces = tuple(surfaces)
        self._validate_surfaces()
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def surfaces(self) -> tuple[ClearSurfaceSpec, ...]:
        return self._surfaces

    def start(
        self,
        *,
        operator_ref: str,
        pre_epoch: int,
        target_epoch: int,
        operation_id: str | None = None,
    ) -> ClearOperation:
        """Durably create the single open operation and its four surfaces."""

        identifier = _validated_operation_id(operation_id or secrets.token_hex(16))
        actor = _validated_actor(operator_ref)
        _validated_epoch_pair(pre_epoch, target_epoch)
        now = _utc_now()
        token = secrets.token_hex(16)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO clear_operation (
                    operation_id, operator_ref, state, started_at, updated_at,
                    pre_epoch, target_epoch, destructive_started, open_slot,
                    revision, execution_token
                ) VALUES (?, ?, 'preparing', ?, ?, ?, ?, 0, 1, 1, ?)
                """,
                (identifier, actor, now, now, pre_epoch, target_epoch, token),
            )
            connection.executemany(
                """
                INSERT INTO clear_surface (
                    operation_id, surface, relative_path, state, updated_at
                ) VALUES (?, ?, ?, 'pending', ?)
                """,
                [
                    (identifier, surface.name, surface.relative_path, now)
                    for surface in self._surfaces
                ],
            )
            self._append_event(
                connection,
                identifier,
                "started",
                actor,
                occurred_at=now,
                resulting_revision=1,
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            if self.get_open_operation() is not None:
                raise ClearOperationConflict("a Memory clear operation is already open") from error
            raise MemoryClearJournalError("Memory clear journal rejected the operation") from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def get_operation(self, operation_id: str) -> ClearOperation | None:
        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM clear_operation WHERE operation_id = ?",
                (identifier,),
            ).fetchone()
        finally:
            connection.close()
        return _operation_from_row(row) if row is not None else None

    def get_open_operation(self) -> ClearOperation | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM clear_operation WHERE open_slot = 1"
            ).fetchone()
        finally:
            connection.close()
        return _operation_from_row(row) if row is not None else None

    def get_surfaces(self, operation_id: str) -> tuple[ClearSurface, ...]:
        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM clear_surface
                WHERE operation_id = ?
                ORDER BY CASE surface
                    WHEN 'queue' THEN 1
                    WHEN 'provider' THEN 2
                    WHEN 'call_log' THEN 3
                    WHEN 'attachments' THEN 4
                END
                """,
                (identifier,),
            ).fetchall()
        finally:
            connection.close()
        if not rows and self.get_operation(identifier) is None:
            raise ClearOperationNotFound(identifier)
        return tuple(_surface_from_row(row) for row in rows)

    def can_abort(self, operation_id: str) -> bool:
        """Report whether the current recovery claim may restore its snapshot."""

        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM clear_operation WHERE operation_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise ClearOperationNotFound(identifier)
            return bool(
                row["state"] == "recovery_needed"
                and row["execution_token"] is None
                and row["resolution"] != "resume"
                and self._abort_snapshot_complete(connection, row)
            )
        finally:
            connection.close()

    def can_resume(self, operation_id: str) -> bool:
        """Report whether the current recovery claim may continue deletion."""

        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM clear_operation WHERE operation_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise ClearOperationNotFound(identifier)
            return bool(
                row["state"] == "recovery_needed"
                and row["execution_token"] is None
                and row["resolution"] != "abort"
                and row["recovery_from_state"] in {"preparing", "prepared", "deleting"}
            )
        finally:
            connection.close()

    def get_events(self, operation_id: str) -> tuple[ClearEvent, ...]:
        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM clear_event WHERE operation_id = ? ORDER BY event_id",
                (identifier,),
            ).fetchall()
        finally:
            connection.close()
        if not rows and self.get_operation(identifier) is None:
            raise ClearOperationNotFound(identifier)
        return tuple(_event_from_row(row) for row in rows)

    def assert_backup_allowed(self) -> None:
        """Fail closed while any non-terminal operation occupies the open slot."""

        operation = self.get_open_operation()
        if operation is not None:
            raise ClearBackupBlocked(operation.operation_id, operation.state)

    def terminal_snapshot_permit(self, operation_id: str) -> _TerminalSnapshotPermit:
        """Issue snapshot-GC authority for a fully audited terminal clear."""

        operation = self._require_operation(_validated_operation_id(operation_id))
        required_surface_state = {
            "completed": "deleted",
            "aborted": "restored",
        }.get(operation.state)
        if required_surface_state is None:
            raise ClearTransitionError("only a terminal Memory clear may remove its snapshot")
        if operation.snapshot_path is None or operation.manifest_sha256 is None:
            raise ClearTransitionError("terminal Memory clear snapshot metadata is missing")
        surfaces = self.get_surfaces(operation.operation_id)
        if len(surfaces) != len(_SURFACE_NAMES) or any(
            surface.state != required_surface_state or surface.present is None
            for surface in surfaces
        ):
            raise ClearTransitionError("terminal Memory clear surface audit is incomplete")
        return _issue_terminal_snapshot_permit(
            snapshot_id=operation.operation_id,
            relative_path=operation.snapshot_path,
            manifest_sha256=operation.manifest_sha256,
            surface_digests=tuple(
                (surface.relative_path, surface.snapshot_digest) for surface in surfaces
            ),
        )

    def terminal_snapshot_permits(self) -> tuple[_TerminalSnapshotPermit, ...]:
        """Return durable GC work left by every eligible terminal clear."""

        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation_id FROM clear_operation
                WHERE state IN ('completed', 'aborted')
                ORDER BY terminal_at, operation_id
                """
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            self.terminal_snapshot_permit(row["operation_id"])
            for row in rows
        )

    def record_snapshot(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        snapshot: MemorySnapshot,
    ) -> ClearOperation:
        """Atomically persist one manager-verified all-surface receipt."""

        identifier = _validated_operation_id(operation_id)
        if not isinstance(snapshot, MemorySnapshot) or snapshot.snapshot_id != identifier:
            raise ValueError("Memory snapshot receipt does not match the clear operation")
        relative_snapshot = _validated_relative_path(snapshot.relative_path)
        if relative_snapshot != f"state/memory/clear-snapshots/{identifier}":
            raise ValueError("Memory snapshot path does not match the clear operation")
        manifest_digest = _validated_digest(snapshot.manifest_sha256)
        receipts = {receipt.path: receipt for receipt in snapshot.surface_receipts}
        expected_paths = {surface.relative_path for surface in self._surfaces}
        if set(receipts) != expected_paths or len(receipts) != len(snapshot.surface_receipts):
            raise ValueError("Memory snapshot receipt must cover exactly four surfaces")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("preparing",),
            )
            pending = connection.execute(
                """
                SELECT COUNT(*) FROM clear_surface
                WHERE operation_id = ? AND state != 'pending'
                """,
                (identifier,),
            ).fetchone()[0]
            if pending:
                raise ClearTransitionError("Memory clear snapshot was already recorded")
            now = _utc_now()
            revision = row["revision"] + 1
            for surface in self._surfaces:
                receipt = receipts[surface.relative_path]
                connection.execute(
                    """
                    UPDATE clear_surface
                    SET relative_snapshot_path = ?, present = ?,
                        pre_clear_digest = ?, snapshot_digest = ?,
                        state = 'snapshotted', updated_at = ?
                    WHERE operation_id = ? AND surface = ?
                    """,
                    (
                        (
                            PurePosixPath(relative_snapshot)
                            / "payload"
                            / surface.relative_path
                        ).as_posix(),
                        int(receipt.present),
                        receipt.pre_clear_digest,
                        receipt.snapshot_digest,
                        now,
                        identifier,
                        surface.name,
                    ),
                )
                self._append_event(
                    connection,
                    identifier,
                    "surface_snapshotted",
                    row["operator_ref"],
                    surface=surface.name,
                    occurred_at=now,
                    resulting_revision=revision,
                )
            connection.execute(
                """
                UPDATE clear_operation
                SET snapshot_path = ?, manifest_sha256 = ?, updated_at = ?, revision = ?
                WHERE operation_id = ?
                """,
                (relative_snapshot, manifest_digest, now, revision, identifier),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    @contextmanager
    def authorize_preparing_snapshot_discard(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> Iterator[_PreparingSnapshotDiscardPermit]:
        """Lease removal of an unjournaled snapshot while holding the write lock."""

        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        permit: _PreparingSnapshotDiscardPermit | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("preparing",),
            )
            nonpending = connection.execute(
                """
                SELECT COUNT(*) FROM clear_surface
                WHERE operation_id = ? AND state != 'pending'
                """,
                (identifier,),
            ).fetchone()[0]
            if (
                nonpending
                or row["snapshot_path"] is not None
                or row["manifest_sha256"] is not None
                or row["destructive_started"]
            ):
                raise ClearTransitionError(
                    "only an unrecorded preparing snapshot may be discarded"
                )
            relative_path = f"state/memory/clear-snapshots/{identifier}"
            permit = _issue_preparing_snapshot_discard_permit(
                snapshot_id=identifier,
                relative_path=relative_path,
            )
            yield permit
            if not _preparing_snapshot_discard_succeeded(permit):
                raise ClearTransitionError("Memory snapshot discard did not complete")
            now = _utc_now()
            revision = row["revision"] + 1
            updated = connection.execute(
                """
                UPDATE clear_operation
                SET updated_at = ?, revision = ?
                WHERE operation_id = ? AND state = 'preparing'
                    AND revision = ? AND execution_token = ?
                """,
                (
                    now,
                    revision,
                    identifier,
                    row["revision"],
                    row["execution_token"],
                ),
            )
            if updated.rowcount != 1:
                raise ClearOperationCASMismatch("Memory clear discard claim is stale")
            self._append_event(
                connection,
                identifier,
                "snapshot_discarded",
                row["operator_ref"],
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if permit is not None:
                _revoke_preparing_snapshot_discard_permit(permit)
            connection.close()
            self._harden_database_files()

    def mark_prepared(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> ClearOperation:
        """Seal the independently verified snapshot before destructive work."""

        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("preparing",),
            )
            pending = connection.execute(
                """
                SELECT COUNT(*) FROM clear_surface
                WHERE operation_id = ? AND (
                    state != 'snapshotted' OR present IS NULL OR
                    (present = 1 AND (
                        pre_clear_digest IS NULL OR snapshot_digest IS NULL
                    )) OR
                    (present = 0 AND (
                        pre_clear_digest IS NOT NULL OR snapshot_digest IS NOT NULL
                    ))
                )
                """,
                (identifier,),
            ).fetchone()[0]
            if pending or row["snapshot_path"] is None or row["manifest_sha256"] is None:
                raise ClearTransitionError("all Memory clear surfaces must be snapshotted first")
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE clear_operation
                SET state = 'prepared', updated_at = ?, revision = ?
                WHERE operation_id = ?
                """,
                (now, revision, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "prepared",
                row["operator_ref"],
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def begin_deleting(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> ClearOperation:
        """Persist destructive intent before the first deletion is allowed."""

        return self._operation_transition(
            operation_id,
            expected_revision=expected_revision,
            execution_token=execution_token,
            allowed_states=("prepared",),
            next_state="deleting",
            event="deleting_started",
            assignments={"destructive_started": 1},
        )

    def record_surface_deleted(
        self,
        operation_id: str,
        surface: ClearSurfaceName,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> ClearOperation:
        """Acknowledge one idempotent deletion already performed by runtime."""

        return self._surface_transition(
            _validated_operation_id(operation_id),
            _validated_surface_name(surface),
            expected_revision=expected_revision,
            execution_token=execution_token,
            operation_state="deleting",
            from_states=("snapshotted",),
            to_state="deleted",
            event="surface_deleted",
        )

    def mark_completed(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> ClearOperation:
        """Close a clear only after all four deletion receipts are durable."""

        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("deleting",),
            )
            incomplete = connection.execute(
                """
                SELECT COUNT(*) FROM clear_surface
                WHERE operation_id = ? AND state != 'deleted'
                """,
                (identifier,),
            ).fetchone()[0]
            if incomplete:
                raise ClearTransitionError("all Memory clear surfaces must be deleted first")
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE clear_operation
                SET state = 'completed', updated_at = ?, terminal_at = ?,
                    open_slot = NULL, revision = ?, execution_token = NULL
                WHERE operation_id = ?
                """,
                (now, now, revision, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "completed",
                row["operator_ref"],
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def mark_recovery_needed(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        closed_error: str = "memory_clear_failed",
    ) -> ClearOperation:
        """Fence an in-process failure at its exact active stage."""

        identifier = _validated_operation_id(operation_id)
        error = _validated_error(closed_error)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("preparing", "prepared", "deleting"),
            )
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE clear_operation
                SET state = 'recovery_needed', recovery_from_state = ?,
                    updated_at = ?, closed_error = ?, revision = ?,
                    execution_token = NULL
                WHERE operation_id = ?
                """,
                (row["state"], now, error, revision, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "recovery_needed",
                row["operator_ref"],
                occurred_at=now,
                closed_error=error,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def mark_boot_recovery_needed(
        self,
        *,
        closed_error: str = "memory_clear_failed",
    ) -> ClearOperation | None:
        """Explicitly expose an interrupted open operation without resuming it.

        Construction is intentionally side-effect free with respect to operation
        state.  Boot orchestration calls this method once it owns the runtime
        fence.  No deletion or restore is performed here.
        """

        error = _validated_error(closed_error)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM clear_operation WHERE open_slot = 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = _utc_now()
            revision = row["revision"] + 1
            recovery_from = (
                row["recovery_from_state"]
                if row["state"] == "recovery_needed"
                else row["state"]
            )
            connection.execute(
                """
                UPDATE clear_operation
                SET state = 'recovery_needed', recovery_from_state = ?,
                    updated_at = ?, closed_error = ?, revision = ?,
                    execution_token = NULL
                WHERE operation_id = ?
                """,
                (recovery_from, now, error, revision, row["operation_id"]),
            )
            self._append_event(
                connection,
                row["operation_id"],
                "recovery_needed",
                "system:boot",
                occurred_at=now,
                closed_error=error,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(row["operation_id"])

    def claim_resume(
        self,
        operation_id: str,
        *,
        operator_ref: str,
        expected_revision: int,
    ) -> ClearOperation:
        """Claim continuation of the exact stage interrupted at boot."""

        identifier = _validated_operation_id(operation_id)
        actor = _validated_actor(operator_ref)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._recovery_claim_row(connection, identifier, actor, expected_revision)
            if row["resolution"] == "abort":
                raise ClearTransitionError("an abort decision cannot be changed to resume")
            next_state = row["recovery_from_state"]
            if next_state not in {"preparing", "prepared", "deleting"}:
                raise ClearTransitionError("Memory clear recovery stage is missing")
            now = _utc_now()
            revision = row["revision"] + 1
            token = secrets.token_hex(16)
            connection.execute(
                """
                UPDATE clear_operation
                SET state = ?, resolution = 'resume', updated_at = ?,
                    closed_error = NULL, revision = ?, execution_token = ?
                WHERE operation_id = ?
                """,
                (next_state, now, revision, token, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "resume_claimed",
                actor,
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def claim_abort(
        self,
        operation_id: str,
        *,
        operator_ref: str,
        expected_revision: int,
    ) -> ClearOperation:
        """Make the abort direction durable and return its execution claim."""

        identifier = _validated_operation_id(operation_id)
        actor = _validated_actor(operator_ref)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._recovery_claim_row(connection, identifier, actor, expected_revision)
            if row["resolution"] == "resume":
                raise ClearTransitionError("a resume decision cannot be changed to abort")
            if not self._abort_snapshot_complete(connection, row):
                raise ClearTransitionError("abort requires a complete verified Memory snapshot")
            now = _utc_now()
            revision = row["revision"] + 1
            token = secrets.token_hex(16)
            connection.execute(
                """
                UPDATE clear_operation
                SET resolution = 'abort', updated_at = ?, revision = ?,
                    execution_token = ?
                WHERE operation_id = ?
                """,
                (now, revision, token, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "abort_claimed",
                actor,
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def release_recovery_claim(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        closed_error: str = "memory_clear_failed",
    ) -> ClearOperation:
        """Release a failed resume/abort claim without changing its decision."""

        identifier = _validated_operation_id(operation_id)
        error = _validated_error(closed_error)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("recovery_needed",),
            )
            if row["resolution"] not in {"resume", "abort"}:
                raise ClearTransitionError("Memory clear recovery decision is missing")
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE clear_operation
                SET updated_at = ?, closed_error = ?, revision = ?,
                    execution_token = NULL
                WHERE operation_id = ?
                """,
                (now, error, revision, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "recovery_claim_failed",
                row["operator_ref"],
                occurred_at=now,
                closed_error=error,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def record_surface_restored(
        self,
        operation_id: str,
        surface: ClearSurfaceName,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> ClearOperation:
        """Acknowledge one verified restore under an abort claim."""

        return self._surface_transition(
            _validated_operation_id(operation_id),
            _validated_surface_name(surface),
            expected_revision=expected_revision,
            execution_token=execution_token,
            operation_state="recovery_needed",
            resolution="abort",
            from_states=("snapshotted", "deleted"),
            to_state="restored",
            event="surface_restored",
        )

    def mark_aborted(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        closed_error: str | None = None,
    ) -> ClearOperation:
        """Close an abort after restoration, or before destructive start."""

        identifier = _validated_operation_id(operation_id)
        error = _validated_optional_error(closed_error)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=("recovery_needed",),
                resolution="abort",
            )
            incomplete = connection.execute(
                """
                SELECT COUNT(*) FROM clear_surface
                WHERE operation_id = ? AND state != 'restored'
                """,
                (identifier,),
            ).fetchone()[0]
            if incomplete:
                raise ClearTransitionError("all Memory clear surfaces must be restored first")
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE clear_operation
                SET state = 'aborted', updated_at = ?, terminal_at = ?,
                    open_slot = NULL, closed_error = ?, revision = ?,
                    execution_token = NULL
                WHERE operation_id = ?
                """,
                (now, now, error, revision, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "aborted",
                row["operator_ref"],
                occurred_at=now,
                closed_error=error,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def _initialize(self) -> None:
        try:
            self._database.prepare()
        except ConfinedFilesystemError as error:
            raise MemoryClearJournalError(
                "Memory clear journal could not be prepared safely"
            ) from error
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, _SCHEMA_VERSION}:
                raise MemoryClearJournalError("unsupported Memory clear journal schema")
            if version == 0:
                connection.executescript(_schema_sql())
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                connection.commit()
            else:
                required = {"clear_operation", "clear_surface", "clear_event"}
                present = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if not required.issubset(present):
                    raise MemoryClearJournalError("Memory clear journal schema is incomplete")
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise MemoryClearJournalError("Memory clear journal failed integrity check")
        except sqlite3.Error as error:
            raise MemoryClearJournalError("Memory clear journal could not be initialized") from error
        finally:
            connection.close()
            self._harden_database_files(sync_parent=True)

    def _connect(self) -> sqlite3.Connection:
        try:
            return self._database.connect()
        except ConfinedFilesystemError as error:
            raise MemoryClearJournalError("Memory clear journal is unsafe") from error

    def _validate_surfaces(self) -> None:
        if len(self._surfaces) != len(_SURFACE_NAMES):
            raise ValueError("Memory clear journal requires exactly four surfaces")
        names = tuple(surface.name for surface in self._surfaces)
        if set(names) != set(_SURFACE_NAMES) or len(set(names)) != len(names):
            raise ValueError("Memory clear journal surfaces must be unique and complete")
        paths = [PurePosixPath(surface.relative_path) for surface in self._surfaces]
        database = PurePosixPath(_relative_to_home(self._database_path, self._effective_home).as_posix())
        for index, path in enumerate(paths):
            if path == database or path in database.parents or database in path.parents:
                raise ValueError("Memory clear journal cannot overlap a managed surface")
            for other in paths[index + 1 :]:
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError("Memory clear journal surface paths cannot overlap")

    def _require_operation(self, operation_id: str) -> ClearOperation:
        operation = self.get_operation(operation_id)
        if operation is None:
            raise ClearOperationNotFound(operation_id)
        return operation

    @staticmethod
    def _cas_row(
        connection: sqlite3.Connection,
        operation_id: str,
        expected_revision: int,
        execution_token: str,
        *,
        allowed_states: Sequence[str],
        resolution: str | None = None,
    ) -> sqlite3.Row:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected revision must be an integer")
        token = _validated_token(execution_token)
        row = connection.execute(
            "SELECT * FROM clear_operation WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ClearOperationNotFound(operation_id)
        if row["revision"] != expected_revision or row["execution_token"] != token:
            raise ClearOperationCASMismatch("Memory clear operation claim is stale")
        if row["state"] not in allowed_states:
            raise ClearTransitionError("Memory clear operation is in the wrong state")
        if resolution is not None and row["resolution"] != resolution:
            raise ClearTransitionError("Memory clear operation has the wrong recovery direction")
        return row

    @staticmethod
    def _recovery_claim_row(
        connection: sqlite3.Connection,
        operation_id: str,
        operator_ref: str,
        expected_revision: int,
    ) -> sqlite3.Row:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected revision must be an integer")
        row = connection.execute(
            "SELECT * FROM clear_operation WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ClearOperationNotFound(operation_id)
        if row["state"] != "recovery_needed":
            raise ClearTransitionError("Memory clear operation does not need recovery")
        if row["revision"] != expected_revision or row["execution_token"] is not None:
            raise ClearOperationCASMismatch("Memory clear recovery claim is stale")
        if row["operator_ref"] != operator_ref:
            raise ClearOperationCASMismatch("Memory clear operator does not own this operation")
        return row

    @staticmethod
    def _abort_snapshot_complete(
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
    ) -> bool:
        if operation["snapshot_path"] is None or operation["manifest_sha256"] is None:
            return False
        summary = connection.execute(
            """
            SELECT
                COUNT(*) AS surface_count,
                COUNT(CASE WHEN (
                    state NOT IN ('snapshotted', 'deleted', 'restored') OR
                    present IS NULL OR
                    (present = 1 AND (
                        pre_clear_digest IS NULL OR snapshot_digest IS NULL
                    )) OR
                    (present = 0 AND (
                        pre_clear_digest IS NOT NULL OR snapshot_digest IS NOT NULL
                    ))
                ) THEN 1 END) AS invalid_count
            FROM clear_surface
            WHERE operation_id = ?
            """,
            (operation["operation_id"],),
        ).fetchone()
        return bool(
            summary is not None
            and summary["surface_count"] == len(_SURFACE_NAMES)
            and summary["invalid_count"] == 0
        )

    def _operation_transition(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        allowed_states: Sequence[str],
        next_state: str,
        event: str,
        assignments: Mapping[str, object] | None = None,
    ) -> ClearOperation:
        identifier = _validated_operation_id(operation_id)
        allowed_assignments = {"destructive_started"}
        values = dict(assignments or {})
        if not set(values).issubset(allowed_assignments):
            raise ValueError("unsupported Memory clear journal assignment")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                identifier,
                expected_revision,
                execution_token,
                allowed_states=allowed_states,
            )
            now = _utc_now()
            revision = row["revision"] + 1
            columns = ["state = ?", "updated_at = ?", "revision = ?"]
            parameters: list[object] = [next_state, now, revision]
            for column, value in values.items():
                columns.append(f"{column} = ?")
                parameters.append(value)
            parameters.append(identifier)
            connection.execute(
                f"UPDATE clear_operation SET {', '.join(columns)} WHERE operation_id = ?",
                parameters,
            )
            self._append_event(
                connection,
                identifier,
                event,
                row["operator_ref"],
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(identifier)

    def _surface_transition(
        self,
        operation_id: str,
        surface: ClearSurfaceName,
        *,
        expected_revision: int,
        execution_token: str,
        operation_state: str,
        from_states: Sequence[str],
        to_state: str,
        event: str,
        resolution: str | None = None,
        assignments: Mapping[str, object] | None = None,
    ) -> ClearOperation:
        allowed_assignments = {
            "relative_snapshot_path",
            "pre_clear_digest",
            "snapshot_digest",
        }
        values = dict(assignments or {})
        if not set(values).issubset(allowed_assignments):
            raise ValueError("unsupported Memory clear surface assignment")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cas_row(
                connection,
                operation_id,
                expected_revision,
                execution_token,
                allowed_states=(operation_state,),
                resolution=resolution,
            )
            surface_row = connection.execute(
                """
                SELECT * FROM clear_surface
                WHERE operation_id = ? AND surface = ?
                """,
                (operation_id, surface),
            ).fetchone()
            if surface_row is None:
                raise ClearTransitionError("Memory clear surface is missing")
            if surface_row["state"] not in from_states:
                raise ClearTransitionError("Memory clear surface is in the wrong state")
            now = _utc_now()
            revision = row["revision"] + 1
            columns = ["state = ?", "updated_at = ?"]
            parameters: list[object] = [to_state, now]
            for column, value in values.items():
                columns.append(f"{column} = ?")
                parameters.append(value)
            parameters.extend((operation_id, surface))
            connection.execute(
                f"""
                UPDATE clear_surface SET {', '.join(columns)}
                WHERE operation_id = ? AND surface = ?
                """,
                parameters,
            )
            connection.execute(
                """
                UPDATE clear_operation
                SET updated_at = ?, revision = ?
                WHERE operation_id = ?
                """,
                (now, revision, operation_id),
            )
            self._append_event(
                connection,
                operation_id,
                event,
                row["operator_ref"],
                surface=surface,
                occurred_at=now,
                resulting_revision=revision,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_database_files()
        return self._require_operation(operation_id)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        operation_id: str,
        event: str,
        actor_ref: str,
        *,
        occurred_at: str,
        resulting_revision: int,
        surface: str | None = None,
        closed_error: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO clear_event (
                operation_id, event, actor_ref, surface, occurred_at,
                closed_error, resulting_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                event,
                actor_ref,
                surface,
                occurred_at,
                closed_error,
                resulting_revision,
            ),
        )

    def _harden_database_files(self, *, sync_parent: bool = False) -> None:
        try:
            self._database.harden(sync_parent=sync_parent)
        except ConfinedFilesystemError as error:
            raise MemoryClearJournalError(
                "Memory clear journal files could not be hardened safely"
            ) from error


def _schema_sql() -> str:
    errors = ", ".join(f"'{value}'" for value in sorted(CLOSED_MEMORY_ERROR_CODES))
    return f"""
        CREATE TABLE clear_operation (
            operation_id TEXT PRIMARY KEY,
            operator_ref TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'preparing', 'prepared', 'deleting', 'recovery_needed',
                'completed', 'aborted'
            )),
            recovery_from_state TEXT NULL CHECK (
                recovery_from_state IS NULL OR recovery_from_state IN (
                    'preparing', 'prepared', 'deleting'
                )
            ),
            resolution TEXT NULL CHECK (resolution IS NULL OR resolution IN ('resume', 'abort')),
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT NULL,
            pre_epoch INTEGER NOT NULL CHECK (pre_epoch >= 0),
            target_epoch INTEGER NOT NULL CHECK (target_epoch = pre_epoch + 1),
            snapshot_path TEXT NULL,
            manifest_sha256 TEXT NULL,
            destructive_started INTEGER NOT NULL CHECK (destructive_started IN (0, 1)),
            closed_error TEXT NULL CHECK (closed_error IS NULL OR closed_error IN ({errors})),
            open_slot INTEGER NULL CHECK (
                (state IN ('completed', 'aborted') AND open_slot IS NULL) OR
                (state NOT IN ('completed', 'aborted') AND open_slot = 1)
            ),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            execution_token TEXT NULL
        );

        CREATE UNIQUE INDEX clear_operation_one_open
        ON clear_operation(open_slot) WHERE open_slot = 1;

        CREATE TABLE clear_surface (
            operation_id TEXT NOT NULL REFERENCES clear_operation(operation_id),
            surface TEXT NOT NULL CHECK (surface IN (
                'queue', 'provider', 'call_log', 'attachments'
            )),
            relative_path TEXT NOT NULL,
            relative_snapshot_path TEXT NULL,
            present INTEGER NULL CHECK (present IS NULL OR present IN (0, 1)),
            pre_clear_digest TEXT NULL,
            snapshot_digest TEXT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'pending', 'snapshotted', 'deleted', 'restored'
            )),
            updated_at TEXT NOT NULL,
            CHECK (
                (state = 'pending' AND relative_snapshot_path IS NULL AND
                    present IS NULL AND pre_clear_digest IS NULL AND snapshot_digest IS NULL) OR
                (state != 'pending' AND relative_snapshot_path IS NOT NULL AND (
                    (present = 0 AND pre_clear_digest IS NULL AND snapshot_digest IS NULL) OR
                    (present = 1 AND pre_clear_digest IS NOT NULL AND snapshot_digest IS NOT NULL)
                ))
            ),
            PRIMARY KEY (operation_id, surface)
        );

        CREATE TABLE clear_event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL REFERENCES clear_operation(operation_id),
            event TEXT NOT NULL CHECK (event IN (
                'started', 'surface_snapshotted', 'prepared', 'deleting_started',
                'surface_deleted', 'recovery_needed', 'resume_claimed',
                'abort_claimed', 'surface_restored', 'snapshot_discarded',
                'recovery_claim_failed', 'completed', 'aborted'
            )),
            actor_ref TEXT NOT NULL,
            surface TEXT NULL CHECK (
                surface IS NULL OR surface IN ('queue', 'provider', 'call_log', 'attachments')
            ),
            occurred_at TEXT NOT NULL,
            closed_error TEXT NULL CHECK (closed_error IS NULL OR closed_error IN ({errors})),
            resulting_revision INTEGER NOT NULL CHECK (resulting_revision >= 1)
        );

        CREATE TRIGGER clear_event_no_update
        BEFORE UPDATE ON clear_event
        BEGIN
            SELECT RAISE(ABORT, 'clear events are append-only');
        END;

        CREATE TRIGGER clear_event_no_delete
        BEFORE DELETE ON clear_event
        BEGIN
            SELECT RAISE(ABORT, 'clear events are append-only');
        END;

        CREATE TRIGGER clear_operation_terminal_no_update
        BEFORE UPDATE ON clear_operation
        WHEN OLD.state IN ('completed', 'aborted')
        BEGIN
            SELECT RAISE(ABORT, 'terminal clear operations are immutable');
        END;

        CREATE TRIGGER clear_operation_valid_transition
        BEFORE UPDATE OF state ON clear_operation
        WHEN NEW.state != OLD.state AND NOT (
            (OLD.state = 'preparing' AND NEW.state IN ('prepared', 'recovery_needed')) OR
            (OLD.state = 'prepared' AND NEW.state IN ('deleting', 'recovery_needed')) OR
            (OLD.state = 'deleting' AND NEW.state IN ('completed', 'recovery_needed')) OR
            (OLD.state = 'recovery_needed' AND NEW.state IN (
                'preparing', 'prepared', 'deleting', 'aborted'
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid clear operation transition');
        END;

        CREATE TRIGGER clear_operation_resolution_one_way
        BEFORE UPDATE OF resolution ON clear_operation
        WHEN OLD.resolution IS NOT NULL AND NEW.resolution IS NOT OLD.resolution
        BEGIN
            SELECT RAISE(ABORT, 'clear recovery direction is immutable');
        END;

        CREATE TRIGGER clear_operation_destructive_one_way
        BEFORE UPDATE OF destructive_started ON clear_operation
        WHEN NEW.destructive_started < OLD.destructive_started OR
             (NEW.destructive_started = 1 AND NEW.state NOT IN (
                 'deleting', 'recovery_needed', 'completed', 'aborted'
             ))
        BEGIN
            SELECT RAISE(ABORT, 'clear destructive intent is invalid');
        END;

        CREATE TRIGGER clear_operation_no_delete
        BEFORE DELETE ON clear_operation
        BEGIN
            SELECT RAISE(ABORT, 'clear operation audit is immutable');
        END;

        CREATE TRIGGER clear_surface_terminal_no_insert
        BEFORE INSERT ON clear_surface
        WHEN (SELECT state FROM clear_operation WHERE operation_id = NEW.operation_id)
             IN ('completed', 'aborted')
        BEGIN
            SELECT RAISE(ABORT, 'terminal clear surfaces are immutable');
        END;

        CREATE TRIGGER clear_surface_terminal_no_update
        BEFORE UPDATE ON clear_surface
        WHEN (SELECT state FROM clear_operation WHERE operation_id = OLD.operation_id)
             IN ('completed', 'aborted')
        BEGIN
            SELECT RAISE(ABORT, 'terminal clear surfaces are immutable');
        END;

        CREATE TRIGGER clear_surface_valid_transition
        BEFORE UPDATE OF state ON clear_surface
        WHEN NEW.state != OLD.state AND NOT (
            (OLD.state = 'pending' AND NEW.state = 'snapshotted') OR
            (OLD.state = 'snapshotted' AND NEW.state IN ('deleted', 'restored')) OR
            (OLD.state = 'deleted' AND NEW.state = 'restored')
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid clear surface transition');
        END;

        CREATE TRIGGER clear_surface_no_delete
        BEFORE DELETE ON clear_surface
        BEGIN
            SELECT RAISE(ABORT, 'clear surface audit is immutable');
        END;
    """


def _operation_from_row(row: sqlite3.Row) -> ClearOperation:
    return ClearOperation(
        operation_id=row["operation_id"],
        operator_ref=row["operator_ref"],
        state=row["state"],
        recovery_from_state=row["recovery_from_state"],
        resolution=row["resolution"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        terminal_at=row["terminal_at"],
        pre_epoch=row["pre_epoch"],
        target_epoch=row["target_epoch"],
        snapshot_path=row["snapshot_path"],
        manifest_sha256=row["manifest_sha256"],
        destructive_started=bool(row["destructive_started"]),
        closed_error=row["closed_error"],
        revision=row["revision"],
        execution_token=row["execution_token"],
    )


def _surface_from_row(row: sqlite3.Row) -> ClearSurface:
    return ClearSurface(
        operation_id=row["operation_id"],
        surface=row["surface"],
        relative_path=row["relative_path"],
        relative_snapshot_path=row["relative_snapshot_path"],
        present=None if row["present"] is None else bool(row["present"]),
        pre_clear_digest=row["pre_clear_digest"],
        snapshot_digest=row["snapshot_digest"],
        state=row["state"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> ClearEvent:
    return ClearEvent(
        event_id=row["event_id"],
        operation_id=row["operation_id"],
        event=row["event"],
        actor_ref=row["actor_ref"],
        surface=row["surface"],
        occurred_at=row["occurred_at"],
        closed_error=row["closed_error"],
        resulting_revision=row["resulting_revision"],
    )


def _validated_operation_id(value: str) -> str:
    if not isinstance(value, str) or _OPERATION_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory clear operation id")
    return value


def _validated_actor(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("invalid Memory clear operator reference")
    return value


def _validated_epoch_pair(pre_epoch: int, target_epoch: int) -> None:
    if (
        isinstance(pre_epoch, bool)
        or not isinstance(pre_epoch, int)
        or pre_epoch < 0
        or isinstance(target_epoch, bool)
        or not isinstance(target_epoch, int)
        or target_epoch != pre_epoch + 1
    ):
        raise ValueError("Memory clear target epoch must equal pre epoch plus one")


def _validated_token(value: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory clear execution token")
    return value


def _validated_digest(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory clear SHA-256 digest")
    return value


def _validated_error(value: str) -> str:
    if not is_memory_error_code(value):
        raise ValueError("invalid closed Memory error code")
    return value


def _validated_optional_error(value: str | None) -> str | None:
    return None if value is None else _validated_error(value)


def _validated_surface_name(value: str) -> ClearSurfaceName:
    if value not in _SURFACE_NAMES:
        raise ValueError("invalid Memory clear surface")
    return value  # type: ignore[return-value]


def _relative_to_home(path: Path, home: Path) -> Path:
    try:
        return path.relative_to(home)
    except ValueError as error:
        raise ValueError("Memory clear journal must stay within effective home") from error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
