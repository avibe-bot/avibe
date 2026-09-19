"""Shared backend restart barrier and bounded drain coordinator."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 0.1
_NATIVE_BACKENDS = frozenset({"claude", "codex", "opencode"})


class NativeMigrationBlockedError(RuntimeError):
    """Credential-free refusal before native credential ownership can change."""

    def __init__(self, reason: str, backends: tuple[str, ...], *, pids: tuple[int, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.backends = backends
        self.pids = pids


def pending_native_backends(directory: Path) -> set[str]:
    """Read only the pending transaction envelope; corrupt state blocks all."""
    try:
        data = json.loads((directory / "current.json").read_text())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return set(_NATIVE_BACKENDS)
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data.get("version") != 1
        or not isinstance(data.get("phase"), str)
        or data.get("phase") not in {
        "prepared", "withdrawn", "exposed", "reverting",
        }
    ):
        return set(_NATIVE_BACKENDS)
    blocked = data.get("backends")
    if not isinstance(blocked, list) or not blocked or any(
        not isinstance(name, str) or name not in _NATIVE_BACKENDS for name in blocked
    ):
        return set(_NATIVE_BACKENDS)
    return set(blocked)


class NativeCredentialLease:
    """Non-reentrant, cross-process ownership of backend-native credentials.

    Every acquire opens a separate file description (not a thread-reentrant
    MigrationFileLock). Never unlink these files: waiters must share one inode.
    An explicit lease may be passed to nested owned operations across threads.
    """

    def __init__(self, backends: tuple[str, ...], *, state_dir: Path | None = None) -> None:
        from config.paths import get_state_dir

        if not backends or any(name not in _NATIVE_BACKENDS for name in backends):
            raise ValueError("Unsupported native credential backend")
        self.backends = tuple(sorted(set(backends)))
        self.directory = (state_dir if state_dir is not None else get_state_dir()) / "native-takeover"
        self._handles: list[Any] = []

    def acquire(self, *, recovery: bool = False) -> NativeCredentialLease:
        from storage.lock import _try_lock

        if self._handles:
            raise RuntimeError("Native credential lease is not reentrant")
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory_stat = self.directory.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode) or (
                hasattr(os, "getuid") and directory_stat.st_uid != os.getuid()
            ):
                raise OSError("Unsafe native lease directory")
            for backend in self.backends:
                fd = os.open(
                    self.directory / f"{backend}.lock",
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                handle = os.fdopen(fd, "a+b")
                self._handles.append(handle)
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                    or info.st_mode & 0o077
                ):
                    raise OSError("Unsafe native lease file")
                if not _try_lock(handle):
                    raise NativeMigrationBlockedError("native_auth_in_progress", (backend,))
            if not recovery:
                if pending_native_backends(self.directory).intersection(self.backends):
                    raise NativeMigrationBlockedError("migration_recovery_pending", self.backends)
            return self
        except BaseException:
            self.release()
            raise

    def assert_owned(self, backend: str) -> None:
        if backend not in self.backends or len(self._handles) != len(self.backends):
            raise RuntimeError("Native credential operation has no ownership")

    def assert_auth_custody(self, backend: str, *, source_id: str | None = None) -> None:
        """Authorize a native writer from durable routing, while holding its lease.

        Runtime enablement is not credential ownership. A Hub backend may
        maintain only the explicitly bound, retained native subscription;
        generic Settings/IM login and API-key writes must use Hub instead.
        Read without load-time migration writes: this is an admission check.
        """
        from config.v2_config import V2Config

        self.assert_owned(backend)
        try:
            config = V2Config.load(persist_migrations=False)
        except FileNotFoundError:
            config = V2Config.default()
        except (OSError, TypeError, ValueError):
            raise NativeMigrationBlockedError("config_recovery", (backend,)) from None
        if config.load_warnings:
            raise NativeMigrationBlockedError("config_recovery", (backend,))
        hub = config.model_hub
        if hub.agents[backend].mode == "direct":
            return
        vendor = {"claude": "anthropic", "codex": "openai"}.get(backend)
        if source_id is not None and vendor is not None and any(
            source.id == source_id
            and source.vendor == vendor
            and source.kind == "subscription"
            and source.supply_channel == "native_cli"
            for source in hub.sources
        ):
            return
        raise NativeMigrationBlockedError("native_auth_hub_owned", (backend,))

    def release(self) -> None:
        # Closing the descriptor releases the OS lock, including on Windows.
        while self._handles:
            self._handles.pop().close()

    def __enter__(self) -> NativeCredentialLease:
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


async def finish_native_operation(awaitable: Awaitable[Any]) -> Any:
    """Keep ownership until a started operation settles, even on cancellation.

    In particular, cancelling asyncio.to_thread does not stop its writer.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        except BaseException:
            # shield retrieved the worker exception; do not leave a writer.
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _native_process_backend(command: list[str], binaries: Mapping[str, str]) -> str | None:
    """Match an executable or its interpreter's script, never prompt arguments."""
    if not command:
        return None
    executable = command[0]
    name = Path(executable).name.lower()
    if name in {"node", "nodejs", "node.exe", "bun", "bun.exe"}:
        # Interpreter options are not scripts. Do not scan past a script into
        # its prompt/config arguments looking for another executable.
        script = next((arg for arg in command[1:] if not arg.startswith("-")), None)
        if script is None:
            return None
        executable = script
        name = Path(script).name.lower()
    for backend, binary in binaries.items():
        if executable == binary or name in {backend, f"{backend}.exe"}:
            return backend
        normalized = executable.replace("\\", "/").lower()
        if backend == "claude" and normalized.endswith("/@anthropic-ai/claude-code/cli.js"):
            return backend
        if backend == "codex" and normalized.endswith("/@openai/codex/bin/codex.js"):
            return backend
        if backend == "opencode" and normalized.endswith("/opencode-ai/bin/opencode"):
            return backend
    return None


def native_cli_processes(binaries: Mapping[str, str]) -> tuple[int, ...]:
    """Read same-user process identities without reading environments or killing.

    After managed retirement any matching process is a blocker, including login,
    interactive CLIs and untracked descendants. An incomplete inventory is not
    evidence that the native credential has no remaining reader.
    """
    import psutil

    backends = tuple(sorted(binaries))
    uid = os.getuid() if hasattr(os, "getuid") else None
    owner_field = "uids" if uid is not None else "username"
    try:
        username = psutil.Process().username() if uid is None else None
        processes = psutil.process_iter(["pid", owner_field, "name", "cmdline", "status"])
        blockers: list[int] = []
        for process in processes:
            try:
                info = process.info
                if info.get("status") in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                    continue
                owner = info.get(owner_field)
                owner = getattr(owner, "real", None) if uid is not None else owner
                if owner is not None and owner != (uid if uid is not None else username):
                    continue
                command = info.get("cmdline")
                if not command:
                    # A known other executable with inaccessible owner metadata
                    # is not a CLI match; same-user unreadable command lines are
                    # conservatively refused rather than silently skipped.
                    name = str(info.get("name") or "")
                    if owner is None and _native_process_backend([name], binaries) is None:
                        continue
                    raise NativeMigrationBlockedError("process_inventory_unavailable", backends)
                if _native_process_backend(command, binaries) is not None:
                    if owner is None:
                        raise NativeMigrationBlockedError("process_inventory_unavailable", backends)
                    blockers.append(int(info["pid"]))
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, KeyError, TypeError, ValueError):
                raise NativeMigrationBlockedError("process_inventory_unavailable", backends) from None
        return tuple(sorted(set(blockers)))
    except psutil.NoSuchProcess:
        raise NativeMigrationBlockedError("process_inventory_unavailable", backends) from None
    except (psutil.Error, OSError, ValueError):
        raise NativeMigrationBlockedError("process_inventory_unavailable", backends) from None


def _configured_drain_timeout() -> float:
    raw = os.environ.get("AVIBE_BACKEND_RESTART_DRAIN_TIMEOUT_SECONDS", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else DEFAULT_DRAIN_TIMEOUT_SECONDS
    except ValueError:
        logger.warning("Ignoring invalid AVIBE_BACKEND_RESTART_DRAIN_TIMEOUT_SECONDS=%r", raw)
        return DEFAULT_DRAIN_TIMEOUT_SECONDS


class BackendRestartCoordinator:
    """Serialize backend cutovers without stopping Avibe's service process."""

    def __init__(
        self,
        controller: Any,
        refresh: Callable[[str, bool], Awaitable[None]],
        *,
        drain_timeout: float | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        process_inventory: Callable[[Mapping[str, str]], tuple[int, ...]] = native_cli_processes,
    ) -> None:
        self.controller = controller
        self._refresh = refresh
        self._drain_timeout = _configured_drain_timeout() if drain_timeout is None else max(0.0, drain_timeout)
        self._poll_interval = max(0.001, poll_interval)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._request_locks: dict[str, asyncio.Lock] = {}
        self._outcomes: dict[str, dict[str, str]] = {}
        self._migration_backends: set[str] = set()
        self._process_inventory = process_inventory

    def _blocked_backends(self) -> set[str]:
        service = getattr(self.controller, "model_hub_service", None)
        return set(getattr(service, "migration_blocked_backends", set())) | pending_native_backends(
            self._native_state_dir() / "native-takeover"
        )

    def _native_state_dir(self) -> Path:
        from config.paths import get_state_dir

        service = getattr(self.controller, "model_hub_service", None)
        event_path = getattr(getattr(service, "events", None), "path", None)
        return Path(event_path).parent if isinstance(event_path, (str, Path)) else get_state_dir()

    def restore_migration_blocks(self) -> None:
        """Apply recovered durable exclusions before any producer is admitted."""
        for backend in self._blocked_backends():
            self.controller.agent_service.begin_backend_drain(backend)
            self.controller.session_turns.begin_backend_drain(backend)

    @staticmethod
    def _migration_targets(backends: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(backends, tuple) or any(backend not in _NATIVE_BACKENDS for backend in backends):
            raise ValueError("Unsupported native migration backend")
        return tuple(sorted(set(backends)))

    def assert_native_auth_available(self, backend: str) -> None:
        if backend in self._migration_backends or backend in self._blocked_backends():
            raise NativeMigrationBlockedError("migration_in_progress", (backend,))

    def _assert_no_native_login(self, targets: tuple[str, ...]) -> None:
        auth_service = getattr(self.controller, "agent_auth_service", None)
        flows = getattr(auth_service, "_flows_by_id", {})
        for backend in targets:
            if any(
                getattr(flow, "backend", None) == backend
                and getattr(flow, "state", "starting") not in {"success", "failed", "cancelled"}
                for flow in flows.values()
            ):
                raise NativeMigrationBlockedError("native_auth_in_progress", (backend,))

    def _native_binaries(self, targets: tuple[str, ...]) -> dict[str, str]:
        result: dict[str, str] = {}
        config = getattr(self.controller, "config", None)
        for backend in targets:
            backend_config = getattr(config, backend, None)
            result[backend] = str(
                getattr(backend_config, "binary", None)
                or getattr(backend_config, "cli_path", None)
                or backend
            )
        return result

    @asynccontextmanager
    async def migration_guard(
        self, backends: tuple[str, ...]
    ) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        """Yield an idle recheck under the same lease and closed admissions.

        Call the recheck immediately before native withdrawal and CPA activation:
        external CLIs do not participate in Avibe's advisory ownership protocol.
        No external process is ever terminated by the check.
        """
        targets = self._migration_targets(backends)
        closed: list[str] = []
        async with AsyncExitStack() as locks:
            for backend in targets:
                await locks.enter_async_context(self._request_locks.setdefault(backend, asyncio.Lock()))
                restart = self._tasks.get(backend)
                if restart is not None and not restart.done():
                    raise NativeMigrationBlockedError("backend_restart_in_progress", (backend,))
            self._assert_no_native_login(targets)
            lease = NativeCredentialLease(targets, state_dir=self._native_state_dir()).acquire(recovery=True)
            locks.callback(lease.release)
            self._migration_backends.update(targets)
            try:
                for backend in targets:
                    self.controller.agent_service.begin_backend_drain(backend)
                    self.controller.session_turns.begin_backend_drain(backend)
                    closed.append(backend)
                for backend in targets:
                    await self.controller.agent_service.prepare_backend_restart(backend)
                deadline = asyncio.get_running_loop().time() + self._drain_timeout
                for backend in targets:
                    while await self._has_active_turns(backend):
                        if asyncio.get_running_loop().time() >= deadline:
                            raise NativeMigrationBlockedError("native_runtime_busy", (backend,))
                        await asyncio.sleep(self._poll_interval)
                self._assert_no_native_login(targets)
                for backend in targets:
                    agent = self.controller.agent_service.agents.get(backend)
                    if agent is None:
                        # A disabled backend has no controller-owned runtime.
                        # The mandatory process inventory still checks its CLI.
                        continue
                    retire = getattr(agent, "retire_for_native_migration", None)
                    if not callable(retire):
                        raise NativeMigrationBlockedError("native_retirement_unavailable", (backend,))
                    try:
                        await finish_native_operation(retire())
                    except Exception:
                        raise NativeMigrationBlockedError("native_retirement_failed", (backend,)) from None
                async def verify_idle() -> None:
                    for backend in targets:
                        lease.assert_owned(backend)
                        if await self._has_active_turns(backend):
                            raise NativeMigrationBlockedError("native_runtime_busy", (backend,))
                    self._assert_no_native_login(targets)
                    pids = await asyncio.to_thread(self._process_inventory, self._native_binaries(targets))
                    if pids:
                        raise NativeMigrationBlockedError("external_native_processes", targets, pids=pids)

                await verify_idle()
                yield verify_idle
            finally:
                self._migration_backends.difference_update(targets)
                for backend in closed:
                    if backend not in self._blocked_backends():
                        self.controller.agent_service.end_backend_drain(backend)
                        await self.controller.session_turns.end_backend_drain(backend)

    async def request_restart(self, backend: str) -> str:
        """Begin or join a restart and return without waiting for a long drain."""
        self.assert_native_auth_available(backend)
        lock = self._request_locks.setdefault(backend, asyncio.Lock())
        async with lock:
            self.assert_native_auth_available(backend)
            existing = self._tasks.get(backend)
            if existing is not None:
                if not existing.done():
                    return "draining"
                self._on_done(backend, existing)

            agent_service = self.controller.agent_service
            session_turns = self.controller.session_turns
            agent_service.begin_backend_drain(backend)
            session_turns.begin_backend_drain(backend)
            try:
                await agent_service.prepare_backend_restart(backend)
                had_active_work = await self._has_active_turns(backend)
            except BaseException as exc:
                self._outcomes[backend] = {"state": "failed", "error": str(exc) or type(exc).__name__}
                if backend not in self._blocked_backends():
                    agent_service.end_backend_drain(backend)
                    await session_turns.end_backend_drain(backend, resume_deferred=False)
                raise
            task = asyncio.create_task(self._run(backend), name=f"backend-restart:{backend}")
            self._tasks[backend] = task
            task.add_done_callback(lambda completed, name=backend: self._on_done(name, completed))

        # Idle refreshes remain synchronous so setup/config errors reach the
        # runtime-command requester. Only genuinely active work makes the
        # restart an acknowledged background drain.
        if not had_active_work:
            await task
            return "restarted"
        return "draining"

    def _on_done(self, backend: str, task: asyncio.Task[None]) -> None:
        current = self._tasks.get(backend) is task
        if current:
            self._tasks.pop(backend, None)
        try:
            task.result()
            if current:
                self._outcomes[backend] = {"state": "applied"}
        except asyncio.CancelledError:
            if current:
                self._outcomes[backend] = {"state": "failed", "error": "cancelled"}
            logger.info("Backend restart cancelled for %s", backend)
        except Exception as exc:
            if current:
                self._outcomes[backend] = {"state": "failed", "error": str(exc) or type(exc).__name__}
            logger.exception("Backend restart failed for %s", backend)

    def snapshot(self, backend: str) -> dict[str, str | bool]:
        """Read application without starting another cutover or credential probe."""
        if backend in self._blocked_backends() or backend in self._migration_backends:
            return {"state": "draining"}
        lock = self._request_locks.get(backend)
        if lock is not None and lock.locked():
            return {"state": "draining"}
        task = self._tasks.get(backend)
        if task is not None:
            if not task.done():
                return {"state": "draining"}
            if task.cancelled():
                return {"state": "failed", "error": "cancelled"}
            error = task.exception()
            if error is not None:
                return {"state": "failed", "error": str(error) or type(error).__name__}
        outcome = self._outcomes.get(backend)
        if task is None and outcome and outcome["state"] == "failed":
            return dict(outcome)
        from config.v2_compat import AppCompatConfig

        config = getattr(self.controller, "config", None)
        registered = backend in self.controller.agent_service.agents
        # Optional compat sections are None only for disabled backends. Claude
        # stays registered with an explicit flag. Use loaded state, never disk
        # or a stale successful outcome to excuse unexpected missing agents.
        if isinstance(config, AppCompatConfig):
            disabled = (backend in {"codex", "opencode"} and getattr(config, backend) is None and not registered) or (
                backend == "claude" and registered and config.claude.enabled is False
            )
            if disabled:
                return {"state": "applied", "disabled": True}
        # Startup-installed agents need no recent restart receipt.
        if not registered:
            return {"state": "unavailable"}
        return {"state": "applied"}

    async def _has_active_turns(self, backend: str) -> bool:
        service = self.controller.agent_service
        if service.runtime_turn_tokens_for_backend(backend):
            return True
        probe = getattr(service, "backend_runtime_active", None)
        if not callable(probe):
            return False
        result = probe(backend)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _run(self, backend: str) -> None:
        forced = False
        refreshed = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._drain_timeout
        try:
            while await self._has_active_turns(backend):
                if loop.time() >= deadline:
                    forced = True
                    session_ids = self.controller.session_turns.active_runtime_session_ids_for_backend(backend)
                    await self.controller.session_turns.release_for_backend_refresh(
                        backend=backend,
                        base_session_ids=session_ids,
                    )
                    await self.controller.agent_service.force_cancel_backend_turns(backend)
                    self.controller.agent_service.force_end_backend_activities(backend)
                    break
                await asyncio.sleep(self._poll_interval)
            await self._refresh(backend, forced)
            refreshed = True
        finally:
            # Runtime admission opens before durable queues are flushed. A flush
            # therefore always enters the refreshed generation.
            if backend not in self._blocked_backends():
                self.controller.agent_service.end_backend_drain(backend)
                await self.controller.session_turns.end_backend_drain(backend, resume_deferred=refreshed)

    async def wait(self, backend: str) -> None:
        """Testing/diagnostic hook: wait for the current restart, if any."""
        task = self._tasks.get(backend)
        if task is not None:
            await asyncio.shield(task)
