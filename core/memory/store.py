"""Identity-only SQLite state for best-effort Memory capture."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from config import paths
from core.memory.confined_filesystem import ConfinedRoot, ensure_private_directory
from core.memory.project_ids import (
    DEFAULT_MEMORY_PROJECT_ID,
    MAX_NAMED_MEMORY_PROJECTS,
    is_legacy_memory_project_id,
    is_named_memory_project_id,
    is_persisted_memory_project_id,
    is_writable_memory_project_id,
)
from core.memory.types import MemoryErrorCode, ProviderSessionRef

MEMORY_STORE_FILENAME = "memory.sqlite"
MEMORY_STORE_DIRNAME = "memory"
MEMORY_STORE_SCHEMA_VERSION = 4


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
    processing_fault_generation: int
    processing_fault_kind: Literal["credential", "engine"] | None
    processing_fault_since: str | None
    processing_alert_active: bool
    processing_recovery_generation: int | None
    processing_recovery_pending_at: str | None
    updated_at: str


@dataclass(frozen=True)
class VolatileAdmission:
    outcome: Literal[
        "accepted", "clear_in_progress", "project_limit", "timestamp_invalid", "store_unavailable"
    ]
    source_message_digest: str | None = None
    provider_session_ref: ProviderSessionRef | None = None
    provider_timestamp_ms: int | None = None
    raw_session_id: str | None = None


def memory_store_path() -> Path:
    return paths.get_state_dir() / MEMORY_STORE_DIRNAME / MEMORY_STORE_FILENAME


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _keyed_digest(scope_key: bytes, value: str) -> str:
    return hmac.new(scope_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _provider_session_ref(
    scope_key: bytes,
    principal_id: str,
    project_ref: str,
    session_id: str,
    epoch: int,
) -> str:
    digest = _keyed_digest(
        scope_key,
        f"{principal_id}:{project_ref}:{session_id}",
    )
    return f"src--{digest}--e{epoch}"


def derive_principal_id(scope_key: bytes, user_key: str) -> str:
    if not isinstance(scope_key, bytes) or len(scope_key) < 16:
        raise ValueError("invalid Memory scope key")
    if not isinstance(user_key, str) or not user_key or user_key != user_key.strip():
        raise ValueError("invalid Memory user key")
    return f"u-{_keyed_digest(scope_key, user_key)[:32]}"


def derive_project_id(scope_key: bytes, workdir: str) -> str:
    if (
        not isinstance(workdir, str)
        or not workdir
        or workdir != workdir.strip()
        or not os.path.isabs(workdir)
        or os.path.abspath(os.path.expanduser(workdir)) != workdir
    ):
        raise ValueError("invalid Memory workdir")
    return f"p-{_keyed_digest(scope_key, workdir)[:32]}"


def is_principal_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 34
        and value.startswith("u-")
        and all(char in "0123456789abcdef" for char in value[2:])
    )


def is_project_id(value: object) -> bool:
    return is_persisted_memory_project_id(value)


def derive_assistant_memory_owner_id(principal_id: str) -> str:
    if not is_principal_id(principal_id):
        raise ValueError("invalid Memory principal")
    return f"{principal_id}-agent"


def is_memory_owner_id(value: object) -> bool:
    return is_principal_id(value) or (
        isinstance(value, str)
        and value.endswith("-agent")
        and is_principal_id(value[:-6])
    )


class MemoryStore:
    """Persist only identity, authority, project catalog, and watermarks."""

    def __init__(self, db_path: Path | None = None, *, effective_home: Path | str | None = None) -> None:
        root = ConfinedRoot.from_home(paths.get_vibe_remote_dir() if effective_home is None else effective_home)
        self._effective_home = root.physical_home
        requested = db_path or root.logical_home / "state" / MEMORY_STORE_DIRNAME / MEMORY_STORE_FILENAME
        self.path = root.confine(requested)
        ensure_private_directory(self._effective_home, self.path.parent)
        self._initialize()

    def ensure_meta(self) -> MemoryMeta:
        with self._transaction() as conn:
            return self._ensure_meta(conn)

    def get_meta(self) -> MemoryMeta | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        return _meta_from_row(row) if row is not None else None

    def principal_for_user_key(self, user_key: str) -> str:
        with self._transaction() as conn:
            return derive_principal_id(self._ensure_meta(conn).scope_key, user_key)

    def project_for_workdir(self, workdir: str) -> str:
        with self._transaction() as conn:
            return derive_project_id(self._ensure_meta(conn).scope_key, workdir)

    def source_message_digest(self, source_message_id: str) -> str:
        if not isinstance(source_message_id, str) or not source_message_id or "\x00" in source_message_id:
            raise ValueError("invalid Memory source message")
        with self._transaction() as conn:
            return _keyed_digest(self._ensure_meta(conn).scope_key, source_message_id)

    def provider_session_ref(
        self,
        *,
        principal_id: str,
        project_ref: str,
        session_id: str,
        memory_owner_id: str | None = None,
    ) -> ProviderSessionRef:
        if not is_principal_id(principal_id) or not is_project_id(project_ref):
            raise ValueError("invalid Memory scope")
        owner = principal_id if memory_owner_id is None else memory_owner_id
        if owner not in {principal_id, derive_assistant_memory_owner_id(principal_id)}:
            raise ValueError("invalid Memory owner")
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ValueError("invalid Memory session")
        with self._transaction() as conn:
            meta = self._ensure_meta(conn)
            if meta.clear_in_progress:
                raise RuntimeError("Memory clear is in progress")
            return ProviderSessionRef(
                principal_id=owner,
                epoch=meta.epoch,
                project_ref=project_ref,
                session_id=_provider_session_ref(meta.scope_key, owner, project_ref, session_id, meta.epoch),
            )

    def admit_volatile_capture(
        self,
        *,
        source_message_id: str,
        session_id: str,
        principal_id: str,
        project_ref: str,
        provenance: Literal["user_input", "agent"],
        occurred_at_ms: int,
        max_provider_timestamp_ms: int,
    ) -> VolatileAdmission:
        if (
            not is_principal_id(principal_id)
            or not is_writable_memory_project_id(project_ref)
            or provenance not in {"user_input", "agent"}
            or not isinstance(source_message_id, str)
            or not source_message_id
            or not isinstance(session_id, str)
            or not session_id
            or "\x00" in source_message_id
            or "\x00" in session_id
        ):
            raise ValueError("invalid Memory capture identity")
        now = utc_now_iso()
        with self._transaction() as conn:
            meta = self._ensure_meta(conn)
            if meta.clear_in_progress:
                return VolatileAdmission("clear_in_progress")
            if is_named_memory_project_id(project_ref):
                named = conn.execute(
                    "SELECT COUNT(*) FROM memory_projects WHERE principal_id = ? AND project_id != ?",
                    (principal_id, DEFAULT_MEMORY_PROJECT_ID),
                ).fetchone()[0]
                exists = conn.execute(
                    "SELECT 1 FROM memory_projects WHERE principal_id = ? AND project_id = ?",
                    (principal_id, project_ref),
                ).fetchone()
                if exists is None and int(named) >= MAX_NAMED_MEMORY_PROJECTS:
                    self._record_skip(conn, "memory_invalid_input", now)
                    return VolatileAdmission("project_limit")
            provider_timestamp = max(int(occurred_at_ms), meta.last_provider_timestamp_ms + 1)
            if provider_timestamp > int(max_provider_timestamp_ms):
                self._record_skip(conn, None, now)
                return VolatileAdmission("timestamp_invalid")
            owner = principal_id if provenance == "user_input" else derive_assistant_memory_owner_id(principal_id)
            ref = ProviderSessionRef(
                principal_id=owner,
                epoch=meta.epoch,
                project_ref=project_ref,
                session_id=_provider_session_ref(meta.scope_key, owner, project_ref, session_id, meta.epoch),
            )
            conn.execute(
                "UPDATE memory_meta SET last_provider_timestamp_ms = ?, updated_at = ? WHERE singleton = 1",
                (provider_timestamp, now),
            )
            conn.execute(
                """INSERT INTO memory_projects(principal_id, project_id, created_at, last_written_at)
                   VALUES(?, ?, ?, ?) ON CONFLICT(principal_id, project_id) DO UPDATE SET last_written_at=excluded.last_written_at""",
                (principal_id, project_ref, now, now),
            )
            return VolatileAdmission(
                "accepted",
                _keyed_digest(meta.scope_key, source_message_id),
                ref,
                provider_timestamp,
                session_id,
            )

    def mark_capture_success(self) -> None:
        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta(conn)
            conn.execute("UPDATE memory_meta SET last_success_at = ?, updated_at = ? WHERE singleton = 1", (now, now))

    def record_capture_skip(self, error: MemoryErrorCode | None) -> None:
        with self._transaction() as conn:
            self._record_skip(conn, error, utc_now_iso())

    def set_last_error(self, error: MemoryErrorCode | None) -> None:
        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta(conn)
            conn.execute("UPDATE memory_meta SET last_error = ?, last_error_at = ?, updated_at = ? WHERE singleton = 1", (error, now, now))

    def list_memory_projects(self, principal_id: str) -> tuple[str, ...]:
        if not is_principal_id(principal_id):
            raise ValueError("invalid Memory principal")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT project_id FROM memory_projects WHERE principal_id = ? AND project_id != ? ORDER BY last_written_at DESC, project_id",
                (principal_id, DEFAULT_MEMORY_PROJECT_ID),
            ).fetchall()
        return (DEFAULT_MEMORY_PROJECT_ID, *(str(row[0]) for row in rows if is_named_memory_project_id(row[0])))

    def clear_in_progress(self) -> bool:
        meta = self.get_meta()
        return bool(meta and meta.clear_in_progress)

    def begin_clear_fence(self) -> MemoryMeta:
        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta(conn)
            conn.execute("UPDATE memory_meta SET clear_in_progress = 1, updated_at = ? WHERE singleton = 1", (now,))
            return self._ensure_meta(conn)

    def release_clear_fence(self) -> MemoryMeta:
        now = utc_now_iso()
        with self._transaction() as conn:
            self._ensure_meta(conn)
            conn.execute("UPDATE memory_meta SET clear_in_progress = 0, updated_at = ? WHERE singleton = 1", (now,))
            return self._ensure_meta(conn)

    def reset_for_clear(self, *, target_epoch: int | None = None, release_clear_fence: bool = True) -> MemoryMeta:
        with self._transaction() as conn:
            meta = self._ensure_meta(conn)
            epoch = meta.epoch + 1 if target_epoch is None else target_epoch
            if epoch not in {meta.epoch, meta.epoch + 1}:
                raise ValueError("Memory clear target epoch does not match current state")
            now = utc_now_iso()
            conn.execute("DELETE FROM memory_projects")
            conn.execute(
                """UPDATE memory_meta SET epoch = ?, clear_in_progress = ?, last_provider_timestamp_ms = 0,
                   missed_count = 0, last_success_at = NULL, last_error = NULL, last_error_at = NULL, updated_at = ?
                   WHERE singleton = 1""",
                (epoch, 0 if release_clear_fence else 1, now),
            )
            return self._ensure_meta(conn)

    def has_provider_data_history(self) -> bool:
        meta = self.get_meta()
        return bool(meta and meta.last_success_at)

    def _record_skip(self, conn: sqlite3.Connection, error: MemoryErrorCode | None, now: str) -> None:
        conn.execute(
            "UPDATE memory_meta SET missed_count = missed_count + 1, last_error = ?, last_error_at = ?, updated_at = ? WHERE singleton = 1",
            (error, now if error else None, now),
        )

    def _ensure_meta(self, conn: sqlite3.Connection) -> MemoryMeta:
        row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        if row is None:
            now = utc_now_iso()
            conn.execute(
                """INSERT INTO memory_meta(singleton, epoch, clear_in_progress, scope_key, provider_root_id,
                   last_provider_timestamp_ms, missed_count, last_success_at, last_error, last_error_at,
                   processing_fault_generation, processing_fault_kind, processing_fault_since,
                   processing_alert_active, processing_recovery_pending_at, processing_recovery_generation, updated_at)
                   VALUES(1, 0, 0, ?, ?, 0, 0, NULL, NULL, NULL, 0, NULL, NULL, 0, NULL, NULL, ?)""",
                (secrets.token_bytes(32), str(uuid.uuid4()), now),
            )
            row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        assert row is not None
        return _meta_from_row(row)

    def _initialize(self) -> None:
        schema_sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = _application_tables(conn)
            if version == MEMORY_STORE_SCHEMA_VERSION and tables == {"memory_meta", "memory_projects"}:
                _verify_schema(conn)
                return
            known = {
                "memory_meta", "memory_projects", "memory_attachment_bundle", "memory_session_flush_state",
                "memory_capture_queue", "memory_flush_settlements",
            }
            if version not in {0, 1, 2, 3} or not tables.issubset(known):
                raise RuntimeError(f"Unsupported Memory store schema version: {version}")
            meta_values = _legacy_meta(conn) if "memory_meta" in tables else None
            projects = _legacy_projects(conn, tables)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in sorted(tables, reverse=True):
                    conn.execute(f'DROP TABLE "{table.replace(chr(34), chr(34) * 2)}"')
                _execute_sql_script(conn, schema_sql)
                now = utc_now_iso()
                if meta_values is not None:
                    conn.execute(
                        """INSERT INTO memory_meta(singleton, epoch, clear_in_progress, scope_key, provider_root_id,
                           last_provider_timestamp_ms, missed_count, last_success_at, last_error, last_error_at,
                           processing_fault_generation, processing_fault_kind, processing_fault_since,
                           processing_alert_active, processing_recovery_pending_at, processing_recovery_generation, updated_at)
                           VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, 0, NULL, NULL, ?)""",
                        meta_values + (now,),
                    )
                conn.execute(f"PRAGMA user_version = {MEMORY_STORE_SCHEMA_VERSION}")
                for principal, project, created, written in projects:
                    if is_principal_id(principal) and is_persisted_memory_project_id(project):
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_projects(principal_id, project_id, created_at, last_written_at) VALUES(?, ?, ?, ?)",
                            (principal, project, created or now, written or created or now),
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            _verify_schema(conn)
        finally:
            conn.close()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
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


def _application_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not str(row[0]).startswith("sqlite_")
    }


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def _verify_schema(conn: sqlite3.Connection) -> None:
    tables = _application_tables(conn)
    if tables != {"memory_meta", "memory_projects"}:
        raise RuntimeError("Memory store schema is incomplete")
    check = conn.execute("PRAGMA quick_check").fetchone()
    if check is None or check[0] != "ok":
        raise RuntimeError("Memory store failed integrity check")


def _legacy_meta(conn: sqlite3.Connection) -> tuple[int, int, bytes, str, int, int, str | None, str | None, str | None] | None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_meta)")}
    row = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
    if row is None:
        return None
    def value(name: str, default: object = None, *aliases: str) -> object:
        for candidate in (name, *aliases):
            if candidate in columns:
                return row[candidate]
        return default

    scope_key = value("scope_key")
    if scope_key is None:
        raise RuntimeError("legacy Memory metadata has no scope identity")
    provider_root_id = value("provider_root_id", "")
    return (
        int(value("epoch", 0)),
        int(value("clear_in_progress", 0)),
        bytes(scope_key),
        str(provider_root_id),
        int(value("last_provider_timestamp_ms", 0, "last_provider_timestamp")),
        int(value("missed_count", 0)),
        str(value("last_success_at")) if value("last_success_at") is not None else None,
        str(value("last_error")) if value("last_error") is not None else None,
        str(value("last_error_at")) if value("last_error_at") is not None else None,
    )


def _legacy_projects(conn: sqlite3.Connection, tables: set[str]) -> list[tuple[str, str, str | None, str | None]]:
    rows: list[tuple[str, str, str | None, str | None]] = []
    if "memory_projects" in tables:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_projects)")}
        if {"principal_id", "project_id"}.issubset(columns):
            created = "created_at" if "created_at" in columns else "NULL"
            written = "last_written_at" if "last_written_at" in columns else created
            for row in conn.execute(
                f"SELECT principal_id, project_id, {created}, {written} FROM memory_projects"
            ):
                rows.append((str(row[0]), str(row[1]), str(row[2]), str(row[3])))
    if not rows and "memory_capture_queue" in tables:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_capture_queue)")}
        if {"principal_id", "project_ref"}.issubset(columns):
            created = "created_at" if "created_at" in columns else "NULL"
            for row in conn.execute(f"SELECT principal_id, project_ref, MIN({created}), MAX({created}) FROM memory_capture_queue GROUP BY principal_id, project_ref"):
                rows.append((str(row[0]), str(row[1]), str(row[2]), str(row[3])))
    return rows


def _meta_from_row(row: sqlite3.Row) -> MemoryMeta:
    return MemoryMeta(
        epoch=int(row["epoch"]),
        clear_in_progress=bool(row["clear_in_progress"]),
        scope_key=bytes(row["scope_key"]),
        provider_root_id=str(row["provider_root_id"]),
        last_provider_timestamp_ms=int(row["last_provider_timestamp_ms"]),
        missed_count=int(row["missed_count"]),
        last_success_at=str(row["last_success_at"]) if row["last_success_at"] is not None else None,
        last_error=row["last_error"],
        last_error_at=str(row["last_error_at"]) if row["last_error_at"] is not None else None,
        processing_fault_generation=int(row["processing_fault_generation"]),
        processing_fault_kind=row["processing_fault_kind"],
        processing_fault_since=row["processing_fault_since"],
        processing_alert_active=bool(row["processing_alert_active"]),
        processing_recovery_generation=(int(row["processing_recovery_generation"]) if row["processing_recovery_generation"] is not None else None),
        processing_recovery_pending_at=row["processing_recovery_pending_at"],
        updated_at=str(row["updated_at"]),
    )


def _valid_bundle_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)
