"""Authorization-scoped, best-effort diagnostics projections.

Provider Call Log and Processing Record remain independent sources. Memory
delivery state is volatile and is deliberately absent from this reader.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

from core.memory.native_processing_record import NativeProcessingRecordReader
from core.memory.processing_record import (
    ProcessingSourceObservations,
    ProviderCheckProjection,
    SourceObservation,
)
from core.memory.project_ids import is_persisted_memory_project_id
from core.memory.secret_scrubber import scrub_json as _scrub_json
from core.memory.secret_scrubber import scrub_text as _scrub_text
from core.memory.store import (
    derive_assistant_memory_owner_id,
    is_memory_owner_id,
    is_principal_id,
)

MemoryReadScope: TypeAlias = tuple[str, str]

_CALL_COLUMNS = """
    id, started_at_ms, duration_ms, kind, stage, model, status, error,
    finish_reason, prompt_tokens, completion_tokens, request_json,
    response_json, request_bytes, response_bytes, memcell_id, project_id,
    owner_id, parent_type, parent_id, dropped_before
"""
_MEMCELL_COLUMNS = """
    memcell_id, app_id, project_id, message_ids_json, sender_ids_json,
    payload_json, timestamp
"""
_MAX_PAYLOAD_BYTES = 12_000
_MAX_ERROR_BYTES = 1_024
_MAX_CURSOR_BYTES = 256
_MAX_MEMCELL_ID_BYTES = 256
_MAX_MESSAGE_IDS_BYTES = 16_000
_MAX_SENDER_IDS_BYTES = 1_024
_MAX_PAYLOAD_JSON_BYTES = 64_000
_APP_ID = "avibe"
_MEMCELL_TIMESTAMP_SQL = (
    "CASE WHEN typeof(timestamp) IN ('integer', 'real') "
    "THEN MAX(0, CAST(timestamp AS INTEGER)) "
    "ELSE MAX(0, COALESCE("
    "CAST(strftime('%s', timestamp) AS INTEGER) * 1000 + "
    "CASE WHEN instr(timestamp, '.') > 0 THEN "
    "CAST(CAST('0.' || substr(timestamp, instr(timestamp, '.') + 1) AS REAL) "
    "* 1000 AS INTEGER) ELSE 0 END, 0)) END"
)


@dataclass(frozen=True, slots=True)
class MemoryInsightPaths:
    everos_root: Path
    capture_db_path: Path
    call_log_db_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "everos_root", Path(self.everos_root))
        object.__setattr__(self, "capture_db_path", Path(self.capture_db_path))
        object.__setattr__(self, "call_log_db_path", Path(self.call_log_db_path))

    @property
    def system_db_path(self) -> Path:
        return self.everos_root / ".index" / "sqlite" / "system.db"

    @property
    def ome_db_path(self) -> Path:
        return self.everos_root / ".index" / "sqlite" / "ome.db"


class MemoryInsightReader:
    """Read diagnostics without joining or inferring from volatile captures."""

    def __init__(
        self,
        paths: MemoryInsightPaths,
        *,
        provider_base_urls: tuple[str, ...] | list[str] = (),
        exact_redaction_values: tuple[str, ...] | list[str] = (),
    ) -> None:
        if isinstance(provider_base_urls, str) or any(
            not isinstance(value, str) for value in provider_base_urls
        ):
            raise TypeError("provider_base_urls must be a sequence of strings")
        if isinstance(exact_redaction_values, str) or any(
            not isinstance(value, str) for value in exact_redaction_values
        ):
            raise TypeError("exact_redaction_values must be a sequence of strings")
        self._paths = paths
        self._provider_base_urls = tuple(
            value.rstrip("/") for value in provider_base_urls if value
        )
        self._redactions = tuple(
            sorted(
                (value for value in exact_redaction_values if value),
                key=len,
                reverse=True,
            )
        )
        self._native = NativeProcessingRecordReader(
            paths.everos_root,
            provider_base_urls=self._provider_base_urls,
            exact_redaction_values=self._redactions,
        )

    def source_observation(self) -> ProcessingSourceObservations:
        return self._native.source_observation()

    def list_processing_records(
        self, scope: MemoryReadScope, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        return self._native.list_records(scope, cursor, limit)

    def processing_record_detail(
        self, scope: MemoryReadScope, memcell_id: str
    ) -> dict[str, Any]:
        return self._native.record_detail(scope, memcell_id)

    def installation_preflight_calls(self) -> ProviderCheckProjection:
        observed = _utc_now()
        with _read_only(self._paths.call_log_db_path) as conn:
            if conn is None:
                return ProviderCheckProjection(
                    SourceObservation(
                        "unavailable", observed, "provider_call_log_unavailable"
                    ),
                    (),
                )
            try:
                _validate_provider_call_source(conn)
                rows = conn.execute(
                    f"SELECT {_CALL_COLUMNS} FROM provider_call "
                    "WHERE stage = 'processing_preflight' "
                    "ORDER BY started_at_ms DESC, id DESC LIMIT 20"
                ).fetchall()
            except sqlite3.Error:
                return ProviderCheckProjection(
                    SourceObservation(
                        "unavailable", observed, "provider_call_log_unavailable"
                    ),
                    (),
                )
        return ProviderCheckProjection(
            SourceObservation("available", observed),
            tuple(self._call_projection(row) for row in rows),
        )

    def list_unlinked_calls(
        self, scope: MemoryReadScope, limit: int
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        return self._list_unlinked_calls(
            limit,
            principal_id=principal_id,
            project_id=project_id,
        )

    def list_admin_unlinked_calls(self, limit: int) -> dict[str, Any]:
        return self._list_unlinked_calls(limit)

    def _list_unlinked_calls(
        self,
        limit: int,
        *,
        principal_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        _validate_limit(limit, 20)
        observed = _utc_now()
        call_source = _call_source_status(self._paths.call_log_db_path, observed)
        rows: list[sqlite3.Row] = []
        if call_source.status == "available":
            try:
                with _read_only(self._paths.call_log_db_path) as conn:
                    if conn is None:
                        raise sqlite3.OperationalError("call log unavailable")
                    _validate_provider_call_source(conn)
                    if principal_id is not None:
                        rows = list(
                            conn.execute(
                                f"SELECT {_CALL_COLUMNS} FROM provider_call "
                                "WHERE memcell_id IS NULL "
                                "AND parent_type IS NOT 'memcell' AND project_id = ? "
                                "AND owner_id IN (?, ?) "
                                "ORDER BY started_at_ms DESC, id DESC LIMIT ?",
                                (
                                    project_id,
                                    principal_id,
                                    derive_assistant_memory_owner_id(principal_id),
                                    limit + 1,
                                ),
                            )
                        )
                    else:
                        conn.create_function(
                            "memory_owner_valid",
                            1,
                            lambda value: int(is_memory_owner_id(value)),
                            deterministic=True,
                        )
                        conn.create_function(
                            "memory_project_valid",
                            1,
                            lambda value: int(
                                is_persisted_memory_project_id(value)
                            ),
                            deterministic=True,
                        )
                        rows = list(
                            conn.execute(
                                f"SELECT {_CALL_COLUMNS} FROM provider_call "
                                "WHERE memcell_id IS NULL "
                                "AND parent_type IS NOT 'memcell' "
                                "AND memory_owner_valid(owner_id) = 1 "
                                "AND memory_project_valid(project_id) = 1 "
                                "ORDER BY started_at_ms DESC, id DESC LIMIT ?",
                                (limit + 1,),
                            )
                        )
            except sqlite3.Error:
                call_source = SourceObservation(
                    "unavailable", observed, "provider_call_log_unavailable"
                )
                rows = []

        calls: list[dict[str, Any]] = []
        for row in rows[:limit]:
            scope = _authorized_call_scope(row)
            if scope is None:
                continue
            caller_principal, row_project = scope
            if principal_id is not None and (
                caller_principal != principal_id or row_project != project_id
            ):
                continue
            calls.append(
                {
                    **self._call_projection(row),
                    "principal_id": caller_principal,
                    "project_id": row_project,
                }
            )
        return {
            "status": "ok",
            "calls": calls,
            "truncated": len(rows) > limit,
            "sections": {
                "capture": _source_payload(
                    SourceObservation(
                        "unavailable", observed, "volatile_delivery_state"
                    )
                ),
                "calls": _source_payload(call_source),
            },
        }

    def list_entries(
        self, scope: MemoryReadScope, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        cursor_key = _decode_cursor(cursor) if cursor is not None else None
        _validate_limit(limit, 50)
        return self._list_entries(
            cursor_key,
            limit,
            principal_id=principal_id,
            project_id=project_id,
        )

    def list_admin_entries(
        self, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        cursor_key = _decode_cursor(cursor) if cursor is not None else None
        _validate_limit(limit, 50)
        return self._list_entries(cursor_key, limit)

    def _list_entries(
        self,
        cursor_key: tuple[int, str] | None,
        limit: int,
        *,
        principal_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        rows, everos_source = self._read_memcells(
            cursor_key,
            limit + 1,
            principal_id=principal_id,
            project_id=project_id,
        )
        page = rows[:limit]
        call_counts, call_source = self._call_counts(page)
        entries = [
            {
                **self._entry_projection(row),
                "run_summary": None,
                "authorized_call_count": (
                    call_counts.get(str(row["memcell_id"]), 0)
                    if call_counts is not None
                    else None
                ),
            }
            for row in page
        ]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                _timestamp_ms(last["timestamp"]), str(last["memcell_id"])
            )
        return {
            "status": "ok",
            "entries": entries,
            "next_cursor": next_cursor,
            "sections": _sections(everos_source, call_source),
        }

    def entry_detail(
        self, scope: MemoryReadScope, memcell_id: str
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        _validate_id(memcell_id)
        return self._entry_detail(
            memcell_id,
            principal_id=principal_id,
            project_id=project_id,
        )

    def admin_entry_detail(self, memcell_id: str) -> dict[str, Any]:
        _validate_id(memcell_id)
        return self._entry_detail(memcell_id)

    def _entry_detail(
        self,
        memcell_id: str,
        *,
        principal_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        rows, everos_source = self._read_memcells(
            None,
            1,
            principal_id=principal_id,
            project_id=project_id,
            memcell_id=memcell_id,
        )
        if not rows:
            if everos_source.status != "available":
                return {
                    "status": "failed",
                    "error": "memory_processing_failed",
                    "sections": _sections(
                        everos_source,
                        _call_source_status(
                            self._paths.call_log_db_path, _utc_now()
                        ),
                    ),
                }
            return {
                "status": "not_found",
                "sections": _sections(
                    everos_source,
                    _call_source_status(self._paths.call_log_db_path, _utc_now()),
                ),
            }
        row = rows[0]
        owner_id, row_project = _memcell_scope(row)
        calls, omitted_call_count, call_source = self._detail_calls(
            memcell_id,
            owner_id=owner_id,
            project_id=row_project,
        )
        entry = self._entry_projection(row)
        return {
            "status": "ok",
            "entry": entry,
            "capture": {
                "status": "unavailable",
                "reason": "volatile_delivery_state",
            },
            "steps": [
                {
                    "type": "memcell",
                    "status": "created",
                    "timestamp_ms": entry["timestamp_ms"],
                    "memcell_id": entry["memcell_id"],
                }
            ],
            "calls": calls,
            "omitted_call_count": omitted_call_count,
            "omitted_step_count": 0,
            "current_state": {
                "status": "unavailable",
                "reason": "processing_timeline_unavailable",
            },
            "sections": _sections(everos_source, call_source),
        }

    def _read_memcells(
        self,
        cursor_key: tuple[int, str] | None,
        limit: int,
        *,
        principal_id: str | None = None,
        project_id: str | None = None,
        memcell_id: str | None = None,
    ) -> tuple[list[sqlite3.Row], SourceObservation]:
        observed = _utc_now()
        with _read_only(self._paths.system_db_path) as conn:
            if conn is None:
                return [], SourceObservation(
                    "unavailable", observed, "provider_memory_unavailable"
                )
            try:
                _register_identity_validators(conn)
                predicates = [
                    "app_id = ?",
                    "length(CAST(sender_ids_json AS BLOB)) <= ?",
                    "json_valid(sender_ids_json)",
                    "json_type(sender_ids_json) = 'array'",
                    "json_array_length(sender_ids_json) = 1",
                    "json_type(sender_ids_json, '$[0]') = 'text'",
                ]
                args: list[object] = [_APP_ID, _MAX_SENDER_IDS_BYTES]
                if principal_id is not None:
                    predicates.extend(
                        [
                            "project_id = ?",
                            "json_extract(sender_ids_json, '$[0]') IN (?, ?)",
                        ]
                    )
                    args.extend(
                        (
                            project_id,
                            principal_id,
                            derive_assistant_memory_owner_id(principal_id),
                        )
                    )
                else:
                    predicates.extend(
                        [
                            "memory_project_valid(project_id) = 1",
                            "memory_owner_valid(json_extract(sender_ids_json, '$[0]')) = 1",
                        ]
                    )
                if memcell_id is not None:
                    predicates.append("memcell_id = ?")
                    args.append(memcell_id)
                inner = f"""
                    SELECT memcell_id, app_id, project_id,
                           CASE WHEN length(CAST(message_ids_json AS BLOB)) <= ?
                                THEN message_ids_json ELSE '[]' END AS message_ids_json,
                           sender_ids_json,
                           CASE WHEN length(CAST(payload_json AS BLOB)) <= ?
                                THEN payload_json ELSE NULL END AS payload_json,
                           timestamp, {_MEMCELL_TIMESTAMP_SQL} AS timestamp_ms
                    FROM memcell
                    WHERE {' AND '.join(predicates)}
                """
                query_args: list[object] = [
                    _MAX_MESSAGE_IDS_BYTES,
                    _MAX_PAYLOAD_JSON_BYTES,
                    *args,
                ]
                sql = f"SELECT * FROM ({inner})"
                if cursor_key is not None:
                    sql += (
                        " WHERE timestamp_ms < ? OR "
                        "(timestamp_ms = ? AND memcell_id < ?)"
                    )
                    query_args.extend(
                        (cursor_key[0], cursor_key[0], cursor_key[1])
                    )
                sql += " ORDER BY timestamp_ms DESC, memcell_id DESC LIMIT ?"
                query_args.append(limit)
                rows = list(conn.execute(sql, query_args))
            except sqlite3.Error:
                return [], SourceObservation(
                    "unavailable", observed, "provider_memory_unavailable"
                )
        return rows, SourceObservation("available", observed)

    def _call_counts(
        self, rows: list[sqlite3.Row]
    ) -> tuple[dict[str, int] | None, SourceObservation]:
        observed = _utc_now()
        source = _call_source_status(self._paths.call_log_db_path, observed)
        if source.status != "available":
            return None, source
        if not rows:
            return {}, source
        page = [
            {
                "memcell_id": str(row["memcell_id"]),
                "owner_id": _memcell_scope(row)[0],
                "project_id": _memcell_scope(row)[1],
            }
            for row in rows
        ]
        try:
            with _read_only(self._paths.call_log_db_path) as conn:
                if conn is None:
                    raise sqlite3.OperationalError("call log unavailable")
                counts = {
                    str(row["memcell_id"]): int(row["call_count"])
                    for row in conn.execute(
                        """
                        WITH page AS MATERIALIZED (
                            SELECT json_extract(value, '$.memcell_id') AS memcell_id,
                                   json_extract(value, '$.owner_id') AS owner_id,
                                   json_extract(value, '$.project_id') AS project_id
                            FROM json_each(?)
                        )
                        SELECT page.memcell_id, COUNT(call.id) AS call_count
                        FROM page
                        LEFT JOIN provider_call AS call
                          ON call.owner_id = page.owner_id
                         AND call.project_id = page.project_id
                         AND (call.memcell_id = page.memcell_id OR
                              (call.parent_type = 'memcell' AND
                               call.parent_id = page.memcell_id))
                        GROUP BY page.memcell_id
                        """,
                        (json.dumps(page, separators=(",", ":")),),
                    )
                }
        except sqlite3.Error:
            return None, SourceObservation(
                "unavailable", observed, "provider_call_log_unavailable"
            )
        return counts, source

    def _detail_calls(
        self,
        memcell_id: str,
        *,
        owner_id: str,
        project_id: str,
    ) -> tuple[list[dict[str, Any]], int, SourceObservation]:
        observed = _utc_now()
        source = _call_source_status(self._paths.call_log_db_path, observed)
        if source.status != "available":
            return [], 0, source
        try:
            with _read_only(self._paths.call_log_db_path) as conn:
                if conn is None:
                    raise sqlite3.OperationalError("call log unavailable")
                rows = conn.execute(
                    f"SELECT {_CALL_COLUMNS}, COUNT(*) OVER () AS total_count "
                    "FROM provider_call "
                    "WHERE owner_id = ? AND project_id = ? AND "
                    "(memcell_id = ? OR (parent_type = 'memcell' AND parent_id = ?)) "
                    "ORDER BY started_at_ms DESC, id DESC LIMIT 20",
                    (owner_id, project_id, memcell_id, memcell_id),
                ).fetchall()
        except sqlite3.Error:
            return [], 0, SourceObservation(
                "unavailable", observed, "provider_call_log_unavailable"
            )
        total_count = int(rows[0]["total_count"]) if rows else 0
        calls = [self._call_projection(row) for row in rows]
        return calls, max(0, total_count - len(calls)), source

    def _entry_projection(self, row: sqlite3.Row) -> dict[str, Any]:
        owner_id, project_id = _memcell_scope(row)
        return {
            "memcell_id": _bounded_text(
                _scrub(
                    str(row["memcell_id"]), self._provider_base_urls, ()
                ),
                _MAX_MEMCELL_ID_BYTES,
            ),
            "project_id": project_id,
            "principal_id": owner_id,
            "timestamp_ms": _timestamp_ms(row["timestamp"]),
            "preview": _memcell_preview(
                row,
                base_urls=self._provider_base_urls,
                exact_values=self._redactions,
            ),
            "message_count": len(_message_ids(row)),
        }

    def _call_projection(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": _bounded_text(_scrub(str(row["id"]), self._provider_base_urls, ()), 256),
            "started_at_ms": _non_negative_int(row["started_at_ms"]) or 0,
            "duration_ms": _non_negative_int(row["duration_ms"]) or 0,
            "kind": _bounded_text(
                _scrub(str(row["kind"]), self._provider_base_urls, ()), 128
            ),
            "stage": _bounded_text(
                _scrub(str(row["stage"]), self._provider_base_urls, ()), 128
            ),
            "model": _bounded_optional_text(
                _scrub_optional(
                    row["model"], self._provider_base_urls, self._redactions
                ),
                1_024,
            ),
            "status": _bounded_text(
                _scrub(str(row["status"]), self._provider_base_urls, ()), 128
            ),
            "error": _bounded_optional_text(
                _scrub_optional(
                    row["error"], self._provider_base_urls, self._redactions
                ),
                _MAX_ERROR_BYTES,
            ),
            "finish_reason": _bounded_optional_text(
                _scrub_optional(
                    row["finish_reason"],
                    self._provider_base_urls,
                    self._redactions,
                ),
                128,
            ),
            "prompt_tokens": _non_negative_int(row["prompt_tokens"]),
            "completion_tokens": _non_negative_int(row["completion_tokens"]),
            "request": _project_json(
                row["request_json"],
                self._provider_base_urls,
                self._redactions,
            ),
            "response": (
                _project_json(
                    row["response_json"],
                    self._provider_base_urls,
                    self._redactions,
                )
                if row["response_json"] is not None
                else None
            ),
            "request_bytes": _non_negative_int(row["request_bytes"]),
            "response_bytes": _non_negative_int(row["response_bytes"]),
            "dropped_before": _non_negative_int(row["dropped_before"]) or 0,
        }


def _validated_scope(scope: MemoryReadScope) -> MemoryReadScope:
    if (
        not isinstance(scope, tuple)
        or len(scope) != 2
        or not is_principal_id(scope[0])
        or not is_persisted_memory_project_id(scope[1])
    ):
        raise ValueError("invalid Memory scope")
    return scope


def _validate_limit(limit: int, maximum: int) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= maximum
    ):
        raise ValueError(f"limit must be between 1 and {maximum}")


def _validate_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
    ):
        raise ValueError("invalid memcell id")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _everos_source_status(path: Path, observed: str) -> SourceObservation:
    with _read_only(path) as conn:
        if conn is None:
            return SourceObservation(
                "unavailable", observed, "provider_memory_unavailable"
            )
        try:
            conn.execute(
                f"SELECT {_MEMCELL_COLUMNS} FROM memcell LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return SourceObservation(
                "unavailable", observed, "provider_memory_unavailable"
            )
    return SourceObservation("available", observed)


def _call_source_status(path: Path, observed: str) -> SourceObservation:
    with _read_only(path) as conn:
        if conn is None:
            return SourceObservation(
                "unavailable", observed, "provider_call_log_unavailable"
            )
        try:
            _validate_provider_call_source(conn)
        except sqlite3.Error:
            return SourceObservation(
                "unavailable", observed, "provider_call_log_unavailable"
            )
    return SourceObservation("available", observed)


def _validate_provider_call_source(conn: sqlite3.Connection) -> None:
    conn.execute(f"SELECT {_CALL_COLUMNS} FROM provider_call LIMIT 1").fetchone()


def _register_identity_validators(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "memory_owner_valid",
        1,
        lambda value: int(is_memory_owner_id(value)),
        deterministic=True,
    )
    conn.create_function(
        "memory_project_valid",
        1,
        lambda value: int(is_persisted_memory_project_id(value)),
        deterministic=True,
    )


def _memcell_scope(row: sqlite3.Row) -> MemoryReadScope:
    senders = _decode_json(row["sender_ids_json"])
    project_id = row["project_id"]
    if (
        row["app_id"] != _APP_ID
        or not isinstance(senders, list)
        or len(senders) != 1
        or not is_memory_owner_id(senders[0])
        or not is_persisted_memory_project_id(project_id)
    ):
        raise ValueError("invalid Memory cell scope")
    return senders[0], project_id


def _authorized_call_scope(row: sqlite3.Row) -> MemoryReadScope | None:
    owner_id = row["owner_id"]
    project_id = row["project_id"]
    if not is_memory_owner_id(owner_id) or not is_persisted_memory_project_id(
        project_id
    ):
        return None
    principal_id = (
        owner_id[:-6]
        if isinstance(owner_id, str) and owner_id.endswith("-agent")
        else owner_id
    )
    if not is_principal_id(principal_id):
        return None
    return principal_id, project_id


def _sections(
    everos: SourceObservation,
    calls: SourceObservation,
) -> dict[str, dict[str, str]]:
    return {
        "everos": _source_payload(everos),
        "capture": _source_payload(
            SourceObservation(
                "unavailable", everos.observed_at, "volatile_delivery_state"
            )
        ),
        "calls": _source_payload(calls),
    }


def _source_payload(source: SourceObservation) -> dict[str, str]:
    payload = {"status": source.status}
    if source.observed_at is not None:
        payload["observed_at"] = source.observed_at
    if source.reason is not None:
        payload["reason"] = source.reason
    return payload


def _timestamp_ms(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    if not isinstance(value, str):
        return 0
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return max(0, int(instant.timestamp() * 1000))
    except (ValueError, OverflowError):
        return 0


def _encode_cursor(timestamp_ms: int, memcell_id: str) -> str:
    raw = json.dumps([timestamp_ms, memcell_id], separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(encoded) > _MAX_CURSOR_BYTES:
        raise ValueError("generated cursor exceeds its budget")
    return encoded


def _decode_cursor(cursor: str) -> tuple[int, str]:
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > _MAX_CURSOR_BYTES
        or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in cursor)
    ):
        raise ValueError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as error:
        raise ValueError("invalid cursor") from error
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], int)
        or isinstance(value[0], bool)
        or not 0 <= value[0] <= 4_102_444_800_000
        or not isinstance(value[1], str)
        or not value[1]
        or len(value[1].encode("utf-8")) > _MAX_MEMCELL_ID_BYTES
        or _encode_cursor(value[0], value[1]) != cursor
    ):
        raise ValueError("invalid cursor")
    return value[0], value[1]


def _decode_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return None


def _message_ids(row: sqlite3.Row) -> set[str]:
    values = _decode_json(row["message_ids_json"])
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        return set()
    return set(values)


def _memcell_preview(
    row: sqlite3.Row,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> str:
    payload = _decode_json(row["payload_json"])
    senders = _decode_json(row["sender_ids_json"])
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("items"), list)
        or not isinstance(senders, list)
        or len(senders) != 1
    ):
        return ""
    text = [
        item["content"]
        for item in payload["items"]
        if isinstance(item, dict)
        and item.get("role") == "user"
        and item.get("sender_id") == senders[0]
        and isinstance(item.get("content"), str)
    ]
    return _bounded_text(_scrub(" ".join(text), base_urls, exact_values), 512)


def _project_json(
    value: object,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> Any:
    try:
        decoded = json.loads(value) if isinstance(value, str) else None
    except (json.JSONDecodeError, RecursionError):
        return {"status": "unavailable", "reason": "malformed"}
    scrubbed = _scrub_json(
        decoded,
        base_urls=base_urls,
        exact_values=exact_values,
    )
    if _json_size(scrubbed) <= _MAX_PAYLOAD_BYTES:
        return scrubbed
    serialized = json.dumps(
        scrubbed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw = serialized.encode("utf-8")
    excerpt = raw[: _MAX_PAYLOAD_BYTES // 2].decode("utf-8", errors="ignore")
    return {
        "excerpt": excerpt,
        "omitted_bytes": len(raw) - len(excerpt.encode("utf-8")),
    }


def _scrub(
    value: str,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> str:
    return _scrub_text(
        value,
        base_urls=base_urls,
        exact_values=exact_values,
    )


def _scrub_optional(
    value: object,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> str | None:
    return _scrub(str(value), base_urls, exact_values) if value is not None else None


def _bounded_text(value: str, limit: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit].decode("utf-8", errors="ignore")


def _bounded_optional_text(value: str | None, limit: int) -> str | None:
    return _bounded_text(value, limit) if value is not None else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


@contextmanager
def _read_only(path: Path):
    try:
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        yield None
        return
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
