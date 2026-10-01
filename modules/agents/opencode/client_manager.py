"""The OpenCode instance as a runtime unit: its generations and launch specs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Optional

from config import paths
from config.atomic_io import write_atomic
from modules.agents.runtime_generations import (
    RuntimeBinding,
    RuntimeGeneration,
    RuntimeGenerationSet,
)
from vibe.opencode_config import (
    get_opencode_auth_path,
    get_opencode_config_paths,
    managed_opencode_runtime_config_content,
    parse_jsonc_object,
)

from .caller_context import PLUGIN_SOURCE, server_environment
from .server import (
    _MANAGED_RUNTIME_POLICY_REVISION,
    OpenCodeGeneration,
    OpenCodeGenerationStartError,
    OpenCodeLaunchSpec,
    OpenCodeServerClient,
    StopOutcome,
    adopt_recorded_generations,
    apply_resource_governance,
    own_generation,
    start_generation,
    stop_generation,
)

logger = logging.getLogger(__name__)

_LAUNCH_SPEC_SCHEMA = 1
# OpenCode rewrites these fields of an OAuth entry whenever it refreshes the
# token, and every live process reads them from auth.json per request.
_VOLATILE_OAUTH_FIELDS = frozenset({"access", "refresh", "expires"})
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*")
_binary_versions: dict[tuple[Any, ...], Optional[str]] = {}
# A lease outlives no caller: the UI process renews nothing and releases on exit.
MAX_LEASE_SECONDS = 1800.0
# The runtime holding each live lease. A release reaches it there even after
# its backend was disabled and its agent unregistered while the lease drains.
_LEASE_HOLDERS: dict[str, "OpenCodeRuntime"] = {}
# The controller sweeps only registered agents, so a disabled backend's runtime
# sweeps itself at this interval until its last generation stopped.
CLOSED_RUNTIME_SWEEP_SECONDS = 60.0
_CLOSED_SWEEPS: set[asyncio.Task[None]] = set()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _probe_binary_version(binary: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(f"{result.stdout or ''} {result.stderr or ''}")
    return match.group(0) if match else None


def _binary_identity(configured: str) -> tuple[str, dict[str, Any]]:
    """The executable a launch runs, and what identifies its exact build.

    The version is probed once per file identity, so an in-place upgrade is
    seen without a probe on every turn.
    """

    resolved = configured if os.path.isabs(configured) else shutil.which(configured)
    if not resolved:
        return configured, {"path": configured, "missing": True}
    real = os.path.realpath(resolved)
    try:
        stat = os.stat(real)
    except OSError:
        return resolved, {"path": real, "missing": True}
    key = (real, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if key not in _binary_versions:
        _binary_versions[key] = _probe_binary_version(resolved)
    return resolved, {"path": real, "stat": list(key[1:]), "version": _binary_versions[key]}


def _global_config_files() -> list[Path]:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_dir = (Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config") / "opencode"
    candidates = [
        *get_opencode_config_paths(),
        *(config_dir / name for name in ("config.json", "opencode.json", "opencode.jsonc")),
    ]
    return list(dict.fromkeys(candidates))


def _file_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _user_config_digest(path: Path) -> Optional[str]:
    raw = _file_bytes(path)
    if raw is None:
        return None
    try:
        payload = parse_jsonc_object(raw.decode("utf-8"))
    except Exception:
        return hashlib.sha256(raw).hexdigest()
    # OpenCode may add ``$schema`` when it loads a file; that changes nothing it runs.
    payload.pop("$schema", None)
    return _digest(payload)


def _credential_digest(path: Path) -> Optional[str]:
    raw = _file_bytes(path)
    if raw is None:
        return None
    try:
        entries = json.loads(raw)
    except ValueError:
        return hashlib.sha256(raw).hexdigest()
    if not isinstance(entries, dict):
        return hashlib.sha256(raw).hexdigest()
    normalized = {
        provider_id: (
            {key: value for key, value in entry.items() if key not in _VOLATILE_OAUTH_FIELDS}
            if isinstance(entry, dict) and entry.get("type") == "oauth"
            else entry
        )
        for provider_id, entry in entries.items()
    }
    return _digest(normalized)


def compute_launch_spec(binary: str, overlay: Any | None, renew_epoch: int) -> OpenCodeLaunchSpec:
    """The launch spec a turn needs, from its Model Hub overlay and the files OpenCode loads."""

    executable, binary_identity = _binary_identity(binary)
    overlay_hash: Optional[str] = None
    provider_ids: tuple[str, ...] = ()
    file_content: Optional[bytes] = None
    inline_content: Optional[str] = None
    if overlay is not None:
        provider_ids = getattr(overlay, "provider_ids", ())
        if (
            not isinstance(provider_ids, tuple)
            or not provider_ids
            or any(not isinstance(provider_id, str) or not provider_id for provider_id in provider_ids)
            or len(set(provider_ids)) != len(provider_ids)
        ):
            raise RuntimeError("Model Hub OpenCode overlay providers are unavailable")
        content = getattr(overlay, "content", None)
        if not isinstance(content, (bytes, str)):
            raise RuntimeError("Model Hub OpenCode overlay content is unavailable")
        file_content = content.encode() if isinstance(content, str) else content
        inline_content = managed_opencode_runtime_config_content(content)
        overlay_hash = str(overlay.content_hash)
        if hashlib.sha256(inline_content.encode()).hexdigest() != overlay_hash:
            raise RuntimeError("Model Hub OpenCode overlay content hash changed")
    digest = _digest(
        {
            "schema": _LAUNCH_SPEC_SCHEMA,
            "binary": binary_identity,
            "model_hub_overlay": overlay_hash,
            "model_hub_overlay_provider_ids": list(provider_ids),
            "policy": _MANAGED_RUNTIME_POLICY_REVISION,
            "plugin": hashlib.sha256(PLUGIN_SOURCE.encode()).hexdigest(),
            "caller_context_path": server_environment()["AVIBE_OPENCODE_CALLER_CONTEXT_PATH"],
            "user_config": {str(path): _user_config_digest(path) for path in _global_config_files()},
            "credentials": _credential_digest(get_opencode_auth_path()),
            "renew_epoch": renew_epoch,
        }
    )
    return OpenCodeLaunchSpec(
        digest=digest,
        binary=executable,
        binary_version=binary_identity.get("version"),
        overlay_hash=overlay_hash,
        overlay_provider_ids=provider_ids,
        overlay_file_content=file_content,
        overlay_inline_content=inline_content,
    )


def _renew_epoch_path() -> Path:
    return paths.get_runtime_dir() / "opencode" / "renew_epoch"


def _read_renew_epoch() -> int:
    try:
        return int(_renew_epoch_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _stop_outcome(wrapper: RuntimeGeneration[Any, OpenCodeGeneration]) -> StopOutcome:
    if wrapper.stopped:
        return StopOutcome.STOPPED
    if wrapper.failed:
        return StopOutcome.FAILED
    return StopOutcome.DRAINING


@dataclass(frozen=True)
class _RecordedSpec:
    """The spec of an adopted process, known only by its recorded digest."""

    digest: str


class OpenCodeRuntime:
    """The generations of the OpenCode instance, Avibe's one OpenCode runtime unit."""

    def __init__(self, opencode_config: Any, *, resource_governor: Any | None = None) -> None:
        self.config = opencode_config
        self.resource_governor = resource_governor
        # Persisted, so a generation a renewal retired before a controller
        # crash never serves new turns again after the restart.
        self._renew_epoch = _read_renew_epoch()
        self._generations: RuntimeGenerationSet[Any, OpenCodeGeneration] = RuntimeGenerationSet(
            start=self._start,
            stop=self._stop,
        )
        self._wrappers: dict[str, RuntimeGeneration[Any, OpenCodeGeneration]] = {}
        self._adopted = False
        self._adopt_lock: Optional[asyncio.Lock] = None
        # Set once the backend is disabled: no generation will serve again.
        self._closed = False
        # Work outside a turn admitted but not yet bound; a native migration
        # counts it as active, so it never overlaps a process start.
        self.outside_turn_acquisitions = 0
        self._closed_sweep: Optional[asyncio.Task[None]] = None
        self._leases: dict[str, tuple[RuntimeBinding[Any, OpenCodeGeneration], Optional[asyncio.TimerHandle]]] = {}
        self._lease_tasks: set[asyncio.Task[None]] = set()
        # Pid of the last start whose process exited on its own: resource evidence.
        self.last_start_failure_pid: Optional[int] = None
        self.start_failures = 0
        # Wired by the agent.
        self.durable_poll_generations: Optional[Callable[[], Mapping[str, Optional[str]]]] = None
        self.on_generation_ready: Optional[Callable[[OpenCodeGeneration], None]] = None
        self.on_generation_stopping: Optional[Callable[[OpenCodeGeneration, bool], Awaitable[None]]] = None

    def renew(self) -> None:
        """New turns start a new generation; running work stays where it is.

        The epoch is persisted before it takes effect. A renewal that cannot be
        persisted fails rather than being undone by the next controller.
        """
        epoch = self._renew_epoch + 1
        path = _renew_epoch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, str(epoch))
        self._renew_epoch = epoch

    async def launch_spec(self, overlay: Any | None) -> OpenCodeLaunchSpec:
        return await asyncio.to_thread(compute_launch_spec, self.config.binary, overlay, self._renew_epoch)

    @property
    def adopted(self) -> bool:
        return self._adopted

    def current(self) -> Optional[OpenCodeGeneration]:
        current = self._generations.current
        return current.runtime if current is not None else None

    def generations(self) -> tuple[OpenCodeGeneration, ...]:
        """Every live generation, oldest first."""
        return tuple(generation.runtime for generation in self._generations.generations)

    def generation(self, generation_id: str) -> Optional[OpenCodeGeneration]:
        wrapper = self._wrappers.get(generation_id)
        return wrapper.runtime if wrapper is not None and not wrapper.stopped else None

    async def ensure_adopted(self, current_spec: Optional[OpenCodeLaunchSpec] = None) -> None:
        """Adopt the generations a previous controller left running, once.

        The newest one whose spec equals ``current_spec`` keeps serving new
        turns. Every other one retires: it stops at once when it runs nothing,
        or once its restored work drains.
        """

        if self._adopted:
            return
        if self._adopt_lock is None:
            self._adopt_lock = asyncio.Lock()
        async with self._adopt_lock:
            if self._adopted:
                return
            adopted = await adopt_recorded_generations(
                request_timeout_seconds=self.config.request_timeout_seconds,
            )
            durable = self._durable_polls()
            serving = next(
                (
                    generation
                    for generation in sorted(adopted, key=lambda item: item.started_at, reverse=True)
                    if current_spec is not None and generation.spec_digest == current_spec.digest
                ),
                None,
            )
            for generation in adopted:
                self._reconcile_run_markers(generation, durable)
                wrapper = await self._generations.adopt(
                    _RecordedSpec(generation.spec_digest),
                    generation,
                    current=generation is serving,
                )
                self._wrappers[generation.generation_id] = wrapper
                # Owned only once attached, so an adoption cut short leaves
                # the rest for its retry.
                own_generation(generation)
                apply_resource_governance(self.resource_governor, generation.pid)
                if self.on_generation_ready is not None:
                    self.on_generation_ready(generation)
                now = time.time()
                for lease_id, expires_at in list(generation.leases.items()):
                    if expires_at <= now:
                        generation.drop_lease(lease_id)
                        continue
                    binding = await self._generations.bind(wrapper)
                    # Never longer than any lease is granted, whatever the clock did.
                    self._hold_lease(lease_id, binding, min(expires_at - now, MAX_LEASE_SECONDS))
            self._adopted = True

    def _durable_polls(self) -> Optional[Mapping[str, Optional[str]]]:
        provider = self.durable_poll_generations
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            logger.warning("Could not read durable OpenCode polls to reconcile run markers", exc_info=True)
            return None

    @staticmethod
    def _reconcile_run_markers(
        generation: OpenCodeGeneration,
        durable: Optional[Mapping[str, Optional[str]]],
    ) -> None:
        """Drop adopted run markers that no durable poll can bring back."""

        if durable is None or not generation.active_run_sessions:
            return
        retained = {
            session_id
            for session_id in generation.active_run_sessions
            if session_id in durable and durable[session_id] in (None, generation.generation_id)
        }
        if retained == generation.active_run_sessions:
            return
        logger.info(
            "Removed %s orphaned OpenCode run marker(s) without durable polls",
            len(generation.active_run_sessions - retained),
        )
        generation.active_run_sessions = retained
        generation.write_record_or_defer("its reconciled run markers")

    async def acquire(self, spec: OpenCodeLaunchSpec) -> RuntimeBinding[Any, OpenCodeGeneration]:
        """Bind new work to the generation that serves ``spec``."""

        await self.ensure_adopted(spec)
        for _attempt in range(2):
            binding = await self._generations.acquire(spec)
            wrapper = binding.generation
            self._wrappers[wrapper.runtime.generation_id] = wrapper
            if wrapper.runtime.process_alive():
                return binding
            # The process ended without Avibe stopping it. Never serve a turn
            # on it; the next acquire starts a fresh generation.
            logger.warning(
                "OpenCode generation %s pid=%s exited; starting another",
                wrapper.runtime.generation_id,
                wrapper.runtime.pid,
            )
            await binding.release()
            await self.forget(wrapper.runtime)
        raise OpenCodeGenerationStartError("OpenCode server exited right after it started")

    async def bind(self, generation: OpenCodeGeneration) -> RuntimeBinding[Any, OpenCodeGeneration]:
        """Bind recovered work, such as a restored poll, to the generation that runs it."""
        wrapper = self._wrappers.get(generation.generation_id)
        if wrapper is None:
            raise RuntimeError("OpenCode generation is no longer tracked")
        return await self._generations.bind(wrapper)

    async def forget(self, generation: OpenCodeGeneration) -> None:
        """Drop a generation whose process already ended."""

        wrapper = self._wrappers.pop(generation.generation_id, None)
        if wrapper is None:
            return
        await self._generations.discard(wrapper)
        if self.on_generation_stopping is not None:
            await self.on_generation_stopping(generation, False)
        try:
            await stop_generation(generation)
        except Exception:
            logger.warning("Could not clean up OpenCode generation %s", generation.generation_id, exc_info=True)

    async def lease(self, spec: OpenCodeLaunchSpec, ttl_seconds: float) -> tuple[str, OpenCodeGeneration]:
        """Pin the generation that serves ``spec`` for a caller outside this process."""

        ttl = min(max(float(ttl_seconds), 1.0), MAX_LEASE_SECONDS)
        binding = await self.acquire(spec)
        generation = binding.generation.runtime
        lease_id = f"ocl_{secrets.token_hex(12)}"
        try:
            # Persisted, so a controller restart keeps the process for its caller.
            generation.set_lease(lease_id, time.time() + ttl)
        except BaseException:
            await binding.release()
            raise
        self._hold_lease(lease_id, binding, ttl)
        return lease_id, generation

    def _hold_lease(
        self,
        lease_id: str,
        binding: RuntimeBinding[Any, OpenCodeGeneration],
        ttl: float,
    ) -> None:
        timer = asyncio.get_running_loop().call_later(ttl, self._expire_lease, lease_id)
        self._leases[lease_id] = (binding, timer)
        _LEASE_HOLDERS[lease_id] = self

    def _expire_lease(self, lease_id: str) -> None:
        task = asyncio.get_running_loop().create_task(self.release_lease(lease_id))
        self._lease_tasks.add(task)
        task.add_done_callback(self._lease_tasks.discard)

    async def release_lease(self, lease_id: str) -> bool:
        held = self._leases.pop(lease_id, None)
        if held is None:
            return False
        _LEASE_HOLDERS.pop(lease_id, None)
        binding, timer = held
        if timer is not None:
            timer.cancel()
        binding.generation.runtime.drop_lease(lease_id)
        await binding.release()
        return True

    async def reap(self) -> None:
        for generation in self.generations():
            generation.flush_record()
        await self._generations.reap()

    async def retire_confirmed(
        self,
        generations: Optional[Iterable[OpenCodeGeneration]] = None,
    ) -> dict[str, StopOutcome]:
        """Retire generations, wait for their stops, and report each outcome.

        This is the controller's one confirmed-stop primitive: a stop counts as
        done only after the reconciler has run it. ``generations`` defaults to
        the current generation.
        """

        if generations is None:
            current = self._generations.current
            wrappers = [current] if current is not None else []
        else:
            wrappers = [
                wrapper
                for generation in generations
                if (wrapper := self._wrappers.get(generation.generation_id)) is not None
            ]
        for wrapper in wrappers:
            await self._generations.retire(wrapper)
        # Waits for the reconciler, and retries graceful stops that declined earlier.
        await self._generations.reap()
        return {wrapper.runtime.generation_id: _stop_outcome(wrapper) for wrapper in wrappers}

    async def close(self) -> None:
        """Admit nothing more; idle generations stop now, bound ones once drained.

        A previous controller's generations are adopted first, so disabling
        the backend stops them too. The runtime then sweeps itself until its
        last generation stopped.
        """
        self._closed = True
        try:
            await self.ensure_adopted(None)
        except Exception:
            # Admission closes regardless; whatever stays unadopted keeps its
            # record for shutdown and ``vibe stop``.
            logger.warning("Could not adopt OpenCode generations before disabling the backend", exc_info=True)
        await self._generations.stop_all(force=False)
        if self._generations.generations and self._closed_sweep is None:
            self._closed_sweep = asyncio.get_running_loop().create_task(self._sweep_until_stopped())
            _CLOSED_SWEEPS.add(self._closed_sweep)
            self._closed_sweep.add_done_callback(_CLOSED_SWEEPS.discard)

    async def _sweep_until_stopped(self) -> None:
        while self._generations.generations:
            await asyncio.sleep(CLOSED_RUNTIME_SWEEP_SECONDS)
            try:
                await self.reap()
            except Exception:
                # The next pass retries; a disabled runtime has no other sweeper.
                logger.exception("Sweeping a disabled OpenCode runtime failed")

    async def retire_all(self) -> None:
        """Admit nothing more to any live generation; each stops once its work drains.

        New turns still start a fresh generation afterwards.
        """

        for generation in self._generations.generations:
            await self._generations.retire(generation)

    async def retire_all_strict(self) -> None:
        """Stop every generation now, failing if any work or process remains.

        A generation still bound, or whose process survives, stays tracked as
        closed, so it never serves again and a later sweep retries its stop.
        """

        outcomes = await self.retire_confirmed(self.generations())
        if any(outcome is not StopOutcome.STOPPED for outcome in outcomes.values()):
            raise RuntimeError("OpenCode server did not exit")

    async def _start(self, spec: Any) -> OpenCodeGeneration:
        try:
            generation = await start_generation(
                spec,
                request_timeout_seconds=self.config.request_timeout_seconds,
                resource_governor=self.resource_governor,
            )
        except OpenCodeGenerationStartError as exc:
            self.start_failures += 1
            self.last_start_failure_pid = exc.exited_pid
            raise
        self.last_start_failure_pid = None
        if self.on_generation_ready is not None:
            self.on_generation_ready(generation)
        return generation

    async def _stop(self, wrapper: RuntimeGeneration[Any, OpenCodeGeneration], force: bool) -> bool:
        """Stop one generation; decline a graceful stop while it still has work.

        Bindings cover turns, restored polls, and leases. Requests and native
        runs outside them, such as a run marker whose poll failed to clear it,
        still keep the process. Safe to retry: when the process survives, the
        set keeps the generation and a later sweep stops it again.
        """

        generation = wrapper.runtime
        if not force and generation.process_alive() and not generation.is_drained():
            if self._closed:
                # A disabled backend serves no native run its bindings do not
                # hold, and nothing sweeps its runtime, so a run marker, such
                # as one whose clear failed, must not keep the process.
                if generation.has_requests_in_flight():
                    return False
            else:
                # A run marker that no durable poll backs, left by an adoption
                # that could not read the durable polls, keeps the process only
                # until a later sweep's retry of that reconciliation succeeds.
                self._reconcile_run_markers(generation, self._durable_polls())
                if not generation.is_drained():
                    return False
        if self.on_generation_stopping is not None:
            await self.on_generation_stopping(generation, force)
        await stop_generation(generation)
        self._wrappers.pop(generation.generation_id, None)
        return True


class OpenCodeRuntimeUnavailableError(RuntimeError):
    """No controller-owned OpenCode generation can serve this caller."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass
class OpenCodeServerLease:
    """A controller-owned generation pinned for one caller until released."""

    server: OpenCodeServerClient
    lease_id: str
    _release: Callable[[], Awaitable[None]]
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            await self._release()
        except Exception:
            # The lease expires on its own; a failed release only shortens nothing.
            logger.warning("Could not release OpenCode lease %s", self.lease_id, exc_info=True)


async def lease_opencode_server(
    purpose: str,
    *,
    ttl_seconds: float,
    controller: Any = None,
) -> OpenCodeServerLease:
    """Pin the current OpenCode generation without ever launching one here.

    In the controller the agent leases directly. Any other process asks the
    controller over control IPC and talks to the leased generation over HTTP.
    """

    agent_service = getattr(controller, "agent_service", None)
    if agent_service is not None:
        agent = getattr(agent_service, "agents", {}).get("opencode")
        lease_generation = getattr(agent, "lease_generation", None)
        if not callable(lease_generation):
            raise OpenCodeRuntimeUnavailableError("opencode_disabled")
        payload = await lease_generation(purpose, ttl_seconds=ttl_seconds)
        generation = payload["server"]
        lease_id = payload["lease_id"]

        async def _release_local() -> None:
            await agent.release_generation_lease(lease_id)

        return OpenCodeServerLease(server=generation, lease_id=lease_id, _release=_release_local)

    from vibe import internal_client

    try:
        result = await internal_client.create_opencode_generation_lease(purpose, ttl_seconds=ttl_seconds)
    except (internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout) as exc:
        raise OpenCodeRuntimeUnavailableError("controller_unavailable", str(exc)) from exc
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    if result.get("status_code") != 200 or not body.get("ok"):
        raise OpenCodeRuntimeUnavailableError(
            str(body.get("error") or "opencode_server_unavailable"),
            str(body.get("detail") or ""),
        )
    lease_id = str(body["lease_id"])
    server = OpenCodeServerClient(
        str(body["base_url"]),
        request_timeout_seconds=int(body.get("request_timeout_seconds") or 60),
        model_hub_provider_ids=tuple(
            item for item in body.get("model_hub_provider_ids") or () if isinstance(item, str) and item
        ),
    )

    async def _release_remote() -> None:
        try:
            await server.close_http_session()
        finally:
            await internal_client.release_opencode_generation_lease(lease_id)

    return OpenCodeServerLease(server=server, lease_id=lease_id, _release=_release_remote)


async def release_opencode_lease(lease_id: str, *, controller: Any) -> Optional[bool]:
    """Release a lease in the runtime holding it, wherever its agent went.

    A lease no runtime holds yet, recorded before a controller restart, goes
    to the registered agent, which adopts it first. ``None`` means OpenCode is
    disabled and holds no such lease.
    """

    holder = _LEASE_HOLDERS.get(lease_id)
    if holder is not None:
        return await holder.release_lease(lease_id)
    agent = getattr(getattr(controller, "agent_service", None), "agents", {}).get("opencode")
    release = getattr(agent, "release_generation_lease", None)
    if not callable(release):
        return None
    return bool(await release(lease_id))


@asynccontextmanager
async def leased_opencode_server(
    purpose: str,
    *,
    ttl_seconds: float = 60.0,
    controller: Any = None,
) -> AsyncIterator[OpenCodeServerClient]:
    """Run one caller's requests on a pinned controller-owned generation."""

    lease = await lease_opencode_server(purpose, ttl_seconds=ttl_seconds, controller=controller)
    try:
        yield lease.server
    finally:
        await lease.release()
