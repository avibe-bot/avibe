"""Managed background watch persistence and runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from config import paths
from core.process_isolation import isolated_subprocess_kwargs, terminate_and_communicate
from core.scheduled_tasks import TaskExecutionStore
from storage.background import SQLiteBackgroundTaskStore
from vibe import runtime

logger = logging.getLogger(__name__)

DEFAULT_RETRY_EXIT_CODE = 75
WATCH_RECONCILE_INTERVAL_SECONDS = 2.0
WATCH_STORE_RECONCILE_FUSE_FAILURES = 3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_signature(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _payload_float(payload: dict[str, Any], key: str, default: float) -> float:
    if key not in payload or payload.get(key) is None:
        return default
    return float(payload[key])


@dataclass
class ManagedWatch:
    id: str
    name: Optional[str]
    session_key: str
    session_id: Optional[str] = None
    agent_name: Optional[str] = None
    session_policy: Optional[str] = None
    command: list[str] = field(default_factory=list)
    shell_command: Optional[str] = None
    prefix: Optional[str] = None
    message: Optional[str] = None
    cwd: Optional[str] = None
    mode: str = "once"
    timeout_seconds: float = 21600.0
    lifetime_timeout_seconds: float = 0.0
    retry_exit_codes: list[int] = field(default_factory=lambda: [DEFAULT_RETRY_EXIT_CODE])
    retry_delay_seconds: float = 30.0
    post_to: Optional[str] = None
    deliver_key: Optional[str] = None
    enabled: bool = True
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    last_started_at: Optional[str] = None
    last_finished_at: Optional[str] = None
    last_event_at: Optional[str] = None
    last_error: Optional[str] = None
    last_exit_code: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManagedWatch":
        return cls(
            id=str(payload.get("id") or uuid4().hex[:12]),
            name=(str(payload["name"]).strip() if payload.get("name") is not None else None) or None,
            session_key=str(payload.get("session_key") or ""),
            session_id=(str(payload["session_id"]).strip() if payload.get("session_id") else None),
            agent_name=(str(payload["agent_name"]).strip() if payload.get("agent_name") else None),
            session_policy=(str(payload["session_policy"]).strip() if payload.get("session_policy") else None),
            command=list(payload.get("command") or []),
            shell_command=(str(payload["shell_command"]).strip() if payload.get("shell_command") else None) or None,
            prefix=(str(payload["prefix"]).strip() if payload.get("prefix") else None) or None,
            message=(str(payload["message"]).strip() if payload.get("message") else None) or None,
            cwd=(str(payload["cwd"]).strip() if payload.get("cwd") else None) or None,
            mode=str(payload.get("mode") or "once"),
            timeout_seconds=_payload_float(payload, "timeout_seconds", 21600.0),
            lifetime_timeout_seconds=_payload_float(payload, "lifetime_timeout_seconds", 0.0),
            retry_exit_codes=[int(code) for code in (payload.get("retry_exit_codes") or [DEFAULT_RETRY_EXIT_CODE])],
            retry_delay_seconds=_payload_float(payload, "retry_delay_seconds", 30.0),
            post_to=payload.get("post_to"),
            deliver_key=payload.get("deliver_key"),
            enabled=bool(payload.get("enabled", True)),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            updated_at=str(payload.get("updated_at") or _utc_now_iso()),
            last_started_at=payload.get("last_started_at"),
            last_finished_at=payload.get("last_finished_at"),
            last_event_at=payload.get("last_event_at"),
            last_error=payload.get("last_error"),
            last_exit_code=(int(payload["last_exit_code"]) if payload.get("last_exit_code") is not None else None),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )


class ManagedWatchStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.get_watches_path()
        self._sqlite = SQLiteBackgroundTaskStore() if path is None else None
        self._signature: Optional[tuple[int, int, int]] = None
        self._watches: dict[str, ManagedWatch] = {}
        self.load()

    def load(self) -> None:
        if self._sqlite is not None:
            self._watches = {
                item["id"]: ManagedWatch.from_dict(item)
                for item in self._sqlite.list_watches()
            }
            return
        if not self.path.exists():
            self._watches = {}
            self._signature = None
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load managed watches: %s", exc)
            self._watches = {}
            self._signature = None
            return

        raw_watches = payload.get("watches", []) if isinstance(payload, dict) else []
        watches: dict[str, ManagedWatch] = {}
        for item in raw_watches:
            if not isinstance(item, dict):
                continue
            watch = ManagedWatch.from_dict(item)
            watches[watch.id] = watch
        self._watches = watches
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
        payload = {"watches": [watch.to_dict() for watch in self.list_watches()]}
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

    def list_watches(self) -> list[ManagedWatch]:
        return sorted(self._watches.values(), key=lambda item: (item.created_at, item.id))

    def get_watch(self, watch_id: str) -> Optional[ManagedWatch]:
        return self._watches.get(watch_id)

    def upsert_watch(self, watch: ManagedWatch) -> ManagedWatch:
        watch.updated_at = _utc_now_iso()
        self._watches[watch.id] = watch
        if self._sqlite is not None:
            self._sqlite.upsert_watch(watch.to_dict())
            return watch
        self._save()
        return watch

    def add_watch(
        self,
        *,
        name: Optional[str],
        session_key: str,
        command: list[str],
        shell_command: Optional[str],
        prefix: Optional[str],
        cwd: Optional[str],
        mode: str,
        timeout_seconds: float,
        lifetime_timeout_seconds: float,
        retry_exit_codes: list[int],
        retry_delay_seconds: float,
        post_to: Optional[str],
        deliver_key: Optional[str],
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ManagedWatch:
        watch = ManagedWatch(
            id=uuid4().hex[:12],
            name=name,
            session_key=session_key,
            session_id=session_id,
            agent_name=agent_name,
            session_policy=session_policy or ("existing" if session_id or session_key else None),
            command=command,
            shell_command=shell_command,
            prefix=prefix,
            message=message or prefix,
            cwd=cwd,
            mode=mode,
            timeout_seconds=timeout_seconds,
            lifetime_timeout_seconds=lifetime_timeout_seconds,
            retry_exit_codes=retry_exit_codes,
            retry_delay_seconds=retry_delay_seconds,
            post_to=post_to,
            deliver_key=deliver_key,
            metadata=dict(metadata or {}),
        )
        return self.upsert_watch(watch)

    def remove_watch(self, watch_id: str) -> bool:
        if watch_id not in self._watches:
            return False
        del self._watches[watch_id]
        if self._sqlite is not None:
            self._sqlite.remove_task(watch_id)
            return True
        self._save()
        return True

    def set_enabled(self, watch_id: str, enabled: bool) -> ManagedWatch:
        watch = self._watches[watch_id]
        watch.enabled = enabled
        watch.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_watch(watch.to_dict())
            return watch
        self._save()
        return watch

    def update_watch(
        self,
        watch_id: str,
        *,
        name: Optional[str],
        session_key: str,
        session_id: Optional[str],
        command: list[str],
        shell_command: Optional[str],
        prefix: Optional[str],
        cwd: Optional[str],
        mode: str,
        timeout_seconds: float,
        lifetime_timeout_seconds: float,
        retry_exit_codes: list[int],
        retry_delay_seconds: float,
        post_to: Optional[str],
        deliver_key: Optional[str],
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ManagedWatch:
        watch = self._watches[watch_id]
        watch.name = name
        watch.session_key = session_key
        watch.session_id = session_id
        watch.agent_name = agent_name
        if session_policy is None:
            session_policy = watch.session_policy or ("existing" if session_id or session_key else None)
        watch.session_policy = session_policy
        watch.command = command
        watch.shell_command = shell_command
        watch.prefix = prefix
        watch.message = message or prefix
        watch.cwd = cwd
        watch.mode = mode
        watch.timeout_seconds = timeout_seconds
        watch.lifetime_timeout_seconds = lifetime_timeout_seconds
        watch.retry_exit_codes = retry_exit_codes
        watch.retry_delay_seconds = retry_delay_seconds
        watch.post_to = post_to
        watch.deliver_key = deliver_key
        if metadata is not None:
            watch.metadata = dict(metadata)
        watch.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_watch(watch.to_dict())
            return watch
        self._save()
        return watch

    def mark_cycle_start(self, watch_id: str) -> bool:
        self.maybe_reload()
        watch = self._watches.get(watch_id)
        if watch is None:
            return False
        watch.last_started_at = _utc_now_iso()
        watch.last_error = None
        watch.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_watch(watch.to_dict())
            return True
        self._save()
        return True

    def mark_cycle_result(
        self,
        watch_id: str,
        *,
        exit_code: Optional[int],
        error: Optional[str],
        event_detected: bool = False,
        disable: bool = False,
    ) -> bool:
        self.maybe_reload()
        watch = self._watches.get(watch_id)
        if watch is None:
            return False
        watch.last_finished_at = _utc_now_iso()
        watch.last_exit_code = exit_code
        watch.last_error = error
        if event_detected:
            watch.last_event_at = watch.last_finished_at
        if disable:
            watch.enabled = False
        watch.updated_at = _utc_now_iso()
        if self._sqlite is not None:
            self._sqlite.upsert_watch(watch.to_dict())
            return True
        self._save()
        return True


class WatchRuntimeStateStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.get_watch_runtime_path()
        self._sqlite = SQLiteBackgroundTaskStore() if path is None else None

    def write(self, payload: dict[str, Any]) -> None:
        if self._sqlite is not None:
            self._sqlite.write_watch_runtime(payload, updated_at=_utc_now_iso())
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def load(self) -> dict[str, Any]:
        if self._sqlite is not None:
            return self._sqlite.load_watch_runtime()
        if not self.path.exists():
            return {"watches": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"watches": {}}
        if not isinstance(payload, dict):
            return {"watches": {}}
        return payload


@dataclass
class _CycleResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class ManagedWatchService:
    def __init__(
        self,
        controller,
        store: Optional[ManagedWatchStore] = None,
        request_store: Optional[TaskExecutionStore] = None,
        runtime_store: Optional[WatchRuntimeStateStore] = None,
    ):
        self.controller = controller
        self.store = store or ManagedWatchStore()
        self.request_store = request_store or TaskExecutionStore()
        self.runtime_store = runtime_store or WatchRuntimeStateStore()
        self._running = False
        self._reconcile_task: Optional[asyncio.Task] = None
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._active_pids: dict[str, int] = {}
        self._watch_started_at: dict[str, str] = {}
        self._fused_watch_ids: set[str] = set()
        self._store_error_fused = False
        self._store_reconcile_failures = 0
        self._requires_service_lease = runtime.service_instance_lock_attached_to_process()
        self._reconcile_dirty = True
        self._runtime_state_dirty = True

    def active_process_pids(self) -> set[int]:
        """Return active waiter process roots owned by managed watches."""
        return {pid for pid in self._active_pids.values() if isinstance(pid, int) and pid > 0}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._reconcile_task = asyncio.create_task(self._watch_store())
        try:
            if self.reconcile_watches():
                self._runtime_state_dirty = True
            self._write_runtime_state()
            self._reconcile_dirty = False
        except Exception as exc:
            self._reconcile_dirty = True
            self._handle_reconcile_store_error(exc)

    async def stop(self) -> None:
        self._begin_stop()
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
        self._active_tasks.clear()
        self._active_pids.clear()
        self._watch_started_at.clear()
        self._runtime_state_dirty = True
        self._write_runtime_state()

    async def _watch_store(self) -> None:
        while self._running:
            if not self._owns_service_instance():
                return
            if self._store_error_fused:
                await asyncio.sleep(WATCH_RECONCILE_INTERVAL_SECONDS)
                continue
            try:
                should_reconcile = self.store.maybe_reload() or self._reconcile_dirty
                if should_reconcile:
                    if self.reconcile_watches():
                        self._runtime_state_dirty = True
                if self._runtime_state_dirty:
                    self._write_runtime_state()
                self._store_reconcile_failures = 0
                self._reconcile_dirty = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reconcile_dirty = True
                self._handle_reconcile_store_error(exc)
            await asyncio.sleep(WATCH_RECONCILE_INTERVAL_SECONDS)

    def reconcile_watches(self) -> bool:
        if not self._owns_service_instance():
            return False
        if self._store_error_fused:
            return False
        watches = self.store.list_watches()
        desired_ids = {watch.id for watch in watches if watch.enabled}
        changed = False
        for watch in watches:
            if not watch.enabled or watch.id in self._active_tasks or watch.id in self._fused_watch_ids:
                continue
            task = asyncio.create_task(self._run_watch(watch.id))
            self._active_tasks[watch.id] = task
            task.add_done_callback(lambda _task, watch_id=watch.id: self._on_watch_done(watch_id))
            changed = True

        for watch_id, task in list(self._active_tasks.items()):
            if watch_id in desired_ids:
                continue
            task.cancel()
            changed = True

        return changed

    def _on_watch_done(self, watch_id: str) -> None:
        self._active_tasks.pop(watch_id, None)
        self._active_pids.pop(watch_id, None)
        self._watch_started_at.pop(watch_id, None)
        self._runtime_state_dirty = True
        self._write_runtime_state()
        self._reconcile_dirty = True

    def _write_runtime_state(self) -> None:
        payload = {"watches": {}}
        now = _utc_now_iso()
        for watch_id, task in self._active_tasks.items():
            payload["watches"][watch_id] = {
                "running": not task.done(),
                "pid": self._active_pids.get(watch_id),
                "started_at": self._watch_started_at.get(watch_id),
                "updated_at": now,
            }
        try:
            self.runtime_store.write(payload)
            self._runtime_state_dirty = False
        except Exception:
            self._runtime_state_dirty = True
            logger.exception("Failed to persist watch runtime state")

    def _fuse_store_after_error(self, operation: str, exc: Exception, *, watch_id: str | None = None) -> None:
        if watch_id is not None:
            self._fused_watch_ids.add(watch_id)
        self._store_error_fused = True
        logger.error(
            "Disabling watch store reconciliation after persistent store error "
            "(watch_id=%s operation=%s): %s",
            watch_id,
            operation,
            exc,
            exc_info=True,
        )

    def _handle_reconcile_store_error(self, exc: Exception) -> None:
        self._store_reconcile_failures += 1
        if self._store_reconcile_failures >= WATCH_STORE_RECONCILE_FUSE_FAILURES:
            self._fuse_store_after_error("reconcile", exc)
            return
        logger.warning(
            "Managed watch reconcile failed; will retry "
            "(attempt=%s/%s): %s",
            self._store_reconcile_failures,
            WATCH_STORE_RECONCILE_FUSE_FAILURES,
            exc,
            exc_info=True,
        )

    def _watch_store_call(self, watch_id: str, operation: str, callback) -> bool:
        try:
            callback()
            return True
        except Exception as exc:
            self._fuse_store_after_error(operation, exc, watch_id=watch_id)
            return False

    def _current_asyncio_task(self) -> Optional["asyncio.Task[Any]"]:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _begin_stop(self, *, cancel_reconcile: bool = True) -> None:
        self._running = False
        current_task = self._current_asyncio_task()
        if cancel_reconcile and self._reconcile_task and self._reconcile_task is not current_task:
            self._reconcile_task.cancel()
        for task in list(self._active_tasks.values()):
            if task is not current_task:
                task.cancel()
        self._runtime_state_dirty = True
        self._write_runtime_state()

    def _owns_service_instance(self) -> bool:
        if not self._requires_service_lease:
            return True
        if runtime.current_process_owns_service_instance():
            return True
        logger.error("Managed watch service stopping because this process no longer owns the service lock")
        self._begin_stop()
        return False

    async def _run_watch(self, watch_id: str) -> None:
        lifetime_started = asyncio.get_running_loop().time()
        self._watch_started_at[watch_id] = _utc_now_iso()
        self._runtime_state_dirty = True
        self._write_runtime_state()

        while self._running:
            if not self._owns_service_instance():
                return
            if watch_id in self._fused_watch_ids:
                return
            if not self._watch_store_call(watch_id, "reload", self.store.maybe_reload):
                return
            watch = self.store.get_watch(watch_id)
            if watch is None or not watch.enabled:
                return

            if watch.mode == "forever" and watch.lifetime_timeout_seconds > 0:
                elapsed = asyncio.get_running_loop().time() - lifetime_started
                remaining_lifetime = watch.lifetime_timeout_seconds - elapsed
                if remaining_lifetime <= 0:
                    self._enqueue_hook(
                        watch,
                        prefix=watch.message or watch.prefix or "Watch stopped after reaching its lifetime timeout.",
                        body=(
                            f"Watch '{watch.name or watch.id}' reached its lifetime timeout after "
                            f"{int(watch.lifetime_timeout_seconds)} second(s)."
                        ),
                    )
                    self._watch_store_call(
                        watch.id,
                        "mark_cycle_result",
                        lambda: self.store.mark_cycle_result(
                            watch.id,
                            exit_code=None,
                            error=None,
                            disable=True,
                        ),
                    )
                    return
                cycle_timeout = watch.timeout_seconds
                if cycle_timeout <= 0:
                    cycle_timeout = remaining_lifetime
                else:
                    cycle_timeout = min(cycle_timeout, remaining_lifetime)
            else:
                cycle_timeout = watch.timeout_seconds

            if not self._watch_store_call(watch.id, "mark_cycle_start", lambda: self.store.mark_cycle_start(watch.id)):
                return
            try:
                result = await self._run_cycle(watch, timeout_seconds=cycle_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = _CycleResult(
                    exit_code=1,
                    stdout="",
                    stderr=str(exc),
                    timed_out=False,
                )

            if not self._owns_service_instance():
                return

            if result.exit_code == 0:
                prompt = _build_prompt(watch.message or watch.prefix, result.stdout)
                if prompt:
                    self._enqueue_hook(watch, prompt=prompt)
                if not self._watch_store_call(
                    watch.id,
                    "mark_cycle_result",
                    lambda: self.store.mark_cycle_result(
                        watch.id,
                        exit_code=0,
                        error=None,
                        event_detected=True,
                        disable=watch.mode == "once",
                    ),
                ):
                    return
                if watch.mode != "forever":
                    return
                continue

            if result.timed_out or result.exit_code == 124:
                error_text = "timed out"
                if watch.mode == "forever" and 124 in set(watch.retry_exit_codes):
                    if not self._watch_store_call(
                        watch.id,
                        "mark_cycle_result",
                        lambda: self.store.mark_cycle_result(watch.id, exit_code=124, error=error_text, disable=False),
                    ):
                        return
                    await asyncio.sleep(watch.retry_delay_seconds)
                    continue
                self._enqueue_failure_hook(
                    watch,
                    exit_code=124,
                    error_text=f"Watch timed out after {int(cycle_timeout)} second(s).",
                )
                self._watch_store_call(
                    watch.id,
                    "mark_cycle_result",
                    lambda: self.store.mark_cycle_result(watch.id, exit_code=124, error=error_text, disable=True),
                )
                return

            error_text = _squash_error(result.stderr) or f"watch command exited with status {result.exit_code}"
            if watch.mode == "forever" and result.exit_code in set(watch.retry_exit_codes):
                if not self._watch_store_call(
                    watch.id,
                    "mark_cycle_result",
                    lambda: self.store.mark_cycle_result(
                        watch.id,
                        exit_code=result.exit_code,
                        error=error_text,
                        disable=False,
                    ),
                ):
                    return
                await asyncio.sleep(watch.retry_delay_seconds)
                continue

            self._enqueue_failure_hook(watch, exit_code=result.exit_code, error_text=error_text)
            self._watch_store_call(
                watch.id,
                "mark_cycle_result",
                lambda: self.store.mark_cycle_result(
                    watch.id,
                    exit_code=result.exit_code,
                    error=error_text,
                    disable=True,
                ),
            )
            return

    async def _run_cycle(self, watch: ManagedWatch, *, timeout_seconds: float) -> _CycleResult:
        if watch.shell_command:
            process = await asyncio.create_subprocess_shell(
                watch.shell_command,
                cwd=watch.cwd or None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **isolated_subprocess_kwargs(),
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *watch.command,
                cwd=watch.cwd or None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **isolated_subprocess_kwargs(),
            )
        self._active_pids[watch.id] = process.pid
        self._runtime_state_dirty = True
        self._write_runtime_state()
        try:
            if timeout_seconds > 0:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            else:
                stdout, stderr = await process.communicate()
            timed_out = False
        except asyncio.CancelledError:
            await terminate_and_communicate(process, logger, f"watch {watch.id}")
            raise
        except asyncio.TimeoutError:
            stdout, stderr = await terminate_and_communicate(process, logger, f"watch {watch.id}")
            return _CycleResult(exit_code=124, stdout="", stderr=stderr.decode("utf-8", errors="replace"), timed_out=True)
        finally:
            self._active_pids.pop(watch.id, None)
            self._runtime_state_dirty = True
            self._write_runtime_state()

        return _CycleResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace").strip(),
            stderr=stderr.decode("utf-8", errors="replace").strip(),
            timed_out=timed_out,
        )

    def _enqueue_hook(
        self,
        watch: ManagedWatch,
        *,
        prompt: Optional[str] = None,
        prefix: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        final_prompt = prompt or _build_prompt(prefix, body)
        if not final_prompt:
            return
        self.request_store.enqueue_hook_send(
            session_key=watch.session_key,
            session_id=watch.session_id,
            post_to=watch.post_to,
            deliver_key=watch.deliver_key,
            prompt=final_prompt,
            agent_name=watch.agent_name,
            session_policy=watch.session_policy,
            run_type="watch",
            definition_id=watch.id,
            source_kind="watch",
            metadata=watch.metadata,
        )

    def _enqueue_failure_hook(self, watch: ManagedWatch, *, exit_code: int, error_text: str) -> None:
        watch_label = watch.name or watch.id
        if exit_code == 124:
            body = (
                f"Watch '{watch_label}' stopped because the waiter timed out.\n"
                f"Check whether the timeout is too short or the waiter is blocked, then recreate the watch if monitoring should continue.\n"
                f"Details: {error_text}"
            )
        else:
            body = (
                f"Watch '{watch_label}' stopped because the waiter exited with code {exit_code}.\n"
                f"Review the error below, fix the waiter or its dependencies, then recreate the watch if monitoring should continue.\n"
                f"Error: {error_text}"
            )
        self._enqueue_hook(watch, prefix=watch.message or watch.prefix, body=body)


def _build_prompt(prefix: Optional[str], body: Optional[str]) -> str:
    parts = []
    if prefix:
        parts.append(prefix.strip())
    if body:
        body_text = body.strip()
        if body_text:
            parts.append(body_text)
    return "\n\n".join(parts).strip()


def _squash_error(text: str, *, limit: int = 240) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
