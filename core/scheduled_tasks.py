"""Scheduled task persistence, parsing, and runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from config import paths
from core.message_context import resolve_context_platform
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.background import SQLiteBackgroundTaskStore
from storage.models import agent_sessions, scope_settings, scopes
from storage.pagination import PageRequest, PageResult, page_sequence

logger = logging.getLogger(__name__)


class _ScopeAgentTarget(NamedTuple):
    agent_name: Optional[str]
    agent_backend: Optional[str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _path_signature(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _run_file_state_for_status(status: Optional[str]) -> Optional[str]:
    if status in {None, ""}:
        return None
    return {
        "queued": "pending",
        "pending": "pending",
        "running": "processing",
        "processing": "processing",
        "succeeded": "completed",
        "failed": "completed",
        "completed": "completed",
        "canceled": "completed",
        "cancelled": "completed",
    }.get(status, status)


def _normalize_requested_run_status(status: Optional[str]) -> Optional[str]:
    if status in {None, ""}:
        return None
    return {
        "pending": "queued",
        "processing": "running",
        "completed": "succeeded",
    }.get(status, status)


def _normalize_file_run_status(payload: dict[str, Any], state: str) -> str:
    raw_status = str(payload.get("status") or "").strip()
    if raw_status in {"queued", "running", "succeeded", "failed", "canceled", "cancelled"}:
        if raw_status == "cancelled":
            return "canceled"
        return raw_status
    if state == "pending":
        return "queued"
    if state == "processing":
        return "running"
    if state == "completed":
        if payload.get("ok") is False or payload.get("error"):
            return "failed"
        return "succeeded"
    return raw_status or state


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "canceled"}


@dataclass(frozen=True)
class ParsedSessionKey:
    platform: str
    scope_type: str
    scope_id: str
    thread_id: Optional[str] = None

    @property
    def session_scope(self) -> str:
        return f"{self.platform}::{self.scope_type}::{self.scope_id}"

    @property
    def is_dm(self) -> bool:
        return self.scope_type == "user"

    def to_key(self, *, include_thread: bool = True) -> str:
        base = f"{self.platform}::{self.scope_type}::{self.scope_id}"
        if include_thread and self.thread_id:
            return f"{base}::thread::{self.thread_id}"
        return base


def parse_session_key(value: str) -> ParsedSessionKey:
    raw = (value or "").strip()
    parts = raw.split("::") if raw else []
    if len(parts) not in {3, 5}:
        raise ValueError("session key must be '<platform>::<channel|user>::<id>[::thread::<thread_id>]'")

    platform, scope_type, scope_id = parts[:3]
    if not platform or not scope_id:
        raise ValueError("session key platform and scope id are required")
    if scope_type not in {"channel", "user"}:
        raise ValueError("session key scope type must be 'channel' or 'user'")

    thread_id: Optional[str] = None
    if len(parts) == 5:
        if parts[3] != "thread" or not parts[4]:
            raise ValueError("session key thread segment must be '::thread::<thread_id>'")
        thread_id = parts[4]

    return ParsedSessionKey(
        platform=platform,
        scope_type=scope_type,
        scope_id=scope_id,
        thread_id=thread_id,
    )


def session_anchor_for_target(target: ParsedSessionKey) -> str:
    anchor_id = target.thread_id or target.scope_id
    return f"{target.platform}_{anchor_id}"


@dataclass(frozen=True)
class ResolvedSessionIdTarget:
    session_id: str
    session_key: ParsedSessionKey
    agent_backend: str
    agent_variant: str
    native_session_id: str
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    workdir: Optional[str] = None
    session_anchor: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    suppress_delivery: bool = False


@dataclass(frozen=True)
class TaskExecutionResult:
    error: Optional[str]
    session_key: str
    session_id: Optional[str]


@dataclass(frozen=True)
class AgentRunExecutionResult:
    error: Optional[str]
    complete_on_return: bool
    requeue_on_return: bool = False


def resolve_session_id_target(session_id: str, *, db_path: Optional[Path] = None) -> ResolvedSessionIdTarget:
    raw = (session_id or "").strip()
    if not raw:
        raise ValueError("session id is required")

    engine = create_sqlite_engine(db_path or paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.status,
                    agent_sessions.c.agent_id,
                    agent_sessions.c.agent_name,
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.agent_variant,
                    agent_sessions.c.model,
                    agent_sessions.c.reasoning_effort,
                    agent_sessions.c.session_anchor,
                    agent_sessions.c.workdir,
                    agent_sessions.c.native_session_id,
                    scopes.c.platform,
                    scopes.c.scope_type,
                    scopes.c.native_id,
                    scopes.c.metadata_json.label("scope_metadata_json"),
                    agent_sessions.c.metadata_json.label("session_metadata_json"),
                )
                .join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
                .where(agent_sessions.c.id == raw)
                .limit(1)
            ).mappings().first()
    except SQLAlchemyError as exc:
        raise ValueError(f"agent session id not found: {raw}") from exc
    finally:
        engine.dispose()

    if row is None:
        raise ValueError(f"agent session id not found: {raw}")
    # Archived sessions are terminal + inert. A task/watch/run that still targets
    # one by id must NOT fire into it — treat it as an unresolvable target so the
    # run is skipped (archive also reclaims bound definitions, so this is defense
    # in depth for manual ``--session-id`` runs and any stragglers).
    if str(row["status"] or "") == "archived":
        raise ValueError(f"agent session is archived: {raw}")
    platform = str(row["platform"] or "")
    scope_type = str(row["scope_type"] or "")
    scope_id = str(row["native_id"] or "")
    # ``project`` is the avibe workbench's scope type (sessions live under
    # ``avibe::project::proj_<hex>``). A session-id target carries the concrete
    # ``session_id`` (the row PK) regardless of scope type, and the dispatch binds
    # the reply to that reserved session via ``agent_session_target`` — so a
    # project-scoped row IS a valid task target. (``--session-key`` targeting stays
    # channel/user-only: a bare project key wouldn't identify a single session.)
    if not platform or scope_type not in {"channel", "user", "project"} or not scope_id:
        raise ValueError(f"agent session id cannot be used as a task target: {raw}")

    anchor = str(row["session_anchor"] or "")
    thread_id = _thread_id_from_session_anchor(anchor, platform=platform, scope_id=scope_id)
    session_metadata = _json_loads(row["session_metadata_json"], {})
    scope_metadata = _json_loads(row["scope_metadata_json"], {})
    suppress_delivery = bool(
        (isinstance(session_metadata, dict) and session_metadata.get("no_delivery"))
        or (isinstance(scope_metadata, dict) and scope_metadata.get("no_delivery"))
    )
    return ResolvedSessionIdTarget(
        session_id=raw,
        session_key=ParsedSessionKey(
            platform=platform,
            scope_type=scope_type,
            scope_id=scope_id,
            thread_id=thread_id,
        ),
        agent_backend=str(row["agent_backend"] or ""),
        agent_variant=str(row["agent_variant"] or ""),
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        model=row["model"],
        reasoning_effort=row["reasoning_effort"],
        native_session_id=str(row["native_session_id"] or ""),
        workdir=row["workdir"],
        session_anchor=str(row["session_anchor"] or ""),
        metadata=session_metadata if isinstance(session_metadata, dict) else {},
        suppress_delivery=suppress_delivery,
    )


def _thread_id_from_session_anchor(anchor: str, *, platform: str, scope_id: str) -> Optional[str]:
    if not anchor:
        return None
    base_anchor = anchor
    if ":" in base_anchor:
        base_anchor = base_anchor.split(":", 1)[0]
    prefix = f"{platform}_"
    if base_anchor.startswith(prefix):
        base_anchor = base_anchor[len(prefix) :]
    if base_anchor and base_anchor != scope_id:
        return base_anchor
    return None


def build_session_key_for_context(
    context: MessageContext,
    *,
    include_thread: bool = False,
    fallback_platform: Optional[str] = None,
) -> ParsedSessionKey:
    payload = context.platform_specific or {}
    platform = resolve_context_platform(context, fallback_platform=fallback_platform)
    is_dm = bool(payload.get("is_dm", False))
    scope_type = "user" if is_dm else "channel"
    scope_id = context.user_id if is_dm else context.channel_id
    return ParsedSessionKey(
        platform=platform,
        scope_type=scope_type,
        scope_id=scope_id,
        thread_id=context.thread_id if include_thread else None,
    )


@dataclass
class ScheduledTask:
    id: str
    name: Optional[str]
    session_key: str
    prompt: str
    schedule_type: str
    agent_name: Optional[str] = None
    session_policy: Optional[str] = None
    session_id: Optional[str] = None
    post_to: Optional[str] = None
    deliver_key: Optional[str] = None
    cron: Optional[str] = None
    run_at: Optional[str] = None
    timezone: str = "UTC"
    enabled: bool = True
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ScheduledTask":
        return cls(
            id=str(payload.get("id") or uuid4().hex[:12]),
            name=(str(payload["name"]).strip() if payload.get("name") is not None else None) or None,
            session_key=str(payload.get("session_key") or ""),
            prompt=str(payload.get("prompt") or ""),
            schedule_type=str(payload.get("schedule_type") or ""),
            agent_name=(str(payload["agent_name"]).strip() if payload.get("agent_name") else None),
            session_policy=(str(payload["session_policy"]).strip() if payload.get("session_policy") else None),
            session_id=(str(payload["session_id"]).strip() if payload.get("session_id") else None),
            post_to=payload.get("post_to"),
            deliver_key=payload.get("deliver_key"),
            cron=payload.get("cron"),
            run_at=payload.get("run_at"),
            timezone=str(payload.get("timezone") or "UTC"),
            enabled=bool(payload.get("enabled", True)),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            updated_at=str(payload.get("updated_at") or _utc_now_iso()),
            last_run_at=payload.get("last_run_at"),
            last_error=payload.get("last_error"),
        )


@dataclass
class TaskExecutionRequest:
    id: str
    request_type: str
    created_at: str = field(default_factory=_utc_now_iso)
    task_id: Optional[str] = None
    session_key: Optional[str] = None
    session_id: Optional[str] = None
    post_to: Optional[str] = None
    deliver_key: Optional[str] = None
    prompt: Optional[str] = None
    message: Optional[str] = None
    source_kind: Optional[str] = None
    source_actor: Optional[str] = None
    parent_run_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_id: Optional[str] = None
    agent_backend: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    session_policy: Optional[str] = None
    callback_session_id: Optional[str] = None
    callback_status: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TaskExecutionRequest":
        return cls(
            id=str(payload.get("id") or uuid4().hex[:12]),
            request_type=str(payload.get("request_type") or ""),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            task_id=payload.get("task_id"),
            session_key=payload.get("session_key"),
            session_id=payload.get("session_id"),
            post_to=payload.get("post_to"),
            deliver_key=payload.get("deliver_key"),
            prompt=payload.get("prompt"),
            message=payload.get("message") or payload.get("prompt"),
            source_kind=payload.get("source_kind"),
            source_actor=payload.get("source_actor"),
            parent_run_id=payload.get("parent_run_id"),
            agent_name=payload.get("agent_name"),
            agent_id=payload.get("agent_id"),
            agent_backend=payload.get("agent_backend"),
            model=payload.get("model"),
            reasoning_effort=payload.get("reasoning_effort"),
            session_policy=payload.get("session_policy"),
            callback_session_id=payload.get("callback_session_id"),
            callback_status=payload.get("callback_status"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )


class ScheduledTaskStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (paths.get_state_dir() / "scheduled_tasks.json")
        self._sqlite = SQLiteBackgroundTaskStore() if path is None else None
        self._signature: Optional[tuple[int, int, int]] = None
        self._tasks: Dict[str, ScheduledTask] = {}
        self.load()

    def load(self) -> None:
        if self._sqlite is not None:
            self._tasks = {
                item["id"]: ScheduledTask.from_dict(item)
                for item in self._sqlite.list_scheduled_tasks()
            }
            return
        if not self.path.exists():
            self._tasks = {}
            self._signature = None
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load scheduled tasks: %s", exc)
            self._tasks = {}
            self._signature = None
            return

        raw_tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
        tasks: Dict[str, ScheduledTask] = {}
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task = ScheduledTask.from_dict(item)
            tasks[task.id] = task
        self._tasks = tasks
        self._signature = _path_signature(self.path)

    def maybe_reload(self) -> bool:
        if self._sqlite is not None:
            changed = self._sqlite.maybe_reload()
            if changed:
                self.load()
            return changed
        signature = _path_signature(self.path)
        if signature == self._signature:
            return False
        self.load()
        return True

    def _save(self) -> None:
        if self._sqlite is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [task.to_dict() for task in self.list_tasks()]}
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, indent=2)
            tmp_path = Path(handle.name)
        tmp_path.replace(self.path)
        self._signature = _path_signature(self.path)

    def list_tasks(self) -> list[ScheduledTask]:
        return sorted(self._tasks.values(), key=lambda item: (item.created_at, item.id))

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        return self._tasks.get(task_id)

    def upsert_task(self, task: ScheduledTask) -> ScheduledTask:
        task.updated_at = _utc_now_iso()
        self._tasks[task.id] = task
        if self._sqlite is not None:
            self._sqlite.upsert_scheduled_task(task.to_dict())
            return task
        self._save()
        return task

    def add_task(
        self,
        *,
        name: Optional[str] = None,
        session_key: str,
        session_id: Optional[str] = None,
        prompt: str,
        schedule_type: str,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        cron: Optional[str] = None,
        run_at: Optional[str] = None,
        timezone_name: str,
    ) -> ScheduledTask:
        task = ScheduledTask(
            id=uuid4().hex[:12],
            name=name,
            session_key=session_key,
            session_id=session_id,
            prompt=prompt,
            schedule_type=schedule_type,
            agent_name=agent_name,
            session_policy=session_policy or ("existing" if session_id or session_key else None),
            post_to=post_to,
            deliver_key=deliver_key,
            cron=cron,
            run_at=run_at,
            timezone=timezone_name,
        )
        return self.upsert_task(task)

    def remove_task(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        if self._sqlite is not None:
            self._sqlite.remove_task(task_id)
            return True
        self._save()
        return True

    def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask:
        task = self._tasks[task_id]
        task.enabled = enabled
        task.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_scheduled_task(task.to_dict())
            return task
        self._save()
        return task

    def update_task(
        self,
        task_id: str,
        *,
        name: Optional[str],
        session_key: str,
        prompt: str,
        schedule_type: str,
        post_to: Optional[str],
        deliver_key: Optional[str],
        cron: Optional[str],
        run_at: Optional[str],
        timezone_name: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
    ) -> ScheduledTask:
        task = self._tasks[task_id]
        task.name = name
        task.session_key = session_key
        task.session_id = session_id
        task.prompt = prompt
        task.schedule_type = schedule_type
        task.agent_name = agent_name
        if session_policy is None:
            session_policy = task.session_policy or ("existing" if session_id or session_key else None)
        task.session_policy = session_policy
        task.post_to = post_to
        task.deliver_key = deliver_key
        task.cron = cron
        task.run_at = run_at
        task.timezone = timezone_name
        task.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_scheduled_task(task.to_dict())
            return task
        self._save()
        return task

    def mark_task_result(self, task_id: str, *, error: Optional[str], disable_one_shot: bool = True) -> bool:
        self.maybe_reload()
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.last_run_at = _utc_now_iso()
        task.last_error = error
        if disable_one_shot and task.schedule_type == "at":
            task.enabled = False
        task.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_scheduled_task(task.to_dict())
            return True
        self._save()
        return True


class TaskExecutionStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or (paths.get_state_dir() / "task_requests")
        self._sqlite = SQLiteBackgroundTaskStore() if root is None else None
        self.pending_dir = self.root / "pending"
        self.processing_dir = self.root / "processing"
        self.completed_dir = self.root / "completed"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        if self._sqlite is not None:
            return
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.processing_dir.mkdir(parents=True, exist_ok=True)
        self.completed_dir.mkdir(parents=True, exist_ok=True)

    def _request_path(self, request_id: str, *, state: str) -> Path:
        directory = {
            "pending": self.pending_dir,
            "processing": self.processing_dir,
            "completed": self.completed_dir,
        }[state]
        return directory / f"{request_id}.json"

    def recover_processing(self) -> None:
        if self._sqlite is not None:
            self._sqlite.recover_processing_runs()
            return
        self._ensure_dirs()
        for path in self.processing_dir.glob("*.json"):
            pending_path = self.pending_dir / path.name
            completed_path = self.completed_dir / path.name
            if pending_path.exists():
                path.unlink(missing_ok=True)
                continue
            if completed_path.exists():
                path.unlink(missing_ok=True)
                continue
            path.replace(pending_path)

    def enqueue(self, request: TaskExecutionRequest) -> TaskExecutionRequest:
        if self._sqlite is not None:
            payload = request.to_dict()
            payload["status"] = "queued"
            payload["updated_at"] = request.created_at
            self._sqlite.enqueue_run(payload)
            return request
        self._ensure_dirs()
        path = self._request_path(request.id, state="pending")
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.pending_dir,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(request.to_dict(), handle, indent=2)
            tmp_path = Path(handle.name)
        tmp_path.replace(path)
        return request

    def enqueue_task_run(
        self,
        task_id: str,
        *,
        source_kind: str = "cli",
        task: Optional[ScheduledTask] = None,
    ) -> TaskExecutionRequest:
        if task is None:
            return self.enqueue(
                TaskExecutionRequest(
                    id=uuid4().hex[:12],
                    request_type="scheduled",
                    task_id=task_id,
                    source_kind=source_kind,
                )
            )
        return self.enqueue_definition_run(
            definition_id=task.id,
            run_type="scheduled",
            source_kind=source_kind,
            session_key=task.session_key,
            session_id=task.session_id,
            post_to=task.post_to,
            deliver_key=task.deliver_key,
            prompt=task.prompt,
            agent_name=task.agent_name,
            session_policy=task.session_policy,
        )

    def enqueue_definition_run(
        self,
        *,
        definition_id: str,
        run_type: str,
        source_kind: str,
        session_key: str,
        session_id: Optional[str],
        post_to: Optional[str],
        deliver_key: Optional[str],
        prompt: str,
        agent_name: Optional[str],
        session_policy: Optional[str],
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
    ) -> TaskExecutionRequest:
        return self.enqueue(
            TaskExecutionRequest(
                id=uuid4().hex[:12],
                request_type=run_type,
                task_id=definition_id,
                session_key=session_key,
                session_id=session_id,
                post_to=post_to,
                deliver_key=deliver_key,
                prompt=prompt,
                message=prompt,
                source_kind=source_kind,
                source_actor=source_actor,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                session_policy=session_policy,
            )
        )

    def enqueue_hook_send(
        self,
        *,
        session_key: str,
        session_id: Optional[str] = None,
        prompt: str,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        run_type: str = "hook_send",
        definition_id: Optional[str] = None,
        source_kind: str = "cli",
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
    ) -> TaskExecutionRequest:
        return self.enqueue(
            TaskExecutionRequest(
                id=uuid4().hex[:12],
                request_type=run_type,
                task_id=definition_id,
                session_key=session_key,
                session_id=session_id,
                post_to=post_to,
                deliver_key=deliver_key,
                prompt=prompt,
                message=prompt,
                source_kind=source_kind,
                source_actor=source_actor,
                parent_run_id=parent_run_id,
                agent_name=agent_name,
                session_policy=session_policy,
            )
        )

    def enqueue_agent_run(
        self,
        *,
        message: str,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_backend: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        session_policy: Optional[str] = None,
        session_key: str = "",
        session_id: Optional[str] = None,
        post_to: Optional[str] = None,
        deliver_key: Optional[str] = None,
        source_kind: str = "cli",
        source_actor: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        callback_session_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TaskExecutionRequest:
        return self.enqueue(
            TaskExecutionRequest(
                id=uuid4().hex[:12],
                request_type="agent_run",
                session_key=session_key,
                session_id=session_id,
                post_to=post_to,
                deliver_key=deliver_key,
                prompt=message,
                message=message,
                source_kind=source_kind,
                source_actor=source_actor,
                parent_run_id=parent_run_id,
                callback_session_id=callback_session_id,
                callback_status="pending" if callback_session_id else None,
                agent_name=agent_name,
                agent_id=agent_id,
                agent_backend=agent_backend,
                model=model,
                reasoning_effort=reasoning_effort,
                session_policy=session_policy,
                metadata=dict(metadata or {}),
            )
        )

    def list_pending(self) -> list[TaskExecutionRequest]:
        if self._sqlite is not None:
            return [
                TaskExecutionRequest.from_dict(item)
                for item in self._sqlite.list_runs(status="pending")
                if item.get("request_type") in {"task_run", "hook_send", "agent_run", "scheduled", "watch", "webhook"}
                and not (item.get("metadata") or {}).get("workbench_queue_holds_run")
            ]
        self._ensure_dirs()
        requests: list[TaskExecutionRequest] = []
        for path in self.pending_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Failed to read task request %s: %s", path, exc)
                continue
            if not isinstance(payload, dict):
                continue
            requests.append(TaskExecutionRequest.from_dict(payload))
        return sorted(requests, key=lambda item: (item.created_at, item.id))

    def list_runs(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.list_runs(status=status)
        return self._list_file_runs(status=status)

    def list_pending_callbacks(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.list_pending_callbacks(limit=limit)
        runs = [
            item
            for item in self._list_file_runs()
            if item.get("callback_session_id")
            and item.get("callback_status") == "pending"
            and item.get("completed_at")
            and (_normalize_requested_run_status(item.get("status")) or item.get("status")) in TERMINAL_RUN_STATUSES
        ]
        return sorted(runs, key=lambda item: (item.get("completed_at") or "", item.get("id") or ""))[:limit]

    def update_callback_status(
        self,
        run_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        callback_run_id: Optional[str] = None,
    ) -> None:
        if self._sqlite is not None:
            self._sqlite.update_callback_status(
                run_id,
                status=status,
                error=error,
                callback_run_id=callback_run_id,
            )
            return
        now = _utc_now_iso()
        for state in ("pending", "processing", "completed"):
            path = self._request_path(run_id, state=state)
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "callback_status": status,
                    "callback_error": error,
                    "callback_run_id": callback_run_id if callback_run_id is not None else payload.get("callback_run_id"),
                    "callback_completed_at": now,
                    "updated_at": now,
                }
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=path.parent,
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                json.dump(payload, handle, indent=2)
                tmp_path = Path(handle.name)
            tmp_path.replace(path)
            return

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
        if self._sqlite is not None:
            return self._sqlite.list_runs_page(
                status=status,
                run_type=run_type,
                agent_name=agent_name,
                agent_backend=agent_backend,
                session_id=session_id,
                definition_id=definition_id,
                created_after=created_after,
                created_before=created_before,
                query=query,
                page_request=page_request,
                newest_first=newest_first,
            )
        runs = self._list_file_runs(status=status)
        if run_type:
            runs = [item for item in runs if (item.get("run_type") or item.get("request_type")) == run_type]
        if agent_name:
            runs = [item for item in runs if item.get("agent_name") == agent_name]
        if agent_backend:
            runs = [item for item in runs if item.get("agent_backend") == agent_backend]
        if session_id:
            runs = [item for item in runs if item.get("session_id") == session_id]
        if definition_id:
            runs = [item for item in runs if (item.get("definition_id") or item.get("task_id")) == definition_id]
        if created_after:
            runs = [item for item in runs if str(item.get("created_at") or "") >= created_after]
        if created_before:
            runs = [item for item in runs if str(item.get("created_at") or "") <= created_before]
        if query:
            needle = query.casefold()
            fields = ("id", "definition_id", "task_id", "agent_name", "session_id", "prompt", "message", "result_text", "error", "stdout", "stderr")
            runs = [
                item
                for item in runs
                if any(needle in str(item.get(field) or "").casefold() for field in fields)
            ]
        runs = sorted(runs, key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=newest_first)
        return page_sequence(runs, page_request)

    def _list_file_runs(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        status_filter = _run_file_state_for_status(status)
        runs: list[dict[str, Any]] = []
        for state, directory in {
            "pending": self.pending_dir,
            "processing": self.processing_dir,
            "completed": self.completed_dir,
        }.items():
            if status_filter and status_filter != state:
                continue
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(payload, dict):
                    normalized_status = _normalize_file_run_status(payload, state)
                    requested_status = _normalize_requested_run_status(status)
                    if requested_status and normalized_status != requested_status:
                        continue
                    payload["status"] = normalized_status
                    runs.append(payload)
        return sorted(runs, key=lambda item: (item.get("created_at") or "", item.get("id") or ""))

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        if self._sqlite is not None:
            return self._sqlite.get_run(run_id)
        for item in self.list_runs():
            if item.get("id") == run_id:
                return item
        return None

    def cancel_run(self, run_id: str) -> bool:
        if self._sqlite is not None:
            return self._sqlite.cancel_run(run_id)
        now = _utc_now_iso()
        pending_path = self._request_path(run_id, state="pending")
        if pending_path.exists():
            try:
                payload = json.loads(pending_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "id": run_id,
                    "status": "canceled",
                    "cancel_requested": True,
                    "cancel_requested_at": now,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            completed_path = self._request_path(run_id, state="completed")
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.completed_dir,
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                json.dump(payload, handle, indent=2)
                tmp_path = Path(handle.name)
            tmp_path.replace(completed_path)
            pending_path.unlink(missing_ok=True)
            return True

        processing_path = self._request_path(run_id, state="processing")
        if processing_path.exists():
            try:
                payload = json.loads(processing_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"id": run_id}
            if not isinstance(payload, dict):
                payload = {"id": run_id}
            payload.update(
                {
                    "id": run_id,
                    "cancel_requested": True,
                    "cancel_requested_at": now,
                    "updated_at": now,
                }
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.processing_dir,
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                json.dump(payload, handle, indent=2)
                tmp_path = Path(handle.name)
            tmp_path.replace(processing_path)
            return True
        return False

    def claim(self, request_id: str) -> Optional[TaskExecutionRequest]:
        if self._sqlite is not None:
            now = _utc_now_iso()
            payload = self._sqlite.claim_pending_run(request_id, started_at=now)
            if payload is None:
                return None
            return TaskExecutionRequest.from_dict(payload)
        pending_path = self._request_path(request_id, state="pending")
        processing_path = self._request_path(request_id, state="processing")
        if not pending_path.exists():
            return None
        pending_path.replace(processing_path)
        payload = json.loads(processing_path.read_text(encoding="utf-8"))
        return TaskExecutionRequest.from_dict(payload)

    def requeue(self, request_id: str, *, metadata: Optional[dict[str, Any]] = None) -> None:
        if self._sqlite is not None:
            if metadata is not None:
                self._sqlite.mark_run_queued_from_running(request_id, updated_at=_utc_now_iso(), metadata=metadata)
            else:
                self._sqlite.update_run_status(request_id, status="queued", updated_at=_utc_now_iso())
            return
        processing_path = self._request_path(request_id, state="processing")
        pending_path = self._request_path(request_id, state="pending")
        if not processing_path.exists():
            return
        if pending_path.exists():
            processing_path.unlink(missing_ok=True)
            return
        processing_path.replace(pending_path)

    def complete(
        self,
        request: TaskExecutionRequest,
        *,
        ok: bool,
        error: Optional[str] = None,
        task_id: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        if self._sqlite is not None:
            self._sqlite.update_run_status(
                request.id,
                status="succeeded" if ok else "failed",
                error=error,
                completed_at=_utc_now_iso(),
                updated_at=_utc_now_iso(),
                task_id=task_id if task_id is not None else request.task_id,
                session_key=session_key if session_key is not None else request.session_key,
                session_id=session_id if session_id is not None else request.session_id,
                metadata={"ok": ok},
            )
            return
        processing_path = self._request_path(request.id, state="processing")
        completed_path = self._request_path(request.id, state="completed")
        payload = request.to_dict()
        payload.update(
            {
                "ok": ok,
                "error": error,
                "completed_at": _utc_now_iso(),
                "task_id": task_id if task_id is not None else request.task_id,
                "session_key": session_key if session_key is not None else request.session_key,
                "session_id": session_id if session_id is not None else request.session_id,
                "callback_session_id": request.callback_session_id,
            }
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.completed_dir,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, indent=2)
            tmp_path = Path(handle.name)
        tmp_path.replace(completed_path)
        processing_path.unlink(missing_ok=True)


class ScheduledTaskService:
    """Controller-owned runtime that executes persisted scheduled tasks."""

    # Upper bound on claimed requests executing concurrently. The drain loop
    # never blocks waiting on an execution: when this many are in flight it
    # simply leaves the rest queued and re-checks on the next tick. This caps
    # fan-out without re-introducing head-of-line blocking.
    _MAX_CONCURRENT_EXECUTIONS = 8

    def __init__(
        self,
        controller,
        store: Optional[ScheduledTaskStore] = None,
        request_store: Optional[TaskExecutionStore] = None,
    ):
        self.controller = controller
        self.store = store or ScheduledTaskStore()
        self.request_store = request_store or TaskExecutionStore()
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._reconcile_task: Optional[asyncio.Task] = None
        self._job_signatures: Dict[str, tuple[Any, ...]] = {}
        self._running = False
        self._watch_store_restart_count = 0
        # Claimed requests currently executing, keyed by request id, so a
        # single slow/hung turn can't stall delivery of every other request.
        self._inflight_executions: Dict[str, "asyncio.Task[Any]"] = {}
        # Canonical conversation keys with an execution in flight. Used to
        # serialize turns per session (never two at once for the same
        # conversation) while still running different sessions concurrently.
        self._inflight_sessions: set[str] = set()
        # Cache of session_id -> canonical lock key (resolution hits SQLite).
        self._session_lock_cache: Dict[str, str] = {}
        self.request_store.recover_processing()

    def validate_platform(self, platform: str) -> None:
        # The real IM platforms have a settings manager; ``avibe`` (the web
        # workbench) is a virtual platform with an IM client but no settings
        # manager — accept it too so scheduled tasks/watches can target a
        # workbench session (they fire like a harness turn, reply via message.new).
        if (
            platform not in self.controller.platform_settings_managers
            and platform not in getattr(self.controller, "im_clients", {})
        ):
            raise ValueError(f"unsupported task platform: {platform}")

    def start(self) -> None:
        if self._running:
            return
        self.scheduler.start()
        self._running = True
        self._spawn_watch_store()
        try:
            self.reconcile_jobs()
        except Exception as exc:
            logger.error("Initial scheduled task reconcile failed: %s", exc, exc_info=True)

    def _spawn_watch_store(self) -> None:
        self._reconcile_task = asyncio.create_task(self._watch_store())
        self._reconcile_task.add_done_callback(self._on_watch_store_done)

    def _on_watch_store_done(self, task: "asyncio.Task[Any]") -> None:
        # Only respawn if the service is still meant to be running. During
        # stop() we deliberately cancel the task and clear _running first.
        if not self._running:
            return
        if task.cancelled():
            cause: Any = "CancelledError"
        else:
            cause = task.exception()
        self._watch_store_restart_count += 1
        logger.error(
            "Scheduled task watch store exited unexpectedly "
            "(restart_count=%d, cause=%r); respawning",
            self._watch_store_restart_count,
            cause,
        )
        self._spawn_watch_store()

    async def stop(self) -> None:
        self._running = False
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        # Cancel any in-flight executions so shutdown is clean. Cancellation is
        # caught by ``_execute_claimed_request``, which requeues the run, so it
        # is picked up again on the next start (and ``recover_processing`` on
        # init backstops anything left ``running`` after a hard crash).
        inflight = list(self._inflight_executions.values())
        for task in inflight:
            task.cancel()
        for task in inflight:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._inflight_executions.clear()
        self._inflight_sessions.clear()
        self.scheduler.shutdown(wait=False)

    async def _watch_store(self) -> None:
        while self._running:
            try:
                if self.store.maybe_reload():
                    self.reconcile_jobs()
                await self._drain_requests()
                await self._drain_callbacks()
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Scheduled task store watch failed: %s", exc, exc_info=True)
                try:
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    raise

    def reconcile_jobs(self) -> None:
        desired_ids = set()
        for task in self.store.list_tasks():
            if not task.enabled:
                continue
            desired_ids.add(task.id)
            signature = (
                task.schedule_type,
                task.cron,
                task.run_at,
                task.timezone,
                task.session_id,
                task.session_key,
                task.prompt,
                task.enabled,
            )
            if self._job_signatures.get(task.id) == signature and self.scheduler.get_job(task.id):
                continue
            if self.scheduler.get_job(task.id):
                self.scheduler.remove_job(task.id)
            try:
                trigger = self._build_trigger(task)
                self.scheduler.add_job(
                    self._run_task,
                    trigger=trigger,
                    id=task.id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    args=[task.id],
                )
            except Exception as exc:
                self._job_signatures.pop(task.id, None)
                logger.error("Failed to reconcile scheduled task %s: %s", task.id, exc, exc_info=True)
                continue
            self._job_signatures[task.id] = signature

        for job in list(self.scheduler.get_jobs()):
            if job.id not in desired_ids:
                self.scheduler.remove_job(job.id)
                self._job_signatures.pop(job.id, None)

    def _build_trigger(self, task: ScheduledTask):
        tz = ZoneInfo(task.timezone)
        if task.schedule_type == "cron":
            if not task.cron:
                raise ValueError(f"scheduled task {task.id} is missing cron expression")
            return CronTrigger.from_crontab(task.cron, timezone=tz)
        if task.schedule_type == "at":
            if not task.run_at:
                raise ValueError(f"scheduled task {task.id} is missing run_at timestamp")
            return DateTrigger(run_date=datetime.fromisoformat(task.run_at).astimezone(tz))
        raise ValueError(f"unknown schedule type: {task.schedule_type}")

    async def _run_task(self, task_id: str) -> None:
        self.store.maybe_reload()
        task = self.store.get_task(task_id)
        if not task or not task.enabled:
            return
        queued = self.request_store.enqueue_task_run(task.id, source_kind="scheduler", task=task)
        request = self.request_store.claim(queued.id)
        if request is None:
            return
        await self._execute_claimed_request(request)

    async def _drain_requests(self) -> None:
        # Claim eligible pending requests and dispatch each as its own task,
        # then return immediately. The previous implementation awaited every
        # execution inline in this loop, so one turn that hung (e.g. an agent
        # backend that never returns) blocked the loop forever and every
        # later request piled up in ``queued``. Dispatching concurrently keeps
        # delivery flowing: a stuck turn only holds up its own session.
        for pending in self.request_store.list_pending():
            if len(self._inflight_executions) >= self._MAX_CONCURRENT_EXECUTIONS:
                # At capacity — leave the rest queued and retry next tick.
                # Crucially we never await here, so the loop can't be stalled.
                break
            if pending.id in self._inflight_executions:
                continue
            lock_key = self._execution_lock_key(pending)
            if lock_key is not None and lock_key in self._inflight_sessions:
                # A turn for this conversation is already running; keep this
                # one queued so we never run two turns for one session at once.
                # The next drain tick picks it up once the session frees.
                continue
            request = self.request_store.claim(pending.id)
            if request is None:
                continue
            self._spawn_execution(request, lock_key)

    async def _drain_callbacks(self) -> None:
        for run in self.request_store.list_pending_callbacks():
            run_id = str(run.get("id") or "")
            if not run_id:
                continue
            try:
                callback_run = self._enqueue_callback_run(run)
            except Exception as exc:
                logger.error("Agent run callback failed for %s: %s", run_id, exc, exc_info=True)
                self.request_store.update_callback_status(run_id, status="failed", error=str(exc))
                continue
            if callback_run is None:
                self.request_store.update_callback_status(run_id, status="skipped")
                continue
            self.request_store.update_callback_status(run_id, status="sent", callback_run_id=callback_run.id)

    def _enqueue_callback_run(self, run: dict[str, Any]) -> Optional[TaskExecutionRequest]:
        callback_session_id = str(run.get("callback_session_id") or "").strip()
        if not callback_session_id:
            return None
        target_info = resolve_session_id_target(callback_session_id)
        message = self._build_callback_message(run)
        if not message.strip():
            return None
        return self.request_store.enqueue_agent_run(
            session_id=callback_session_id,
            session_key=target_info.session_key.to_key(),
            message=message,
            agent_name=target_info.agent_name,
            agent_id=target_info.agent_id,
            agent_backend=target_info.agent_backend,
            model=target_info.model,
            reasoning_effort=target_info.reasoning_effort,
            session_policy="existing",
            source_kind="callback",
            source_actor=str(run.get("id") or ""),
            parent_run_id=str(run.get("id") or "") or None,
        )

    def _build_callback_message(self, run: dict[str, Any]) -> str:
        status = _normalize_requested_run_status(run.get("status")) or str(run.get("status") or "")
        result_text = str(run.get("result_text") or "").strip()
        if not result_text:
            result_text = self._fallback_callback_result(run, status=status)
        return result_text.strip()

    @staticmethod
    def _fallback_callback_result(run: dict[str, Any], *, status: str) -> str:
        parts: list[str] = []
        if run.get("error"):
            parts.append(f"Error: {run['error']}")
        if run.get("stderr"):
            parts.append(str(run["stderr"]))
        if run.get("stdout") and status != "succeeded":
            parts.append(str(run["stdout"]))
        if parts:
            return "\n\n".join(part.strip() for part in parts if part and part.strip())
        if status == "canceled":
            return "The run was canceled before producing a result."
        if status == "failed":
            return "The run failed before producing a result."
        return ""

    def _execution_lock_key(self, request: TaskExecutionRequest) -> Optional[str]:
        """Canonical conversation identity for per-session single-flight.

        Resolves task-only and session-id-only requests down to one canonical
        key so any two requests targeting the same conversation serialize,
        regardless of which identifier form they carry:

        - ``scheduled``/``task_run`` rows may carry only a ``task_id``; the
          real target lives on the task definition (mirrors
          ``_execute_claimed_request``).
        - a ``session_id`` is resolved to its canonical session key, so it
          matches a legacy/watch run that only carries that ``session_key``.

        Returns ``None`` for ``create_per_run`` (fresh session each time) and
        unkeyable requests.
        """
        session_policy = request.session_policy
        session_id = request.session_id
        session_key = request.session_key
        task_id = request.task_id
        if request.request_type in {"task_run", "scheduled"} and task_id:
            task = self.store.get_task(task_id)
            if task is not None:
                session_policy = task.session_policy or session_policy
                session_id = task.session_id or session_id
                session_key = task.session_key or session_key
        if session_policy == "create_per_run":
            return None
        if session_id:
            return self._canonical_session_lock(session_id, session_key)
        if session_key:
            return self._normalize_session_key(session_key)
        if task_id:
            return f"task:{task_id}"
        return None

    def _canonical_session_lock(self, session_id: str, session_key: Optional[str]) -> str:
        cached = self._session_lock_cache.get(session_id)
        if cached is not None:
            return cached
        try:
            resolved = resolve_session_id_target(session_id)
            # avibe/workbench sessions are 1:1 with the session id — a project scope
            # holds many INDEPENDENT sessions, so locking on the project key would
            # serialize unrelated conversations. Lock on the concrete session id.
            if resolved.session_key.platform == "avibe" or resolved.session_key.scope_type == "project":
                key = f"sid:{session_id}"
            else:
                key = f"key:{resolved.session_key.to_key()}"
        except Exception:
            # avibe/web sessions (no IM scope) or unresolved ids: fall back to a
            # carried session key if present, else the id is its own identity.
            key = self._normalize_session_key(session_key) if session_key else f"sid:{session_id}"
        self._session_lock_cache[session_id] = key
        return key

    @staticmethod
    def _normalize_session_key(session_key: str) -> str:
        try:
            return f"key:{parse_session_key(session_key).to_key()}"
        except Exception:
            return f"key:{session_key}"

    def _spawn_execution(self, request: TaskExecutionRequest, lock_key: Optional[str]) -> None:
        if lock_key is not None:
            self._inflight_sessions.add(lock_key)
        task = asyncio.create_task(self._execute_claimed_request(request))
        self._inflight_executions[request.id] = task
        task.add_done_callback(
            lambda finished, rid=request.id, key=lock_key: self._on_execution_done(rid, key, finished)
        )

    def _on_execution_done(
        self, request_id: str, lock_key: Optional[str], task: "asyncio.Task[Any]"
    ) -> None:
        self._inflight_executions.pop(request_id, None)
        if lock_key is not None:
            self._inflight_sessions.discard(lock_key)
        # ``_execute_claimed_request`` already records failures and requeues on
        # cancellation; this only surfaces unexpected crashes in the wrapper.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Claimed request %s crashed: %r", request_id, exc, exc_info=exc)

    async def _execute_claimed_request(self, request: TaskExecutionRequest) -> None:
        error: Optional[str] = None
        should_complete = True
        task_id = request.task_id
        session_key = request.session_key
        session_id = request.session_id
        try:
            if request.request_type in {"task_run", "scheduled"}:
                self.store.maybe_reload()
                task = self.store.get_task(request.task_id or "")
                if task is None:
                    raise ValueError(f"task '{request.task_id}' not found")
                task_id = task.id
                session_key = task.session_key
                session_id = task.session_id
                result = await self._execute_task(
                    task,
                    execution_id=request.id,
                    disable_one_shot=request.source_kind == "scheduler",
                )
                error = result.error
                session_key = result.session_key
                session_id = result.session_id
            elif request.request_type in {"hook_send", "watch", "webhook"}:
                if not request.prompt:
                    raise ValueError("hook request requires prompt")
                if request.session_policy == "create_per_run":
                    session_id = self._reserve_runtime_session(
                        agent_name=request.agent_name,
                        deliver_key=request.deliver_key,
                    )
                    session_key = ""
                elif not (request.session_id or request.session_key):
                    raise ValueError("hook request requires session_id or session_key")
                error = await self._execute_request(
                    session_key=session_key,
                    session_id=session_id,
                    post_to=request.post_to,
                    deliver_key=request.deliver_key,
                    prompt=request.prompt,
                    execution_id=request.id,
                    task_id=task_id,
                    trigger_kind=request.request_type if request.request_type != "hook_send" else "hook",
                    agent_name=request.agent_name,
                )
            elif request.request_type == "agent_run":
                if not request.message:
                    raise ValueError("agent run requires message")
                if not (request.session_id or request.session_key):
                    raise ValueError("agent run currently requires session_id or a resolvable session target")
                result = await self._execute_agent_run(
                    session_key=request.session_key,
                    session_id=request.session_id,
                    post_to=request.post_to,
                    deliver_key=request.deliver_key,
                    message=request.message,
                    execution_id=request.id,
                    agent_name=request.agent_name,
                    metadata=request.metadata,
                )
                error = result.error
                should_complete = result.complete_on_return
                if result.requeue_on_return:
                    self.request_store.requeue(request.id, metadata={"workbench_queue_holds_run": True})
            else:
                raise ValueError(f"unknown task request type: {request.request_type}")
        except asyncio.CancelledError:
            self.request_store.requeue(request.id)
            should_complete = False
            raise
        except Exception as exc:
            error = str(exc)
            logger.error("Task execution request %s failed: %s", request.id, exc, exc_info=True)
            should_complete = True
        finally:
            if should_complete:
                self.request_store.complete(
                    request,
                    ok=not error,
                    error=error,
                    task_id=task_id,
                    session_key=session_key,
                    session_id=session_id,
                )
                await self._drain_callbacks()

    async def _execute_task(
        self,
        task: ScheduledTask,
        *,
        execution_id: str,
        disable_one_shot: bool,
    ) -> TaskExecutionResult:
        error: Optional[str] = None
        session_id = task.session_id
        session_key = task.session_key
        try:
            if task.session_policy == "create_per_run":
                session_id = self._reserve_runtime_session(
                    agent_name=task.agent_name,
                    deliver_key=task.deliver_key,
                )
                session_key = ""
            error = await self._execute_request(
                session_key=session_key,
                session_id=session_id,
                post_to=task.post_to,
                deliver_key=task.deliver_key,
                prompt=task.prompt,
                execution_id=execution_id,
                task_id=task.id,
                trigger_kind="scheduled",
                agent_name=task.agent_name,
            )
        except asyncio.CancelledError:
            self.reconcile_jobs()
            raise
        except Exception as exc:
            error = str(exc)
            logger.error("Scheduled task %s failed: %s", task.id, exc, exc_info=True)
        self.store.mark_task_result(task.id, error=error, disable_one_shot=disable_one_shot)
        self.reconcile_jobs()
        return TaskExecutionResult(error=error, session_key=session_key, session_id=session_id)

    async def _execute_agent_run(
        self,
        *,
        session_key: Optional[str],
        post_to: Optional[str],
        deliver_key: Optional[str],
        message: str,
        execution_id: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AgentRunExecutionResult:
        """Execute one direct Agent Run and wait for the real terminal result.

        Direct ``vibe agent run`` records model one concrete Agent turn. Async
        backends (Codex/Claude) return from ``handle_scheduled_message`` once the
        prompt is submitted, while their actual result arrives later through
        ``emit_agent_message``. Use the shared dispatch sink so the run stays
        ``running`` until that terminal result is emitted.
        """
        from core.services.dispatch import SOURCE_SCHEDULED, dispatch_turn

        target_info = resolve_session_id_target(session_id) if session_id else None
        target = target_info.session_key if target_info else parse_session_key(session_key or "")
        delivery_target = self._resolve_delivery_target(
            session_target=target,
            post_to=post_to,
            deliver_key=deliver_key,
        )
        context = await self._build_context(
            target,
            delivery_target=delivery_target,
            execution_id=execution_id,
            trigger_kind="agent_run",
            session_id=session_id,
            agent_name=agent_name,
            target_info=target_info,
            metadata=metadata,
        )

        gate = getattr(self.controller, "session_turn_gate", None)
        if target.platform == "avibe" and session_id and gate is not None:
            state = await gate.submit_scheduled(session_id, context, message)
            if state == "enqueued":
                return AgentRunExecutionResult(error=None, complete_on_return=False, requeue_on_return=True)
            return AgentRunExecutionResult(error=None, complete_on_return=False)

        async def _noop_chunk(_envelope: dict) -> None:
            return None

        result = await dispatch_turn(
            self.controller,
            context,
            message,
            source=SOURCE_SCHEDULED,
            on_chunk=_noop_chunk,
        )
        if result:
            return AgentRunExecutionResult(error=str(result), complete_on_return=True)
        return AgentRunExecutionResult(error=None, complete_on_return=False)

    def _reserve_runtime_session(self, *, agent_name: Optional[str], deliver_key: Optional[str]) -> str:
        if not deliver_key:
            raise ValueError("session creation requires deliver_key")
        from config import paths as config_paths
        from core.vibe_agents import VibeAgentStore
        from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
        from storage.sessions_service import SQLiteSessionsService

        target = parse_session_key(deliver_key)
        ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(config_paths.get_state_dir()))
        agent_store = VibeAgentStore()
        try:
            scope_target = self._resolve_scope_agent_target(deliver_key) if not agent_name else _ScopeAgentTarget(None, None)
            resolved_agent_name = agent_name or scope_target.agent_name
            if not resolved_agent_name and scope_target.agent_backend:
                raise ValueError(
                    "scope routing still references a legacy backend without an Agent; "
                    "choose an Agent before creating sessions for this Scope"
                )
            agent = agent_store.require_enabled(resolved_agent_name) if resolved_agent_name else None
        finally:
            agent_store.close()
        agent_backend = agent.backend if agent else self.controller.agent_router.global_default
        service = SQLiteSessionsService(config_paths.get_sqlite_state_path())
        try:
            session_id = service.reserve_agent_session(
                scope_key=target.session_scope,
                agent_backend=agent_backend,
                session_anchor=session_anchor_for_target(target),
                agent_id=agent.id if agent else None,
                agent_name=agent.name if agent else None,
                model=agent.model if agent else None,
                reasoning_effort=agent.reasoning_effort if agent else None,
            )
        finally:
            service.close()
        if not session_id:
            raise ValueError("failed to reserve runtime session")
        return session_id

    def _resolve_scope_agent_target(self, deliver_key: str) -> "_ScopeAgentTarget":
        try:
            target = parse_session_key(deliver_key)
        except ValueError:
            return _ScopeAgentTarget(None, None)
        from config import paths as config_paths
        from storage.settings_service import make_scope_id

        scope_id = make_scope_id(target.platform, target.scope_type, target.scope_id)
        engine = create_sqlite_engine(config_paths.get_sqlite_state_path())
        try:
            with engine.connect() as conn:
                value = conn.execute(
                    select(scope_settings.c.agent_name, scope_settings.c.agent_backend)
                    .where(scope_settings.c.scope_id == scope_id)
                    .limit(1)
                ).first()
        finally:
            engine.dispose()
        if value is None:
            return _ScopeAgentTarget(None, None)
        agent_name = str(value.agent_name).strip() if value.agent_name else None
        agent_backend = str(value.agent_backend).strip() if value.agent_backend else None
        return _ScopeAgentTarget(agent_name, agent_backend)

    async def _execute_request(
        self,
        *,
        session_key: Optional[str],
        post_to: Optional[str],
        deliver_key: Optional[str],
        prompt: str,
        execution_id: str,
        task_id: Optional[str] = None,
        trigger_kind: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[str]:
        target_info = resolve_session_id_target(session_id) if session_id else None
        target = target_info.session_key if target_info else parse_session_key(session_key or "")
        delivery_target = self._resolve_delivery_target(
            session_target=target,
            post_to=post_to,
            deliver_key=deliver_key,
        )
        context = await self._build_context(
            target,
            delivery_target=delivery_target,
            execution_id=execution_id,
            task_id=task_id,
            trigger_kind=trigger_kind,
            session_id=session_id,
            agent_name=agent_name,
            target_info=target_info,
        )
        # A scheduled avibe turn drives the sidebar dot through the SAME two
        # chokepoints as any other turn — inbound AgentService.handle_message
        # (running) and the outbound terminal result (idle/failed) — because its
        # ``context`` carries the avibe ``agent_session_id`` (set in
        # ``_build_context``). No dot bookkeeping here.
        #
        # Route avibe runs through the per-session turn gate the Chat HTTP path
        # uses, so a scheduled / watch / webhook / agent_run turn targeting an
        # avibe session QUEUES behind an active Chat turn (never preempts it) and
        # gets the in_flight + turn.start / turn.end lifecycle that makes the Chat
        # page show the working indicator + Stop (Codex P2). The gate runs on the
        # controller's loop and is published by ``internal_server.create_app``.
        # Returning ``None`` keeps ``ok = not error`` true (the run's own outcome
        # surfaces via the outbound terminal result + sidebar dot, exactly as the
        # interactive Chat turn does). IM targets NEVER touch the gate — they keep
        # the direct ``handle_scheduled_message`` path byte-for-byte.
        gate = getattr(self.controller, "session_turn_gate", None)
        if target.platform == "avibe" and session_id and gate is not None:
            await gate.submit_scheduled(session_id, context, prompt)
            return None
        return await self.controller.message_handler.handle_scheduled_message(
            context=context,
            message=prompt,
            parsed_session_key=target,
        )

    async def _build_context(
        self,
        target: ParsedSessionKey,
        *,
        delivery_target: Optional[ParsedSessionKey] = None,
        execution_id: str,
        task_id: Optional[str] = None,
        trigger_kind: str = "scheduled",
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        target_info: Optional[ResolvedSessionIdTarget] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MessageContext:
        platform = target.platform
        self.validate_platform(platform)
        delivery_target = delivery_target or target
        session_target_context = self._resolve_target_context(target)
        delivery_target_context = self._resolve_target_context(delivery_target)
        delivery_strategy = self._build_delivery_alias_strategy(
            session_target=target,
            delivery_target=delivery_target,
            session_context=session_target_context,
            delivery_context=delivery_target_context,
        )

        # avibe workbench: the context IDENTITY is the concrete session, not the
        # project scope — an avibe project holds many independent sessions, so
        # keying the context off the project id would make _get_session_key /
        # consolidated-log grouping collide between concurrent runs in the same
        # project (they'd edit/merge each other's log). Use session_id as the
        # channel_id (matches how the interactive Chat dispatch builds the context);
        # persistence/routing still resolves the project scope via agent_session_id.
        channel_id = session_target_context["channel_id"]
        if platform == "avibe" and session_id:
            channel_id = session_id
        from core.services.session_fork import fork_metadata_from_request, fork_metadata_from_session_metadata

        native_session_fork = fork_metadata_from_request(metadata)
        if native_session_fork is None and target_info and not str(target_info.native_session_id or "").strip():
            native_session_fork = fork_metadata_from_session_metadata(getattr(target_info, "metadata", None))

        return MessageContext(
            user_id=session_target_context["user_id"],
            channel_id=channel_id,
            platform=platform,
            thread_id=target.thread_id,
            message_id=self._build_message_id(
                execution_id=execution_id,
                task_id=task_id,
                trigger_kind=trigger_kind,
            ),
            platform_specific={
                "platform": platform,
                "is_dm": target.is_dm,
                "turn_source": "scheduled",
                "agent_session_id": session_id,
                "session_key_external": target.to_key(),
                "delivery_key_external": delivery_target.to_key(),
                "delivery_scope_session_key": delivery_target.session_scope,
                "delivery_override": {
                    "user_id": delivery_target_context["user_id"],
                    "channel_id": delivery_target_context["channel_id"],
                    "thread_id": delivery_target.thread_id,
                    "platform": platform,
                    "is_dm": delivery_target.is_dm,
                },
                "scheduled_delivery_alias": delivery_strategy,
                "task_execution_id": execution_id,
                "task_trigger_kind": trigger_kind,
                # Provenance source_id for harness-originated turns: the run
                # definition id (task / watch). Carried so the message mirror can
                # attribute the injected prompt to its precise definition.
                "task_definition_id": task_id,
                "vibe_agent_name": agent_name,
                "suppress_delivery": bool(target_info.suppress_delivery) if target_info else False,
                "agent_session_target": (
                    {
                        "id": target_info.session_id,
                        "agent_id": target_info.agent_id,
                        "agent_name": target_info.agent_name,
                        "agent_backend": target_info.agent_backend,
                        "agent_variant": target_info.agent_variant,
                        "model": target_info.model,
                        "reasoning_effort": target_info.reasoning_effort,
                        "native_session_id": target_info.native_session_id,
                        "native_session_fork": native_session_fork,
                        "workdir": target_info.workdir,
                        "session_anchor": target_info.session_anchor,
                        "metadata": getattr(target_info, "metadata", None) or {},
                        "suppress_delivery": target_info.suppress_delivery,
                    }
                    if target_info
                    else None
                ),
            },
        )

    def _resolve_target_context(self, target: ParsedSessionKey) -> Dict[str, Any]:
        platform = target.platform
        if platform not in self.controller.platform_settings_managers:
            # Virtual platform (avibe workbench): no per-platform settings manager
            # and no DM bindings — the scope_id IS the session/channel, and a
            # scheduled run is attributed to a synthetic "scheduled" user.
            return {"user_id": "scheduled", "channel_id": target.scope_id}
        settings_manager = self.controller.platform_settings_managers[platform]

        channel_id = target.scope_id
        user_id = "scheduled"
        if target.is_dm:
            user_id = target.scope_id
            bound_user = settings_manager.get_store().get_user(target.scope_id, platform=platform)
            if platform == "lark":
                dm_chat_id = getattr(bound_user, "dm_chat_id", "") if bound_user else ""
                if not dm_chat_id:
                    raise ValueError(f"lark user {target.scope_id} is missing dm_chat_id binding")
                channel_id = dm_chat_id
            elif bound_user and getattr(bound_user, "dm_chat_id", ""):
                channel_id = bound_user.dm_chat_id

        return {
            "user_id": user_id,
            "channel_id": channel_id,
        }

    def _resolve_delivery_target(
        self,
        *,
        session_target: ParsedSessionKey,
        post_to: Optional[str],
        deliver_key: Optional[str],
    ) -> ParsedSessionKey:
        if deliver_key:
            delivery_target = parse_session_key(deliver_key)
            if delivery_target.platform != session_target.platform:
                raise ValueError("--deliver-key must stay on the same platform as the session target")
            return delivery_target
        if post_to == "channel":
            return ParsedSessionKey(
                platform=session_target.platform,
                scope_type=session_target.scope_type,
                scope_id=session_target.scope_id,
                thread_id=None,
            )
        if post_to == "thread":
            if not session_target.thread_id:
                raise ValueError("--post-to thread requires a thread-bound session target or an explicit --deliver-key")
            return session_target
        return session_target

    def _supports_threaded_delivery(self, target: ParsedSessionKey) -> bool:
        getter = getattr(self.controller, "get_im_client_for_context", None)
        context = MessageContext(
            user_id=target.scope_id if target.is_dm else "scheduled",
            channel_id=target.scope_id,
            platform=target.platform,
            platform_specific={"platform": target.platform, "is_dm": target.is_dm},
        )
        if callable(getter):
            im_client = getter(context)
        else:
            im_client = getattr(self.controller, "im_client", None)
        if im_client is None:
            return False
        if target.is_dm:
            return bool(getattr(im_client, "should_use_thread_for_dm_session", lambda: False)())
        return bool(getattr(im_client, "should_use_thread_for_reply", lambda: False)())

    def _build_delivery_alias_strategy(
        self,
        *,
        session_target: ParsedSessionKey,
        delivery_target: ParsedSessionKey,
        session_context: Dict[str, Any],
        delivery_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        source_session_key = session_target.session_scope
        target_session_key = delivery_target.session_scope
        same_scope = source_session_key == target_session_key
        clear_provisional_source = session_target.thread_id is None and self._supports_threaded_delivery(session_target)

        if delivery_target.thread_id:
            alias_base = f"{delivery_target.platform}_{delivery_target.thread_id}"
            if same_scope and alias_base == f"{session_target.platform}_{session_target.thread_id}":
                return {"mode": "none"}
            return {
                "mode": "fixed_base",
                "session_key": target_session_key,
                "base_session_id": alias_base,
                "clear_source": clear_provisional_source,
            }

        if self._supports_threaded_delivery(delivery_target):
            return {
                "mode": "sent_message",
                "session_key": target_session_key,
                "clear_source": clear_provisional_source,
            }

        delivery_base_id = delivery_context["channel_id"]
        source_base_id = session_context["channel_id"]
        if same_scope and session_target.thread_id is None and delivery_base_id == source_base_id:
            return {"mode": "none"}
        return {
            "mode": "fixed_base",
            "session_key": target_session_key,
            "base_session_id": f"{delivery_target.platform}_{delivery_base_id}",
            "clear_source": clear_provisional_source,
        }

    @staticmethod
    def _build_message_id(*, execution_id: str, task_id: Optional[str], trigger_kind: str) -> str:
        if trigger_kind == "hook":
            return f"hook:{execution_id}"
        if trigger_kind == "watch":
            return f"watch:{task_id}:{execution_id}" if task_id else f"watch:{execution_id}"
        if trigger_kind == "webhook":
            return f"webhook:{task_id}:{execution_id}" if task_id else f"webhook:{execution_id}"
        if trigger_kind == "agent_run":
            return f"agent_run:{execution_id}"
        if task_id:
            return f"scheduled:{task_id}:{execution_id}"
        return f"scheduled:{execution_id}"
