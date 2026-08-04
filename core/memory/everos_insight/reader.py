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

from .recorder import _scrub_json, _scrub_text

MemoryReadScope: TypeAlias = tuple[str, str]

_APP_ID = "avibe"
_MAX_CURSOR_BYTES = 88
_MAX_MEMCELL_ID_BYTES = 256
_MAX_DETAIL_CALLS = 20
_MAX_DETAIL_RUNS = 50
_MAX_PAYLOAD_FIELD_BYTES = 12_000
_MAX_ERROR_BYTES = 1_024
_MAX_RESPONSE_BYTES = 1_000_000
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]+")
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_PRINCIPAL_RE = re.compile(r"u-[0-9a-f]{32}")
_PROJECT_RE = re.compile(r"p-[0-9a-f]{32}")
_ATTACHMENT_TYPES = frozenset(
    {"attachment", "audio", "document", "file", "image", "input_audio", "input_file", "input_image"}
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


class MemoryInsightReader:
    """Synchronous, owner-scoped projection over pinned EverOS diagnostics."""

    def __init__(
        self,
        paths: MemoryInsightPaths,
        *,
        provider_base_urls: Sequence[str] = (),
    ) -> None:
        if isinstance(provider_base_urls, str) or any(
            not isinstance(url, str) for url in provider_base_urls
        ):
            raise TypeError("provider_base_urls must be a sequence of strings")
        self._paths = paths
        self._provider_base_urls = tuple(url.rstrip("/") for url in provider_base_urls if url)

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

        memcells, everos_section = self._read_memcells(project_id=project_id)
        queues, capture_section = self._read_capture_rows()
        calls, call_section = self._read_call_rows()
        runs, runs_section = self._read_run_rows()
        sections = {
            "everos": _combine_everos_section(everos_section, runs_section),
            "capture": capture_section,
            "calls": call_section,
        }
        if memcells is None:
            return {
                "status": "ok",
                "entries": [],
                "next_cursor": None,
                "sections": sections,
            }

        authorized = [
            row
            for row in memcells
            if _memcell_owned_by(row, principal_id=principal_id, project_id=project_id)
        ]
        authorized.sort(key=lambda row: (_memcell_timestamp_ms(row), str(row["memcell_id"])), reverse=True)
        if cursor_key is not None:
            authorized = [
                row
                for row in authorized
                if (_memcell_timestamp_ms(row), str(row["memcell_id"])) < cursor_key
            ]

        page = authorized[: limit + 1]
        has_more = len(page) > limit
        page = page[:limit]
        entries = [
            self._list_entry(
                row,
                principal_id=principal_id,
                project_id=project_id,
                queues=queues,
                runs=runs,
                calls=calls,
                base_urls=self._provider_base_urls,
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

    def entry_detail(self, scope: MemoryReadScope, memcell_id: str) -> dict[str, Any]:
        principal_id, project_id = _validated_scope(scope)
        if not isinstance(memcell_id, str) or not _ID_RE.fullmatch(memcell_id):
            raise ValueError("invalid memcell id")

        memcells, everos_section = self._read_memcells(
            project_id=project_id,
            memcell_id=memcell_id,
        )
        if memcells is None:
            return {
                "status": "not_found",
                "sections": {
                    "everos": everos_section,
                    "capture": _source_status(self._paths.capture_db_path),
                    "calls": _source_status(self._paths.call_log_db_path),
                },
            }
        row = next(
            (
                candidate
                for candidate in memcells
                if _memcell_owned_by(candidate, principal_id=principal_id, project_id=project_id)
            ),
            None,
        )
        if row is None:
            return {"status": "not_found"}

        queues, capture_section = self._read_capture_rows()
        calls, call_section = self._read_call_rows()
        runs, runs_section = self._read_run_rows()
        owned_runs = _authorized_runs(
            runs or [],
            memcell_id=memcell_id,
            principal_id=principal_id,
            project_id=project_id,
        )
        owned_calls = _authorized_calls(
            calls or [],
            memcell=row,
            queues=queues or [],
            runs=owned_runs,
            principal_id=principal_id,
            project_id=project_id,
        )
        owned_calls.sort(
            key=lambda item: (
                _optional_non_negative_int(item["started_at_ms"]) or 0,
                str(item["id"]),
            ),
            reverse=True,
        )
        owned_runs.sort(key=lambda item: (_timestamp_ms(item["started_at"]), str(item["run_id"])), reverse=True)

        selected_calls = owned_calls[:_MAX_DETAIL_CALLS]
        selected_runs = owned_runs[:_MAX_DETAIL_RUNS]
        capture = _capture_projection(
            row,
            queues,
            capture_section,
            principal_id=principal_id,
            project_id=project_id,
            base_urls=self._provider_base_urls,
        )
        steps = _steps_projection(
            row,
            capture,
            selected_runs,
            base_urls=self._provider_base_urls,
        )
        current_state = self._current_state(principal_id, project_id, everos_section)
        result: dict[str, Any] = {
            "status": "ok",
            "entry": _entry_projection(row, base_urls=self._provider_base_urls),
            "capture": capture,
            "steps": steps,
            "calls": [
                _call_projection(call, base_urls=self._provider_base_urls)
                for call in selected_calls
            ],
            "omitted_call_count": len(owned_calls) - len(selected_calls),
            "omitted_step_count": len(owned_runs) - len(selected_runs),
            "current_state": current_state,
            "sections": {
                "everos": _combine_everos_section(everos_section, runs_section),
                "capture": capture_section,
                "calls": call_section,
            },
        }
        if _encoded_size(result) > _MAX_RESPONSE_BYTES:
            raise AssertionError("memory insight detail exceeded its fixed response budget")
        return result

    def _list_entry(
        self,
        row: sqlite3.Row,
        *,
        principal_id: str,
        project_id: str,
        queues: list[sqlite3.Row] | None,
        runs: list[sqlite3.Row] | None,
        calls: list[sqlite3.Row] | None,
        base_urls: tuple[str, ...],
    ) -> dict[str, Any]:
        owned_runs = _authorized_runs(
            runs or [],
            memcell_id=str(row["memcell_id"]),
            principal_id=principal_id,
            project_id=project_id,
        )
        owned_calls = _authorized_calls(
            calls or [],
            memcell=row,
            queues=queues or [],
            runs=owned_runs,
            principal_id=principal_id,
            project_id=project_id,
        )
        statuses: dict[str, int] = {}
        for run in owned_runs:
            status = _bounded_string(_scrub(str(run["status"]), base_urls), 128)
            statuses[status] = statuses.get(status, 0) + 1
        return {
            **_entry_projection(row, base_urls=base_urls),
            "run_summary": {"total": len(owned_runs), "statuses": statuses} if runs is not None else None,
            "authorized_call_count": len(owned_calls) if calls is not None else None,
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
                    _scrub(str(row["status"]), self._provider_base_urls),
                    128,
                ),
                "updated_at_ms": _timestamp_ms(row["last_changed_at"]),
                "error": _bounded_optional_string(
                    _scrub_optional(row["error"], self._provider_base_urls),
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

    def _read_memcells(
        self,
        *,
        project_id: str,
        memcell_id: str | None = None,
    ) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.system_db_path) as conn:
                sql = (
                    "SELECT memcell_id, app_id, project_id, message_ids_json, "
                    "sender_ids_json, payload_json, timestamp FROM memcell"
                )
                sql += " WHERE app_id = ? AND project_id = ?"
                args: tuple[str, ...] = (_APP_ID, project_id)
                if memcell_id is not None:
                    sql += " AND memcell_id = ?"
                    args = (*args, memcell_id)
                return list(conn.execute(sql, args)), {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_capture_rows(self) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.capture_db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT session_id, principal_id, project_ref, "
                        "provider_timestamp_ms, state, occurred_at_ms, add_request_id, flush_request_id "
                        "FROM memory_capture_queue"
                    )
                )
            return rows, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_run_rows(self) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.ome_db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT run_id, strategy_name, status, attempt, started_at, finished_at, "
                        "error, event_topic, event_payload FROM run_record"
                    )
                )
            return rows, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}

    def _read_call_rows(self) -> tuple[list[sqlite3.Row] | None, dict[str, str]]:
        try:
            with _read_only(self._paths.call_log_db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT id, started_at_ms, duration_ms, kind, stage, model, status, "
                        "error, finish_reason, prompt_tokens, completion_tokens, request_json, "
                        "response_json, request_bytes, response_bytes, request_id, run_id, "
                        "memcell_id, app_id, project_id, owner_id, parent_type, parent_id, "
                        "dropped_before FROM provider_call"
                    )
                )
            return rows, {"status": "available"}
        except _Unavailable as unavailable:
            return None, {"status": "unavailable", "reason": unavailable.reason}


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


def _validated_scope(scope: MemoryReadScope) -> MemoryReadScope:
    if not isinstance(scope, tuple) or len(scope) != 2:
        raise ValueError("invalid memory scope")
    principal_id, project_id = scope
    if not isinstance(principal_id, str) or not _PRINCIPAL_RE.fullmatch(principal_id):
        raise ValueError("invalid memory principal")
    if not isinstance(project_id, str) or not _PROJECT_RE.fullmatch(project_id):
        raise ValueError("invalid memory project")
    return principal_id, project_id


def _decode_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return None


def _memcell_owned_by(row: sqlite3.Row, *, principal_id: str, project_id: str) -> bool:
    if row["app_id"] != _APP_ID or row["project_id"] != project_id:
        return False
    senders = _decode_json(row["sender_ids_json"])
    return (
        isinstance(senders, list)
        and len(senders) == 1
        and isinstance(senders[0], str)
        and senders[0] == principal_id
    )


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


def _request_ids_for_scope(
    memcell: sqlite3.Row,
    queues: list[sqlite3.Row],
    *,
    principal_id: str,
    project_id: str,
) -> set[str]:
    related = _related_queue_rows(
        memcell,
        queues,
        principal_id=principal_id,
        project_id=project_id,
    )
    candidate_ids = {
        request_id
        for row in related
        for request_id in (row["add_request_id"], row["flush_request_id"])
        if isinstance(request_id, str) and request_id
    }
    accepted: set[str] = set()
    for request_id in candidate_ids:
        group = [
            row
            for row in queues
            if row["add_request_id"] == request_id or row["flush_request_id"] == request_id
        ]
        if group and all(
            row["principal_id"] == principal_id and row["project_ref"] == project_id for row in group
        ):
            accepted.add(request_id)
    return accepted


def _event_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    value = _decode_json(row["event_payload"])
    return value if isinstance(value, dict) else None


def _authorized_runs(
    runs: list[sqlite3.Row],
    *,
    memcell_id: str,
    principal_id: str,
    project_id: str,
) -> list[sqlite3.Row]:
    accepted: list[sqlite3.Row] = []
    for row in runs:
        event = _event_payload(row)
        if (
            event is None
            or event.get("memcell_id") != memcell_id
            or event.get("app_id") != _APP_ID
            or event.get("project_id") != project_id
        ):
            continue
        if "owner_id" in event and event["owner_id"] != principal_id:
            continue
        accepted.append(row)
    return accepted


def _episode_entry_ids(runs: list[sqlite3.Row], memcell_id: str) -> set[str]:
    result: set[str] = set()
    for row in runs:
        event = _event_payload(row)
        topic = row["event_topic"]
        if (
            event is not None
            and event.get("memcell_id") == memcell_id
            and isinstance(topic, str)
            and topic.endswith(":EpisodeExtracted")
            and isinstance(event.get("episode_entry_id"), str)
        ):
            result.add(event["episode_entry_id"])
    return result


def _authorized_calls(
    calls: list[sqlite3.Row],
    *,
    memcell: sqlite3.Row,
    queues: list[sqlite3.Row],
    runs: list[sqlite3.Row],
    principal_id: str,
    project_id: str,
) -> list[sqlite3.Row]:
    memcell_id = str(memcell["memcell_id"])
    request_ids = _request_ids_for_scope(
        memcell,
        queues,
        principal_id=principal_id,
        project_id=project_id,
    )
    run_ids = {str(row["run_id"]) for row in runs}
    episode_ids = _episode_entry_ids(runs, memcell_id)
    accepted: list[sqlite3.Row] = []
    for row in calls:
        direct = row["memcell_id"] == memcell_id
        boundary = isinstance(row["request_id"], str) and row["request_id"] in request_ids
        strategy = isinstance(row["run_id"], str) and row["run_id"] in run_ids
        exact_cascade_scope = (
            row["stage"] == "cascade"
            and row["app_id"] == _APP_ID
            and row["project_id"] == project_id
            and row["owner_id"] == principal_id
        )
        cascade = exact_cascade_scope and (
            (row["parent_type"] == "memcell" and row["parent_id"] == memcell_id)
            or (row["parent_type"] == "episode" and row["parent_id"] in episode_ids)
        )
        if direct or boundary or strategy or cascade:
            accepted.append(row)
    return accepted


def _entry_projection(row: sqlite3.Row, *, base_urls: tuple[str, ...]) -> dict[str, Any]:
    return {
        "memcell_id": _bounded_string(
            _scrub(str(row["memcell_id"]), base_urls),
            _MAX_MEMCELL_ID_BYTES,
        ),
        "timestamp_ms": _memcell_timestamp_ms(row),
        "preview": _memcell_preview(row, base_urls=base_urls),
        "message_count": len(_message_ids(row)),
    }


def _memcell_preview(row: sqlite3.Row, *, base_urls: tuple[str, ...]) -> str:
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
                        _safe_basename(name, base_urls=base_urls)
                        if isinstance(name, str)
                        else "attachment"
                    )
                    text.append(f"[{kind}: {basename}]")
    return _bounded_string(_scrub(" ".join(text), base_urls), 512)


def _safe_basename(value: str, *, base_urls: tuple[str, ...]) -> str:
    basename = value.replace("\\", "/").rsplit("/", 1)[-1] or "attachment"
    return _bounded_string(_scrub(basename, base_urls), 128)


def _capture_projection(
    memcell: sqlite3.Row,
    queues: list[sqlite3.Row] | None,
    section: dict[str, str],
    *,
    principal_id: str,
    project_id: str,
    base_urls: tuple[str, ...],
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
            _bounded_string(_scrub(str(row["state"]), base_urls), 128)
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
    capture: dict[str, Any],
    runs: list[sqlite3.Row],
    *,
    base_urls: tuple[str, ...],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if capture.get("status") == "available":
        delivery_states = capture.get("delivery_states")
        capture_status = (
            delivery_states[0]
            if isinstance(delivery_states, list) and len(delivery_states) == 1
            else "mixed"
        )
        steps.append({"type": "capture", "status": capture_status})
    else:
        steps.append({"type": "capture", **capture})
    steps.append(
        {
            "type": "memcell",
            "status": "created",
            "timestamp_ms": _memcell_timestamp_ms(memcell),
            "memcell_id": _bounded_string(
                _scrub(str(memcell["memcell_id"]), base_urls),
                _MAX_MEMCELL_ID_BYTES,
            ),
        }
    )
    steps.extend(_run_projection(run, base_urls=base_urls) for run in runs)
    steps.sort(key=lambda step: (int(step.get("started_at_ms", step.get("timestamp_ms", 0))), str(step.get("run_id", ""))))
    return steps


def _run_projection(row: sqlite3.Row, *, base_urls: tuple[str, ...]) -> dict[str, Any]:
    strategy = _bounded_string(_scrub(str(row["strategy_name"]), base_urls), 128)
    return {
        "type": "strategy",
        "run_id": _bounded_string(_scrub(str(row["run_id"]), base_urls), 256),
        "strategy": strategy,
        "relation": "profile_trigger" if strategy == "extract_user_profile" else "run",
        "status": _bounded_string(_scrub(str(row["status"]), base_urls), 128),
        "attempt": _optional_non_negative_int(row["attempt"]) or 0,
        "started_at_ms": _timestamp_ms(row["started_at"]),
        "finished_at_ms": _timestamp_ms(row["finished_at"]) if row["finished_at"] is not None else None,
        "error": _bounded_optional_string(
            _scrub_optional(row["error"], base_urls),
            _MAX_ERROR_BYTES,
        ),
    }


def _call_projection(row: sqlite3.Row, *, base_urls: tuple[str, ...]) -> dict[str, Any]:
    request = _project_stored_json(row["request_json"], base_urls=base_urls)
    response = (
        _project_stored_json(row["response_json"], base_urls=base_urls)
        if row["response_json"] is not None
        else None
    )
    return {
        "id": _bounded_string(_scrub(str(row["id"]), base_urls), 256),
        "started_at_ms": _optional_non_negative_int(row["started_at_ms"]) or 0,
        "duration_ms": _optional_non_negative_int(row["duration_ms"]) or 0,
        "kind": _bounded_string(_scrub(str(row["kind"]), base_urls), 128),
        "stage": _bounded_string(_scrub(str(row["stage"]), base_urls), 128),
        "model": _bounded_optional_string(
            _scrub_optional(row["model"], base_urls),
            1_024,
        ),
        "status": _bounded_string(_scrub(str(row["status"]), base_urls), 128),
        "error": _bounded_optional_string(
            _scrub_optional(row["error"], base_urls),
            _MAX_ERROR_BYTES,
        ),
        "finish_reason": _bounded_optional_string(
            _scrub_optional(row["finish_reason"], base_urls),
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


def _project_stored_json(value: object, *, base_urls: tuple[str, ...]) -> Any:
    decoded = _decode_json(value)
    if decoded is None and value != "null":
        return {"status": "unavailable", "reason": "malformed"}
    scrubbed = _scrub_json(decoded, base_urls=base_urls)
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


def _scrub(value: str, base_urls: tuple[str, ...]) -> str:
    return _scrub_text(value, base_urls=base_urls)


def _scrub_optional(value: object, base_urls: tuple[str, ...]) -> str | None:
    return _scrub(str(value), base_urls) if value is not None else None


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
