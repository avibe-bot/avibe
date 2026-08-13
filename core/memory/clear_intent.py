"""Durable, idempotent intent marker for Memory Clear.

The marker is deliberately smaller than the former snapshot/journal state
machine.  It records enough information to classify an interrupted destructive
operation and to repeat the four existing high-level deletion primitives.
"""

from __future__ import annotations

import json
import os
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
LEGACY_RERUN_ERROR_CODE = "memory_clear_legacy_state_requires_rerun"
MAX_CLEAR_INTENT_BYTES = 16 * 1024


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

    def reconcile_legacy(self, *, current_epoch: int) -> ClearIntent | None:
        """Fence any legacy open state without interpreting legacy semantics."""

        existing = self.load()
        journal_path = self.home / LEGACY_JOURNAL_RELATIVE_PATH
        if existing is not None:
            _best_effort_remove(self.home, journal_path)
            return existing
        if not os.path.lexists(journal_path):
            return None

        requires_rerun = False
        connection: sqlite3.Connection | None = None
        try:
            connection = PrivateSqliteDatabase(self.home, journal_path).connect_read_only()
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM clear_operation "
                "WHERE open_slot IS NOT NULL LIMIT 1)"
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("legacy clear probe returned no row")
            requires_rerun = bool(row[0])
        except (ConfinedFilesystemError, sqlite3.Error, OSError, TypeError, ValueError):
            requires_rerun = True
        finally:
            if connection is not None:
                connection.close()

        intent = None
        if requires_rerun:
            intent = ClearIntent.new(
                operator_ref="legacy-clear-journal",
                pre_epoch=current_epoch,
            ).failed(LEGACY_RERUN_ERROR_CODE)
            self.write(intent)
        _best_effort_remove(self.home, journal_path)
        return intent


def cleanup_legacy_backup_storage(effective_home: Path | str) -> tuple[str, ...]:
    home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
    failures: list[str] = []
    for relative in (
        BACKUP_JOURNAL_RELATIVE_PATH,
        BACKUP_ROOT_RELATIVE_PATH,
        LEGACY_SNAPSHOT_RELATIVE_PATH,
    ):
        if not _best_effort_remove(home, home / relative):
            failures.append(relative)
    return tuple(failures)


def _best_effort_remove(home: Path, path: Path) -> bool:
    try:
        remove_confined_path(home, path)
    except (ConfinedFilesystemError, OSError):
        return False
    return not os.path.lexists(path)


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
        raise ValueError("clear intent operation id must be UUID4") from None
    if parsed_operation_id.version != 4:
        raise ValueError("clear intent operation id must be UUID4")
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
    for field in ("created_at", "updated_at"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"invalid clear intent {field}")
    return ClearIntent(**value)  # type: ignore[arg-type]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
