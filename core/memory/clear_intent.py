"""Durable, idempotent intent marker for Memory Clear.

The marker is deliberately smaller than the former snapshot/journal state
machine.  It records enough information to classify an interrupted destructive
operation and to repeat the four existing high-level deletion primitives.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    PrivateSqliteDatabase,
    ensure_private_directory,
    fsync_directory,
    open_confined_regular_file,
    remove_confined_path,
    replace_confined,
    create_confined_file,
)


ClearIntentState = Literal["deleting", "failed"]
MARKER_RELATIVE_PATH = "state/memory/clear-intent.json"
LEGACY_JOURNAL_RELATIVE_PATH = "state/memory/clear-journal.sqlite"
LEGACY_SNAPSHOT_RELATIVE_PATH = "state/memory/clear-snapshots"
BACKUP_JOURNAL_RELATIVE_PATH = "state/memory/backup-restore-journal.sqlite"
BACKUP_ROOT_RELATIVE_PATH = "state/memory/backups"
MARKER_SCHEMA_VERSION = 1
LEGACY_ABORT_ERROR_CODE = "memory_clear_legacy_abort_unsupported"
MAX_CLEAR_INTENT_BYTES = 16 * 1024
_LEGACY_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LEGACY_BACKUP_OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_LEGACY_BACKUP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LEGACY_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LEGACY_ROWS = 1024
_MAX_LEGACY_FIELD_BYTES = 4096


class ClearIntentError(RuntimeError):
    """Base class for marker and legacy-state failures."""


class ClearIntentUnreadable(ClearIntentError):
    """A marker or legacy clear journal cannot be safely interpreted."""


@dataclass(frozen=True, slots=True)
class ClearSurface:
    surface: Literal["queue", "provider", "call_log", "attachments"]
    relative_path: str


DEFAULT_CLEAR_SURFACES: tuple[ClearSurface, ...] = (
    ClearSurface("queue", "state/memory/memory.sqlite"),
    ClearSurface("provider", "memory/everos-root"),
    ClearSurface("call_log", "memory/call-log/call-log.db"),
    ClearSurface("attachments", "memory/attachments"),
)


@dataclass(frozen=True, slots=True)
class ClearIntent:
    schema_version: int
    operation_id: str
    operator_ref: str
    pre_epoch: int
    target_epoch: int
    state: ClearIntentState
    error_code: str | None
    created_at: str
    updated_at: str

    @classmethod
    def new(cls, *, operator_ref: str, pre_epoch: int) -> "ClearIntent":
        now = _utc_now()
        return cls(
            schema_version=MARKER_SCHEMA_VERSION,
            operation_id=str(uuid.uuid4()),
            operator_ref=operator_ref,
            pre_epoch=pre_epoch,
            target_epoch=pre_epoch + 1,
            state="deleting",
            error_code=None,
            created_at=now,
            updated_at=now,
        )

    def failed(self, error_code: str) -> "ClearIntent":
        return replace(self, state="failed", error_code=error_code, updated_at=_utc_now())

    def deleting(self) -> "ClearIntent":
        return replace(self, state="deleting", error_code=None, updated_at=_utc_now())


class ClearIntentStore:
    """Read and atomically mutate one effective-home-relative marker."""

    def __init__(self, effective_home: Path | str) -> None:
        self.home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
        self.path = self.home / MARKER_RELATIVE_PATH

    def load(self) -> ClearIntent | None:
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ClearIntentUnreadable("Memory clear intent marker is unreadable") from error
        try:
            descriptor = open_confined_regular_file(self.home, self.path)
        except (ConfinedFilesystemError, OSError) as error:
            if not os.path.lexists(self.path):
                # Another process may have completed the terminal clear after
                # our lstat but before this anchored open.
                return None
            raise ClearIntentUnreadable("Memory clear intent marker is unreadable") from error
        try:
            if os.fstat(descriptor).st_size > MAX_CLEAR_INTENT_BYTES:
                raise ValueError("Memory clear intent marker is too large")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, MAX_CLEAR_INTENT_BYTES + 1 - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_CLEAR_INTENT_BYTES:
                    raise ValueError("Memory clear intent marker is too large")
            return _decode(bytes(payload))
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ClearIntentUnreadable("Memory clear intent marker is invalid") from error
        finally:
            os.close(descriptor)

    def write(self, intent: ClearIntent) -> None:
        ensure_private_directory(self.home, self.path.parent)
        temporary = self.path.with_name(f".clear-intent.{secrets.token_hex(8)}.tmp")
        descriptor: int | None = None
        try:
            descriptor = create_confined_file(self.home, temporary)
            payload = json.dumps(_encode(intent), sort_keys=True, separators=(",", ":")).encode()
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("Memory clear intent marker write failed")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            replace_confined(self.home, temporary, self.path)
            fsync_directory(self.path.parent)
        except (ConfinedFilesystemError, OSError) as error:
            raise ClearIntentError("Memory clear intent marker could not be written") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                remove_confined_path(self.home, temporary)
            except (ConfinedFilesystemError, OSError):
                pass

    def remove(self) -> None:
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ClearIntentError("Memory clear intent marker could not be read") from error
        try:
            remove_confined_path(self.home, self.path)
        except (ConfinedFilesystemError, OSError) as error:
            raise ClearIntentError("Memory clear intent marker could not be removed") from error

    def consume_legacy_clear_state(self) -> None:
        """Remove the released clear journal after an explicit replacement Clear."""

        legacy_path = self.home / LEGACY_JOURNAL_RELATIVE_PATH
        try:
            os.lstat(legacy_path)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ClearIntentError("legacy Memory clear journal could not be read") from error
        try:
            remove_confined_path(self.home, legacy_path)
        except (ConfinedFilesystemError, OSError) as error:
            raise ClearIntentError("legacy Memory clear journal could not be removed") from error

    def consume_legacy_snapshots(self) -> None:
        _remove_required(
            self.home,
            self.home / LEGACY_SNAPSHOT_RELATIVE_PATH,
            "legacy Memory clear snapshots",
        )

    def migrate_legacy(self, *, current_epoch: int | None) -> ClearIntent | None:
        """Convert one released clear journal before deleting its old storage."""

        existing = self.load()
        if existing is not None:
            _remove_required(
                self.home,
                self.home / LEGACY_JOURNAL_RELATIVE_PATH,
                "legacy Memory clear journal",
            )
            return existing
        journal_path = self.home / LEGACY_JOURNAL_RELATIVE_PATH
        if not os.path.lexists(journal_path):
            return None
        connection = None
        try:
            connection = PrivateSqliteDatabase(self.home, journal_path).connect()
            rows = []
            cursor = connection.execute("SELECT * FROM clear_operation")
            for row_number, candidate in enumerate(cursor, start=1):
                if row_number > _MAX_LEGACY_ROWS:
                    raise ValueError("legacy Memory clear journal has too many rows")
                _validate_legacy_row_size(candidate)
                rows.append(candidate)

            open_rows = []
            for candidate in rows:
                state = candidate["state"]
                open_slot = candidate["open_slot"]
                started_at = candidate["started_at"]
                if not isinstance(started_at, str) or not started_at.strip():
                    raise ValueError("legacy clear timestamp is invalid")
                operation_id = _validated_legacy_operation_id(candidate["operation_id"])
                _validated_legacy_operator(candidate["operator_ref"])
                if state in {"completed", "aborted"}:
                    if open_slot is not None:
                        raise ValueError("terminal legacy clear row has an open slot")
                    resolution = candidate["resolution"] if "resolution" in candidate.keys() else None
                    _validate_legacy_resolution(state, resolution)
                    _validate_legacy_surfaces(
                        connection,
                        operation_id=operation_id,
                        operation_state=state,
                        resolution=resolution,
                    )
                elif state in {"preparing", "prepared", "deleting", "recovery_needed"}:
                    if (
                        not isinstance(open_slot, int)
                        or isinstance(open_slot, bool)
                        or open_slot != 1
                    ):
                        raise ValueError("nonterminal legacy clear row has no open slot")
                    open_rows.append(candidate)
                else:
                    raise ValueError("legacy Memory clear journal has an invalid state")

            if not open_rows:
                connection.close()
                connection = None
                _remove_required(self.home, journal_path, "legacy Memory clear journal")
                return None
            if len(open_rows) != 1:
                raise ValueError("legacy Memory clear journal has multiple open operations")
            row = open_rows[0]
            operation_id = _validated_legacy_operation_id(row["operation_id"])
            operator_ref = _validated_legacy_operator(row["operator_ref"])
            pre_epoch_value = row["pre_epoch"]
            if (
                not isinstance(pre_epoch_value, int)
                or isinstance(pre_epoch_value, bool)
                or pre_epoch_value < 0
            ):
                raise ValueError("legacy clear pre epoch is invalid")
            pre_epoch = pre_epoch_value
            columns = set(row.keys())
            resolution = row["resolution"] if "resolution" in columns else None
            started_at = row["started_at"]
            if not isinstance(started_at, str) or not started_at.strip():
                raise ValueError("legacy clear timestamp is invalid")
            target_value = row["target_epoch"] if "target_epoch" in columns else None
            if target_value is None:
                # Older released journals did not persist the target. Defer
                # until the Memory store is attached so the two-state replay
                # rule can distinguish a queue clear that already advanced the
                # epoch from one that did not.
                if current_epoch is None:
                    target_epoch = pre_epoch + 1
                else:
                    target_epoch = pre_epoch + 1
                    if current_epoch not in {pre_epoch, target_epoch}:
                        raise ValueError("legacy clear epoch is not replay-safe")
            else:
                if (
                    not isinstance(target_value, int)
                    or isinstance(target_value, bool)
                    or target_value < 0
                ):
                    raise ValueError("legacy clear target epoch is invalid")
                target_epoch = target_value
            if target_epoch != pre_epoch + 1:
                raise ValueError("legacy clear target epoch is invalid")
            state = row["state"]
            if state not in {
                "preparing",
                "prepared",
                "deleting",
                "recovery_needed",
                "completed",
                "aborted",
            }:
                raise ValueError("legacy clear state is invalid")
            if resolution not in {None, "resume", "abort"}:
                raise ValueError("legacy clear resolution is invalid")
            valid_resolution = {
                "preparing": {None, "resume"},
                "prepared": {None, "resume"},
                "deleting": {None, "resume"},
                "recovery_needed": {None, "resume", "abort"},
                "completed": {None, "resume"},
                "aborted": {"abort"},
            }
            if resolution not in valid_resolution[state]:
                raise ValueError("legacy clear resolution is out of state")
            _validate_legacy_surfaces(
                connection,
                operation_id=operation_id,
                operation_state=state,
                resolution=resolution,
            )
            if target_value is None and current_epoch is None:
                return None
            migrated_state = "failed" if resolution == "abort" else "deleting"
            migrated_error = LEGACY_ABORT_ERROR_CODE if resolution == "abort" else None
            now = _utc_now()
            intent = ClearIntent(
                schema_version=MARKER_SCHEMA_VERSION,
                operation_id=operation_id,
                operator_ref=operator_ref,
                pre_epoch=pre_epoch,
                target_epoch=target_epoch,
                state=migrated_state,
                error_code=migrated_error,
                created_at=started_at,
                updated_at=now,
            )
            connection.close()
            connection = None
            self.write(intent)
            _remove_required(self.home, journal_path, "legacy Memory clear journal")
            return intent
        except ClearIntentError:
            raise
        except (
            ConfinedFilesystemError,
            sqlite3.Error,
            OSError,
            UnicodeError,
            KeyError,
            TypeError,
            ValueError,
            IndexError,
        ) as error:
            raise ClearIntentUnreadable("legacy Memory clear journal is unreadable") from error
        finally:
            if connection is not None:
                connection.close()


def cleanup_legacy_backup_storage(effective_home: Path | str) -> tuple[str, ...]:
    home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
    if inspect_legacy_backup_restore(home) == "open":
        # An open restore is still an authority fence. Leave both the journal
        # and its backup tree in place until a supported recovery path exists.
        return (BACKUP_JOURNAL_RELATIVE_PATH, BACKUP_ROOT_RELATIVE_PATH)
    failures: list[str] = []
    for relative in (
        BACKUP_JOURNAL_RELATIVE_PATH,
        BACKUP_ROOT_RELATIVE_PATH,
        LEGACY_SNAPSHOT_RELATIVE_PATH,
    ):
        if not _best_effort_remove(home, home / relative):
            failures.append(relative)
    return tuple(failures)


def inspect_legacy_clear_abort(effective_home: Path | str) -> bool:
    """Return whether a released clear chose Abort and still owns snapshots."""

    home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
    journal_path = home / LEGACY_JOURNAL_RELATIVE_PATH
    if not os.path.lexists(journal_path):
        return False
    connection = None
    try:
        connection = PrivateSqliteDatabase(home, journal_path).connect()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(clear_operation)")
        }
        if "resolution" not in columns:
            return False
        row = connection.execute(
            "SELECT COUNT(*) FROM clear_operation "
            "WHERE open_slot = 1 AND state = 'recovery_needed' AND resolution = 'abort'"
        ).fetchone()
        return row is not None and row[0] == 1
    except (ConfinedFilesystemError, sqlite3.Error, OSError) as error:
        raise ClearIntentUnreadable("legacy Memory clear journal is unreadable") from error
    finally:
        if connection is not None:
            connection.close()


def inspect_legacy_backup_restore(effective_home: Path | str) -> Literal["absent", "terminal", "open"]:
    """Classify the retired backup-restore journal without mutating it."""

    home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
    journal_path = home / BACKUP_JOURNAL_RELATIVE_PATH
    if not os.path.lexists(journal_path):
        return "absent"
    connection = None
    try:
        connection = PrivateSqliteDatabase(home, journal_path).connect()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "backup_restore_operation" not in tables:
            raise ValueError("legacy backup restore journal schema is incomplete")
        open_count = 0
        for row_number, row in enumerate(
            connection.execute("SELECT * FROM backup_restore_operation"), start=1
        ):
            if row_number > _MAX_LEGACY_ROWS:
                raise ValueError("legacy backup restore journal has too many rows")
            _validate_legacy_row_size(row)
            _validate_legacy_backup_row(row)
            state = row["state"]
            open_slot = row["open_slot"]
            if state == "completed":
                if open_slot is not None:
                    raise ValueError("terminal backup restore row has an open slot")
            elif state in {"restoring", "recovery_needed"}:
                if open_slot != 1 or isinstance(open_slot, bool):
                    raise ValueError("open backup restore row has no open slot")
                open_count += 1
            else:
                raise ValueError("legacy backup restore state is invalid")
        if open_count > 1:
            raise ValueError("legacy backup restore journal has multiple open operations")
        return "open" if open_count else "terminal"
    except (ConfinedFilesystemError, sqlite3.Error, OSError, KeyError, TypeError, ValueError) as error:
        raise ClearIntentUnreadable("legacy backup restore journal is unreadable") from error
    finally:
        if connection is not None:
            connection.close()


def _validate_legacy_row_size(row: sqlite3.Row) -> None:
    for value in row:
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_LEGACY_FIELD_BYTES:
            raise ValueError("legacy journal field is too large")
        if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > _MAX_LEGACY_FIELD_BYTES:
            raise ValueError("legacy journal field is too large")


def _validate_legacy_backup_row(row: sqlite3.Row) -> None:
    required = {
        "operation_id",
        "backup_id",
        "manifest_sha256",
        "surface_digests_json",
        "state",
        "started_at",
        "updated_at",
        "terminal_at",
        "attempt_count",
        "last_error",
        "open_slot",
        "revision",
        "execution_token",
    }
    if not required.issubset(row.keys()):
        raise ValueError("legacy backup restore journal schema is incomplete")
    if (
        not isinstance(row["operation_id"], str)
        or _LEGACY_BACKUP_OPERATION_ID_RE.fullmatch(row["operation_id"]) is None
    ):
        raise ValueError("invalid legacy backup restore operation id")
    if (
        not isinstance(row["backup_id"], str)
        or _LEGACY_BACKUP_ID_RE.fullmatch(row["backup_id"]) is None
    ):
        raise ValueError("invalid legacy backup id")
    if (
        not isinstance(row["manifest_sha256"], str)
        or _SHA256_RE.fullmatch(row["manifest_sha256"]) is None
    ):
        raise ValueError("invalid legacy backup manifest digest")
    if not isinstance(row["started_at"], str) or not row["started_at"].strip():
        raise ValueError("invalid legacy backup start time")
    if not isinstance(row["updated_at"], str) or not row["updated_at"].strip():
        raise ValueError("invalid legacy backup update time")
    if (
        not isinstance(row["attempt_count"], int)
        or isinstance(row["attempt_count"], bool)
        or row["attempt_count"] < 1
    ):
        raise ValueError("invalid legacy backup attempt count")
    if row["last_error"] not in {None, "memory_clear_failed"}:
        raise ValueError("invalid legacy backup error")
    state = row["state"]
    terminal_at = row["terminal_at"]
    open_slot = row["open_slot"]
    execution_token = row["execution_token"]
    if state == "completed":
        if open_slot is not None or not isinstance(terminal_at, str) or not terminal_at.strip():
            raise ValueError("invalid completed legacy backup restore state")
        if row["last_error"] is not None or execution_token is not None:
            raise ValueError("completed legacy backup restore has recovery fields")
    elif state == "restoring":
        if open_slot != 1 or isinstance(open_slot, bool) or terminal_at is not None:
            raise ValueError("invalid restoring legacy backup restore state")
        if row["last_error"] is not None or not isinstance(execution_token, str):
            raise ValueError("restoring legacy backup restore has invalid recovery fields")
    elif state == "recovery_needed":
        if open_slot != 1 or isinstance(open_slot, bool) or terminal_at is not None:
            raise ValueError("invalid recovering legacy backup restore state")
        if row["last_error"] != "memory_clear_failed" or execution_token is not None:
            raise ValueError("recovering legacy backup restore has invalid recovery fields")
    else:
        raise ValueError("invalid legacy backup restore state")
    if (
        not isinstance(row["revision"], int)
        or isinstance(row["revision"], bool)
        or row["revision"] < 1
    ):
        raise ValueError("invalid legacy backup revision")
    token = row["execution_token"]
    if token is not None and (
        not isinstance(token, str) or _LEGACY_TOKEN_RE.fullmatch(token) is None
    ):
        raise ValueError("invalid legacy backup execution token")
    try:
        decoded = json.loads(row["surface_digests_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid legacy backup surface digests") from error
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError("invalid legacy backup surface digests")
    for path, digest in decoded.items():
        if not isinstance(path, str) or not path or path.startswith("/") or "\x00" in path:
            raise ValueError("invalid legacy backup surface path")
        if digest is not None and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ValueError("invalid legacy backup surface digest")


def _validated_legacy_operation_id(value: object) -> str:
    if not isinstance(value, str) or _LEGACY_OPERATION_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid legacy Memory clear operation id")
    return value


def _validated_legacy_operator(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("invalid legacy Memory clear operator reference")
    return value


def _validate_legacy_resolution(state: object, resolution: object) -> None:
    if resolution not in {None, "resume", "abort"}:
        raise ValueError("legacy clear resolution is invalid")
    valid_resolution = {
        "completed": {None, "resume"},
        "aborted": {"abort"},
    }
    if resolution not in valid_resolution[state]:
        raise ValueError("legacy clear resolution is out of state")


def _validate_legacy_surfaces(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    operation_state: str,
    resolution: object,
) -> None:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "clear_surface" not in tables:
        raise ValueError("legacy clear journal is missing surface receipts")
    rows = []
    for row_number, row in enumerate(
        connection.execute(
            "SELECT * FROM clear_surface WHERE operation_id = ?", (operation_id,)
        ),
        start=1,
    ):
        if row_number > len(DEFAULT_CLEAR_SURFACES):
            raise ValueError("legacy clear journal has too many surface receipts")
        _validate_legacy_row_size(row)
        rows.append(row)
    if len(rows) != len(DEFAULT_CLEAR_SURFACES):
        raise ValueError("legacy clear journal has incomplete surface receipts")
    expected_paths = {surface.surface: surface.relative_path for surface in DEFAULT_CLEAR_SURFACES}
    states: dict[str, str] = {}
    for row in rows:
        required = {"operation_id", "surface", "relative_path", "state"}
        if not required.issubset(row.keys()):
            raise ValueError("legacy clear surface receipt is incomplete")
        if row["operation_id"] != operation_id:
            raise ValueError("legacy clear surface operation id is invalid")
        surface = row["surface"]
        if not isinstance(surface, str) or surface not in expected_paths or surface in states:
            raise ValueError("legacy clear surface name is invalid")
        if row["relative_path"] != expected_paths[surface]:
            raise ValueError("legacy clear surface path is invalid")
        state = row["state"]
        if state not in {"pending", "snapshotted", "deleted", "restored"}:
            raise ValueError("legacy clear surface state is invalid")
        states[surface] = state
        if "relative_snapshot_path" in row.keys():
            snapshot_path = row["relative_snapshot_path"]
            if state == "pending":
                if snapshot_path is not None:
                    raise ValueError("pending legacy clear surface has a snapshot")
            elif not isinstance(snapshot_path, str) or not snapshot_path:
                raise ValueError("legacy clear surface snapshot is invalid")
        if "present" in row.keys():
            present = row["present"]
            if state == "pending":
                if present is not None:
                    raise ValueError("pending legacy clear surface has presence data")
            elif present not in {0, 1} or isinstance(present, bool):
                raise ValueError("legacy clear surface presence is invalid")
        pre_digest = row["pre_clear_digest"] if "pre_clear_digest" in row.keys() else None
        snapshot_digest = row["snapshot_digest"] if "snapshot_digest" in row.keys() else None
        if state == "pending" and (pre_digest is not None or snapshot_digest is not None):
            raise ValueError("pending legacy clear surface has a digest")
        if state != "pending" and "present" in row.keys():
            if present == 0 and (pre_digest is not None or snapshot_digest is not None):
                raise ValueError("absent legacy clear surface has a digest")
            if present == 1 and (
                not isinstance(pre_digest, str)
                or _SHA256_RE.fullmatch(pre_digest) is None
                or not isinstance(snapshot_digest, str)
                or _SHA256_RE.fullmatch(snapshot_digest) is None
            ):
                raise ValueError("present legacy clear surface is missing digests")
        for digest_name in ("pre_clear_digest", "snapshot_digest"):
            if digest_name not in row.keys():
                continue
            digest = row[digest_name]
            if digest is not None and (
                not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
            ):
                raise ValueError("legacy clear surface digest is invalid")
    state_set = set(states.values())
    if operation_state == "preparing":
        if state_set not in ({"pending"}, {"snapshotted"}):
            raise ValueError("legacy clear preparing receipts are inconsistent")
    elif operation_state == "prepared":
        if state_set != {"snapshotted"}:
            raise ValueError("legacy clear prepared receipts are inconsistent")
    elif operation_state == "deleting":
        if not state_set <= {"snapshotted", "deleted"}:
            raise ValueError("legacy clear deleting receipts are inconsistent")
    elif operation_state == "recovery_needed":
        recovery_from = _legacy_recovery_from_state(connection, operation_id)
        if recovery_from is None and resolution == "abort":
            if state_set not in (
                {"snapshotted"},
                {"snapshotted", "deleted"},
                {"deleted"},
                {"restored"},
            ):
                raise ValueError("legacy clear abort receipts are inconsistent")
            return
        valid_states = {
            "preparing": ({"pending"}, {"snapshotted"}),
            "prepared": ({"snapshotted"},),
            "deleting": (
                {"snapshotted"},
                {"snapshotted", "deleted"},
                {"deleted"},
            ),
        }
        if recovery_from not in valid_states or state_set not in valid_states[recovery_from]:
            raise ValueError("legacy clear recovery receipts contradict their source state")
        if resolution == "abort" and state_set == {"pending"}:
            raise ValueError("legacy clear abort has no prepared surfaces")
    elif operation_state == "completed":
        if state_set != {"deleted"}:
            raise ValueError("legacy clear completed receipts are inconsistent")
    elif operation_state == "aborted" and state_set != {"restored"}:
        raise ValueError("legacy clear aborted receipts are inconsistent")


def _legacy_recovery_from_state(connection: sqlite3.Connection, operation_id: str) -> object:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(clear_operation)")
    }
    if "recovery_from_state" not in columns:
        return None
    row = connection.execute(
        "SELECT recovery_from_state FROM clear_operation WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    return None if row is None else row[0]


def _best_effort_remove(home: Path, path: Path) -> bool:
    try:
        remove_confined_path(home, path)
    except (ConfinedFilesystemError, OSError):
        # A post-unlink directory fsync failure can make the path disappear
        # before the removal is durable. Keep the migration fence in that
        # case instead of inferring success from the current path lookup.
        return False
    return not os.path.lexists(path)


def _remove_required(home: Path, path: Path, description: str) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ClearIntentError(f"{description} could not be inspected") from error
    try:
        remove_confined_path(home, path)
    except (ConfinedFilesystemError, OSError) as error:
        raise ClearIntentError(f"{description} could not be removed durably") from error


def _encode(intent: ClearIntent) -> dict[str, object]:
    return {
        "schema_version": intent.schema_version,
        "operation_id": intent.operation_id,
        "operator_ref": intent.operator_ref,
        "pre_epoch": intent.pre_epoch,
        "target_epoch": intent.target_epoch,
        "state": intent.state,
        "error_code": intent.error_code,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
    }


def _decode(payload: bytes) -> ClearIntent:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("unsupported clear intent schema")
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != MARKER_SCHEMA_VERSION
    ):
        raise ValueError("unsupported clear intent schema")
    required = {"operation_id", "operator_ref", "pre_epoch", "target_epoch", "state", "error_code", "created_at", "updated_at"}
    if set(value) != required | {"schema_version"}:
        raise ValueError("invalid clear intent fields")
    pre_epoch = value["pre_epoch"]
    target_epoch = value["target_epoch"]
    if not isinstance(pre_epoch, int) or isinstance(pre_epoch, bool) or pre_epoch < 0:
        raise ValueError("invalid clear intent pre epoch")
    if not isinstance(target_epoch, int) or isinstance(target_epoch, bool) or target_epoch != pre_epoch + 1:
        raise ValueError("invalid clear intent target epoch")
    operation_id = value["operation_id"]
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("invalid clear intent operation id")
    try:
        parsed_operation_id = uuid.UUID(operation_id)
    except (ValueError, AttributeError, TypeError):
        parsed_operation_id = None
    if parsed_operation_id is not None:
        if parsed_operation_id.version == 4:
            pass
        elif re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise ValueError("clear intent operation id must be UUID4")
    elif _LEGACY_OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid clear intent operation id")
    operator_ref = value["operator_ref"]
    if (
        not isinstance(operator_ref, str)
        or not operator_ref
        or len(operator_ref.encode("utf-8")) > 512
        or any(ord(char) < 0x20 for char in operator_ref)
    ):
        raise ValueError("invalid clear intent operator")
    state = value["state"]
    if state not in {"deleting", "failed"}:
        raise ValueError("invalid clear intent state")
    error_code = value["error_code"]
    if error_code is not None and (
        not isinstance(error_code, str)
        or not error_code
        or len(error_code) > 128
        or any(ord(char) < 0x20 for char in error_code)
    ):
        raise ValueError("invalid clear intent error")
    if state == "deleting" and error_code is not None:
        raise ValueError("deleting clear intent cannot have an error")
    if state == "failed" and (not isinstance(error_code, str) or not error_code):
        raise ValueError("failed clear intent must have an error")
    if error_code == LEGACY_ABORT_ERROR_CODE and state != "failed":
        raise ValueError("legacy abort error requires a failed clear intent")
    for field in ("created_at", "updated_at"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"invalid clear intent {field}")
    return ClearIntent(**value)  # type: ignore[arg-type]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
