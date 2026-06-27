from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import func, insert, or_, select, update

from config import paths
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.migrations import background_tables_ready, initialize_background_tables
from storage.models import agent_runs, agent_sessions, run_definitions, scopes
from storage.pagination import PageRequest, PageResult, page_result_from_limit_plus_one

logger = logging.getLogger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_next_run_at(
    *,
    enabled: bool,
    schedule_type: Optional[str],
    cron: Optional[str],
    run_at: Optional[str],
    timezone_name: Optional[str],
) -> Optional[str]:
    """Next fire time (tz-aware ISO) for a scheduled task, or None.

    Shared by the harness API payload and the CLI so the two never drift. A
    disabled task, an unparseable schedule, or an ``at`` task whose time has
    already passed all yield ``None``.
    """
    if not enabled:
        return None
    try:
        tz = ZoneInfo(timezone_name or "UTC")
        now = datetime.now(tz)
        if schedule_type == "cron":
            if not cron:
                return None
            trigger = CronTrigger.from_crontab(cron, timezone=tz)
        elif schedule_type == "at":
            if not run_at:
                return None
            instant = datetime.fromisoformat(run_at)
            instant = instant.replace(tzinfo=tz) if instant.tzinfo is None else instant.astimezone(tz)
            if instant <= now:
                # A one-shot whose time has already passed has no next run.
                return None
            trigger = DateTrigger(run_date=instant)
        else:
            return None
        next_fire = trigger.get_next_fire_time(None, now)
        return next_fire.isoformat() if next_fire else None
    except Exception:
        return None


RUN_STATUS_ALIASES: dict[str, str] = {
    "pending": "queued",
    "queued": "queued",
    "processing": "running",
    "running": "running",
    "completed": "succeeded",
    "succeeded": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
}
_LIKE_ESCAPE = "\\"
DEFINITION_STATUS_COUNTS = ("all", "enabled", "disabled")
RUN_STATUS_COUNTS = ("all", "queued", "running", "succeeded", "failed", "canceled")


def normalize_run_status(status: Any) -> str:
    return RUN_STATUS_ALIASES.get(str(status or "").strip(), str(status or "").strip() or "queued")


def _status_query_values(status: str) -> list[str]:
    normalized = normalize_run_status(status)
    values = [raw for raw, public in RUN_STATUS_ALIASES.items() if public == normalized]
    return values or [normalized]


def _like_contains_pattern(value: str) -> str:
    escaped = (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _coalesced_agent_run_metadata(rows: dict[str, Any], run_ids: list[str]) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    prompt_parts: list[str] = []
    for run_id in run_ids:
        row = rows[run_id]
        message = str(row["message"] or row["prompt"] or "")
        messages.append({"execution_id": run_id, "message": message})
        if message:
            prompt_parts.append(message)
    metadata: dict[str, Any] = {
        "execution_ids": run_ids,
        "messages": messages,
    }
    if prompt_parts:
        metadata["prompt"] = "\n\n---\n\n".join(prompt_parts)
    return metadata


def complete_coalesced_agent_runs_for_workbench_in_connection(
    conn: Any,
    run_ids: list[str],
    *,
    ok: bool,
    error: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> list[str]:
    normalized_run_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_run_ids.append(run_id)
    if not normalized_run_ids:
        return []
    rows = {
        row["id"]: row
        for row in conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_run_ids))).mappings()
    }
    now = completed_at or _utc_now_iso()
    completed_ids: list[str] = []
    for run_id in normalized_run_ids:
        row = rows.get(run_id)
        if row is None:
            continue
        status = normalize_run_status(row["status"])
        values: dict[str, Any] = {"updated_at": now}
        if bool(row["cancel_requested"]) or status == "canceled":
            values["status"] = "canceled"
            values["completed_at"] = now
        else:
            values["status"] = "succeeded" if ok else "failed"
            values["completed_at"] = now
            if error is not None:
                values["error"] = error
        result = conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))
        if result.rowcount:
            completed_ids.append(run_id)
    return completed_ids


def claim_queued_runs_for_workbench_in_connection(
    conn: Any,
    run_ids: list[str],
    *,
    started_at: Optional[str] = None,
) -> list[str]:
    normalized_run_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_run_ids.append(run_id)
    queued_run_ids, stale_run_ids = inspect_queued_runs_for_workbench_in_connection(conn, normalized_run_ids)
    if stale_run_ids or queued_run_ids != normalized_run_ids:
        return []
    primary_run_id = normalized_run_ids[0] if normalized_run_ids else ""
    if not primary_run_id:
        return []
    now = started_at or _utc_now_iso()
    rows = {
        row["id"]: row
        for row in conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_run_ids))).mappings()
    }
    for run_id in normalized_run_ids:
        row = rows[run_id]
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["workbench_queue_holds_run"] = run_id != primary_run_id
        metadata["effective_run_id"] = primary_run_id
        if run_id == primary_run_id and len(normalized_run_ids) > 1:
            metadata["coalesced_queue"] = _coalesced_agent_run_metadata(rows, normalized_run_ids)
        if run_id != primary_run_id:
            metadata["coalesced_into_run_id"] = primary_run_id
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(_status_query_values("queued")))
                .values(
                    updated_at=now,
                    metadata_json=_json_dumps(metadata),
                )
            )
            if not result.rowcount:
                raise RuntimeError(f"failed to claim queued agent run {run_id}")
            continue
        result = conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .where(agent_runs.c.status.in_(_status_query_values("queued")))
            .values(
                status="running",
                started_at=now,
                updated_at=now,
                metadata_json=_json_dumps(metadata),
            )
        )
        if not result.rowcount:
            raise RuntimeError(f"failed to claim queued agent run {run_id}")
    return normalized_run_ids


def _refresh_recovered_coalesced_workbench_runs_in_connection(conn: Any, *, now: str) -> None:
    rows = list(
        conn.execute(
            select(agent_runs)
            .where(agent_runs.c.run_type == "agent_run")
            .where(agent_runs.c.status.in_(_status_query_values("queued")))
        ).mappings()
    )
    rows_by_id = {row["id"]: row for row in rows}
    processed: set[str] = set()
    for row in rows:
        run_id = str(row["id"] or "")
        if run_id in processed:
            continue
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        coalesced = metadata.get("coalesced_queue") if isinstance(metadata, dict) else None
        raw_ids = coalesced.get("execution_ids") if isinstance(coalesced, dict) else None
        if not isinstance(raw_ids, list):
            continue
        run_ids: list[str] = []
        for value in raw_ids:
            coalesced_id = str(value or "").strip()
            if coalesced_id and coalesced_id not in run_ids:
                run_ids.append(coalesced_id)
        if run_id not in run_ids:
            run_ids.insert(0, run_id)
        live_ids = [
            candidate
            for candidate in run_ids
            if candidate in rows_by_id
            and not bool(rows_by_id[candidate]["cancel_requested"])
            and normalize_run_status(rows_by_id[candidate]["status"]) == "queued"
        ]
        if not live_ids:
            processed.update(run_ids)
            continue
        primary_id = live_ids[0]
        live_rows = {candidate: rows_by_id[candidate] for candidate in live_ids}
        primary_metadata = _json_loads(rows_by_id[primary_id]["metadata_json"], {})
        if not isinstance(primary_metadata, dict):
            primary_metadata = {}
        primary_metadata["workbench_queue_holds_run"] = False
        primary_metadata["effective_run_id"] = primary_id
        primary_metadata.pop("coalesced_into_run_id", None)
        if len(live_ids) > 1:
            primary_metadata["coalesced_queue"] = _coalesced_agent_run_metadata(live_rows, live_ids)
        else:
            primary_metadata.pop("coalesced_queue", None)
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == primary_id)
            .values(metadata_json=_json_dumps(primary_metadata), updated_at=now)
        )
        for child_id in live_ids[1:]:
            child_metadata = _json_loads(rows_by_id[child_id]["metadata_json"], {})
            if not isinstance(child_metadata, dict):
                child_metadata = {}
            child_metadata["workbench_queue_holds_run"] = True
            child_metadata["effective_run_id"] = primary_id
            child_metadata["coalesced_into_run_id"] = primary_id
            child_metadata.pop("coalesced_queue", None)
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == child_id)
                .values(metadata_json=_json_dumps(child_metadata), updated_at=now)
            )
        processed.update(run_ids)


def inspect_queued_runs_for_workbench_in_connection(conn: Any, run_ids: list[str]) -> tuple[list[str], list[str]]:
    normalized_run_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_run_ids.append(run_id)
    if not normalized_run_ids:
        return [], []
    rows = {
        row["id"]: row
        for row in conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_run_ids))).mappings()
    }
    queued_run_ids: list[str] = []
    stale_run_ids: list[str] = []
    cancel_requested_run_ids: list[str] = []
    for run_id in normalized_run_ids:
        row = rows.get(run_id)
        if row is None:
            stale_run_ids.append(run_id)
            continue
        if bool(row["cancel_requested"]):
            if normalize_run_status(row["status"]) == "queued":
                cancel_requested_run_ids.append(run_id)
            stale_run_ids.append(run_id)
            continue
        if normalize_run_status(row["status"]) != "queued":
            stale_run_ids.append(run_id)
            continue
        queued_run_ids.append(run_id)
    if cancel_requested_run_ids:
        now = _utc_now_iso()
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id.in_(cancel_requested_run_ids))
            .where(agent_runs.c.status.in_(_status_query_values("queued")))
            .values(status="canceled", completed_at=now, updated_at=now)
        )
    return queued_run_ids, stale_run_ids


def reset_workbench_claimed_runs_in_connection(conn: Any, run_ids: list[str]) -> None:
    now = _utc_now_iso()
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
        if not row:
            continue
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["workbench_queue_holds_run"] = True
        metadata.pop("effective_run_id", None)
        metadata.pop("coalesced_into_run_id", None)
        values = {
            "updated_at": now,
            "metadata_json": _json_dumps(metadata),
        }
        if normalize_run_status(row["status"]) == "running":
            values["status"] = "queued"
            values["started_at"] = None
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(**values)
        )


class SQLiteBackgroundTaskStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or paths.get_sqlite_state_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path is None:
            from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config

            ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
        if not background_tables_ready(self.db_path):
            initialize_background_tables(self.db_path)
        self.engine = create_sqlite_engine(self.db_path)
        self._probe = SqliteInvalidationProbe(self.engine)

    def close(self) -> None:
        self._probe.close()
        self.engine.dispose()

    def maybe_reload(self) -> bool:
        return self._probe.has_external_write()

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(run_definitions)
                    .where(run_definitions.c.definition_type == "scheduled")
                    .where(run_definitions.c.deleted_at.is_(None))
                    .order_by(run_definitions.c.created_at, run_definitions.c.id)
                ).mappings()
            )
            return [self._enrich_task(self._scheduled_task_from_row(row), conn) for row in rows]

    def list_scheduled_tasks_page(
        self,
        *,
        status: Optional[str] = None,
        query: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
    ) -> PageResult[dict[str, Any]]:
        stmt = self._definitions_query("scheduled", status=status, query=query)
        activity = func.coalesce(
            run_definitions.c.last_run_at,
            run_definitions.c.updated_at,
            run_definitions.c.created_at,
            "",
        )
        if newest_first:
            stmt = stmt.order_by(activity.desc(), run_definitions.c.id.desc())
        else:
            stmt = stmt.order_by(activity, run_definitions.c.id)
        if page_request is not None:
            stmt = stmt.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = [self._enrich_task(self._scheduled_task_from_row(row), conn) for row in conn.execute(stmt).mappings()]
        return page_result_from_limit_plus_one(rows, page_request)

    def count_scheduled_tasks(self, *, query: Optional[str] = None) -> dict[str, int]:
        return self._definition_counts("scheduled", query=query)

    def get_scheduled_task(self, definition_id: str) -> Optional[dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(run_definitions)
                .where(run_definitions.c.definition_type == "scheduled")
                .where(run_definitions.c.id == definition_id)
                .where(run_definitions.c.deleted_at.is_(None))
                .limit(1)
            ).mappings().first()
            return self._enrich_task(self._scheduled_task_from_row(row), conn) if row else None

    def upsert_scheduled_task(self, payload: dict[str, Any]) -> None:
        values = self._scheduled_task_values(payload)
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(run_definitions.c.id).where(run_definitions.c.id == values["id"]).limit(1)
            ).scalar_one_or_none()
            if existing:
                conn.execute(update(run_definitions).where(run_definitions.c.id == values["id"]).values(**values))
            else:
                conn.execute(insert(run_definitions).values(**values))

    def remove_task(self, definition_id: str, *, deleted_at: Optional[str] = None) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .where(run_definitions.c.deleted_at.is_(None))
                .values(deleted_at=deleted_at or _utc_now_iso())
            )
            return bool(result.rowcount)

    def set_definition_enabled(
        self,
        definition_id: str,
        enabled: bool,
        *,
        definition_type: Optional[str] = None,
    ) -> bool:
        with self.engine.begin() as conn:
            stmt = (
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .where(run_definitions.c.deleted_at.is_(None))
                .values(enabled=1 if enabled else 0, updated_at=_utc_now_iso())
            )
            if definition_type is not None:
                stmt = stmt.where(run_definitions.c.definition_type == definition_type)
            result = conn.execute(stmt)
            return bool(result.rowcount)

    def list_watches(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(run_definitions)
                    .where(run_definitions.c.definition_type == "watch")
                    .where(run_definitions.c.deleted_at.is_(None))
                    .order_by(run_definitions.c.created_at, run_definitions.c.id)
                ).mappings()
            )
            return [self._enrich_watch(self._watch_from_row(row), conn) for row in rows]

    def list_watches_page(
        self,
        *,
        status: Optional[str] = None,
        query: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
    ) -> PageResult[dict[str, Any]]:
        stmt = self._definitions_query("watch", status=status, query=query)
        activity = func.coalesce(
            run_definitions.c.last_event_at,
            run_definitions.c.last_started_at,
            run_definitions.c.updated_at,
            run_definitions.c.created_at,
            "",
        )
        if newest_first:
            stmt = stmt.order_by(activity.desc(), run_definitions.c.id.desc())
        else:
            stmt = stmt.order_by(activity, run_definitions.c.id)
        if page_request is not None:
            stmt = stmt.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = [self._enrich_watch(self._watch_from_row(row), conn) for row in conn.execute(stmt).mappings()]
        return page_result_from_limit_plus_one(rows, page_request)

    def count_watches(self, *, query: Optional[str] = None) -> dict[str, int]:
        return self._definition_counts("watch", query=query)

    def get_watch(self, watch_id: str) -> Optional[dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(run_definitions)
                .where(run_definitions.c.definition_type == "watch")
                .where(run_definitions.c.id == watch_id)
                .where(run_definitions.c.deleted_at.is_(None))
                .limit(1)
            ).mappings().first()
            return self._enrich_watch(self._watch_from_row(row), conn) if row else None

    def upsert_watch(self, payload: dict[str, Any]) -> None:
        values = self._watch_values(payload)
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(run_definitions.c.id).where(run_definitions.c.id == values["id"]).limit(1)
            ).scalar_one_or_none()
            if existing:
                conn.execute(update(run_definitions).where(run_definitions.c.id == values["id"]).values(**values))
            else:
                conn.execute(insert(run_definitions).values(**values))

    def enqueue_run(self, payload: dict[str, Any]) -> None:
        values = self._run_values(payload)
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(agent_runs.c.id).where(agent_runs.c.id == values["id"]).limit(1)
            ).scalar_one_or_none()
            if existing:
                conn.execute(update(agent_runs).where(agent_runs.c.id == values["id"]).values(**values))
            else:
                conn.execute(insert(agent_runs).values(**values))

    def list_runs(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        stmt = self._runs_query(status=status).order_by(agent_runs.c.created_at, agent_runs.c.id)
        with self.engine.connect() as conn:
            return [self._run_from_row(row) for row in conn.execute(stmt).mappings()]

    def list_runs_page(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
    ) -> PageResult[dict[str, Any]]:
        stmt = self._runs_query(
            status=status,
            run_type=run_type,
            agent_name=agent_name,
            agent_backend=agent_backend,
            session_id=session_id,
            definition_id=definition_id,
            created_after=created_after,
            created_before=created_before,
            query=query,
        )
        if newest_first:
            stmt = stmt.order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
        else:
            stmt = stmt.order_by(agent_runs.c.created_at, agent_runs.c.id)
        if page_request is not None:
            stmt = stmt.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = [self._run_from_row(row) for row in conn.execute(stmt).mappings()]
        return page_result_from_limit_plus_one(rows, page_request)

    def count_runs(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
    ) -> int:
        stmt = self._runs_query(
            status=status,
            run_type=run_type,
            agent_name=agent_name,
            agent_backend=agent_backend,
            session_id=session_id,
            definition_id=definition_id,
            created_after=created_after,
            created_before=created_before,
            query=query,
            count=True,
        )
        with self.engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one() or 0)

    def count_runs_by_status(
        self,
        *,
        run_type: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
    ) -> dict[str, int]:
        stmt = self._runs_query(
            run_type=run_type,
            agent_name=agent_name,
            agent_backend=agent_backend,
            session_id=session_id,
            definition_id=definition_id,
            created_after=created_after,
            created_before=created_before,
            query=query,
            columns=(agent_runs.c.status, func.count()),
        ).group_by(agent_runs.c.status)
        counts = {key: 0 for key in RUN_STATUS_COUNTS}
        with self.engine.connect() as conn:
            for raw_status, count in conn.execute(stmt).all():
                public_status = normalize_run_status(raw_status)
                if public_status not in counts:
                    counts[public_status] = 0
                value = int(count or 0)
                counts[public_status] += value
                counts["all"] += value
        return counts

    def _runs_query(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
        count: bool = False,
        columns: Any = None,
    ):
        if columns is not None:
            stmt = select(*columns) if isinstance(columns, tuple) else select(columns)
        elif count:
            stmt = select(func.count()).select_from(agent_runs)
        else:
            stmt = select(agent_runs)
        if status:
            stmt = stmt.where(agent_runs.c.status.in_(_status_query_values(status)))
        if run_type:
            stmt = stmt.where(agent_runs.c.run_type == run_type)
        if agent_name:
            stmt = stmt.where(agent_runs.c.agent_name == agent_name)
        if agent_backend:
            stmt = stmt.where(agent_runs.c.agent_backend == agent_backend)
        if session_id:
            stmt = stmt.where(agent_runs.c.session_id == session_id)
        if definition_id:
            stmt = stmt.where(agent_runs.c.definition_id == definition_id)
        if created_after:
            stmt = stmt.where(agent_runs.c.created_at >= created_after)
        if created_before:
            stmt = stmt.where(agent_runs.c.created_at <= created_before)
        if query:
            pattern = _like_contains_pattern(query)
            stmt = stmt.where(
                or_(
                    agent_runs.c.id.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.definition_id.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.agent_name.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.session_id.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.prompt.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.message.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.result_text.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.error.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.stdout.like(pattern, escape=_LIKE_ESCAPE),
                    agent_runs.c.stderr.like(pattern, escape=_LIKE_ESCAPE),
                )
            )
        return stmt

    def _definitions_query(
        self,
        definition_type: str,
        *,
        status: Optional[str] = None,
        query: Optional[str] = None,
        columns: Any = None,
    ):
        if columns is not None:
            stmt = select(*columns) if isinstance(columns, tuple) else select(columns)
        else:
            stmt = select(run_definitions)
        stmt = (
            stmt.where(run_definitions.c.definition_type == definition_type)
            .where(run_definitions.c.deleted_at.is_(None))
        )
        if status and status != "all":
            if status not in {"enabled", "disabled"}:
                raise ValueError("status must be one of: all, enabled, disabled")
            stmt = stmt.where(run_definitions.c.enabled == (1 if status == "enabled" else 0))
        if query:
            pattern = _like_contains_pattern(query)
            fields = [
                run_definitions.c.id,
                run_definitions.c.name,
                run_definitions.c.agent_name,
                run_definitions.c.session_id,
                run_definitions.c.legacy_session_key,
                run_definitions.c.message,
            ]
            if definition_type == "scheduled":
                fields.extend(
                    [
                        run_definitions.c.prompt,
                        run_definitions.c.schedule_type,
                        run_definitions.c.cron,
                        run_definitions.c.run_at,
                    ]
                )
            elif definition_type == "watch":
                fields.extend(
                    [
                        run_definitions.c.command_json,
                        run_definitions.c.shell_command,
                        run_definitions.c.prefix,
                        run_definitions.c.cwd,
                    ]
                )
            stmt = stmt.where(or_(*(field.like(pattern, escape=_LIKE_ESCAPE) for field in fields)))
        return stmt

    def _definition_counts(self, definition_type: str, *, query: Optional[str] = None) -> dict[str, int]:
        stmt = self._definitions_query(
            definition_type,
            query=query,
            columns=(run_definitions.c.enabled, func.count()),
        ).group_by(run_definitions.c.enabled)
        counts = {key: 0 for key in DEFINITION_STATUS_COUNTS}
        with self.engine.connect() as conn:
            for enabled, count in conn.execute(stmt).all():
                key = "enabled" if bool(enabled) else "disabled"
                value = int(count or 0)
                counts[key] += value
                counts["all"] += value
        return counts

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            return self._run_from_row(row) if row else None

    def list_pending_callbacks(self, *, limit: int = 20) -> list[dict[str, Any]]:
        terminal_statuses = _status_query_values("succeeded") + _status_query_values("failed") + _status_query_values("canceled")
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_runs)
                    .where(agent_runs.c.callback_session_id.is_not(None))
                    .where(agent_runs.c.callback_session_id != "")
                    .where(agent_runs.c.callback_status == "pending")
                    .where(agent_runs.c.completed_at.is_not(None))
                    .where(agent_runs.c.status.in_(terminal_statuses))
                    .order_by(agent_runs.c.completed_at, agent_runs.c.id)
                    .limit(limit)
                ).mappings()
            )
            return [self._run_from_row(row) for row in rows]

    def cancel_run(self, run_id: str, *, requested_at: Optional[str] = None) -> bool:
        now = requested_at or _utc_now_iso()
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_runs.c.status).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            if not row:
                return False
            status = normalize_run_status(row["status"])
            values: dict[str, Any] = {
                "cancel_requested": 1,
                "cancel_requested_at": now,
                "updated_at": now,
            }
            if status == "queued":
                values["status"] = "canceled"
                values["completed_at"] = now
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(**values)
            )
            return bool(result.rowcount)

    def claim_pending_run(self, run_id: str, *, started_at: str) -> Optional[dict[str, Any]]:
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            if not row:
                return None
            if bool(row["cancel_requested"]) or normalize_run_status(row["status"]) == "canceled":
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .values(status="canceled", completed_at=started_at, updated_at=started_at)
                )
                return None
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(_status_query_values("queued")))
                .values(status="running", started_at=started_at, updated_at=started_at)
            )
            if not result.rowcount:
                return None
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            return self._run_from_row(row) if row else None

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        updated_at: str,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        pid: Optional[int] = None,
        definition_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        result_text: Optional[str] = None,
        result_payload: Optional[dict[str, Any]] = None,
        message_ids: Optional[list[str]] = None,
        cancel_requested: Optional[bool] = None,
        cancel_requested_at: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        callback_status: Optional[str] = None,
        callback_error: Optional[str] = None,
        callback_run_id: Optional[str] = None,
        callback_completed_at: Optional[str] = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "updated_at": updated_at,
        }
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        if exit_code is not None:
            values["exit_code"] = exit_code
        if error is not None:
            values["error"] = error
        if stdout is not None:
            values["stdout"] = stdout
        if stderr is not None:
            values["stderr"] = stderr
        if pid is not None:
            values["pid"] = pid
        resolved_definition_id = definition_id or task_id
        if resolved_definition_id is not None:
            values["definition_id"] = resolved_definition_id
        if session_key is not None:
            values["legacy_session_key"] = session_key
        if session_id is not None:
            values["session_id"] = session_id
        if result_text is not None:
            values["result_text"] = result_text
        if result_payload is not None:
            values["result_payload_json"] = _json_dumps(result_payload)
        if message_ids is not None:
            values["message_ids_json"] = _json_dumps(message_ids)
        if cancel_requested is not None:
            values["cancel_requested"] = 1 if cancel_requested else 0
        if cancel_requested_at is not None:
            values["cancel_requested_at"] = cancel_requested_at
        if metadata is not None:
            existing = self.get_run(run_id) or {}
            merged = dict(existing.get("metadata") or {})
            merged.update(metadata)
            values["metadata_json"] = _json_dumps(merged)
        if callback_status is not None:
            values["callback_status"] = callback_status
        if callback_error is not None:
            values["callback_error"] = callback_error
        if callback_run_id is not None:
            values["callback_run_id"] = callback_run_id
        if callback_completed_at is not None:
            values["callback_completed_at"] = callback_completed_at
        with self.engine.begin() as conn:
            conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))

    def update_callback_status(
        self,
        run_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        callback_run_id: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        now = completed_at or _utc_now_iso()
        values: dict[str, Any] = {
            "callback_status": status,
            "callback_error": error,
            "callback_completed_at": now,
            "updated_at": now,
        }
        if callback_run_id is not None:
            values["callback_run_id"] = callback_run_id
        with self.engine.begin() as conn:
            conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))

    def mark_run_queued_from_running(
        self,
        run_id: str,
        *,
        updated_at: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        now = updated_at or _utc_now_iso()
        values: dict[str, Any] = {
            "status": "queued",
            "started_at": None,
            "updated_at": now,
        }
        if metadata is not None:
            existing = self.get_run(run_id) or {}
            merged = dict(existing.get("metadata") or {})
            merged.update(metadata)
            values["metadata_json"] = _json_dumps(merged)
        with self.engine.begin() as conn:
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(_status_query_values("running")))
                .values(**values)
            )
            return bool(result.rowcount)

    def claim_queued_run_for_workbench(
        self,
        run_id: str,
        *,
        started_at: Optional[str] = None,
    ) -> bool:
        return self.claim_queued_runs_for_workbench([run_id], started_at=started_at) == [run_id]

    def claim_queued_runs_for_workbench(
        self,
        run_ids: list[str],
        *,
        started_at: Optional[str] = None,
    ) -> list[str]:
        with self.engine.begin() as conn:
            return claim_queued_runs_for_workbench_in_connection(conn, run_ids, started_at=started_at)

    def inspect_queued_runs_for_workbench(self, run_ids: list[str]) -> tuple[list[str], list[str]]:
        with self.engine.begin() as conn:
            return inspect_queued_runs_for_workbench_in_connection(conn, run_ids)

    def record_run_message(
        self,
        run_id: str,
        *,
        text: str,
        message_id: str | None = None,
        terminal_status: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        now = updated_at or _utc_now_iso()
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            if not row:
                return
            existing_text = str(row["result_text"] or "")
            incoming = str(text or "")
            if existing_text and incoming:
                result_text = f"{existing_text}\n\n{incoming}"
            else:
                result_text = existing_text or incoming
            message_ids = _json_loads(row["message_ids_json"], [])
            if message_id:
                message_ids.append(message_id)
            values: dict[str, Any] = {
                "result_text": result_text,
                "message_ids_json": _json_dumps(message_ids),
                "updated_at": now,
            }
            if terminal_status:
                values["status"] = normalize_run_status(terminal_status)
                values["completed_at"] = now
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(**values)
            )

    def recover_processing_runs(self) -> None:
        with self.engine.begin() as conn:
            now = _utc_now_iso()
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.status.in_(_status_query_values("running")))
                .where(agent_runs.c.run_type != "watch_runtime")
                .values(status="queued", started_at=None, pid=None, updated_at=now)
            )
            _refresh_recovered_coalesced_workbench_runs_in_connection(conn, now=now)

    def write_watch_runtime(self, payload: dict[str, Any], *, updated_at: str) -> None:
        watches = payload.get("watches", {}) if isinstance(payload, dict) else {}
        with self.engine.begin() as conn:
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.run_type == "watch_runtime")
                .where(agent_runs.c.status.in_(_status_query_values("running") + _status_query_values("queued")))
                .values(status="succeeded", completed_at=updated_at, updated_at=updated_at)
            )
            for watch_id, runtime_payload in watches.items():
                if not isinstance(runtime_payload, dict):
                    continue
                run_id = f"runtime:{watch_id}"
                values = self._run_values(
                    {
                        "id": run_id,
                        "request_type": "watch_runtime",
                        "status": "running" if runtime_payload.get("running") else "completed",
                        "definition_id": watch_id,
                        "pid": runtime_payload.get("pid"),
                        "created_at": runtime_payload.get("started_at") or updated_at,
                        "started_at": runtime_payload.get("started_at"),
                        "updated_at": runtime_payload.get("updated_at") or updated_at,
                        "metadata": runtime_payload,
                    }
                )
                existing = conn.execute(
                    select(agent_runs.c.id).where(agent_runs.c.id == run_id).limit(1)
                ).scalar_one_or_none()
                if existing:
                    conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))
                else:
                    conn.execute(insert(agent_runs).values(**values))

    def load_watch_runtime(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(agent_runs)
                .where(agent_runs.c.run_type == "watch_runtime")
                .where(agent_runs.c.status == "running")
            ).mappings()
            watches: dict[str, Any] = {}
            for row in rows:
                payload = _json_loads(row["metadata_json"], {})
                watch_id = row["definition_id"]
                if watch_id:
                    watches[str(watch_id)] = {
                        "running": True,
                        "pid": row["pid"],
                        "started_at": row["started_at"],
                        "updated_at": row["updated_at"],
                    } | (payload if isinstance(payload, dict) else {})
            return {"watches": watches}

    def _scheduled_task_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload["id"],
            "definition_type": "scheduled",
            "name": payload.get("name"),
            "agent_name": payload.get("agent_name"),
            "session_policy": payload.get("session_policy") or ("existing" if payload.get("session_id") or payload.get("session_key") else None),
            "session_id": payload.get("session_id"),
            "legacy_session_key": payload.get("session_key") or None,
            "prompt": payload.get("prompt") or payload.get("message") or "",
            "message": payload.get("message") or payload.get("prompt") or "",
            "message_payload_json": self._message_payload_json(payload),
            "schedule_type": payload.get("schedule_type") or "",
            "cron": payload.get("cron"),
            "run_at": payload.get("run_at"),
            "timezone": payload.get("timezone") or "UTC",
            "command_json": None,
            "shell_command": None,
            "prefix": None,
            "cwd": None,
            "mode": None,
            "timeout_seconds": None,
            "lifetime_timeout_seconds": None,
            "retry_exit_codes_json": None,
            "retry_delay_seconds": None,
            "post_to": payload.get("post_to"),
            "deliver_key": payload.get("deliver_key"),
            "enabled": 1 if payload.get("enabled", True) else 0,
            "deleted_at": payload.get("deleted_at"),
            "created_at": payload.get("created_at") or payload.get("updated_at"),
            "updated_at": payload.get("updated_at") or payload.get("created_at"),
            "last_started_at": None,
            "last_finished_at": None,
            "last_event_at": None,
            "last_run_at": payload.get("last_run_at"),
            "last_run_id": payload.get("last_run_id"),
            "last_error": payload.get("last_error"),
            "last_exit_code": None,
            "metadata_json": _json_dumps(payload.get("metadata") or {}),
        }

    def _watch_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload["id"],
            "definition_type": "watch",
            "name": payload.get("name"),
            "agent_name": payload.get("agent_name"),
            "session_policy": payload.get("session_policy") or ("existing" if payload.get("session_id") or payload.get("session_key") else None),
            "session_id": payload.get("session_id"),
            "legacy_session_key": payload.get("session_key") or None,
            "prompt": None,
            "message": payload.get("message") or payload.get("prefix"),
            "message_payload_json": self._message_payload_json(payload),
            "schedule_type": None,
            "cron": None,
            "run_at": None,
            "timezone": None,
            "command_json": _json_dumps(payload.get("command") or []),
            "shell_command": payload.get("shell_command"),
            "prefix": payload.get("prefix"),
            "cwd": payload.get("cwd"),
            "mode": payload.get("mode") or "once",
            "timeout_seconds": float(payload.get("timeout_seconds", 21600.0)),
            "lifetime_timeout_seconds": float(payload.get("lifetime_timeout_seconds", 0.0)),
            "retry_exit_codes_json": _json_dumps(payload.get("retry_exit_codes") or []),
            "retry_delay_seconds": float(payload.get("retry_delay_seconds", 30.0)),
            "post_to": payload.get("post_to"),
            "deliver_key": payload.get("deliver_key"),
            "enabled": 1 if payload.get("enabled", True) else 0,
            "deleted_at": payload.get("deleted_at"),
            "created_at": payload.get("created_at") or payload.get("updated_at"),
            "updated_at": payload.get("updated_at") or payload.get("created_at"),
            "last_started_at": payload.get("last_started_at"),
            "last_finished_at": payload.get("last_finished_at"),
            "last_event_at": payload.get("last_event_at"),
            "last_run_at": None,
            "last_error": payload.get("last_error"),
            "last_exit_code": payload.get("last_exit_code"),
            "metadata_json": _json_dumps(payload.get("metadata") or {}),
        }

    def _run_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        created_at = payload.get("created_at") or payload.get("updated_at")
        message = payload.get("message") or payload.get("prompt")
        return {
            "id": payload["id"],
            "definition_id": payload.get("definition_id") or payload.get("task_id"),
            "run_type": payload.get("request_type") or payload.get("run_type") or "hook_send",
            "status": normalize_run_status(payload.get("status")),
            "source_kind": payload.get("source_kind"),
            "source_actor": payload.get("source_actor"),
            "parent_run_id": payload.get("parent_run_id"),
            "agent_name": payload.get("agent_name"),
            "agent_id": payload.get("agent_id"),
            "agent_backend": payload.get("agent_backend"),
            "model": payload.get("model") or payload.get("agent_model"),
            "reasoning_effort": payload.get("reasoning_effort") or payload.get("agent_reasoning_effort"),
            "session_policy": payload.get("session_policy"),
            "session_id": payload.get("session_id"),
            "legacy_session_key": payload.get("session_key") or payload.get("legacy_session_key"),
            "post_to": payload.get("post_to"),
            "deliver_key": payload.get("deliver_key"),
            "prompt": payload.get("prompt") or message,
            "message": message,
            "message_payload_json": self._message_payload_json(payload),
            "result_text": payload.get("result_text"),
            "result_payload_json": self._payload_json(payload, "result_payload", "result_payload_json"),
            "message_ids_json": self._payload_json(payload, "message_ids", "message_ids_json"),
            "callback_session_id": payload.get("callback_session_id"),
            "callback_status": payload.get("callback_status") or ("pending" if payload.get("callback_session_id") else None),
            "callback_error": payload.get("callback_error"),
            "callback_run_id": payload.get("callback_run_id"),
            "callback_completed_at": payload.get("callback_completed_at"),
            "cancel_requested": 1 if payload.get("cancel_requested") else 0,
            "cancel_requested_at": payload.get("cancel_requested_at"),
            "pid": payload.get("pid"),
            "exit_code": payload.get("exit_code"),
            "error": payload.get("error"),
            "stdout": payload.get("stdout"),
            "stderr": payload.get("stderr"),
            "created_at": created_at,
            "started_at": payload.get("started_at"),
            "completed_at": payload.get("completed_at"),
            "updated_at": payload.get("updated_at") or created_at,
            "metadata_json": _json_dumps(payload.get("metadata") or {}),
        }

    @staticmethod
    def _scheduled_task_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_name": row["agent_name"],
            "session_policy": row["session_policy"],
            "session_key": row["legacy_session_key"] or "",
            "session_id": row["session_id"],
            "prompt": row["prompt"] or "",
            "message": row["message"] or row["prompt"] or "",
            "message_payload": _json_loads(row["message_payload_json"], None),
            "schedule_type": row["schedule_type"] or "",
            "post_to": row["post_to"],
            "deliver_key": row["deliver_key"],
            "cron": row["cron"],
            "run_at": row["run_at"],
            "timezone": row["timezone"] or "UTC",
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_run_at": row["last_run_at"],
            "last_run_id": row["last_run_id"],
            "last_error": row["last_error"],
            "metadata": _json_loads(row["metadata_json"], {}),
        }

    @staticmethod
    def _watch_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_name": row["agent_name"],
            "session_policy": row["session_policy"],
            "session_key": row["legacy_session_key"] or "",
            "session_id": row["session_id"],
            "command": _json_loads(row["command_json"], []),
            "shell_command": row["shell_command"],
            "prefix": row["prefix"],
            "message": row["message"] or row["prefix"],
            "message_payload": _json_loads(row["message_payload_json"], None),
            "cwd": row["cwd"],
            "mode": row["mode"] or "once",
            "timeout_seconds": float(row["timeout_seconds"] or 21600.0),
            "lifetime_timeout_seconds": float(row["lifetime_timeout_seconds"] or 0.0),
            "retry_exit_codes": [int(code) for code in _json_loads(row["retry_exit_codes_json"], [])],
            "retry_delay_seconds": float(row["retry_delay_seconds"] or 30.0),
            "post_to": row["post_to"],
            "deliver_key": row["deliver_key"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_started_at": row["last_started_at"],
            "last_finished_at": row["last_finished_at"],
            "last_event_at": row["last_event_at"],
            "last_error": row["last_error"],
            "last_exit_code": row["last_exit_code"],
            "metadata": _json_loads(row["metadata_json"], {}),
        }

    @staticmethod
    def _run_from_row(row: Any) -> dict[str, Any]:
        metadata = _json_loads(row["metadata_json"], {})
        return {
            "id": row["id"],
            "request_type": row["run_type"],
            "run_type": row["run_type"],
            "status": normalize_run_status(row["status"]),
            "definition_id": row["definition_id"],
            "task_id": row["definition_id"],
            "source_kind": row["source_kind"],
            "source_actor": row["source_actor"],
            "parent_run_id": row["parent_run_id"],
            "agent_name": row["agent_name"],
            "agent_id": row["agent_id"],
            "agent_backend": row["agent_backend"],
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "session_policy": row["session_policy"],
            "session_key": row["legacy_session_key"],
            "session_id": row["session_id"],
            "post_to": row["post_to"],
            "deliver_key": row["deliver_key"],
            "prompt": row["prompt"],
            "message": row["message"] or row["prompt"],
            "message_payload": _json_loads(row["message_payload_json"], None),
            "result_text": row["result_text"],
            "result_payload": _json_loads(row["result_payload_json"], None),
            "message_ids": _json_loads(row["message_ids_json"], []),
            "callback_session_id": row["callback_session_id"],
            "callback_status": row["callback_status"],
            "callback_error": row["callback_error"],
            "callback_run_id": row["callback_run_id"],
            "callback_completed_at": row["callback_completed_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "cancel_requested_at": row["cancel_requested_at"],
            "pid": row["pid"],
            "exit_code": row["exit_code"],
            "error": row["error"],
            "stdout": row["stdout"],
            "stderr": row["stderr"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
            "metadata": metadata,
            "session_fork": metadata.get("session_fork") if isinstance(metadata, dict) else None,
            "ok": None if row["completed_at"] is None else normalize_run_status(row["status"]) == "succeeded",
        }

    def _enrich_task(self, task: dict[str, Any], conn: Any) -> dict[str, Any]:
        task.update(
            self._session_summary(
                conn, task.get("session_id"), task.get("session_key"), task.get("deliver_key")
            )
        )
        task["next_run_at"] = compute_next_run_at(
            enabled=bool(task.get("enabled")),
            schedule_type=task.get("schedule_type"),
            cron=task.get("cron"),
            run_at=task.get("run_at"),
            timezone_name=task.get("timezone"),
        )
        return task

    def _enrich_watch(self, watch: dict[str, Any], conn: Any) -> dict[str, Any]:
        watch.update(
            self._session_summary(
                conn, watch.get("session_id"), watch.get("session_key"), watch.get("deliver_key")
            )
        )
        return watch

    @staticmethod
    def _session_summary(
        conn: Any,
        session_id: Optional[str],
        session_key: Optional[str],
        deliver_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve a task/watch's bound session into UI-facing display fields.

        A workbench binding (avibe ``project`` scope) carries a concrete
        ``session_id`` and a human ``title`` and is linkable to its chat. A
        legacy IM binding lives in ``session_key``. A ``create_per_run``
        definition has neither — it mints a fresh session each run and stores
        only its target scope in ``deliver_key`` — so that is used as a final
        fallback for the platform + channel label. Key-based targets are never
        linkable (no concrete session to open). Best-effort: never raises into
        the harness list.
        """
        summary: dict[str, Any] = {
            "session_title": None,
            "session_platform": None,
            "session_scope_kind": None,
            "session_label": None,
            "session_is_workbench": False,
        }
        try:
            if session_id:
                row = conn.execute(
                    select(
                        agent_sessions.c.title,
                        scopes.c.platform,
                        scopes.c.scope_type,
                        scopes.c.native_id,
                        scopes.c.display_name,
                    )
                    .select_from(
                        agent_sessions.join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
                    )
                    .where(agent_sessions.c.id == session_id)
                    .limit(1)
                ).mappings().first()
                if row is not None:
                    platform = (row["platform"] or "").strip()
                    scope_type = (row["scope_type"] or "").strip()
                    is_workbench = platform == "avibe" or scope_type == "project"
                    summary["session_platform"] = platform or None
                    summary["session_scope_kind"] = scope_type or None
                    summary["session_is_workbench"] = is_workbench
                    summary["session_title"] = row["title"]
                    summary["session_label"] = (
                        row["title"] if is_workbench else (row["display_name"] or row["native_id"])
                    )
                    return summary
            for key in (session_key, deliver_key):
                resolved = SQLiteBackgroundTaskStore._summary_from_session_key(conn, key)
                if resolved is not None:
                    return resolved
        except Exception:
            logger.debug("harness session summary resolution failed", exc_info=True)
        return summary

    @staticmethod
    def _summary_from_session_key(conn: Any, key: Optional[str]) -> Optional[dict[str, Any]]:
        """Parse a "<platform>::<channel|user>::<native_id>[::thread::<id>]" key
        into a non-linkable session summary, resolving the channel display name.
        Shared by the legacy ``session_key`` and the ``create_per_run``
        ``deliver_key`` paths. Returns None when ``key`` is empty/malformed."""
        if not key:
            return None
        parts = key.split("::")
        if len(parts) < 3 or not parts[0] or not parts[2]:
            return None
        platform, scope_type, native_id = parts[0], parts[1], parts[2]
        label = native_id
        drow = conn.execute(
            select(scopes.c.display_name)
            .where(scopes.c.platform == platform)
            .where(scopes.c.scope_type == scope_type)
            .where(scopes.c.native_id == native_id)
            .limit(1)
        ).mappings().first()
        if drow is not None and drow["display_name"]:
            label = drow["display_name"]
        return {
            "session_title": None,
            "session_platform": platform,
            "session_scope_kind": scope_type,
            "session_label": label,
            "session_is_workbench": False,
        }

    @staticmethod
    def _message_payload_json(payload: dict[str, Any]) -> Optional[str]:
        return SQLiteBackgroundTaskStore._payload_json(payload, "message_payload", "message_payload_json")

    @staticmethod
    def _payload_json(payload: dict[str, Any], object_key: str, json_key: str) -> Optional[str]:
        if payload.get(json_key) is not None:
            return payload.get(json_key)
        if payload.get(object_key) is not None:
            return _json_dumps(payload.get(object_key))
        return None
