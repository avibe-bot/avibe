from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

from core.memory.project_ids import (
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
)
from core.memory.processing_record import (
    ProcessingSourceObservations,
    SourceObservation,
)

from .recorder import _scrub_json, _scrub_text

MemoryReadScope: TypeAlias = tuple[str, str]

_APP_ID = "avibe"
_MAX_CURSOR_BYTES = 256
_MAX_MEMCELL_ID_BYTES = 256
_MAX_DETAIL_CALLS = 20
_MAX_DETAIL_RUNS = 50
_MAX_PAYLOAD_FIELD_BYTES = 12_000
_MAX_ERROR_BYTES = 1_024
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_MEMCELL_PAYLOAD_JSON_BYTES = 64_000
_MAX_MEMCELL_MESSAGE_IDS_JSON_BYTES = 16_000
_MAX_MEMCELL_SENDER_IDS_JSON_BYTES = 1_024
_LIST_RUN_STATUSES = ("running", "success", "failed", "dead_letter", "crashed")
# Keep SQL ordering identical to _timestamp_ms: stored numeric values are
# already milliseconds, while ISO fractional seconds are truncated to millis.
_MEMCELL_TIMESTAMP_SQL = (
    "CASE WHEN typeof(timestamp) IN ('integer', 'real') "
    "THEN MAX(0, CAST(timestamp AS INTEGER)) "
    "ELSE MAX(0, COALESCE("
    "CAST(strftime('%s', timestamp) AS INTEGER) * 1000 + "
    "CASE WHEN instr(timestamp, '.') > 0 THEN "
    "CAST(CAST('0.' || substr(timestamp, instr(timestamp, '.') + 1) AS REAL) "
    "* 1000 AS INTEGER) ELSE 0 END, 0)) END"
)
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]+")
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_PRINCIPAL_RE = re.compile(r"u-[0-9a-f]{32}")
_PROJECT_RE = re.compile(r"p-[0-9a-f]{32}")
_PRINCIPAL_GLOB = "u-" + "[0-9a-f]" * 32
_PROJECT_GLOB = "p-" + "[0-9a-f]" * 32
_ATTACHMENT_TYPES = frozenset(
    {
        "attachment",
        "audio",
        "doc",
        "document",
        "email",
        "file",
        "html",
        "image",
        "input_audio",
        "input_file",
        "input_image",
        "pdf",
    }
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


@dataclass(frozen=True, slots=True)
class _Unavailable(Exception):
    reason: str


@dataclass(frozen=True, slots=True)
class _ScopedMemcellFilter:
    principal_id: str
    project_id: str


@dataclass(frozen=True, slots=True)
class _AdminMemcellFilter:
    pass


_MemcellFilter: TypeAlias = _ScopedMemcellFilter | _AdminMemcellFilter
_ADMIN_MEMCELL_FILTER = _AdminMemcellFilter()


class MemoryInsightReader:
    """Synchronous projections over pinned EverOS diagnostics."""

    def __init__(
        self,
        paths: MemoryInsightPaths,
        *,
        provider_base_urls: Sequence[str] = (),
        exact_redaction_values: Sequence[str] = (),
    ) -> None:
        if isinstance(provider_base_urls, str) or any(
            not isinstance(url, str) for url in provider_base_urls
        ):
            raise TypeError("provider_base_urls must be a sequence of strings")
        if isinstance(exact_redaction_values, str) or any(
            not isinstance(value, str) for value in exact_redaction_values
        ):
            raise TypeError("exact_redaction_values must be a sequence of strings")
        self._paths = paths
        self._provider_base_urls = tuple(url.rstrip("/") for url in provider_base_urls if url)
        self._exact_redaction_values = tuple(
            sorted(
                (value for value in exact_redaction_values if value),
                key=len,
                reverse=True,
            )
        )

    def source_observation(self) -> ProcessingSourceObservations:
        """Perform compact representative reads for each Processing Record source."""

        system_section = self._memcell_status()
        _, runs_section = self._read_run_summaries([])
        capture_section = self._capture_status()
        _, calls_section = self._read_call_counts(
            [],
            capture_available=False,
            runs_available=False,
        )
        observed_at = _utc_observed_at()
        sections = _observed_sections(
            {
                "everos": _combine_everos_section(system_section, runs_section),
                "capture": capture_section,
                "calls": calls_section,
            },
            observed_at=observed_at,
        )
        return ProcessingSourceObservations(
            everos=_source_observation(sections["everos"]),
            capture=_source_observation(sections["capture"]),
            calls=_source_observation(sections["calls"]),
        )

    def installation_preflight_calls(self) -> tuple[dict[str, Any], ...]:
        """Project recent installation-level checks without conversation scope."""

        with _read_only(self._paths.call_log_db_path) as conn:
            _validate_provider_call_source(conn)
            rows = conn.execute(
                """
                SELECT id, started_at_ms, duration_ms, kind, stage, model, status,
                       error, finish_reason, prompt_tokens, completion_tokens,
                       request_json, response_json, request_bytes, response_bytes,
                       dropped_before
                FROM provider_call
                WHERE stage = 'processing_preflight'
                  AND request_id IS NULL AND strategy_name IS NULL
                  AND run_id IS NULL AND attempt IS NULL
                  AND memcell_id IS NULL AND app_id IS NULL
                  AND project_id IS NULL AND owner_id IS NULL
                  AND md_path IS NULL AND entry_id IS NULL
                  AND parent_type IS NULL AND parent_id IS NULL
                ORDER BY started_at_ms DESC, id DESC
                LIMIT ?
                """,
                (_MAX_DETAIL_CALLS,),
            ).fetchall()
        return tuple(
            _call_projection(
                row,
                base_urls=self._provider_base_urls,
                exact_values=self._exact_redaction_values,
            )
            for row in rows
        )

    def list_unlinked_calls(
        self,
        scope: MemoryReadScope,
        limit: int,
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        return self._list_unlinked_calls(
            query_filter=_ScopedMemcellFilter(
                principal_id=principal_id,
                project_id=project_id,
            ),
            limit=limit,
        )

    def list_admin_unlinked_calls(self, limit: int) -> dict[str, Any]:
        return self._list_unlinked_calls(
            query_filter=_ADMIN_MEMCELL_FILTER,
            limit=limit,
        )

    def _list_unlinked_calls(
        self,
        *,
        query_filter: _MemcellFilter,
        limit: int,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")

        everos_section = self._memcell_status()
        capture_section = self._capture_status()
        _, calls_section = self._read_call_counts(
            [],
            capture_available=False,
            runs_available=False,
        )
        sections = _observed_sections(
            {
                "everos": everos_section,
                "capture": capture_section,
                "calls": calls_section,
            },
            observed_at=_utc_observed_at(),
        )
        if any(
            section["status"] != "available"
            for section in (everos_section, capture_section, calls_section)
        ):
            return {
                "status": "ok",
                "calls": [],
                "truncated": False,
                "sections": sections,
            }

        rows, query_section = self._read_unlinked_call_rows(
            query_filter=query_filter,
            limit=limit + 1,
        )
        if rows is None:
            sections["calls"] = {
                **query_section,
                "observed_at": None,
            }
            return {
                "status": "ok",
                "calls": [],
                "truncated": False,
                "sections": sections,
            }

        projected: list[dict[str, Any]] = []
        for row in rows:
            row_scope = _unlinked_call_scope(row)
            if row_scope is None:
                continue
            if (
                isinstance(query_filter, _ScopedMemcellFilter)
                and row_scope != (query_filter.principal_id, query_filter.project_id)
            ):
                continue
            principal_id, project_id = row_scope
            projected.append(
                {
                    **_call_projection(
                        row,
                        base_urls=self._provider_base_urls,
                        exact_values=self._exact_redaction_values,
                    ),
                    "principal_id": principal_id,
                    "project_id": project_id,
                }
            )
        return {
            "status": "ok",
            "calls": projected[:limit],
            "truncated": len(projected) > limit,
            "sections": sections,
        }

    def _read_unlinked_call_rows(
        self,
        *,
        query_filter: _MemcellFilter,
        limit: int,
    ) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        if isinstance(query_filter, _ScopedMemcellFilter):
            scope_sql = (
                "request_scope.principal_id = :principal_id "
                "AND request_scope.project_id = :project_id"
            )
            scope_args: dict[str, object] = {
                "principal_id": query_filter.principal_id,
                "project_id": query_filter.project_id,
            }
        elif isinstance(query_filter, _AdminMemcellFilter):
            scope_sql = "1"
            scope_args = {}
        else:
            raise TypeError("unsupported unlinked-call query filter")

        try:
            with _read_only(self._paths.call_log_db_path) as conn:
                _attach_read_only(conn, self._paths.capture_db_path, "capture")
                _attach_read_only(conn, self._paths.system_db_path, "everos_system")
                _validate_provider_call_source(conn)
                rows = list(
                    conn.execute(
                        f"""
                        WITH request_origins AS MATERIALIZED (
                            SELECT queue.add_request_id AS request_id,
                                   queue.principal_id AS principal_id,
                                   queue.project_ref AS project_id,
                                   queue.session_id AS session_id,
                                   queue.provider_timestamp_ms AS provider_timestamp_ms
                            FROM capture.memory_capture_queue AS queue
                            WHERE typeof(queue.add_request_id) = 'text'
                              AND queue.add_request_id != ''
                              AND typeof(queue.principal_id) = 'text'
                              AND typeof(queue.project_ref) = 'text'

                            UNION

                            SELECT settlement.request_id AS request_id,
                                   queue.principal_id AS principal_id,
                                   queue.project_ref AS project_id,
                                   queue.session_id AS session_id,
                                   queue.provider_timestamp_ms AS provider_timestamp_ms
                            FROM capture.memory_flush_settlements AS settlement
                            JOIN capture.memory_capture_queue AS queue
                              ON queue.provider_session_ref = settlement.provider_session_ref
                             AND queue.epoch = settlement.epoch
                             AND queue.generation = settlement.generation
                            WHERE settlement.operation_kind = 'flush'
                              AND typeof(settlement.request_id) = 'text'
                              AND settlement.request_id != ''
                              AND typeof(queue.principal_id) = 'text'
                              AND typeof(queue.project_ref) = 'text'
                        ),
                        request_scopes AS MATERIALIZED (
                            SELECT request_id,
                                   MIN(principal_id) AS principal_id,
                                   MIN(project_id) AS project_id
                            FROM request_origins
                            GROUP BY request_id
                            HAVING MIN(principal_id) = MAX(principal_id)
                               AND MIN(project_id) = MAX(project_id)
                        ),
                        linked_requests AS MATERIALIZED (
                            SELECT DISTINCT origin.request_id
                            FROM request_origins AS origin
                            JOIN everos_system.memcell AS memcell
                              ON memcell.app_id = :app_id
                             AND memcell.project_id = origin.project_id
                             AND length(CAST(memcell.sender_ids_json AS BLOB))
                                   <= {_MAX_MEMCELL_SENDER_IDS_JSON_BYTES}
                             AND length(CAST(memcell.message_ids_json AS BLOB))
                                   <= {_MAX_MEMCELL_MESSAGE_IDS_JSON_BYTES}
                             AND CASE WHEN json_valid(memcell.sender_ids_json) THEN
                                   json_type(memcell.sender_ids_json) = 'array'
                                   AND json_array_length(memcell.sender_ids_json) = 1
                                   AND json_type(memcell.sender_ids_json, '$[0]') = 'text'
                                   AND json_extract(memcell.sender_ids_json, '$[0]') = origin.principal_id
                                 ELSE 0 END
                             AND CASE WHEN json_valid(memcell.message_ids_json) THEN
                                   EXISTS (
                                       SELECT 1
                                       FROM json_each(memcell.message_ids_json) AS message_id
                                       WHERE message_id.type = 'text'
                                         AND message_id.value =
                                             'm_' || origin.session_id || '_'
                                             || CAST(origin.provider_timestamp_ms AS TEXT) || '_000'
                                   )
                                 ELSE 0 END
                        )
                        SELECT call.id, call.started_at_ms, call.duration_ms,
                               call.kind, call.stage, call.model, call.status,
                               call.error, call.finish_reason, call.prompt_tokens,
                               call.completion_tokens, call.request_json,
                               call.response_json, call.request_bytes,
                               call.response_bytes, call.dropped_before,
                               request_scope.principal_id, request_scope.project_id
                        FROM request_scopes AS request_scope
                        CROSS JOIN provider_call AS call
                            INDEXED BY provider_call_request_id_idx
                        WHERE call.request_id = request_scope.request_id
                          AND typeof(call.request_id) = 'text'
                          AND call.request_id != ''
                          AND call.memcell_id IS NULL
                          AND call.parent_type IS NOT 'memcell'
                          AND NOT EXISTS (
                              SELECT 1 FROM linked_requests AS linked
                              WHERE linked.request_id = request_scope.request_id
                          )
                          AND {scope_sql}
                        ORDER BY call.started_at_ms DESC, call.id DESC
                        LIMIT :limit
                        """,
                        {
                            "app_id": _APP_ID,
                            "limit": limit,
                            **scope_args,
                        },
                    )
                )
            return rows, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def list_entries(
        self,
        scope: MemoryReadScope,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        cursor_key = _decode_cursor(cursor) if cursor is not None else None

        memcells, everos_section = self._read_memcell_page(
            query_filter=_ScopedMemcellFilter(
                principal_id=principal_id,
                project_id=project_id,
            ),
            cursor_key=cursor_key,
            limit=limit + 1,
        )
        capture_section = self._capture_status()
        page = (memcells or [])[:limit]
        run_summaries, runs_section = self._read_run_summaries(page)
        call_counts, call_section = self._read_call_counts(
            page,
            capture_available=capture_section["status"] == "available",
            runs_available=runs_section["status"] == "available",
        )
        sections = _observed_sections(
            {
                "everos": _combine_everos_section(everos_section, runs_section),
                "capture": capture_section,
                "calls": call_section,
            },
            observed_at=_utc_observed_at(),
        )
        if memcells is None:
            return {
                "status": "ok",
                "entries": [],
                "next_cursor": None,
                "sections": sections,
            }

        # SQL applies the exact singleton-owner predicate, while this second
        # check keeps authorization fail-closed if the stored row shape drifts.
        page = [
            row
            for row in page
            if _memcell_owned_by(row, principal_id=principal_id, project_id=project_id)
        ]
        has_more = len(memcells) > limit
        entries = [
            self._list_entry(
                row,
                run_summary=(run_summaries or {}).get(str(row["memcell_id"])),
                calls_available=call_counts is not None,
                authorized_call_count=(call_counts or {}).get(str(row["memcell_id"]), 0),
                base_urls=self._provider_base_urls,
                exact_values=self._exact_redaction_values,
            )
            for row in page
        ]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_cursor(_memcell_timestamp_ms(last), str(last["memcell_id"]))
        return {
            "status": "ok",
            "entries": entries,
            "next_cursor": next_cursor,
            "sections": sections,
        }

    def list_admin_entries(
        self,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        cursor_key = _decode_cursor(cursor) if cursor is not None else None

        memcells, everos_section = self._read_memcell_page(
            query_filter=_ADMIN_MEMCELL_FILTER,
            cursor_key=cursor_key,
            limit=limit + 1,
        )
        capture_section = self._capture_status()
        page = (memcells or [])[:limit]
        run_summaries, runs_section = self._read_run_summaries(page)
        call_counts, call_section = self._read_call_counts(
            page,
            capture_available=capture_section["status"] == "available",
            runs_available=runs_section["status"] == "available",
        )

        sections = _observed_sections(
            {
                "everos": _combine_everos_section(everos_section, runs_section),
                "capture": capture_section,
                "calls": call_section,
            },
            observed_at=_utc_observed_at(),
        )
        if memcells is None:
            return {
                "status": "ok",
                "entries": [],
                "next_cursor": None,
                "sections": sections,
            }

        page = [row for row in page if _memcell_scope(row) is not None]
        entries = [
            self._list_entry(
                row,
                run_summary=(run_summaries or {}).get(str(row["memcell_id"])),
                calls_available=call_counts is not None,
                authorized_call_count=(call_counts or {}).get(str(row["memcell_id"]), 0),
                base_urls=self._provider_base_urls,
                exact_values=self._exact_redaction_values,
            )
            for row in page
        ]
        next_cursor = None
        if len(memcells) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(_memcell_timestamp_ms(last), str(last["memcell_id"]))
        return {
            "status": "ok",
            "entries": entries,
            "next_cursor": next_cursor,
            "sections": sections,
        }

    def entry_detail(self, scope: MemoryReadScope, memcell_id: str) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        if not isinstance(memcell_id, str) or not _ID_RE.fullmatch(memcell_id):
            raise ValueError("invalid memcell id")

        row, everos_section = self._read_detail_memcell(
            query_filter=_ScopedMemcellFilter(
                principal_id=principal_id,
                project_id=project_id,
            ),
            memcell_id=memcell_id,
        )
        return self._entry_detail_result(
            row,
            principal_id=principal_id,
            project_id=project_id,
            everos_section=everos_section,
        )

    def admin_entry_detail(self, memcell_id: str) -> dict[str, Any]:
        if not isinstance(memcell_id, str) or not _ID_RE.fullmatch(memcell_id):
            raise ValueError("invalid memcell id")
        row, everos_section = self._read_detail_memcell(
            query_filter=_ADMIN_MEMCELL_FILTER,
            memcell_id=memcell_id,
        )
        scope = _memcell_scope(row) if row is not None else None
        if scope is None:
            row = None
            principal_id, project_id = "", ""
        else:
            principal_id, project_id = scope
        return self._entry_detail_result(
            row,
            principal_id=principal_id,
            project_id=project_id,
            everos_section=everos_section,
        )

    def _entry_detail_result(
        self,
        row: sqlite3.Row | None,
        *,
        principal_id: str,
        project_id: str,
        everos_section: dict[str, str],
    ) -> dict[str, Any]:
        if row is None:
            if everos_section["status"] == "available":
                return {"status": "not_found"}
            return {
                "status": "not_found",
                "sections": _observed_sections(
                    {
                        "everos": everos_section,
                        "capture": _source_status(self._paths.capture_db_path),
                        "calls": _source_status(self._paths.call_log_db_path),
                    },
                    observed_at=_utc_observed_at(),
                ),
            }
        queues, capture_section = self._read_detail_capture_rows(
            row,
            principal_id=principal_id,
            project_id=project_id,
        )
        runs, owned_run_count, runs_section = self._read_detail_runs(
            memcell_id=str(row["memcell_id"]),
            principal_id=principal_id,
            project_id=project_id,
        )
        calls, owned_call_count, call_section = self._read_detail_calls(
            row,
            principal_id=principal_id,
            project_id=project_id,
            capture_available=capture_section["status"] == "available",
            runs_available=runs_section["status"] == "available",
        )
        selected_calls = calls or []
        selected_runs = runs or []
        capture = _capture_projection(
            row,
            queues,
            capture_section,
            principal_id=principal_id,
            project_id=project_id,
            base_urls=self._provider_base_urls,
            exact_values=self._exact_redaction_values,
        )
        steps = _steps_projection(
            row,
            selected_runs,
            base_urls=self._provider_base_urls,
            exact_values=self._exact_redaction_values,
        )
        current_state = self._current_state(principal_id, project_id, everos_section)
        result: dict[str, Any] = {
            "status": "ok",
            "entry": _entry_projection(
                row,
                base_urls=self._provider_base_urls,
                exact_values=self._exact_redaction_values,
            ),
            "capture": capture,
            "steps": steps,
            "calls": [
                _call_projection(
                    call,
                    base_urls=self._provider_base_urls,
                    exact_values=self._exact_redaction_values,
                )
                for call in selected_calls
            ],
            "omitted_call_count": max(0, owned_call_count - len(selected_calls)),
            "omitted_step_count": max(0, owned_run_count - len(selected_runs)),
            "current_state": current_state,
            "sections": _observed_sections(
                {
                    "everos": _combine_everos_section(everos_section, runs_section),
                    "capture": capture_section,
                    "calls": call_section,
                },
                observed_at=_utc_observed_at(),
            ),
        }
        if _encoded_size(result) > _MAX_RESPONSE_BYTES:
            raise AssertionError("memory insight detail exceeded its fixed response budget")
        return result

    def _list_entry(
        self,
        row: sqlite3.Row,
        *,
        run_summary: dict[str, Any] | None,
        calls_available: bool,
        authorized_call_count: int,
        base_urls: tuple[str, ...],
        exact_values: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            **_entry_projection(row, base_urls=base_urls, exact_values=exact_values),
            "run_summary": run_summary,
            "authorized_call_count": authorized_call_count if calls_available else None,
        }

    def _current_state(
        self,
        principal_id: str,
        project_id: str,
        everos_section: dict[str, str],
    ) -> dict[str, Any]:
        if everos_section["status"] != "available":
            return {"status": "unavailable", "reason": everos_section["reason"]}
        relative_profile = f"avibe/{project_id}/users/{principal_id}/user.md"
        try:
            with _read_only(self._paths.system_db_path) as conn:
                row = conn.execute(
                    "SELECT status, last_changed_at, error FROM md_change_state WHERE md_path = ?",
                    (relative_profile,),
                ).fetchone()
        except _Unavailable as unavailable:
            return {"status": "unavailable", "reason": unavailable.reason}

        profile_path = self._paths.everos_root / relative_profile
        try:
            profile_exists = profile_path.is_file()
            profile_updated_at_ms = int(profile_path.stat().st_mtime * 1000) if profile_exists else None
        except OSError:
            profile_exists = False
            profile_updated_at_ms = None
        indexing: dict[str, Any]
        if row is None:
            indexing = {"status": "not_seen"}
        else:
            indexing = {
                "status": _bounded_string(
                    _scrub(
                        str(row["status"]),
                        self._provider_base_urls,
                        self._exact_redaction_values,
                    ),
                    128,
                ),
                "updated_at_ms": _timestamp_ms(row["last_changed_at"]),
                "error": _bounded_optional_string(
                    _scrub_optional(
                        row["error"],
                        self._provider_base_urls,
                        self._exact_redaction_values,
                    ),
                    _MAX_ERROR_BYTES,
                ),
            }
        return {
            "status": "available",
            "profile": {
                "status": "present" if profile_exists else "missing",
                "updated_at_ms": profile_updated_at_ms,
            },
            "indexing": indexing,
            "label": "current_state",
        }

    def _read_memcell_page(
        self,
        *,
        query_filter: _MemcellFilter,
        cursor_key: tuple[int, str] | None,
        limit: int,
    ) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.system_db_path) as conn:
                project_sql, sender_sql, scope_args = _memcell_scope_sql(query_filter)
                sql = f"""
                    SELECT memcell_id, app_id, project_id, message_ids_json,
                           sender_ids_json, payload_json, timestamp, timestamp_ms
                    FROM (
                        SELECT memcell_id, app_id, project_id,
                               CASE WHEN length(CAST(message_ids_json AS BLOB))
                                             <= {_MAX_MEMCELL_MESSAGE_IDS_JSON_BYTES}
                                    THEN message_ids_json ELSE '[]' END AS message_ids_json,
                               CASE WHEN length(CAST(sender_ids_json AS BLOB))
                                             <= {_MAX_MEMCELL_SENDER_IDS_JSON_BYTES}
                                    THEN sender_ids_json ELSE '[]' END AS sender_ids_json,
                               CASE WHEN length(CAST(payload_json AS BLOB))
                                             <= {_MAX_MEMCELL_PAYLOAD_JSON_BYTES}
                                    THEN payload_json ELSE NULL END AS payload_json,
                               timestamp,
                               {_MEMCELL_TIMESTAMP_SQL} AS timestamp_ms
                        FROM memcell
                        WHERE app_id = ? AND {project_sql}
                          AND length(CAST(sender_ids_json AS BLOB))
                                <= {_MAX_MEMCELL_SENDER_IDS_JSON_BYTES}
                          AND CASE WHEN json_valid(sender_ids_json) THEN
                                json_type(sender_ids_json) = 'array'
                                AND json_array_length(sender_ids_json) = 1
                                AND json_type(sender_ids_json, '$[0]') = 'text'
                                AND {sender_sql}
                              ELSE 0 END
                    )
                """
                args: list[object] = [_APP_ID, *scope_args]
                if cursor_key is not None:
                    sql += " WHERE timestamp_ms < ? OR (timestamp_ms = ? AND memcell_id < ?)"
                    args.extend((cursor_key[0], cursor_key[0], cursor_key[1]))
                sql += " ORDER BY timestamp_ms DESC, memcell_id DESC LIMIT ?"
                args.append(limit)
                return list(conn.execute(sql, args)), {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _memcell_status(self) -> dict[str, str]:
        try:
            with _read_only(self._paths.system_db_path) as conn:
                conn.execute(
                    """
                    SELECT memcell_id, app_id, project_id, message_ids_json,
                           sender_ids_json, payload_json, timestamp
                    FROM memcell
                    LIMIT 1
                    """
                ).fetchone()
                conn.execute(
                    """
                    SELECT md_path, status, last_changed_at, error
                    FROM md_change_state
                    LIMIT 1
                    """
                ).fetchone()
            return {"status": "available"}
        except _Unavailable as unavailable:
            return {"status": "unavailable", "reason": unavailable.reason}

    def _capture_status(self) -> dict[str, str]:
        try:
            with _read_only(self._paths.capture_db_path) as conn:
                conn.execute(
                    """
                    SELECT queue.session_id, queue.provider_session_ref, queue.epoch,
                           queue.generation, queue.principal_id, queue.project_ref,
                           queue.provider_timestamp_ms, queue.state,
                           queue.occurred_at_ms, queue.add_request_id,
                           settlement.request_id
                    FROM memory_capture_queue AS queue
                    LEFT JOIN memory_flush_settlements AS settlement
                      ON settlement.provider_session_ref = queue.provider_session_ref
                     AND settlement.epoch = queue.epoch
                     AND settlement.generation = queue.generation
                     AND settlement.operation_kind = 'flush'
                    LIMIT 1
                    """
                ).fetchone()
            return {"status": "available"}
        except _Unavailable as unavailable:
            return {"status": "unavailable", "reason": unavailable.reason}

    def _read_run_summaries(
        self,
        memcells: list[sqlite3.Row],
    ) -> tuple[dict[str, dict[str, Any]] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.ome_db_path) as conn:
                if not memcells:
                    _validate_run_record_source(conn)
                    return {}, {"status": "available"}
                page = [
                    {
                        "memcell_id": str(row["memcell_id"]),
                        "project_id": scope[1],
                        "owner_id": scope[0],
                    }
                    for row in memcells
                    if (scope := _memcell_scope(row)) is not None
                ]
                if not page:
                    _validate_run_record_source(conn)
                    return {}, {"status": "available"}
                page_json = json.dumps(page, separators=(",", ":"))
                status_columns = ", ".join(
                    f"SUM(rr.status = '{status}') AS {status}"
                    for status in _LIST_RUN_STATUSES
                )
                rows = conn.execute(
                    f"""
                    WITH page AS MATERIALIZED (
                        SELECT json_extract(value, '$.memcell_id') AS memcell_id,
                               json_extract(value, '$.project_id') AS project_id,
                               json_extract(value, '$.owner_id') AS owner_id
                        FROM json_each(:page_json)
                    )
                    SELECT page.memcell_id AS memcell_id,
                           COUNT(*) AS total, {status_columns}
                    FROM run_record AS rr
                    JOIN page ON CASE WHEN json_valid(rr.event_payload) THEN
                            json_type(rr.event_payload) = 'object'
                            AND json_type(rr.event_payload, '$.memcell_id') = 'text'
                            AND json_extract(rr.event_payload, '$.memcell_id') = page.memcell_id
                            AND json_extract(rr.event_payload, '$.app_id') = :app_id
                            AND json_extract(rr.event_payload, '$.project_id') = page.project_id
                            AND (
                                json_type(rr.event_payload, '$.owner_id') IS NULL
                                OR (
                                    json_type(rr.event_payload, '$.owner_id') = 'text'
                                    AND json_extract(rr.event_payload, '$.owner_id') = page.owner_id
                                )
                            )
                          ELSE 0 END
                    GROUP BY page.memcell_id
                    """,
                    {"page_json": page_json, "app_id": _APP_ID},
                )
                summaries: dict[str, dict[str, Any]] = {}
                for row in rows:
                    total = int(row["total"])
                    statuses = {
                        status: int(row[status])
                        for status in _LIST_RUN_STATUSES
                        if int(row[status]) > 0
                    }
                    known_total = sum(statuses.values())
                    if known_total < total:
                        statuses["other"] = total - known_total
                    summaries[str(row["memcell_id"])] = {
                        "total": total,
                        "statuses": statuses,
                    }
                for item in page:
                    summaries.setdefault(item["memcell_id"], {"total": 0, "statuses": {}})
                return summaries, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_call_counts(
        self,
        memcells: list[sqlite3.Row],
        *,
        capture_available: bool,
        runs_available: bool,
    ) -> tuple[dict[str, int] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.call_log_db_path) as conn:
                if capture_available:
                    _attach_read_only(conn, self._paths.capture_db_path, "capture")
                if runs_available:
                    _attach_read_only(conn, self._paths.ome_db_path, "ome")
                if not memcells:
                    _validate_provider_call_source(conn)
                    return {}, {"status": "available"}

                page_json = json.dumps(
                    [
                        {
                            "memcell_id": str(memcell["memcell_id"]),
                            "message_ids": sorted(_message_ids(memcell)),
                            "project_id": scope[1],
                            "owner_id": scope[0],
                        }
                        for memcell in memcells
                        if (scope := _memcell_scope(memcell)) is not None
                    ],
                    separators=(",", ":"),
                )
                ctes = [
                    """
                    page AS MATERIALIZED (
                        SELECT json_extract(page_item.value, '$.memcell_id') AS memcell_id,
                               json_extract(page_item.value, '$.message_ids') AS message_ids_json,
                               json_extract(page_item.value, '$.project_id') AS project_id,
                               json_extract(page_item.value, '$.owner_id') AS owner_id
                        FROM json_each(:page_json) AS page_item
                    )
                    """
                ]
                call_branches = [
                    """
                    SELECT page.memcell_id, pc.id AS call_id
                    FROM page
                    CROSS JOIN provider_call AS pc INDEXED BY provider_call_memcell_id_idx
                    WHERE pc.memcell_id = page.memcell_id
                    """,
                    """
                    SELECT page.memcell_id, pc.id AS call_id
                    FROM page
                    CROSS JOIN provider_call AS pc INDEXED BY provider_call_parent_idx
                    WHERE pc.parent_type = 'memcell' AND pc.parent_id = page.memcell_id
                      AND pc.stage = 'cascade' AND pc.app_id = :app_id
                      AND pc.project_id = page.project_id AND pc.owner_id = page.owner_id
                    """,
                ]
                if capture_available:
                    ctes.append(
                        """
                        capture_candidates AS MATERIALIZED (
                        SELECT page.memcell_id, page.owner_id, page.project_id,
                               owned_queue.add_request_id AS request_id
                        FROM page
                        JOIN capture.memory_capture_queue AS owned_queue
                          ON owned_queue.principal_id = page.owner_id
                         AND owned_queue.project_ref = page.project_id
                        WHERE typeof(owned_queue.add_request_id) = 'text'
                          AND owned_queue.add_request_id != ''
                          AND EXISTS (
                              SELECT 1 FROM json_each(page.message_ids_json) AS message_id
                              WHERE message_id.type = 'text'
                                AND message_id.value =
                                    'm_' || owned_queue.session_id || '_'
                                    || CAST(owned_queue.provider_timestamp_ms AS TEXT) || '_000'
                          )

                        UNION

                        SELECT page.memcell_id, page.owner_id, page.project_id,
                               owned_settlement.request_id AS request_id
                        FROM page
                        JOIN capture.memory_capture_queue AS owned_queue
                          ON owned_queue.principal_id = page.owner_id
                         AND owned_queue.project_ref = page.project_id
                        JOIN capture.memory_flush_settlements AS owned_settlement
                          ON owned_settlement.provider_session_ref =
                                owned_queue.provider_session_ref
                         AND owned_settlement.epoch = owned_queue.epoch
                         AND owned_settlement.generation = owned_queue.generation
                         AND owned_settlement.operation_kind = 'flush'
                        WHERE typeof(owned_settlement.request_id) = 'text'
                          AND owned_settlement.request_id != ''
                          AND EXISTS (
                              SELECT 1 FROM json_each(page.message_ids_json) AS message_id
                              WHERE message_id.type = 'text'
                                AND message_id.value =
                                    'm_' || owned_queue.session_id || '_'
                                    || CAST(owned_queue.provider_timestamp_ms AS TEXT) || '_000'
                          )
                        ),
                        capture_links AS MATERIALIZED (
                        SELECT candidate.memcell_id, candidate.request_id
                        FROM capture_candidates AS candidate
                        WHERE NOT EXISTS (
                              SELECT 1 FROM capture.memory_capture_queue AS any_queue
                              WHERE any_queue.add_request_id = candidate.request_id
                                AND (
                                  any_queue.principal_id IS NOT candidate.owner_id
                                  OR any_queue.project_ref IS NOT candidate.project_id
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM capture.memory_flush_settlements AS any_settlement
                              JOIN capture.memory_capture_queue AS any_queue
                                ON any_queue.provider_session_ref =
                                      any_settlement.provider_session_ref
                               AND any_queue.epoch = any_settlement.epoch
                               AND any_queue.generation = any_settlement.generation
                              WHERE any_settlement.operation_kind = 'flush'
                                AND any_settlement.request_id = candidate.request_id
                                AND (
                                    any_queue.principal_id IS NOT candidate.owner_id
                                    OR any_queue.project_ref IS NOT candidate.project_id
                                )
                          )
                        )
                        """
                    )
                    call_branches.append(
                        """
                        SELECT capture_links.memcell_id, pc.id AS call_id
                        FROM capture_links
                        CROSS JOIN provider_call AS pc INDEXED BY provider_call_request_id_idx
                        WHERE pc.request_id = capture_links.request_id
                          AND typeof(pc.request_id) = 'text' AND pc.request_id != ''
                        """
                    )
                if runs_available:
                    run_scope = """
                        CASE WHEN json_valid(rr.event_payload) THEN
                            json_type(rr.event_payload) = 'object'
                            AND json_type(rr.event_payload, '$.memcell_id') = 'text'
                            AND json_extract(rr.event_payload, '$.memcell_id') = page.memcell_id
                            AND json_extract(rr.event_payload, '$.app_id') = :app_id
                            AND json_extract(rr.event_payload, '$.project_id') = page.project_id
                            AND (
                                json_type(rr.event_payload, '$.owner_id') IS NULL
                                OR (
                                    json_type(rr.event_payload, '$.owner_id') = 'text'
                                    AND json_extract(rr.event_payload, '$.owner_id') = page.owner_id
                                )
                            )
                        ELSE 0 END
                    """
                    ctes.append(
                        f"""
                        authorized_runs AS MATERIALIZED (
                            SELECT page.memcell_id, page.project_id, page.owner_id,
                                   rr.run_id,
                                   CASE
                                     WHEN substr(rr.event_topic, -length(':EpisodeExtracted'))
                                              = ':EpisodeExtracted' COLLATE BINARY
                                      AND json_type(
                                              rr.event_payload, '$.episode_entry_id'
                                          ) = 'text'
                                     THEN json_extract(
                                              rr.event_payload, '$.episode_entry_id'
                                          )
                                   END AS episode_entry_id
                            FROM ome.run_record AS rr
                            CROSS JOIN page
                            WHERE {run_scope}
                        )
                        """
                    )
                    call_branches.extend(
                        (
                            """
                            SELECT authorized_runs.memcell_id, pc.id AS call_id
                            FROM authorized_runs
                            CROSS JOIN provider_call AS pc INDEXED BY provider_call_run_id_idx
                            WHERE pc.run_id = authorized_runs.run_id
                              AND typeof(pc.run_id) = 'text'
                            """,
                            """
                            SELECT authorized_runs.memcell_id, pc.id AS call_id
                            FROM authorized_runs
                            CROSS JOIN provider_call AS pc INDEXED BY provider_call_parent_idx
                            WHERE authorized_runs.episode_entry_id IS NOT NULL
                              AND pc.parent_type = 'episode'
                              AND pc.parent_id = authorized_runs.episode_entry_id
                              AND pc.stage = 'cascade' AND pc.app_id = :app_id
                              AND pc.project_id = authorized_runs.project_id
                              AND pc.owner_id = authorized_runs.owner_id
                            """,
                        )
                    )
                ctes.append(f"authorized_calls AS ({' UNION '.join(call_branches)})")
                rows = conn.execute(
                    f"""
                    WITH {', '.join(ctes)}
                    SELECT memcell_id, COUNT(*) AS total
                    FROM authorized_calls
                    GROUP BY memcell_id
                    """,
                    {
                        "page_json": page_json,
                        "app_id": _APP_ID,
                    },
                )
                counts = {str(row["memcell_id"]): int(row["total"]) for row in rows}
                for memcell in memcells:
                    counts.setdefault(str(memcell["memcell_id"]), 0)
                return counts, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_detail_calls(
        self,
        memcell: sqlite3.Row,
        *,
        principal_id: str,
        project_id: str,
        capture_available: bool,
        runs_available: bool,
    ) -> tuple[list[sqlite3.Row] | None, int, dict[str, str]]:
        try:
            with _read_only(self._paths.call_log_db_path) as conn:
                if capture_available:
                    _attach_read_only(conn, self._paths.capture_db_path, "capture")
                if runs_available:
                    _attach_read_only(conn, self._paths.ome_db_path, "ome")
                ctes = [
                    """
                    page AS MATERIALIZED (
                        SELECT :memcell_id AS memcell_id, :message_ids_json AS message_ids_json
                    )
                    """
                ]
                branches = [
                    """
                    SELECT pc.id AS call_id FROM page
                    CROSS JOIN provider_call AS pc INDEXED BY provider_call_memcell_id_idx
                    WHERE pc.memcell_id = page.memcell_id
                    """,
                    """
                    SELECT pc.id AS call_id FROM page
                    CROSS JOIN provider_call AS pc INDEXED BY provider_call_parent_idx
                    WHERE pc.parent_type = 'memcell' AND pc.parent_id = page.memcell_id
                      AND pc.stage = 'cascade' AND pc.app_id = :app_id
                      AND pc.project_id = :project_id AND pc.owner_id = :owner_id
                    """,
                ]
                if capture_available:
                    ctes.append(
                        """
                        capture_candidates AS MATERIALIZED (
                        SELECT owned_queue.add_request_id AS request_id
                        FROM page JOIN capture.memory_capture_queue AS owned_queue
                          ON owned_queue.principal_id = :owner_id
                         AND owned_queue.project_ref = :project_id
                        WHERE typeof(owned_queue.add_request_id) = 'text'
                          AND owned_queue.add_request_id != ''
                          AND EXISTS (
                              SELECT 1 FROM json_each(page.message_ids_json) AS message_id
                              WHERE message_id.type = 'text' AND message_id.value =
                                  'm_' || owned_queue.session_id || '_'
                                  || CAST(owned_queue.provider_timestamp_ms AS TEXT) || '_000'
                          )

                        UNION

                        SELECT owned_settlement.request_id AS request_id
                        FROM page JOIN capture.memory_capture_queue AS owned_queue
                          ON owned_queue.principal_id = :owner_id
                         AND owned_queue.project_ref = :project_id
                        JOIN capture.memory_flush_settlements AS owned_settlement
                          ON owned_settlement.provider_session_ref =
                                owned_queue.provider_session_ref
                         AND owned_settlement.epoch = owned_queue.epoch
                         AND owned_settlement.generation = owned_queue.generation
                         AND owned_settlement.operation_kind = 'flush'
                        WHERE typeof(owned_settlement.request_id) = 'text'
                          AND owned_settlement.request_id != ''
                          AND EXISTS (
                              SELECT 1 FROM json_each(page.message_ids_json) AS message_id
                              WHERE message_id.type = 'text' AND message_id.value =
                                  'm_' || owned_queue.session_id || '_'
                                  || CAST(owned_queue.provider_timestamp_ms AS TEXT) || '_000'
                          )
                        ),
                        capture_links AS MATERIALIZED (
                        SELECT candidate.request_id
                        FROM capture_candidates AS candidate
                        WHERE NOT EXISTS (
                              SELECT 1 FROM capture.memory_capture_queue AS any_queue
                              WHERE any_queue.add_request_id = candidate.request_id
                                AND (any_queue.principal_id IS NOT :owner_id
                                     OR any_queue.project_ref IS NOT :project_id)
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM capture.memory_flush_settlements AS any_settlement
                              JOIN capture.memory_capture_queue AS any_queue
                                ON any_queue.provider_session_ref =
                                      any_settlement.provider_session_ref
                               AND any_queue.epoch = any_settlement.epoch
                               AND any_queue.generation = any_settlement.generation
                              WHERE any_settlement.operation_kind = 'flush'
                                AND any_settlement.request_id = candidate.request_id
                                AND (any_queue.principal_id IS NOT :owner_id
                                     OR any_queue.project_ref IS NOT :project_id)
                          )
                        )
                        """
                    )
                    branches.append(
                        """
                        SELECT pc.id AS call_id FROM capture_links
                        CROSS JOIN provider_call AS pc INDEXED BY provider_call_request_id_idx
                        WHERE pc.request_id = capture_links.request_id
                          AND typeof(pc.request_id) = 'text' AND pc.request_id != ''
                        """
                    )
                if runs_available:
                    run_scope = """
                        CASE WHEN json_valid(rr.event_payload) THEN
                            json_type(rr.event_payload) = 'object'
                            AND json_type(rr.event_payload, '$.memcell_id') = 'text'
                            AND json_extract(rr.event_payload, '$.memcell_id') = page.memcell_id
                            AND json_extract(rr.event_payload, '$.app_id') = :app_id
                            AND json_extract(rr.event_payload, '$.project_id') = :project_id
                            AND (json_type(rr.event_payload, '$.owner_id') IS NULL OR
                                 (json_type(rr.event_payload, '$.owner_id') = 'text' AND
                                  json_extract(rr.event_payload, '$.owner_id') = :owner_id))
                        ELSE 0 END
                    """
                    ctes.append(
                        f"""
                        authorized_runs AS MATERIALIZED (
                            SELECT rr.run_id,
                                   CASE WHEN substr(rr.event_topic, -length(':EpisodeExtracted'))
                                             = ':EpisodeExtracted' COLLATE BINARY
                                          AND json_type(rr.event_payload, '$.episode_entry_id') = 'text'
                                        THEN json_extract(rr.event_payload, '$.episode_entry_id') END AS episode_entry_id
                            FROM ome.run_record AS rr CROSS JOIN page WHERE {run_scope}
                        )
                        """
                    )
                    branches.extend((
                        """
                        SELECT pc.id AS call_id FROM authorized_runs
                        CROSS JOIN provider_call AS pc INDEXED BY provider_call_run_id_idx
                        WHERE pc.run_id = authorized_runs.run_id AND typeof(pc.run_id) = 'text'
                        """,
                        """
                        SELECT pc.id AS call_id FROM authorized_runs
                        CROSS JOIN provider_call AS pc INDEXED BY provider_call_parent_idx
                        WHERE authorized_runs.episode_entry_id IS NOT NULL
                          AND pc.parent_type = 'episode' AND pc.parent_id = authorized_runs.episode_entry_id
                          AND pc.stage = 'cascade' AND pc.app_id = :app_id
                          AND pc.project_id = :project_id AND pc.owner_id = :owner_id
                        """,
                    ))
                ctes.extend(
                    (
                        f"authorized_calls AS MATERIALIZED ({' UNION '.join(branches)})",
                        """
                        selected_calls AS MATERIALIZED (
                            SELECT pc.id, pc.started_at_ms
                            FROM provider_call AS pc
                            WHERE pc.id IN (SELECT call_id FROM authorized_calls)
                            ORDER BY pc.started_at_ms DESC, pc.id DESC
                            LIMIT :limit
                        )
                        """,
                        """
                        call_total AS (
                            SELECT COUNT(*) AS total_count FROM authorized_calls
                        )
                        """,
                    )
                )
                rows = list(conn.execute(
                    f"""
                    WITH {', '.join(ctes)}
                    SELECT pc.id, pc.started_at_ms, pc.duration_ms, pc.kind, pc.stage, pc.model,
                           pc.status, pc.error, pc.finish_reason, pc.prompt_tokens,
                           pc.completion_tokens, pc.request_json, pc.response_json, pc.request_bytes,
                           pc.response_bytes, pc.request_id, pc.run_id, pc.memcell_id, pc.app_id,
                           pc.project_id, pc.owner_id, pc.parent_type, pc.parent_id, pc.dropped_before,
                           call_total.total_count
                    FROM selected_calls
                    JOIN provider_call AS pc ON pc.id = selected_calls.id
                    CROSS JOIN call_total
                    ORDER BY selected_calls.started_at_ms DESC, selected_calls.id DESC
                    """,
                    {"memcell_id": str(memcell["memcell_id"]),
                     "message_ids_json": str(memcell["message_ids_json"]), "app_id": _APP_ID,
                     "project_id": project_id, "owner_id": principal_id, "limit": _MAX_DETAIL_CALLS},
                ))
                return rows, int(rows[0]["total_count"]) if rows else 0, {"status": "available"}
        except _Unavailable as unavailable:
            return None, 0, {"status": "unavailable", "reason": unavailable.reason}

    def _read_detail_memcell(
        self,
        *,
        query_filter: _MemcellFilter,
        memcell_id: str,
    ) -> tuple[sqlite3.Row | None, dict[str, str]]:
        try:
            with _read_only(self._paths.system_db_path) as conn:
                project_sql, sender_sql, scope_args = _memcell_scope_sql(query_filter)
                row = conn.execute(
                    f"""
                    SELECT memcell_id, app_id, project_id,
                           CASE WHEN length(CAST(message_ids_json AS BLOB))
                                         <= {_MAX_MEMCELL_MESSAGE_IDS_JSON_BYTES}
                                THEN message_ids_json ELSE '[]' END AS message_ids_json,
                           CASE WHEN length(CAST(sender_ids_json AS BLOB))
                                         <= {_MAX_MEMCELL_SENDER_IDS_JSON_BYTES}
                                THEN sender_ids_json ELSE '[]' END AS sender_ids_json,
                           CASE WHEN length(CAST(payload_json AS BLOB))
                                         <= {_MAX_MEMCELL_PAYLOAD_JSON_BYTES}
                                THEN payload_json ELSE NULL END AS payload_json,
                           timestamp
                    FROM memcell
                    WHERE memcell_id = ? AND app_id = ? AND {project_sql}
                      AND length(CAST(sender_ids_json AS BLOB))
                            <= {_MAX_MEMCELL_SENDER_IDS_JSON_BYTES}
                      AND CASE WHEN json_valid(sender_ids_json) THEN
                            json_type(sender_ids_json) = 'array'
                            AND json_array_length(sender_ids_json) = 1
                            AND json_type(sender_ids_json, '$[0]') = 'text'
                            AND {sender_sql}
                          ELSE 0 END
                    """,
                    (memcell_id, _APP_ID, *scope_args),
                ).fetchone()
                return row, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_detail_capture_rows(
        self, memcell: sqlite3.Row, *, principal_id: str, project_id: str
    ) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.capture_db_path) as conn:
                rows = list(conn.execute(
                    """
                    SELECT session_id, principal_id, project_ref, provider_timestamp_ms,
                           state, occurred_at_ms, add_request_id
                    FROM memory_capture_queue
                    WHERE principal_id = :owner_id AND project_ref = :project_id
                      AND EXISTS (
                          SELECT 1 FROM json_each(:message_ids_json) AS message_id
                          WHERE message_id.type = 'text' AND message_id.value =
                              'm_' || session_id || '_' || CAST(provider_timestamp_ms AS TEXT) || '_000'
                      )
                    """,
                    {
                        "owner_id": principal_id,
                        "project_id": project_id,
                        "message_ids_json": str(memcell["message_ids_json"]),
                    },
                ))
                return rows, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_detail_runs(
        self, *, memcell_id: str, principal_id: str, project_id: str
    ) -> tuple[list[sqlite3.Row] | None, int, dict[str, str]]:
        try:
            with _read_only(self._paths.ome_db_path) as conn:
                rows = list(conn.execute(
                    f"""
                    WITH authorized_runs AS MATERIALIZED (
                        SELECT run_id,
                               {_MEMCELL_TIMESTAMP_SQL.replace('timestamp', 'started_at')} AS started_at_ms
                        FROM run_record
                        WHERE CASE WHEN json_valid(event_payload) THEN
                            json_type(event_payload) = 'object'
                            AND json_type(event_payload, '$.memcell_id') = 'text'
                            AND json_extract(event_payload, '$.memcell_id') = :memcell_id
                            AND json_extract(event_payload, '$.app_id') = :app_id
                            AND json_extract(event_payload, '$.project_id') = :project_id
                            AND (json_type(event_payload, '$.owner_id') IS NULL OR
                                 (json_type(event_payload, '$.owner_id') = 'text' AND
                                  json_extract(event_payload, '$.owner_id') = :owner_id))
                        ELSE 0 END
                    ), selected_runs AS MATERIALIZED (
                        SELECT run_id, started_at_ms
                        FROM authorized_runs
                        ORDER BY started_at_ms DESC, run_id DESC
                        LIMIT :limit
                    ), run_total AS (
                        SELECT COUNT(*) AS total_count FROM authorized_runs
                    )
                    SELECT rr.run_id, rr.strategy_name, rr.status, rr.attempt,
                           rr.started_at, rr.finished_at, rr.error, rr.event_topic,
                           rr.event_payload, run_total.total_count
                    FROM selected_runs
                    JOIN run_record AS rr ON rr.run_id = selected_runs.run_id
                    CROSS JOIN run_total
                    ORDER BY selected_runs.started_at_ms DESC, selected_runs.run_id DESC
                    """,
                    {"memcell_id": memcell_id, "app_id": _APP_ID, "project_id": project_id,
                     "owner_id": principal_id, "limit": _MAX_DETAIL_RUNS},
                ))
                return rows, int(rows[0]["total_count"]) if rows else 0, {"status": "available"}
        except _Unavailable as unavailable:
            return None, 0, {"status": "unavailable", "reason": unavailable.reason}

def _validate_run_record_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        SELECT run_id, strategy_name, status, attempt, started_at, finished_at,
               error, event_topic, event_payload
        FROM run_record
        LIMIT 1
        """
    ).fetchone()


def _validate_provider_call_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        SELECT id, started_at_ms, duration_ms, kind, stage, model, status, error,
               finish_reason, prompt_tokens, completion_tokens, request_json,
               response_json, request_bytes, response_bytes, request_id, run_id,
               memcell_id, app_id, project_id, owner_id, parent_type, parent_id,
               dropped_before
        FROM provider_call INDEXED BY provider_call_memcell_id_idx
        LIMIT 1
        """
    ).fetchone()
    conn.execute(
        """
        SELECT id, request_id
        FROM provider_call INDEXED BY provider_call_request_id_idx
        LIMIT 1
        """
    ).fetchone()
    conn.execute(
        """
        SELECT id, run_id
        FROM provider_call INDEXED BY provider_call_run_id_idx
        LIMIT 1
        """
    ).fetchone()
    conn.execute(
        """
        SELECT id, parent_type, parent_id
        FROM provider_call INDEXED BY provider_call_parent_idx
        LIMIT 1
        """
    ).fetchone()


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise _Unavailable("missing")
    conn: sqlite3.Connection | None = None
    try:
        uri = path.absolute().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise _Unavailable(_sqlite_reason(exc)) from exc
    try:
        yield conn
    except sqlite3.Error as exc:
        raise _Unavailable(_sqlite_reason(exc)) from exc
    finally:
        conn.close()


def _attach_read_only(conn: sqlite3.Connection, path: Path, schema: str) -> None:
    uri = path.absolute().as_uri() + "?mode=ro"
    conn.execute(f"ATTACH DATABASE ? AS {schema}", (uri,))


def _sqlite_reason(exc: sqlite3.Error) -> str:
    message = str(exc).casefold()
    if "locked" in message or "busy" in message:
        return "busy"
    return "malformed"


def _source_status(path: Path) -> dict[str, str]:
    return {"status": "available"} if path.is_file() else {"status": "unavailable", "reason": "missing"}


def _combine_everos_section(
    system: dict[str, str],
    runs: dict[str, str],
) -> dict[str, str]:
    if system["status"] != "available":
        return system
    if runs["status"] != "available":
        return {"status": "partial", "reason": f"runs_{runs['reason']}"}
    return {"status": "available"}


def _observed_sections(
    sections: dict[str, dict[str, str]],
    *,
    observed_at: str,
) -> dict[str, dict[str, str | None]]:
    return {
        name: {
            **section,
            "observed_at": (
                observed_at
                if section["status"] in {"available", "partial"}
                else None
            ),
        }
        for name, section in sections.items()
    }


def _source_observation(section: dict[str, str | None]) -> SourceObservation:
    status = section["status"]
    if status not in {"available", "partial", "unavailable"}:
        status = "unavailable"
    return SourceObservation(
        status=status,
        observed_at=section.get("observed_at"),
        reason=section.get("reason"),
    )


def _utc_observed_at() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _validated_scope(scope: MemoryReadScope) -> MemoryReadScope:
    if not isinstance(scope, tuple) or len(scope) != 2:
        raise ValueError("invalid memory scope")
    principal_id, project_id = scope
    if not isinstance(principal_id, str) or not _PRINCIPAL_RE.fullmatch(principal_id):
        raise ValueError("invalid memory principal")
    if not isinstance(project_id, str) or not is_new_stored_memory_project_id(project_id):
        raise ValueError("invalid memory project")
    return principal_id, project_id


def _memcell_scope_sql(
    query_filter: _MemcellFilter,
) -> tuple[str, str, tuple[str, str]]:
    if isinstance(query_filter, _ScopedMemcellFilter):
        return (
            "project_id = ?",
            "json_extract(sender_ids_json, '$[0]') = ?",
            (query_filter.project_id, query_filter.principal_id),
        )
    if isinstance(query_filter, _AdminMemcellFilter):
        return (
            "(project_id = 'default' OR project_id GLOB ? OR ("
            "length(project_id) BETWEEN 1 AND 63 "
            "AND substr(project_id, 1, 1) GLOB '[a-z]' "
            "AND project_id NOT GLOB '*[^a-z0-9_-]*' "
            "AND project_id NOT IN ('all', 'personal') "
            "AND substr(project_id, 1, 2) NOT IN ('p-', 'u-')))",
            "json_extract(sender_ids_json, '$[0]') GLOB ?",
            (_PROJECT_GLOB, _PRINCIPAL_GLOB),
        )
    raise TypeError("unsupported memcell query filter")


def _decode_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return None


def _memcell_owned_by(row: sqlite3.Row, *, principal_id: str, project_id: str) -> bool:
    return _memcell_scope(row) == (principal_id, project_id)


def _memcell_scope(row: sqlite3.Row) -> MemoryReadScope | None:
    if row["app_id"] != _APP_ID or not isinstance(row["project_id"], str):
        return None
    project_id = row["project_id"]
    if not is_persisted_memory_project_id(project_id):
        return None
    senders = _decode_json(row["sender_ids_json"])
    if (
        not isinstance(senders, list)
        or len(senders) != 1
        or not isinstance(senders[0], str)
        or _PRINCIPAL_RE.fullmatch(senders[0]) is None
    ):
        return None
    return senders[0], project_id


def _unlinked_call_scope(row: sqlite3.Row) -> MemoryReadScope | None:
    principal_id = row["principal_id"]
    project_id = row["project_id"]
    if (
        not isinstance(principal_id, str)
        or _PRINCIPAL_RE.fullmatch(principal_id) is None
        or not isinstance(project_id, str)
        or not is_persisted_memory_project_id(project_id)
    ):
        return None
    return principal_id, project_id


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


def _memcell_timestamp_ms(row: sqlite3.Row) -> int:
    if "timestamp_ms" in row.keys():
        value = row["timestamp_ms"]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return _timestamp_ms(row["timestamp"])


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
        or _CURSOR_RE.fullmatch(cursor) is None
    ):
        raise ValueError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        decoded = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
    if _encode_cursor_value(raw) != cursor:
        raise ValueError("invalid cursor")
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not isinstance(decoded[0], int)
        or isinstance(decoded[0], bool)
        or not 0 <= decoded[0] <= 4_102_444_800_000
        or not isinstance(decoded[1], str)
        or _ID_RE.fullmatch(decoded[1]) is None
    ):
        raise ValueError("invalid cursor")
    canonical = json.dumps(decoded, separators=(",", ":")).encode()
    if canonical != raw:
        raise ValueError("invalid cursor")
    return decoded[0], decoded[1]


def _encode_cursor_value(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _message_ids(row: sqlite3.Row) -> set[str]:
    values = _decode_json(row["message_ids_json"])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        return set()
    return set(values)


def _related_queue_rows(
    memcell: sqlite3.Row,
    queues: list[sqlite3.Row],
    *,
    principal_id: str | None = None,
    project_id: str | None = None,
) -> list[sqlite3.Row]:
    message_ids = _message_ids(memcell)
    return [
        row
        for row in queues
        if f"m_{row['session_id']}_{row['provider_timestamp_ms']}_000" in message_ids
        and (principal_id is None or row["principal_id"] == principal_id)
        and (project_id is None or row["project_ref"] == project_id)
    ]


def _entry_projection(
    row: sqlite3.Row,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> dict[str, Any]:
    scope = _memcell_scope(row)
    if scope is None:
        raise ValueError("invalid memcell scope")
    principal_id, project_id = scope
    return {
        "memcell_id": _bounded_string(
            _scrub(str(row["memcell_id"]), base_urls, ()),
            _MAX_MEMCELL_ID_BYTES,
        ),
        "project_id": project_id,
        "principal_id": principal_id,
        "timestamp_ms": _memcell_timestamp_ms(row),
        "preview": _memcell_preview(
            row,
            base_urls=base_urls,
            exact_values=exact_values,
        ),
        "message_count": len(_message_ids(row)),
    }


def _memcell_preview(
    row: sqlite3.Row,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> str:
    payload = _decode_json(row["payload_json"])
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return ""
    senders = _decode_json(row["sender_ids_json"])
    if not isinstance(senders, list) or len(senders) != 1 or not isinstance(senders[0], str):
        return ""
    owner_id = senders[0]
    text: list[str] = []
    for item in payload["items"]:
        if (
            not isinstance(item, dict)
            or item.get("role") != "user"
            or item.get("sender_id") != owner_id
        ):
            continue
        content = item.get("content")
        if isinstance(content, str):
            text.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    text.append(part["text"])
                elif isinstance(kind, str) and kind.casefold() in _ATTACHMENT_TYPES:
                    name = part.get("name")
                    basename = (
                        _safe_basename(
                            name,
                            base_urls=base_urls,
                            exact_values=exact_values,
                        )
                        if isinstance(name, str)
                        else "attachment"
                    )
                    text.append(f"[{kind}: {basename}]")
    return _bounded_string(_scrub(" ".join(text), base_urls, exact_values), 512)


def _safe_basename(
    value: str,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> str:
    basename = value.replace("\\", "/").rsplit("/", 1)[-1] or "attachment"
    return _bounded_string(_scrub(basename, base_urls, exact_values), 128)


def _capture_projection(
    memcell: sqlite3.Row,
    queues: list[sqlite3.Row] | None,
    section: dict[str, str],
    *,
    principal_id: str,
    project_id: str,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> dict[str, Any]:
    if queues is None:
        return dict(section)
    related = _related_queue_rows(
        memcell,
        queues,
        principal_id=principal_id,
        project_id=project_id,
    )
    if not related:
        return {"status": "unavailable", "reason": "expired"}
    states = sorted(
        {
            _bounded_string(
                _scrub(str(row["state"]), base_urls, exact_values),
                128,
            )
            for row in related
        }
    )
    return {
        "status": "available",
        "delivery_states": states,
        "matched_message_count": len(related),
    }


def _steps_projection(
    memcell: sqlite3.Row,
    runs: list[sqlite3.Row],
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "type": "memcell",
            "status": "created",
            "timestamp_ms": _memcell_timestamp_ms(memcell),
            "memcell_id": _bounded_string(
                _scrub(str(memcell["memcell_id"]), base_urls, ()),
                _MAX_MEMCELL_ID_BYTES,
            ),
        }
    ]
    steps.extend(
        _run_projection(
            run,
            base_urls=base_urls,
            exact_values=exact_values,
        )
        for run in runs
    )
    steps.sort(key=lambda step: (int(step.get("started_at_ms", step.get("timestamp_ms", 0))), str(step.get("run_id", ""))))
    return steps


def _run_projection(
    row: sqlite3.Row,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> dict[str, Any]:
    strategy = _bounded_string(
        _scrub(str(row["strategy_name"]), base_urls, ()),
        128,
    )
    return {
        "type": "strategy",
        "run_id": _bounded_string(
            _scrub(str(row["run_id"]), base_urls, ()),
            256,
        ),
        "strategy": strategy,
        "relation": "profile_trigger" if strategy == "extract_user_profile" else "run",
        "status": _bounded_string(
            _scrub(str(row["status"]), base_urls, ()),
            128,
        ),
        "attempt": _optional_non_negative_int(row["attempt"]) or 0,
        "started_at_ms": _timestamp_ms(row["started_at"]),
        "finished_at_ms": _timestamp_ms(row["finished_at"]) if row["finished_at"] is not None else None,
        "error": _bounded_optional_string(
            _scrub_optional(row["error"], base_urls, exact_values),
            _MAX_ERROR_BYTES,
        ),
    }


def _call_projection(
    row: sqlite3.Row,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> dict[str, Any]:
    request = _project_stored_json(
        row["request_json"],
        base_urls=base_urls,
        exact_values=exact_values,
    )
    response = (
        _project_stored_json(
            row["response_json"],
            base_urls=base_urls,
            exact_values=exact_values,
        )
        if row["response_json"] is not None
        else None
    )
    return {
        "id": _bounded_string(
            _scrub(str(row["id"]), base_urls, ()),
            256,
        ),
        "started_at_ms": _optional_non_negative_int(row["started_at_ms"]) or 0,
        "duration_ms": _optional_non_negative_int(row["duration_ms"]) or 0,
        "kind": _bounded_string(
            _scrub(str(row["kind"]), base_urls, ()),
            128,
        ),
        "stage": _bounded_string(
            _scrub(str(row["stage"]), base_urls, ()),
            128,
        ),
        "model": _bounded_optional_string(
            _scrub_optional(row["model"], base_urls, exact_values),
            1_024,
        ),
        "status": _bounded_string(
            _scrub(str(row["status"]), base_urls, ()),
            128,
        ),
        "error": _bounded_optional_string(
            _scrub_optional(row["error"], base_urls, exact_values),
            _MAX_ERROR_BYTES,
        ),
        "finish_reason": _bounded_optional_string(
            _scrub_optional(row["finish_reason"], base_urls, exact_values),
            128,
        ),
        "prompt_tokens": _optional_non_negative_int(row["prompt_tokens"]),
        "completion_tokens": _optional_non_negative_int(row["completion_tokens"]),
        "request": request,
        "response": response,
        "request_bytes": _optional_non_negative_int(row["request_bytes"]),
        "response_bytes": _optional_non_negative_int(row["response_bytes"]),
        "dropped_before": _optional_non_negative_int(row["dropped_before"]) or 0,
    }


def _project_stored_json(
    value: object,
    *,
    base_urls: tuple[str, ...],
    exact_values: tuple[str, ...],
) -> Any:
    decoded = _decode_json(value)
    if decoded is None and value != "null":
        return {"status": "unavailable", "reason": "malformed"}
    scrubbed = _scrub_json(
        decoded,
        base_urls=base_urls,
        exact_values=exact_values,
    )
    return _bounded_json(scrubbed, _MAX_PAYLOAD_FIELD_BYTES)


def _bounded_json(value: Any, limit: int) -> Any:
    if _encoded_size(value) <= limit:
        return value
    if isinstance(value, str):
        return _bounded_string_marker(value, limit)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _bounded_string_marker(serialized, limit)


def _bounded_string_marker(value: str, limit: int) -> dict[str, Any]:
    raw = value.encode("utf-8")
    low, high = 0, len(raw)
    best: dict[str, Any] = {"omitted_bytes": len(raw)}
    while low <= high:
        midpoint = (low + high) // 2
        excerpt = raw[:midpoint].decode("utf-8", errors="ignore")
        candidate = {
            "excerpt": excerpt,
            "omitted_bytes": len(raw) - len(excerpt.encode("utf-8")),
        }
        if _encoded_size(candidate) <= limit:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _bounded_string(value: str, limit: int) -> str:
    if _encoded_size(value) <= limit:
        return value
    raw = value.encode("utf-8")
    low, high = 0, len(raw)
    best = f"[omitted_bytes={len(raw)}]"
    while low <= high:
        midpoint = (low + high) // 2
        excerpt = raw[:midpoint].decode("utf-8", errors="ignore")
        omitted = len(raw) - len(excerpt.encode("utf-8"))
        candidate = f"{excerpt} [omitted_bytes={omitted}]"
        if _encoded_size(candidate) <= limit:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _bounded_optional_string(value: str | None, limit: int) -> str | None:
    return _bounded_string(value, limit) if value is not None else None


def _optional_non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


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


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
