"""Durable fence for crash-recoverable ordinary Memory backup restores."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    PrivateSqliteDatabase,
)
from core.memory.snapshot import MemorySnapshot


BackupRestoreState = Literal["restoring", "recovery_needed", "completed"]

_OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_SCHEMA_VERSION = 1


class MemoryBackupRestoreJournalError(RuntimeError):
    """Base class for refused or failed backup-restore journal operations."""


class BackupRestoreConflict(MemoryBackupRestoreJournalError):
    """A different operation already owns the durable restore fence."""


class BackupRestoreCASMismatch(MemoryBackupRestoreJournalError):
    """The caller no longer owns the restore operation revision."""


@dataclass(frozen=True, slots=True)
class BackupRestoreOperation:
    operation_id: str
    backup_id: str
    manifest_sha256: str
    surface_digests: tuple[tuple[str, str | None], ...]
    state: BackupRestoreState
    started_at: str
    updated_at: str
    terminal_at: str | None
    attempt_count: int
    last_error: str | None
    revision: int
    execution_token: str | None

    def digest_mapping(self) -> dict[str, str | None]:
        return dict(self.surface_digests)


@dataclass(frozen=True, slots=True)
class BackupRestoreEvent:
    event_id: int
    operation_id: str
    event: str
    actor_ref: str
    occurred_at: str
    resulting_revision: int
    error_code: str | None


class MemoryBackupRestoreJournal:
    """One-at-a-time durable intent and audit log for ordinary restores."""

    def __init__(
        self,
        effective_home: Path | str,
        *,
        database_path: Path | str = "state/memory/backup-restore-journal.sqlite",
    ) -> None:
        self._effective_home = Path(
            os.path.abspath(os.path.expanduser(os.fspath(effective_home)))
        )
        database = Path(database_path)
        if database.is_absolute():
            self._database_path = Path(os.path.abspath(database))
            self._database_path.relative_to(self._effective_home)
        else:
            if database.as_posix().startswith("../") or database.as_posix().startswith("/"):
                raise ValueError("invalid backup restore journal path")
            self._database_path = self._effective_home / database
        self._database = PrivateSqliteDatabase(self._effective_home, self._database_path)
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def start(self, snapshot: MemorySnapshot) -> BackupRestoreOperation:
        """Publish restart-visible restore intent before the first replacement."""

        backup_id = _validated_snapshot_id(snapshot.snapshot_id)
        manifest = _validated_sha256(snapshot.manifest_sha256)
        digests = _normalized_digests(snapshot.surface_digests())
        operation_id = secrets.token_hex(16)
        token = secrets.token_hex(16)
        now = _utc_now()
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO backup_restore_operation (
                        operation_id, backup_id, manifest_sha256, surface_digests_json,
                        state, started_at, updated_at, attempt_count, open_slot,
                        revision, execution_token
                    ) VALUES (?, ?, ?, ?, 'restoring', ?, ?, 1, 1, 1, ?)
                    """,
                    (
                        operation_id,
                        backup_id,
                        manifest,
                        _encode_digests(digests),
                        now,
                        now,
                        token,
                    ),
                )
                self._append_event(
                    connection,
                    operation_id,
                    "started",
                    "system:runtime",
                    occurred_at=now,
                    resulting_revision=1,
                )
        except sqlite3.IntegrityError as error:
            raise BackupRestoreConflict("a Memory backup restore is already open") from error
        return self._require_operation(operation_id)

    def get_operation(self, operation_id: str) -> BackupRestoreOperation | None:
        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM backup_restore_operation WHERE operation_id = ?",
                (identifier,),
            ).fetchone()
        finally:
            connection.close()
        return _operation_from_row(row) if row is not None else None

    def get_open_operation(self) -> BackupRestoreOperation | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM backup_restore_operation WHERE open_slot = 1"
            ).fetchone()
        finally:
            connection.close()
        return _operation_from_row(row) if row is not None else None

    def get_events(self, operation_id: str) -> tuple[BackupRestoreEvent, ...]:
        identifier = _validated_operation_id(operation_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM backup_restore_event WHERE operation_id = ? ORDER BY event_id",
                (identifier,),
            ).fetchall()
        finally:
            connection.close()
        if not rows and self.get_operation(identifier) is None:
            raise MemoryBackupRestoreJournalError("backup restore operation not found")
        return tuple(_event_from_row(row) for row in rows)

    def assert_idle(self) -> None:
        operation = self.get_open_operation()
        if operation is not None:
            raise BackupRestoreConflict(
                f"Memory backup operation is blocked by restore {operation.operation_id!r}"
            )

    def mark_boot_recovery_needed(self) -> BackupRestoreOperation | None:
        """Release a dead process claim while retaining the restore fence."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM backup_restore_operation WHERE open_slot = 1"
            ).fetchone()
            if row is None:
                return None
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE backup_restore_operation
                SET state = 'recovery_needed', updated_at = ?, last_error = ?,
                    revision = ?, execution_token = NULL
                WHERE operation_id = ?
                """,
                (now, "memory_clear_failed", revision, row["operation_id"]),
            )
            self._append_event(
                connection,
                row["operation_id"],
                "recovery_needed",
                "system:boot",
                occurred_at=now,
                resulting_revision=revision,
                error_code="memory_clear_failed",
            )
            identifier = row["operation_id"]
        return self._require_operation(identifier)

    def claim_retry(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        actor_ref: str,
    ) -> BackupRestoreOperation:
        identifier = _validated_operation_id(operation_id)
        actor = _validated_actor(actor_ref)
        with self._transaction() as connection:
            row = self._require_row(
                connection,
                identifier,
                expected_revision=expected_revision,
                allowed_states=("recovery_needed",),
                require_token=False,
            )
            now = _utc_now()
            revision = row["revision"] + 1
            token = secrets.token_hex(16)
            connection.execute(
                """
                UPDATE backup_restore_operation
                SET state = 'restoring', updated_at = ?, attempt_count = attempt_count + 1,
                    last_error = NULL, revision = ?, execution_token = ?
                WHERE operation_id = ?
                """,
                (now, revision, token, identifier),
            )
            self._append_event(
                connection,
                identifier,
                "retry_started",
                actor,
                occurred_at=now,
                resulting_revision=revision,
            )
        return self._require_operation(identifier)

    def mark_recovery_needed(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
    ) -> BackupRestoreOperation:
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            execution_token=execution_token,
            next_state="recovery_needed",
            event="recovery_needed",
            actor_ref="system:runtime",
            terminal=False,
            error_code="memory_clear_failed",
        )

    def mark_completed(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        actor_ref: str = "system:runtime",
    ) -> BackupRestoreOperation:
        return self._transition(
            operation_id,
            expected_revision=expected_revision,
            execution_token=execution_token,
            next_state="completed",
            event="completed",
            actor_ref=actor_ref,
            terminal=True,
            error_code=None,
        )

    def _transition(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        next_state: BackupRestoreState,
        event: str,
        actor_ref: str,
        terminal: bool,
        error_code: str | None,
    ) -> BackupRestoreOperation:
        identifier = _validated_operation_id(operation_id)
        token = _validated_token(execution_token)
        actor = _validated_actor(actor_ref)
        with self._transaction() as connection:
            row = self._require_row(
                connection,
                identifier,
                expected_revision=expected_revision,
                allowed_states=("restoring",),
                require_token=True,
                execution_token=token,
            )
            now = _utc_now()
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE backup_restore_operation
                SET state = ?, updated_at = ?, terminal_at = ?, last_error = ?,
                    open_slot = ?, revision = ?, execution_token = NULL
                WHERE operation_id = ?
                """,
                (
                    next_state,
                    now,
                    now if terminal else None,
                    error_code,
                    None if terminal else 1,
                    revision,
                    identifier,
                ),
            )
            self._append_event(
                connection,
                identifier,
                event,
                actor,
                occurred_at=now,
                resulting_revision=revision,
                error_code=error_code,
            )
        return self._require_operation(identifier)

    @staticmethod
    def _require_row(
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        expected_revision: int,
        allowed_states: tuple[str, ...],
        require_token: bool,
        execution_token: str | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM backup_restore_operation WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise MemoryBackupRestoreJournalError("backup restore operation not found")
        if row["revision"] != expected_revision or row["state"] not in allowed_states:
            raise BackupRestoreCASMismatch("Memory backup restore claim is stale")
        if require_token and row["execution_token"] != execution_token:
            raise BackupRestoreCASMismatch("Memory backup restore execution token is stale")
        if not require_token and row["execution_token"] is not None:
            raise BackupRestoreCASMismatch("Memory backup restore is still claimed")
        return row

    def _require_operation(self, operation_id: str) -> BackupRestoreOperation:
        operation = self.get_operation(operation_id)
        if operation is None:
            raise MemoryBackupRestoreJournalError("backup restore operation not found")
        return operation

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        operation_id: str,
        event: str,
        actor_ref: str,
        *,
        occurred_at: str,
        resulting_revision: int,
        error_code: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO backup_restore_event (
                operation_id, event, actor_ref, occurred_at,
                resulting_revision, error_code
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                event,
                actor_ref,
                occurred_at,
                resulting_revision,
                error_code,
            ),
        )

    def _initialize(self) -> None:
        try:
            self._database.prepare()
        except ConfinedFilesystemError as error:
            raise MemoryBackupRestoreJournalError(
                "Memory backup restore journal could not be prepared safely"
            ) from error
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, _SCHEMA_VERSION}:
                raise MemoryBackupRestoreJournalError(
                    "unsupported Memory backup restore journal schema"
                )
            if version == 0:
                connection.executescript(_schema_sql())
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                connection.commit()
            else:
                required = {"backup_restore_operation", "backup_restore_event"}
                present = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if not required.issubset(present):
                    raise MemoryBackupRestoreJournalError(
                        "Memory backup restore journal schema is incomplete"
                    )
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise MemoryBackupRestoreJournalError(
                    "Memory backup restore journal failed integrity check"
                )
        except sqlite3.Error as error:
            raise MemoryBackupRestoreJournalError(
                "Memory backup restore journal could not be initialized"
            ) from error
        finally:
            connection.close()
            self._harden_database_files(sync_parent=True)

    def _connect(self) -> sqlite3.Connection:
        try:
            return self._database.connect()
        except ConfinedFilesystemError as error:
            raise MemoryBackupRestoreJournalError(
                "Memory backup restore journal is unsafe"
            ) from error

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._database.transaction(
            translate_connect_error=lambda _error: MemoryBackupRestoreJournalError(
                "Memory backup restore journal is unsafe"
            ),
            translate_harden_error=lambda _error: MemoryBackupRestoreJournalError(
                "Memory backup restore journal files could not be hardened safely"
            ),
        )

    def _harden_database_files(self, *, sync_parent: bool = False) -> None:
        try:
            self._database.harden(sync_parent=sync_parent)
        except ConfinedFilesystemError as error:
            raise MemoryBackupRestoreJournalError(
                "Memory backup restore journal files could not be hardened safely"
            ) from error


def _schema_sql() -> str:
    return """
        CREATE TABLE backup_restore_operation (
            operation_id TEXT PRIMARY KEY,
            backup_id TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            surface_digests_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'restoring', 'recovery_needed', 'completed'
            )),
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            terminal_at TEXT NULL,
            attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
            last_error TEXT NULL CHECK (
                last_error IS NULL OR last_error = 'memory_clear_failed'
            ),
            open_slot INTEGER NULL CHECK (
                (state = 'completed' AND open_slot IS NULL) OR
                (state != 'completed' AND open_slot = 1)
            ),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            execution_token TEXT NULL
        );

        CREATE UNIQUE INDEX backup_restore_one_open
        ON backup_restore_operation(open_slot) WHERE open_slot = 1;

        CREATE TABLE backup_restore_event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL REFERENCES backup_restore_operation(operation_id),
            event TEXT NOT NULL CHECK (event IN (
                'started', 'recovery_needed', 'retry_started', 'completed'
            )),
            actor_ref TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            resulting_revision INTEGER NOT NULL CHECK (resulting_revision >= 1),
            error_code TEXT NULL CHECK (
                error_code IS NULL OR error_code = 'memory_clear_failed'
            )
        );

        CREATE TRIGGER backup_restore_event_no_update
        BEFORE UPDATE ON backup_restore_event
        BEGIN
            SELECT RAISE(ABORT, 'backup restore events are append-only');
        END;

        CREATE TRIGGER backup_restore_event_no_delete
        BEFORE DELETE ON backup_restore_event
        BEGIN
            SELECT RAISE(ABORT, 'backup restore events are append-only');
        END;

        CREATE TRIGGER backup_restore_terminal_no_update
        BEFORE UPDATE ON backup_restore_operation
        WHEN OLD.state = 'completed'
        BEGIN
            SELECT RAISE(ABORT, 'terminal backup restore operations are immutable');
        END;

        CREATE TRIGGER backup_restore_valid_transition
        BEFORE UPDATE OF state ON backup_restore_operation
        WHEN NEW.state != OLD.state AND NOT (
            (OLD.state = 'restoring' AND NEW.state IN ('recovery_needed', 'completed')) OR
            (OLD.state = 'recovery_needed' AND NEW.state = 'restoring')
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid backup restore transition');
        END;
    """


def _operation_from_row(row: sqlite3.Row) -> BackupRestoreOperation:
    return BackupRestoreOperation(
        operation_id=_validated_operation_id(row["operation_id"]),
        backup_id=_validated_snapshot_id(row["backup_id"]),
        manifest_sha256=_validated_sha256(row["manifest_sha256"]),
        surface_digests=_decode_digests(row["surface_digests_json"]),
        state=row["state"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        terminal_at=row["terminal_at"],
        attempt_count=row["attempt_count"],
        last_error=row["last_error"],
        revision=row["revision"],
        execution_token=(
            None if row["execution_token"] is None else _validated_token(row["execution_token"])
        ),
    )


def _event_from_row(row: sqlite3.Row) -> BackupRestoreEvent:
    return BackupRestoreEvent(
        event_id=row["event_id"],
        operation_id=_validated_operation_id(row["operation_id"]),
        event=row["event"],
        actor_ref=row["actor_ref"],
        occurred_at=row["occurred_at"],
        resulting_revision=row["resulting_revision"],
        error_code=row["error_code"],
    )


def _normalized_digests(
    values: Mapping[str, str | None],
) -> tuple[tuple[str, str | None], ...]:
    normalized: list[tuple[str, str | None]] = []
    for path, digest in sorted(values.items()):
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in Path(path).parts)
        ):
            raise ValueError("invalid Memory backup surface path")
        normalized.append((path, None if digest is None else _validated_sha256(digest)))
    if not normalized:
        raise ValueError("Memory backup restore requires surface digests")
    return tuple(normalized)


def _encode_digests(values: tuple[tuple[str, str | None], ...]) -> str:
    return json.dumps(dict(values), sort_keys=True, separators=(",", ":"))


def _decode_digests(value: str) -> tuple[tuple[str, str | None], ...]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise MemoryBackupRestoreJournalError("invalid restore surface digests") from error
    if not isinstance(decoded, dict):
        raise MemoryBackupRestoreJournalError("invalid restore surface digests")
    try:
        return _normalized_digests(decoded)
    except ValueError as error:
        raise MemoryBackupRestoreJournalError("invalid restore surface digests") from error


def _validated_operation_id(value: str) -> str:
    if not isinstance(value, str) or _OPERATION_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid backup restore operation id")
    return value


def _validated_snapshot_id(value: str) -> str:
    if not isinstance(value, str) or _SNAPSHOT_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory backup id")
    return value


def _validated_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("invalid Memory backup SHA-256 digest")
    return value


def _validated_token(value: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError("invalid backup restore execution token")
    return value


def _validated_actor(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 256:
        raise ValueError("invalid backup restore actor")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
