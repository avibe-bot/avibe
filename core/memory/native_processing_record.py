"""Bounded, authorization-scoped projection of native EverOS processing data."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

import yaml

from core.memory.processing_record import (
    ProcessingSourceObservations,
    SourceObservation,
)
from vibe.memory_project_ids import is_persisted_memory_project_id
from core.memory.secret_scrubber import scrub_text
from core.memory.store import (
    derive_assistant_memory_owner_id,
    is_memory_owner_id,
    is_principal_id,
)


MemoryReadScope: TypeAlias = tuple[str, str]

_APP_ID = "avibe"
_MEMCELL_COLUMNS = (
    "memcell_id, app_id, project_id, session_id, track, raw_type, "
    "message_ids_json, sender_ids_json, payload_json, timestamp"
)
_RUN_COLUMNS = (
    "run_id, strategy_name, status, attempt, started_at, finished_at, error, "
    "event_topic, event_payload, max_retries_snapshot, event_id"
)
_INDEX_COLUMNS = (
    "md_path, kind, change_type, mtime, first_seen_at, last_changed_at, lsn, "
    "status, retryable, last_attempt_at, retry_count, error"
)
_RUN_STATUSES = {"running", "success", "failed", "dead_letter", "crashed"}
_ENTRY_OPEN_RE = re.compile(r"<!-- entry:([A-Za-z0-9_-]+) -->")
_INLINE_RE = re.compile(
    r"^\*\*(?P<key>[^*\n]+?)\*\*:\s*(?P<value>.*?)\s*$", re.MULTILINE
)
_SECTION_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_MAX_CURSOR_BYTES = 256
_MAX_MEMCELL_ID_BYTES = 174
_MAX_ID_BYTES = 256
_MAX_SESSION_ID_BYTES = 256
_MAX_TIMESTAMP_MS = 4_102_444_800_000
_MAX_MESSAGE_IDS_BYTES = 16 * 1024
_MAX_JSON_BYTES = 64 * 1024
_MAX_MARKDOWN_BYTES = 256 * 1024
_MAX_MARKDOWN_FILES = 32
_MAX_PAYLOAD_ITEMS = 20
_MAX_TEXT_BYTES = 8 * 1024
_MAX_RUNS = 50
_MAX_SEMANTIC_ITEMS = 50
_MEMCELL_TIMESTAMP_SQL = (
    f"MIN({_MAX_TIMESTAMP_MS}, CASE WHEN typeof(timestamp) IN ('integer', 'real') "
    "THEN MAX(0, CAST(timestamp AS INTEGER)) "
    "ELSE MAX(0, COALESCE(CAST(strftime('%s', timestamp) AS INTEGER) * 1000 + "
    "CASE WHEN instr(timestamp, '.') > 0 THEN "
    "CAST(CAST('0.' || substr(timestamp, instr(timestamp, '.') + 1) AS REAL) "
    "* 1000 AS INTEGER) ELSE 0 END, 0)) END)"
)


class NativeProcessingRecordReader:
    """Read retained EverOS facts without inventing cross-source linkage."""

    def __init__(
        self,
        everos_root: Path,
        *,
        provider_base_urls: tuple[str, ...] = (),
        exact_redaction_values: tuple[str, ...] = (),
    ) -> None:
        self._root = Path(everos_root)
        self._system_db = self._root / ".index" / "sqlite" / "system.db"
        self._ome_db = self._root / ".index" / "sqlite" / "ome.db"
        self._base_urls = provider_base_urls
        self._redactions = exact_redaction_values

    def source_observation(self) -> ProcessingSourceObservations:
        observed = _utc_now()
        return ProcessingSourceObservations(
            memcells=_sqlite_source(
                self._root,
                self._system_db,
                f"SELECT {_MEMCELL_COLUMNS} FROM memcell LIMIT 1",
                observed,
                "native_memcells_unavailable",
            ),
            runs=_sqlite_source(
                self._root,
                self._ome_db,
                f"SELECT {_RUN_COLUMNS} FROM run_record LIMIT 1",
                observed,
                "native_runs_unavailable",
            ),
            semantic=_directory_source(self._root, observed),
        )

    def list_records(
        self,
        scope: MemoryReadScope,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        cursor_key = _decode_cursor(cursor) if cursor is not None else None
        _validate_limit(limit)
        rows, memcell_source = self._read_memcells(
            principal_id,
            project_id,
            cursor_key=cursor_key,
            limit=limit + 1,
        )
        page = rows[:limit]
        run_source = _sqlite_source(
            self._root,
            self._ome_db,
            f"SELECT {_RUN_COLUMNS} FROM run_record LIMIT 1",
            _utc_now(),
            "native_runs_unavailable",
        )
        runs_by_memcell = self._runs_projections(page)
        entries = []
        for row in page:
            memcell_id = str(row["memcell_id"])
            owner = _memcell_owner(row)
            payload = _payload_projection(row, owner)
            runs = runs_by_memcell[memcell_id]
            entries.append(
                {
                    "memcell_id": memcell_id,
                    "project_id": str(row["project_id"]),
                    "session_id": str(row["session_id"]),
                    "owner_id": owner,
                    "timestamp_ms": _timestamp_ms(row["timestamp"]),
                    "preview": _payload_preview(payload),
                    "payload": _section_summary(payload),
                    "runs": _run_summary(runs),
                }
            )
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
            "sections": {
                "memcells": _source_payload(memcell_source),
                "runs": _source_payload(run_source),
                "semantic": _source_payload(
                    _directory_source(self._root, _utc_now())
                ),
            },
        }

    def record_detail(
        self,
        scope: MemoryReadScope,
        memcell_id: str,
    ) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        _validate_id(memcell_id)
        rows, source = self._read_memcells(
            principal_id,
            project_id,
            memcell_id=memcell_id,
            limit=1,
        )
        if not rows:
            if source.status == "available":
                return {"status": "not_found"}
            return {
                "status": "failed",
                "error": "memory_processing_failed",
                "sections": {"payload": _source_payload(source)},
            }
        row = rows[0]
        owner = _memcell_owner(row)
        payload = _payload_projection(row, owner)
        runs = self._runs_projections([row])[str(row["memcell_id"])]
        semantic, linked_paths = self._semantic_projection(row)
        current = self._current_state_projection(row, linked_paths)
        return {
            "status": "ok",
            "entry": {
                "memcell_id": str(row["memcell_id"]),
                "project_id": str(row["project_id"]),
                "session_id": str(row["session_id"]),
                "owner_id": owner,
                "timestamp_ms": _timestamp_ms(row["timestamp"]),
            },
            "payload": payload,
            "runs": runs,
            "semantic": semantic,
            "current_state": current,
        }

    def _read_memcells(
        self,
        principal_id: str,
        project_id: str,
        *,
        cursor_key: tuple[int, str] | None = None,
        memcell_id: str | None = None,
        limit: int,
    ) -> tuple[list[sqlite3.Row], SourceObservation]:
        observed = _utc_now()
        with _read_only(self._root, self._system_db) as conn:
            if conn is None:
                return [], SourceObservation(
                    "unavailable", observed, "native_memcells_unavailable"
                )
            try:
                conn.execute(
                    f"SELECT {_MEMCELL_COLUMNS} FROM memcell LIMIT 1"
                ).fetchone()
                predicates = [
                    "app_id = ?",
                    "project_id = ?",
                    "typeof(memcell_id) = 'text'",
                    "length(CAST(memcell_id AS BLOB)) BETWEEN 1 AND ?",
                    "typeof(session_id) = 'text'",
                    "length(CAST(session_id AS BLOB)) BETWEEN 1 AND ?",
                    "length(CAST(sender_ids_json AS BLOB)) <= 1024",
                    "json_valid(sender_ids_json)",
                    "json_type(sender_ids_json) = 'array'",
                    "json_array_length(sender_ids_json) = 1",
                    "json_type(sender_ids_json, '$[0]') = 'text'",
                    "json_extract(sender_ids_json, '$[0]') IN (?, ?)",
                ]
                args: list[object] = [
                    _APP_ID,
                    project_id,
                    _MAX_MEMCELL_ID_BYTES,
                    _MAX_SESSION_ID_BYTES,
                    principal_id,
                    derive_assistant_memory_owner_id(principal_id),
                ]
                if memcell_id is not None:
                    predicates.append("memcell_id = ?")
                    args.append(memcell_id)
                inner = f"""
                    SELECT memcell_id, app_id, project_id, session_id, track,
                           raw_type,
                           CASE WHEN length(CAST(message_ids_json AS BLOB)) <= ?
                                THEN message_ids_json ELSE NULL END AS message_ids_json,
                           CASE WHEN length(CAST(message_ids_json AS BLOB)) > ?
                                THEN 1 ELSE 0 END AS message_ids_withheld,
                           sender_ids_json,
                           CASE WHEN length(CAST(payload_json AS BLOB)) <= ?
                                THEN payload_json ELSE NULL END AS payload_json,
                           CASE WHEN length(CAST(payload_json AS BLOB)) > ?
                                THEN 1 ELSE 0 END AS payload_withheld,
                           timestamp, {_MEMCELL_TIMESTAMP_SQL} AS timestamp_ms
                    FROM memcell
                    WHERE {' AND '.join(predicates)}
                """
                query_args: list[object] = [
                    _MAX_MESSAGE_IDS_BYTES,
                    _MAX_MESSAGE_IDS_BYTES,
                    _MAX_JSON_BYTES,
                    _MAX_JSON_BYTES,
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
            except (sqlite3.Error, UnicodeError):
                return [], SourceObservation(
                    "unavailable", observed, "native_memcells_unavailable"
                )
        return rows, SourceObservation("available", observed)

    def _runs_projections(
        self, rows: list[sqlite3.Row]
    ) -> dict[str, dict[str, Any]]:
        memcells = {str(row["memcell_id"]): row for row in rows}
        if not memcells:
            return {}
        unavailable = {
            memcell_id: _unavailable("native_runs_unavailable")
            for memcell_id in memcells
        }
        with _read_only(self._root, self._ome_db) as conn:
            if conn is None:
                return unavailable
            try:
                conn.execute(
                    f"SELECT {_RUN_COLUMNS} FROM run_record LIMIT 1"
                ).fetchone()
                placeholders = ",".join("?" for _ in memcells)
                candidates = list(
                    conn.execute(
                        f"""
                        WITH ranked AS (
                            SELECT {_RUN_COLUMNS},
                                   json_extract(event_payload, '$.memcell_id')
                                       AS linked_memcell_id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY json_extract(
                                           event_payload, '$.memcell_id'
                                       )
                                       ORDER BY started_at DESC, run_id DESC
                                   ) AS row_number
                            FROM run_record
                            WHERE length(CAST(event_payload AS BLOB)) <= ?
                              AND json_valid(event_payload)
                              AND json_extract(event_payload, '$.app_id') = ?
                              AND json_extract(event_payload, '$.project_id') = ?
                              AND json_extract(event_payload, '$.memcell_id')
                                  IN ({placeholders})
                        )
                        SELECT {_RUN_COLUMNS}, linked_memcell_id
                        FROM ranked
                        WHERE row_number <= ?
                        ORDER BY linked_memcell_id, started_at DESC, run_id DESC
                        """,
                        (
                            _MAX_JSON_BYTES,
                            _APP_ID,
                            str(rows[0]["project_id"]),
                            *memcells,
                            _MAX_RUNS + 1,
                        ),
                    )
                )
            except (sqlite3.Error, UnicodeError):
                return unavailable
        grouped: dict[str, list[sqlite3.Row]] = {
            memcell_id: [] for memcell_id in memcells
        }
        for candidate in candidates:
            linked_memcell_id = candidate["linked_memcell_id"]
            if isinstance(linked_memcell_id, str) and linked_memcell_id in grouped:
                grouped[linked_memcell_id].append(candidate)
        return {
            memcell_id: self._project_run_candidates(row, grouped[memcell_id])
            for memcell_id, row in memcells.items()
        }

    def _project_run_candidates(
        self, row: sqlite3.Row, candidates: list[sqlite3.Row]
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        rejected = 0
        for candidate in candidates[:_MAX_RUNS]:
            projected = self._run_projection(row, candidate)
            if projected is None:
                rejected += 1
            else:
                items.append(projected)
        if not items:
            return _unavailable("native_runs_missing_or_retained")
        return {
            "status": "partial",
            "reason": "native_run_retention_bounded",
            "items": items,
            "omitted_count": max(0, len(candidates) - _MAX_RUNS) + rejected,
        }

    def _run_projection(
        self, memcell: sqlite3.Row, run: sqlite3.Row
    ) -> dict[str, Any] | None:
        payload = _decode_json(run["event_payload"])
        owner = _memcell_owner(memcell)
        if not isinstance(payload, dict):
            return None
        has_session = "session_id" in payload
        has_owner = "owner_id" in payload
        if (
            (has_session and payload.get("session_id") != memcell["session_id"])
            or (has_owner and payload.get("owner_id") != owner)
        ):
            return None
        status = run["status"]
        attempt = run["attempt"]
        started_at = _validated_timestamp(run["started_at"])
        finished_at = _validated_timestamp(run["finished_at"])
        if (
            status not in _RUN_STATUSES
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 0
            or started_at is None
            or (run["finished_at"] is not None and finished_at is None)
            or not isinstance(run["run_id"], str)
            or not isinstance(run["strategy_name"], str)
            or not isinstance(run["event_topic"], str)
        ):
            return None
        error = run["error"]
        scrubbed_error = (
            _bounded_text(
                scrub_text(
                    str(error),
                    base_urls=self._base_urls,
                    exact_values=self._redactions,
                ),
                1024,
            )
            if error is not None
            else None
        )
        return {
            "run_id": _bounded_text(str(run["run_id"]), _MAX_ID_BYTES),
            "strategy": _bounded_text(str(run["strategy_name"]), 256),
            "attempt": attempt,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "error": scrubbed_error,
            "event_topic": _bounded_text(str(run["event_topic"]), 256),
        }

    def _semantic_projection(
        self, row: sqlite3.Row
    ) -> tuple[dict[str, Any], set[str]]:
        owner = _memcell_owner(row)
        root = self._owner_root(str(row["project_id"]), owner)
        episodes, episode_paths, incomplete = _linked_entries(
            self._root,
            root / "episodes",
            "episode-*.md",
            expected_file_type="episode_daily",
            owner_id=owner,
            session_id=str(row["session_id"]),
            parent_type="memcell",
            parent_ids={str(row["memcell_id"])},
            content_section="Content",
            kind="episode",
        )
        episode_ids = {item["entry_id"] for item in episodes}
        facts, fact_paths, fact_incomplete = _linked_entries(
            self._root,
            root / ".atomic_facts",
            "atomic_fact-*.md",
            expected_file_type="atomic_fact_daily",
            owner_id=owner,
            session_id=str(row["session_id"]),
            parent_type="episode",
            parent_ids=episode_ids,
            content_section="Fact",
            kind="fact",
        )
        items = (episodes + facts)[:_MAX_SEMANTIC_ITEMS]
        linked_paths = episode_paths | fact_paths
        incomplete = (
            incomplete
            or fact_incomplete
            or len(episodes) + len(facts) > _MAX_SEMANTIC_ITEMS
        )
        if not items:
            return _unavailable("semantic_results_missing_or_retained"), linked_paths
        return (
            {
                "status": "partial" if incomplete else "available",
                "reason": "semantic_projection_bounded" if incomplete else None,
                "items": items,
                "omitted_count": max(
                    0, len(episodes) + len(facts) - _MAX_SEMANTIC_ITEMS
                ),
            },
            linked_paths,
        )

    def _current_state_projection(
        self, row: sqlite3.Row, linked_paths: set[str]
    ) -> dict[str, Any]:
        owner = _memcell_owner(row)
        owner_root = self._owner_root(str(row["project_id"]), owner)
        profile_path = owner_root / "user.md"
        profile = {"status": "missing", "updated_at_ms": None}
        profile_rel = _relative_path(self._root, profile_path)
        if profile_rel is not None and _safe_regular_file(self._root, profile_path):
            linked_paths.add(profile_rel)
            parsed = _read_markdown(profile_path)
            if parsed is not None:
                frontmatter, _body = parsed
                if (
                    frontmatter.get("type") == "user_profile"
                    and frontmatter.get("user_id") == owner
                    and frontmatter.get("track") == "user"
                ):
                    profile = {
                        "status": "present",
                        "updated_at_ms": _non_negative_int(
                            frontmatter.get("profile_timestamp_ms")
                        ),
                    }
        indexing = self._index_state(linked_paths)
        if profile["status"] == "missing" and indexing["status"] == "unavailable":
            return _unavailable("current_state_unavailable")
        return {
            "status": (
                "available" if indexing["status"] == "available" else "partial"
            ),
            "reason": (
                None
                if indexing["status"] == "available"
                else indexing.get("reason")
            ),
            "label": "current_unattributed",
            "profile": profile,
            "indexing": indexing,
        }

    def _index_state(self, paths: set[str]) -> dict[str, Any]:
        if not paths:
            return _unavailable("index_state_unavailable")
        with _read_only(self._root, self._system_db) as conn:
            if conn is None:
                return _unavailable("index_state_unavailable")
            try:
                conn.execute(
                    f"SELECT {_INDEX_COLUMNS} FROM md_change_state LIMIT 1"
                ).fetchone()
                bounded_paths = sorted(paths)[:_MAX_SEMANTIC_ITEMS]
                placeholders = ",".join("?" for _ in bounded_paths)
                rows = list(
                    conn.execute(
                        "SELECT md_path, status, last_changed_at, error "
                        f"FROM md_change_state WHERE md_path IN ({placeholders}) "
                        "ORDER BY md_path",
                        bounded_paths,
                    )
                )
            except (sqlite3.Error, UnicodeError):
                return _unavailable("index_state_unavailable")
        items = [
            {
                "md_path": str(item["md_path"]),
                "status": str(item["status"]),
                "updated_at": _bounded_optional_text(item["last_changed_at"], 128),
                "error": (
                    _bounded_text(
                        scrub_text(
                            str(item["error"]),
                            base_urls=self._base_urls,
                            exact_values=self._redactions,
                        ),
                        1024,
                    )
                    if item["error"] is not None
                    else None
                ),
            }
            for item in rows
        ]
        if not items:
            return _unavailable("index_state_missing_or_retained")
        omitted_count = len(paths) - len(items)
        complete = omitted_count == 0 and {
            str(item["md_path"]) for item in rows
        } == set(bounded_paths)
        return {
            "status": "available" if complete else "partial",
            "reason": None if complete else "index_state_incomplete",
            "items": items,
            "omitted_count": omitted_count,
        }

    def _owner_root(self, project_id: str, owner_id: str) -> Path:
        project_dir = "default_project" if project_id == "default" else project_id
        return self._root / _APP_ID / project_dir / "users" / owner_id


def _payload_projection(row: sqlite3.Row, owner_id: str) -> dict[str, Any]:
    if row["payload_json"] is None:
        return _unavailable(
            "payload_projection_limit" if row["payload_withheld"] else "payload_unavailable"
        )
    payload = _decode_json(row["payload_json"])
    message_ids = _decode_json(row["message_ids_json"])
    if row["message_ids_withheld"]:
        return _unavailable("payload_projection_limit")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("items"), list)
        or not isinstance(message_ids, list)
        or any(not isinstance(value, str) for value in message_ids)
    ):
        return _unavailable("payload_malformed")
    allowed_ids = set(message_ids)
    projected: list[dict[str, Any]] = []
    rejected = 0
    projection_incomplete = False
    for item in payload["items"]:
        if len(projected) >= _MAX_PAYLOAD_ITEMS:
            rejected += 1
            continue
        item_projection = _authorized_payload_item(item, owner_id, allowed_ids)
        if item_projection is None:
            rejected += 1
        else:
            value, unsupported_blocks_omitted = item_projection
            projected.append(value)
            projection_incomplete = (
                projection_incomplete
                or unsupported_blocks_omitted
                or any(block["omitted_bytes"] > 0 for block in value["content"])
            )
    if not projected:
        return _unavailable("authorized_user_payload_unavailable")
    return {
        "status": "partial" if rejected or projection_incomplete else "available",
        "reason": (
            "unauthorized_or_bounded_items_omitted"
            if rejected or projection_incomplete
            else None
        ),
        "items": projected,
        "omitted_count": rejected,
    }


def _authorized_payload_item(
    value: object, owner_id: str, message_ids: set[str]
) -> tuple[dict[str, Any], bool] | None:
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    timestamp = value.get("timestamp")
    if (
        value.get("kind") != "text"
        or value.get("role") != "user"
        or value.get("sender_id") != owner_id
        or not _valid_utf8_text(item_id, _MAX_ID_BYTES)
        or item_id not in message_ids
        or not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or timestamp < 0
    ):
        return None
    content = value.get("content")
    unsupported_blocks_omitted = False
    if isinstance(content, str):
        blocks = [_text_block(content)]
    elif isinstance(content, list):
        blocks = []
        for block in content:
            if (
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                unsupported_blocks_omitted = True
                continue
            blocks.append(_text_block(block["text"]))
    else:
        return None
    if not blocks:
        return None
    return (
        {
            "id": item_id,
            "timestamp_ms": timestamp,
            "sender_id": owner_id,
            "content": blocks,
        },
        unsupported_blocks_omitted,
    )


def _text_block(text: str) -> dict[str, Any]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= _MAX_TEXT_BYTES:
        return {
            "type": "text",
            "text": raw.decode("utf-8"),
            "omitted_bytes": 0,
        }
    excerpt = raw[:_MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return {
        "type": "text",
        "text": excerpt,
        "omitted_bytes": len(raw) - len(excerpt.encode("utf-8")),
    }


def _linked_entries(
    memory_root: Path,
    directory: Path,
    pattern: str,
    *,
    expected_file_type: str,
    owner_id: str,
    session_id: str,
    parent_type: str,
    parent_ids: set[str],
    content_section: str,
    kind: str,
) -> tuple[list[dict[str, Any]], set[str], bool]:
    if not parent_ids or not directory.is_dir() or directory.is_symlink():
        return [], set(), False
    try:
        candidates = sorted(directory.glob(pattern), reverse=True)
    except OSError:
        return [], set(), True
    incomplete = len(candidates) > _MAX_MARKDOWN_FILES
    items: list[dict[str, Any]] = []
    paths: set[Path] = set()
    for path in candidates[:_MAX_MARKDOWN_FILES]:
        if not _safe_regular_file(memory_root, path):
            incomplete = True
            continue
        parsed = _read_markdown(path)
        if parsed is None:
            incomplete = True
            continue
        frontmatter, body = parsed
        if (
            frontmatter.get("file_type") != expected_file_type
            or frontmatter.get("user_id") != owner_id
            or frontmatter.get("track") != "user"
        ):
            incomplete = True
            continue
        for entry_id, entry_body in _split_entries(body):
            inline, sections = _structured_entry(entry_body)
            if (
                inline.get("owner_id") != owner_id
                or inline.get("session_id") != session_id
                or inline.get("parent_type") != parent_type
                or inline.get("parent_id") not in parent_ids
            ):
                continue
            content = sections.get(content_section)
            if not isinstance(content, str):
                incomplete = True
                continue
            if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
                incomplete = True
            item = {
                "kind": kind,
                "entry_id": entry_id,
                "timestamp": _bounded_optional_text(inline.get("timestamp"), 128),
                "content": _bounded_text(content, _MAX_TEXT_BYTES),
            }
            if kind == "episode":
                item["subject"] = _bounded_optional_text(
                    sections.get("Subject"), _MAX_TEXT_BYTES
                )
                item["summary"] = _bounded_optional_text(
                    sections.get("Summary"), _MAX_TEXT_BYTES
                )
            items.append(item)
            paths.add(path)
    relative_paths = {
        relative
        for path in paths
        if (relative := _relative_path(memory_root, path)) is not None
    }
    return items, relative_paths, incomplete


def _read_markdown(path: Path) -> tuple[dict[str, Any], str] | None:
    try:
        if path.stat().st_size > _MAX_MARKDOWN_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        frontmatter = yaml.safe_load(text[4:end])
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    if not isinstance(frontmatter, dict):
        return None
    return frontmatter, text[end + 5 :]


def _split_entries(body: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    position = 0
    while match := _ENTRY_OPEN_RE.search(body, position):
        entry_id = match.group(1)
        close = re.compile(rf"<!-- /entry:{re.escape(entry_id)} -->").search(
            body, match.end()
        )
        if close is None:
            break
        entries.append((entry_id, body[match.end() : close.start()].strip("\r\n")))
        position = close.end()
    return entries


def _structured_entry(body: str) -> tuple[dict[str, str], dict[str, str]]:
    parts = _SECTION_RE.split(body.strip("\n"))
    inline = {
        match.group("key").strip(): match.group("value").strip()
        for match in _INLINE_RE.finditer(parts[0])
    }
    sections: dict[str, str] = {}
    for index in range(1, len(parts), 2):
        title = parts[index].strip()
        sections[title] = (
            parts[index + 1].strip("\n").rstrip()
            if index + 1 < len(parts)
            else ""
        )
    return inline, sections


def _safe_regular_file(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        current = root
        if current.is_symlink():
            return False
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
        return path.is_file()
    except (OSError, ValueError):
        return False


def _relative_path(root: Path, path: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _memcell_owner(row: sqlite3.Row) -> str:
    senders = _decode_json(row["sender_ids_json"])
    if (
        not isinstance(senders, list)
        or len(senders) != 1
        or not is_memory_owner_id(senders[0])
    ):
        raise ValueError("invalid native memcell owner")
    return senders[0]


def _payload_preview(payload: dict[str, Any]) -> str:
    if payload.get("status") not in {"available", "partial"}:
        return ""
    blocks = payload["items"][0]["content"]
    return _bounded_text(" ".join(block["text"] for block in blocks), 512)


def _section_summary(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": section["status"],
        "reason": section.get("reason"),
        "item_count": len(section.get("items", [])),
    }


def _run_summary(section: dict[str, Any]) -> dict[str, Any]:
    items = section.get("items", [])
    statuses: dict[str, int] = {}
    for item in items:
        status = item["status"]
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "status": section["status"],
        "reason": section.get("reason"),
        "total": len(items),
        "statuses": statuses,
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


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")


def _validate_id(value: str) -> None:
    if not _valid_utf8_text(value, _MAX_MEMCELL_ID_BYTES):
        raise ValueError("invalid memcell id")


def _sqlite_source(
    root: Path, path: Path, query: str, observed: str, reason: str
) -> SourceObservation:
    with _read_only(root, path) as conn:
        if conn is None:
            return SourceObservation("unavailable", observed, reason)
        try:
            conn.execute(query).fetchone()
        except (sqlite3.Error, UnicodeError):
            return SourceObservation("unavailable", observed, reason)
    return SourceObservation("available", observed)


def _directory_source(path: Path, observed: str) -> SourceObservation:
    try:
        available = path.is_dir() and not path.is_symlink()
    except OSError:
        available = False
    return (
        SourceObservation("available", observed)
        if available
        else SourceObservation("unavailable", observed, "native_semantic_unavailable")
    )


def _source_payload(source: SourceObservation) -> dict[str, Any]:
    return {
        "status": source.status,
        "observed_at": source.observed_at,
        "reason": source.reason,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "items": []}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _timestamp_ms(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return min(_MAX_TIMESTAMP_MS, max(0, int(value)))
        except (OverflowError, ValueError):
            return 0
    if not isinstance(value, str):
        return 0
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return min(
            _MAX_TIMESTAMP_MS,
            max(0, int(instant.timestamp() * 1000)),
        )
    except (ValueError, OverflowError):
        return 0


def _encode_cursor(timestamp_ms: int, memcell_id: str) -> str:
    raw = json.dumps(
        [timestamp_ms, memcell_id], separators=(",", ":"), ensure_ascii=False
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(encoded) > _MAX_CURSOR_BYTES:
        raise ValueError("generated cursor exceeds its budget")
    return encoded


def _decode_cursor(cursor: str) -> tuple[int, str]:
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > _MAX_CURSOR_BYTES
        or any(
            char
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for char in cursor
        )
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
        or not 0 <= value[0] <= _MAX_TIMESTAMP_MS
        or not _valid_utf8_text(value[1], _MAX_MEMCELL_ID_BYTES)
        or _encode_cursor(value[0], value[1]) != cursor
    ):
        raise ValueError("invalid cursor")
    return value[0], value[1]


def _decode_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None


def _bounded_text(value: str, limit: int) -> str:
    raw = value.encode("utf-8", errors="replace")
    return raw[:limit].decode("utf-8", errors="ignore")


def _bounded_optional_text(value: object, limit: int) -> str | None:
    return _bounded_text(str(value), limit) if value is not None else None


def _validated_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not _valid_utf8_text(value, 128):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _non_negative_int(value: object) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_TIMESTAMP_MS
    ):
        return value
    return None


@contextmanager
def _read_only(root: Path, path: Path):
    if not _safe_regular_file(root, path):
        yield None
        return
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


def _valid_utf8_text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False
