"""Codex agent — persistent app-server mode with JSON-RPC 2.0 transport."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import os
import shlex
import shutil
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Sequence

from config import paths
from config.v2_config import (
    DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS,
    DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER,
)
from core.backend_failure import emit_backend_failure
from core.agent_input import AgentInputMetadata
from core.caller_context import caller_env_for_platform_payload
from core.message_output import stop_output_for, terminal_output_for
from core.managed_skills import (
    managed_skill_claude_cli_path,
    managed_skill_environment,
    managed_skill_project_base,
)
from core.native_dispatch_phase import (
    backend_dispatch_attempted,
    mark_backend_dispatch_attempted,
    mark_prewrite_recovery_required,
)
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.run_settlement import SETTLED_BY_BACKEND_REFRESH, SETTLED_BY_STOPPED
from core.prompt_registry import prompt_text
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    SteerResult,
    result as steer_result,
)
from core.services.session_fork import fork_source_state, pending_native_fork
from core.system_prompt_injection import (
    build_forked_session_correction_prompt,
    build_system_prompt_injection,
    get_enabled_agents_for_prompt,
)
from core.resource_governance import (
    observe_agent_resource_pressure,
    governor_from_controller,
    pids_failure_labels,
)
from core.runtime_activation import RuntimeActivationIdentity
from core.runtime_ownership import (
    RuntimeResourceTarget,
    RuntimeSessionBinding,
    wake_runtime_ownership,
)
from modules.agents.base import AgentRequest, BaseAgent
from modules.im.base import FileAttachment
from modules.agents.subagent_router import SubagentDefinition, load_codex_subagent
from modules.agents.codex.event_handler import CodexEventHandler
from modules.agents.codex.session import CodexSessionManager
from modules.agents.codex.transport import CodexResponseTooLargeError, CodexRPCError, CodexTransport
from modules.agents.codex.turn_state import CodexTurnRegistry
from modules.agents.runtime_generations import (
    RuntimeBinding,
    RuntimeGeneration,
    RuntimeGenerationSet,
    RuntimeUnitStopping,
)
from vibe.codex_config import (
    LEGACY_MANAGED_PROVIDER_IDS,
    MANAGED_PROVIDER_ID,
    codex_credential_identity,
)
from vibe.desktop_backends import desktop_backend_subprocess_environment
from vibe.i18n import t as i18n_t
from vibe.message_identity import is_input_turn

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from modules.agents.model_hub import ModelHubLaunch
    from vibe.backend_model_catalog import CodexHubCatalog

_CODEX_MANAGED_PROVIDER_IDS = frozenset((MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS))
_CODEX_MODEL_HUB_PROVIDER_ID = "avibe_model_hub"
_CODEX_DEFAULT_PROVIDER_ID = "openai"
_CODEX_REBINDABLE_SAME_ID_PROVIDERS = _CODEX_MANAGED_PROVIDER_IDS | frozenset(
    (_CODEX_MODEL_HUB_PROVIDER_ID,)
)
CODEX_CALLER_ENV_DIR = "codex-caller-env"
CODEX_CONNECTION_PROBE_DIR = "codex-connection-probe"
CODEX_PROMPT_STRATEGY_METADATA_KEY = "codex_prompt_strategy"
_STEER_RECONCILIATION_TTL_SECONDS = 300.0
_MAX_STEER_RECONCILIATION_TARGETS = 128
# How long a Session waits for the app-server generation it leaves to release
# its thread. Codex bounds its own thread shutdown at 10 s.
_THREAD_RELEASE_TIMEOUT_SECONDS = 15.0
# Numbers agent instances: each owns the Model Hub scope of its own processes.
_AGENT_INSTANCE_SERIALS = itertools.count(1)
# Hub catalogs kept prepared for future launches; running generations hold
# their own pins.
_CACHED_HUB_CATALOGS = 2


@dataclass(frozen=True, eq=False)
class CodexLaunchSpec:
    """Every process-level input of one app-server.

    Two generations whose digests match were started from the same binary,
    arguments, environment, credentials, and directory, so either can serve a
    turn that needs this spec.
    """

    digest: str
    cwd: str
    binary: str
    args: tuple[str, ...]
    extra_args: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    hub: bool = False
    catalog: CodexHubCatalog | None = field(default=None, repr=False)

    def close(self) -> None:
        """Release this spec's catalog pin; a started transport holds its own."""
        if self.catalog is not None:
            self.catalog.close()


@dataclass(frozen=True, eq=False)
class _LaunchInputs:
    """A turn's whole configuration, captured in one synchronous step at admission.

    Its Model Hub launch resolution, Hub catalog, launch spec digest, and
    process all derive from this load, so a save or renewal that lands while
    any of them is awaited changes nothing about the turn; the directory's
    next turn moves. Nothing on the turn path reads the mutable source again.
    """

    # The Model Hub snapshot the launch resolves against; None without a router.
    hub_config: Any = field(repr=False)
    binary: str
    extra_args: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    # Renewal epoch, binary and credential identity, and the cwd's inode.
    identity: Mapping[str, Any] = field(repr=False)


@dataclass(eq=False)
class _CodexRuntime:
    """One app-server process serving a working directory."""

    cwd: str
    serial: int
    transport: CodexTransport
    hub: bool = False
    activation: RuntimeActivationIdentity | None = None
    # base_session_id -> the Codex thread loaded in this process for it
    threads: dict[str, str] = field(default_factory=dict)
    # thread id -> set when this process reports ``thread/closed``
    released_threads: dict[str, asyncio.Event] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.monotonic)
    # Set once the process is gone. The shared core detaches a generation
    # before its teardown runs, and a graceful teardown may still decline.
    ended: bool = False
    # Set while a teardown holds the activation fence; done when it finishes.
    teardown: asyncio.Future[None] | None = None
    # No unit holds this running process: it outlived its own failed start, or
    # its setup failed after the spawn. The sweep retries its stop.
    orphaned: bool = False


_CodexGeneration = RuntimeGeneration[CodexLaunchSpec, _CodexRuntime]


@dataclass(frozen=True)
class _CodexSteerReconciliationTarget:
    target: ActiveSteerTarget
    target_session_id: str
    logical_turn_id: str
    native_turn_id: str
    thread_id: str
    cwd: str
    recorded_at: float
    runtime: _CodexRuntime | None = None


class _CodexConnectionProbeState:
    def __init__(self, on_diagnostic: Callable[[str], None] | None = None) -> None:
        self.terminal: asyncio.Future[tuple[str, str]] = (
            asyncio.get_running_loop().create_future()
        )
        self.response_text = ""
        self.turn_id = ""
        self.on_diagnostic = on_diagnostic

    def record_diagnostic(self, detail: str) -> None:
        text = str(detail or "").strip()
        if not text or self.on_diagnostic is None:
            return
        try:
            self.on_diagnostic(text)
        except Exception:
            logger.debug("Codex probe diagnostic callback failed", exc_info=True)


class CodexConnectionProbeRuntimeMismatchError(RuntimeError):
    """The cached transport does not represent direct Codex credentials."""


class CodexThreadReleaseUnavailableError(RuntimeError):
    """The generation a Session leaves could not release the Session's thread."""

    reason = "codex_thread_release_unavailable"


class CodexModelHubCatalogUnavailableError(RuntimeError):
    """The configured Codex binary could not provide Hub launch metadata."""


class CodexPromptRefreshUnavailableError(RuntimeError):
    """The current app-server cannot safely refresh a persisted thread prompt."""


class CodexForkBoundaryUnavailableError(RuntimeError):
    """The source history cannot prove a safe inclusive fork boundary."""


class CodexResumeUnavailableError(RuntimeError):
    """The Codex thread associated with this session can no longer be resumed.

    Raised instead of silently starting a fresh thread, so the user is told their
    conversation context is gone rather than landing in an empty thread without
    knowing (product decision: no silent fallbacks)."""

    def __init__(self, thread_id: str, detail: str = "") -> None:
        self.thread_id = thread_id
        msg = (
            f"Could not resume the previous Codex conversation ({thread_id}); it may have expired. "
            "Not starting a new conversation to avoid silently losing context — start a new session to continue."
        )
        super().__init__(f"{msg} ({detail})" if detail else msg)


class CodexAgent(BaseAgent):
    """Codex CLI integration via persistent ``codex app-server`` subprocesses.

    Each working directory has a set of app-server generations. New turns use
    the current one; a generation started from older launch inputs keeps
    serving the turns already running on it and stops once they drain.
    Sessions in one directory share a generation, each with its own thread.
    """

    name = "codex"

    def __init__(
        self,
        controller: Any,
        codex_config: Any,
        *,
        registered_runtime: bool = True,
    ) -> None:
        super().__init__(controller)
        self.codex_config = codex_config
        self._registered_runtime = registered_runtime
        # (binary identity, configured models digest) -> prepared Hub catalog
        self._model_hub_catalogs: OrderedDict[tuple[str, str], CodexHubCatalog] = OrderedDict()
        self._model_hub_catalog_lock = asyncio.Lock()

        # cwd -> the app-server generations serving that working directory.
        # New turns use the current generation; a retiring one keeps serving
        # the turns already running on it and stops once they drain.
        self._units: Dict[str, RuntimeGenerationSet[CodexLaunchSpec, _CodexRuntime]] = {}
        # base_session_id -> the generation its Codex thread is loaded in
        self._session_generations: Dict[str, _CodexGeneration] = {}
        self._generation_serials = itertools.count(1)
        # cwd -> processes not yet ended, attached to their unit or not
        self._runtimes: Dict[str, set[_CodexRuntime]] = {}
        # Shutdown, which also serves disabling the backend, ends every
        # process without the runtime-update notice and admits nothing more.
        self._shutting_down = False
        self._instance_serial = next(_AGENT_INSTANCE_SERIALS)
        # Set by a disable's shutdown: every forced stop then settles the work
        # bound to its generation with this reason. None shows no notice.
        self._shutdown_settle_reason: str | None = None
        # Part of every launch spec: renewing moves each cwd to a new process
        # at its next turn.
        self._runtime_epoch = 0
        self._reap_tasks: set[asyncio.Task[None]] = set()
        self._session_last_activity: Dict[str, float] = {}

        self._session_mgr = CodexSessionManager()
        self._turn_registry = CodexTurnRegistry()
        self._event_handler = CodexEventHandler(self)

        # base_session_id → asyncio.Lock (serialize turn lifecycle per session)
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # A native steer can be accepted just before the Codex turn completes.
        # Keep its exact thread/session addressing briefly so a lost RPC response
        # can still be reconciled after the live turn registry is cleaned up.
        self._steer_reconciliation_targets: dict[str, _CodexSteerReconciliationTarget] = {}
        # base_session_id → (thread_id, developer_instructions)
        self._thread_developer_instructions: Dict[str, tuple[str, str]] = {}
        # base_session_id → (thread_id, collaboration | fallback |
        # fallback_pending_clear | fallback_pending_injection |
        # fallback_pending_clear_injection | injected_pending_persist | unavailable)
        self._thread_prompt_strategies: Dict[str, tuple[str, str]] = {}
        # base_session_id → (thread_id, developer_instructions, target_strategy)
        self._thread_unpersisted_prompts: Dict[str, tuple[str, str, str]] = {}
        # base_session_id → (thread_id, active model, active reasoning effort)
        self._thread_model_settings: Dict[str, tuple[str, str, Optional[str]]] = {}
        # base_session_id → (thread_id, AVIBE_* caller env)
        self._thread_caller_env_configs: Dict[str, tuple[str, dict[str, str]]] = {}
        # base_session_id → (thread_id, effective Git PATH, PATH override persisted)
        self._thread_git_path_configs: Dict[str, tuple[str, str, bool]] = {}
        # Turn ids the USER stopped. ``turn/interrupt`` and the ``turn/completed``
        # notification it provokes race each other, and whichever arrives first
        # clears the 👀 — so the stop intent has to outlive both and be consumed
        # by the winner. See ``consume_user_stop_intent``.
        self._user_stopped_turn_ids: set[str] = set()
        self._fork_correction_pending_base_sessions: set[str] = set()
        self._connection_probes: Dict[str, _CodexConnectionProbeState] = {}
        self._connection_probe_turns: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    def backend_alive(self, context) -> Optional[bool]:
        """Liveness of the app-server generation this turn's Session is bound to.

        Returns None (unknown) when anything can't be resolved, so the status
        bubble never false-alarms ⚠️."""
        payload = getattr(context, "platform_specific", None) or {}
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        transport = self.transport_for_session(base_session_id) if base_session_id else None
        if transport is None:
            return None
        return self._transport_alive(transport)

    @staticmethod
    def _transport_alive(transport: CodexTransport) -> Optional[bool]:
        try:
            return bool(
                transport.is_alive
                or getattr(transport, "has_pending_notifications", False)
            )
        except Exception:
            return None

    def capture_backend_liveness(
        self,
        context: Any,
    ) -> Callable[[], Optional[bool]]:
        """Bind liveness to the app-server generation that accepted the turn."""

        payload = getattr(context, "platform_specific", None) or {}
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        transport = self.transport_for_session(base_session_id) if base_session_id else None
        if transport is None:
            return lambda: None
        return lambda: self._transport_alive(transport)

    def capture_backend_exit_failure(
        self,
        context: Any,
    ) -> Callable[[], tuple[str, str] | None] | None:
        """Bind resource diagnosis to the app-server generation owning this turn."""

        payload = getattr(context, "platform_specific", None) or {}
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        transport = self.transport_for_session(base_session_id) if base_session_id else None
        if transport is None:
            return None

        cached_diagnosis: tuple[str, str] | None = None
        exit_checked = False

        def diagnose() -> tuple[str, str] | None:
            nonlocal cached_diagnosis, exit_checked
            if exit_checked:
                return cached_diagnosis
            process = getattr(transport, "_process", None)
            if process is None or getattr(process, "returncode", None) is None:
                # A liveness failure may precede a definitive process exit.
                return None
            failure = self._resource_failure_for_transport(transport)
            if failure is None:
                # Shared cgroup observations belong to this exit boundary only.
                # A later retry must not attach a newer event to an older death.
                exit_checked = True
                return None
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            if failure.kind == "pids":
                visible = i18n_t(
                    "error.agentPidsLimit",
                    language,
                    **pids_failure_labels(failure, language),
                )
            elif failure.kind == "memory":
                visible = i18n_t("error.agentMemoryLimit", language)
            else:
                exit_checked = True
                return None
            cached_diagnosis = (
                f"backend_runtime_exited_before_terminal\nResource diagnosis: {failure.message}",
                f"❌ {visible}",
            )
            exit_checked = True
            return cached_diagnosis

        return diagnose

    def can_reuse_direct_connection_probe(self, cwd: str) -> bool:
        """Return whether a live generation can test the current direct credentials."""

        return self._direct_probe_generation(cwd) is not None

    def _direct_probe_generation(self, cwd: str) -> _CodexGeneration | None:
        """The live generation started exactly as a direct launch would start now."""

        unit = self._units.get(cwd)
        if unit is None or not os.path.isdir(cwd):
            return None
        inputs = self._launch_inputs(cwd)
        digest = self._launch_spec_digest(inputs, args=(), env=inputs.env)
        for generation in reversed(unit.generations):
            if (
                not generation.closed
                and generation.spec.digest == digest
                and generation.runtime.transport.is_initialized
            ):
                return generation
        return None

    async def probe_connection(
        self,
        cwd: str,
        *,
        model: str | None = None,
        on_diagnostic: Callable[[str], None] | None = None,
    ) -> str:
        """Run a read-only ephemeral turn on the normal persistent app-server."""

        from core.agent_model_selection import require_agent_model

        model = require_agent_model(model, "codex")
        probe_cwd = paths.get_runtime_dir() / CODEX_CONNECTION_PROBE_DIR
        probe_cwd.mkdir(parents=True, exist_ok=True)
        transport: CodexTransport | None = None
        state: _CodexConnectionProbeState | None = None
        thread_id = ""
        closed_task: asyncio.Task[None] | None = None
        binding: RuntimeBinding[CodexLaunchSpec, _CodexRuntime] | None = None
        try:
            if getattr(self, "_registered_runtime", True):
                # Never start or promote a process just to probe: that would
                # change which generation serves this directory's turns.
                generation = self._direct_probe_generation(cwd)
                if generation is None:
                    raise CodexConnectionProbeRuntimeMismatchError(
                        "No live direct Codex generation is available for the probe"
                    )
                binding = await self._unit(cwd).bind(generation)
            else:
                binding = await self._acquire_generation(cwd)
            transport = binding.generation.runtime.transport

            thread_response = await transport.send_request(
                "thread/start",
                {
                    "cwd": str(probe_cwd),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "model": model,
                    "developerInstructions": (
                        "This is a connection probe. Do not use tools. "
                        "Reply with a short greeting."
                    ),
                },
            )
            thread = thread_response.get("thread")
            thread_id = str(
                thread_response.get("id")
                or (thread.get("id") if isinstance(thread, dict) else "")
                or ""
            )
            if not thread_id:
                raise RuntimeError("Codex thread/start returned no thread id")

            state = _CodexConnectionProbeState(on_diagnostic)
            self._connection_probes[thread_id] = state
            turn_params: Dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Hi"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "effort": "low",
                "model": model,
            }
            turn_response = await transport.send_request("turn/start", turn_params)
            turn = turn_response.get("turn")
            turn_id = turn_response.get("id") or (
                turn.get("id") if isinstance(turn, dict) else None
            )
            if not turn_id:
                raise RuntimeError("Codex turn/start returned no turn id")
            state.turn_id = str(turn_id)
            self._connection_probe_turns[state.turn_id] = thread_id

            closed_task = asyncio.create_task(transport.wait_closed())
            done, _ = await asyncio.wait(
                {state.terminal, closed_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if state.terminal not in done:
                raise ConnectionError("Codex app-server exited during the connection probe")
            outcome, result = state.terminal.result()
            if outcome == "error":
                raise RuntimeError(result)
            if not result.strip():
                raise RuntimeError("Codex Agent turn returned no response")
            self._touch_runtime(binding.generation.runtime)
            return result
        finally:
            try:
                if (
                    transport is not None
                    and state is not None
                    and state.turn_id
                    and not state.terminal.done()
                    and transport.is_initialized
                ):
                    try:
                        await asyncio.wait_for(
                            transport.send_request(
                                "turn/interrupt",
                                {"threadId": thread_id, "turnId": state.turn_id},
                            ),
                            timeout=2.0,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to interrupt cancelled Codex connection probe",
                            exc_info=True,
                        )
            finally:
                if thread_id:
                    self._connection_probes.pop(thread_id, None)
                if state is not None and state.turn_id:
                    self._connection_probe_turns.pop(state.turn_id, None)
                if state is not None and not state.terminal.done():
                    state.terminal.cancel()
                try:
                    if closed_task is not None:
                        closed_task.cancel()
                        await asyncio.gather(closed_task, return_exceptions=True)
                finally:
                    # Even a cancellation landing above must not leak the binding.
                    if binding is not None:
                        await binding.release()

    async def _record_model_hub_native_failure(self, context: Any, diagnostic: str) -> bool:
        router = getattr(self.controller, "model_hub_runtime", None)
        recorder = getattr(router, "record_native_failure", None)
        if not callable(recorder):
            return False
        try:
            return bool(await recorder(context, diagnostic))
        except Exception:
            logger.warning("Failed to record Model Hub native cooldown", exc_info=True)
            return False

    async def handle_message(self, request: AgentRequest) -> None:
        """Process a user message by routing it through app-server.

        Flow:
        1. Bind to the working directory's app-server generation for this
           turn's launch spec, moving the Session's thread there if needed
        2. Get or create a Codex thread for this Slack thread
        3. If a turn is active → interrupt it first
        4. Start a new turn with the user's message
        """
        async with self.session_lifecycle(request.base_session_id):
            # A binding keeps the generation this turn admits to alive until the
            # turn is registered there or has failed; a registered turn then
            # keeps its generation alive as idle evidence of its own.
            bindings: list[RuntimeBinding[CodexLaunchSpec, _CodexRuntime]] = []
            try:
                await self._handle_message_locked(request, bindings)
            except asyncio.CancelledError:
                # A cancelled admission owns no turn, whether it was cancelled on
                # its first attempt or on its retry; a pending start left behind
                # would keep its generation from ever stopping.
                self._turn_registry.clear_pending_turn_start(request.base_session_id, request)
                raise
            finally:
                for binding in bindings:
                    await binding.release()

    @asynccontextmanager
    async def session_lifecycle(self, base_session_id: str):
        """Serialize one Session's lifecycle.

        Turn admission holds it, and so must every other operation that
        changes the Session's thread binding or in-memory state (End,
        ``/new``, resume preparation), so none can interleave with a turn. A
        holder may forget the lock as its last step; a waiter that then wakes
        on the forgotten lock retries on the current one, so two holders
        never overlap.
        """
        while True:
            lock = self._session_locks.get(base_session_id)
            if lock is None:
                lock = self._session_locks[base_session_id] = asyncio.Lock()
            async with lock:
                if self._session_locks.get(base_session_id) is lock:
                    yield
                    return

    def _forget_session_lock(self, base_session_id: str) -> None:
        """Forget a Session's lock unless a turn admission holds it."""
        lock = self._session_locks.get(base_session_id)
        if lock is not None and not lock.locked():
            self._session_locks.pop(base_session_id, None)

    async def _handle_message_locked(
        self,
        request: AgentRequest,
        bindings: list[RuntimeBinding[CodexLaunchSpec, _CodexRuntime]],
    ) -> None:
        launch = None
        inputs: _LaunchInputs | None = None
        try:
            if self._shutting_down:
                # Past the agent lookup already: refuse before anything is
                # recorded or a Hub launch is resolved for this turn.
                raise RuntimeUnitStopping("runtime unit is stopping")
            # Register a complete durable binding before any transport
            # acquisition or resume can fail and require ownership checks.
            self.ensure_agent_session_id(request)
            self._session_mgr.set_session_key(request.base_session_id, request.session_key)
            self._session_mgr.set_cwd(request.base_session_id, request.working_path)
            self._bind_runtime_agent_session_id(request)
            router = getattr(self.controller, "model_hub_runtime", None)
            hub_snapshot = getattr(router, "snapshot", None)
            # The turn's whole configuration is one load, taken here before
            # anything awaits: its Hub launch, catalog, spec, and process all
            # derive from it, so a concurrent save or renewal cannot split them.
            inputs = self._launch_inputs(
                request.working_path,
                hub_config=hub_snapshot() if callable(hub_snapshot) else None,
            )
            if router is not None:
                from modules.agents.model_hub import bind_launch, resolve_model_hub_launch

                _, requested_model, _, _ = self._resolve_codex_agent_settings(request)
                launch = await resolve_model_hub_launch(
                    self.controller,
                    "codex",
                    requested_model or "",
                    process_scope=self._hub_process_scope(request.working_path),
                    context=request.context,
                    config=inputs.hub_config,
                )
                bind_launch(request.context, launch)
            binding = await self._acquire_generation(request.working_path, launch, inputs=inputs)
            bindings.append(binding)
            await self._move_session_to(binding.generation, request)
        except FileNotFoundError:
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                "Codex CLI not found",
                display_text="❌ Codex CLI not found. Please install it or set CODEX_CLI_PATH.",
                request=request,
            )
            await self._remove_ack_reaction(request)
            self._event_handler._release_stream_turn(request.context)
            return
        except Exception as e:
            from modules.agents.model_hub import launch_refusal_copy

            logger.error("Failed to start Codex transport: %s", e, exc_info=True)
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            refusal = launch_refusal_copy(self.controller, e)
            if isinstance(e, CodexThreadReleaseUnavailableError):
                # Not a source failure: no Hub cooldown. Hold the unwritten
                # input for an explicit retry.
                mark_prewrite_recovery_required(request.context, e.reason)
                display_text = f"❌ {i18n_t('error.codexThreadReleaseUnavailable', language)}"
            elif isinstance(e, RuntimeUnitStopping):
                # Not a source failure: no Hub cooldown.
                display_text = (
                    f"❌ {i18n_t('error.agentRuntimeRetired', language, agent=i18n_t('backend.codex', language))}"
                )
            elif isinstance(e, CodexModelHubCatalogUnavailableError):
                await self._record_model_hub_native_failure(request.context, str(e))
                display_text = f"❌ {i18n_t('modelHub.errors.codex_catalog_unavailable', language)}"
            elif refusal is not None:
                await self._record_model_hub_native_failure(request.context, str(e))
                display_text = f"❌ {refusal}"
            else:
                await self._record_model_hub_native_failure(request.context, str(e))
                display_text = f"❌ Failed to start Codex CLI: {e}"
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                str(e),
                display_text=display_text,
                request=request,
                cause=e,
            )
            await self._remove_ack_reaction(request)
            self._event_handler._release_stream_turn(request.context)
            return

        generation = binding.generation
        transport = generation.runtime.transport
        self._touch_runtime(generation.runtime)
        await self._delete_ack(request)

        self._turn_registry.remember_request(request)
        developer_instructions: Optional[str] = None
        prompt_rendered = False
        try:
            # Set only while the thread is loaded in ``generation``.
            thread_id = self._session_mgr.get_thread_id(request.base_session_id)

            if not thread_id:
                developer_instructions = await self._build_thread_developer_instructions(request)
                prompt_rendered = True
                thread_id = await self._open_session_thread(
                    generation, request, developer_instructions=developer_instructions
                )

            # If a turn is active, interrupt it first
            active_turn = self._turn_registry.get_active_turn(request.base_session_id)
            if active_turn:
                try:
                    await transport.send_request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": active_turn},
                    )
                except Exception as e:
                    if self._is_recoverable_transport_error(e):
                        raise
                    logger.warning("Failed to interrupt turn %s: %s", active_turn, e)
                    await emit_backend_failure(
                        self.controller,
                        request.context,
                        self.name,
                        str(e),
                        display_text=f"❌ Failed to interrupt previous Codex turn: {e}",
                        request=request,
                    )
                    await self._remove_ack_reaction(request)
                    self._event_handler._release_stream_turn(request.context)
                    return
                interrupted_request = self._event_handler.clear_pending(active_turn)
                if interrupted_request:
                    await self._remove_ack_reaction(interrupted_request)

            # Render once at the actual Turn boundary. Besides keeping the
            # payload byte-stable, this avoids repeating admission
            # side effects while the same request refreshes and starts.
            if not prompt_rendered:
                developer_instructions = await self._build_thread_developer_instructions(request)
                prompt_rendered = True
            await self._refresh_thread_developer_instructions_if_needed(
                transport,
                request,
                thread_id,
            )
            self._bind_runtime_agent_session_id(request)
            thread_id = await self._start_turn(
                transport,
                request,
                thread_id,
                developer_instructions=developer_instructions,
            )

        except Exception as e:
            # Safety net: if the app-server broke before this turn reached it,
            # replace that generation and retry once on a working one.
            if (
                self._is_recoverable_transport_error(e)
                and backend_dispatch_attempted(request.context) is False
            ):
                logger.warning(
                    "Recoverable Codex transport failure for session %s, restarting transport and retrying: %s",
                    request.base_session_id,
                    e,
                )
                if await self._drop_generation_after_failure(generation, request, binding):
                    try:
                        # The retry keeps the turn's admission load.
                        binding = await self._acquire_generation(
                            request.working_path, launch, inputs=inputs
                        )
                        bindings.append(binding)
                        generation = binding.generation
                        transport = generation.runtime.transport
                        self._touch_runtime(generation.runtime)
                        if not prompt_rendered:
                            self.ensure_agent_session_id(request)
                            developer_instructions = await self._build_thread_developer_instructions(request)
                            prompt_rendered = True
                        thread_id = await self._open_session_thread(
                            generation, request, developer_instructions=developer_instructions
                        )
                        self._bind_runtime_agent_session_id(request)
                        await self._start_turn(
                            transport,
                            request,
                            thread_id,
                            developer_instructions=developer_instructions,
                        )
                        return  # retry succeeded
                    except Exception as retry_err:
                        e = retry_err  # fall through to normal error handling

            # FAIL LOUD on a server-side "thread not found": the conversation is
            # gone, so surface the error instead of silently clearing the
            # mapping and forking a fresh thread (which hid the context loss).
            # The mapping is kept so the failure is consistent until the user
            # explicitly starts a new session (product decision: no silent
            # fallbacks).
            if isinstance(e, (CodexResumeUnavailableError, CodexResponseTooLargeError)):
                mark_prewrite_recovery_required(request.context, "codex_resume_unavailable")
            self._turn_registry.clear_pending_turn_start(request.base_session_id, request)
            logger.error("Error in Codex handle_message: %s", e, exc_info=True)
            await self._record_model_hub_native_failure(request.context, str(e))
            # A successful replacement consumes no shared pressure evidence.
            # Diagnose only the transport whose failure is actually reported.
            resource_failure = self._resource_failure_for_transport(transport)
            error_text = self._error_display_text(
                e,
                resource_failure=resource_failure,
            )
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                str(e),
                display_text=error_text,
                request=request,
            )
            await self._remove_ack_reaction(request)
            # The turn never started (all retries failed) — release the
            # web-Chat working/Stop state instead of leaving it until the
            # fallback timeout (Codex P2).
            self._event_handler._release_stream_turn(request.context)

    def steering_native_turn_id(self, target: ActiveSteerTarget) -> Optional[str]:
        active_request = target.agent_request
        if active_request is None:
            return None
        return self._turn_registry.get_active_turn(active_request.base_session_id)

    def _prune_steer_reconciliation_targets(self) -> None:
        targets = getattr(self, "_steer_reconciliation_targets", None)
        if not targets:
            return
        cutoff = time.monotonic() - _STEER_RECONCILIATION_TTL_SECONDS
        for attempt_id, retained in list(targets.items()):
            if retained.recorded_at < cutoff:
                targets.pop(attempt_id, None)
        if len(targets) <= _MAX_STEER_RECONCILIATION_TARGETS:
            return
        oldest = sorted(targets.items(), key=lambda item: item[1].recorded_at)
        for attempt_id, _retained in oldest[: len(targets) - _MAX_STEER_RECONCILIATION_TARGETS]:
            targets.pop(attempt_id, None)

    def _remember_steer_reconciliation_target(
        self,
        request: SteerRequest,
        target: ActiveSteerTarget,
        *,
        thread_id: str,
        cwd: str,
        runtime: _CodexRuntime | None = None,
    ) -> None:
        if not request.attempt_id or not thread_id or not cwd:
            return
        targets = getattr(self, "_steer_reconciliation_targets", None)
        if targets is None:
            targets = {}
            self._steer_reconciliation_targets = targets
        self._prune_steer_reconciliation_targets()
        targets[request.attempt_id] = _CodexSteerReconciliationTarget(
            target=target,
            target_session_id=request.target_session_id,
            logical_turn_id=request.expected_logical_turn_id,
            native_turn_id=request.expected_native_turn_id,
            thread_id=thread_id,
            cwd=cwd,
            recorded_at=time.monotonic(),
            runtime=runtime,
        )

    def _forget_steer_reconciliation_target(self, attempt_id: str) -> None:
        if not attempt_id:
            return
        targets = getattr(self, "_steer_reconciliation_targets", None)
        if targets is not None:
            targets.pop(attempt_id, None)

    def _finish_steer_receipt(
        self,
        request: SteerRequest,
        receipt: SteerResult,
    ) -> SteerResult:
        if receipt.outcome is not SteerOutcome.UNKNOWN:
            self._forget_steer_reconciliation_target(request.attempt_id)
        return receipt

    @staticmethod
    def _durable_reconciliation_binding(
        session_id: str,
        *,
        backend: str,
    ) -> tuple[str, str] | None:
        """Return the persisted native thread and workdir for a Session."""

        try:
            from core.scheduled_tasks import resolve_session_id_target

            target = resolve_session_id_target(session_id)
        except Exception:
            logger.debug(
                "Could not resolve durable Codex reconciliation binding for Session=%s",
                session_id,
                exc_info=True,
            )
            return None
        if str(target.agent_backend or "").strip() != backend:
            return None
        native_session_id = str(target.native_session_id or "").strip()
        workdir = str(target.workdir or "").strip()
        if not native_session_id or not workdir:
            return None
        return native_session_id, workdir

    def reconciliation_steer_target(
        self,
        request: SteerReconcileRequest,
    ) -> ActiveSteerTarget | None:
        """Return a live or durable target for a write whose acknowledgement was lost."""

        self._prune_steer_reconciliation_targets()
        targets = getattr(self, "_steer_reconciliation_targets", None) or {}
        retained = targets.get(request.attempt_id)
        if retained is not None:
            if (
                retained.target_session_id != request.target_session_id
                or retained.logical_turn_id != request.expected_logical_turn_id
                or retained.native_turn_id != request.expected_native_turn_id
            ):
                return None
            return retained.target

        # Recovery may run after a restart, when the in-memory target was lost.
        # The Session row still pins the Codex thread and workdir, so a read-only
        # reconciliation can proceed without reconstructing a live Turn gate.
        if self._durable_reconciliation_binding(
            request.target_session_id,
            backend=self.name,
        ) is None:
            return None
        return ActiveSteerTarget(
            runtime_key=request.target_session_id,
            logical_turn_id=request.expected_logical_turn_id,
            context=None,
            agent_request=None,
            agent=self,
        )

    async def steer_active_turn(
        self,
        request: SteerRequest,
        target: ActiveSteerTarget,
    ) -> SteerResult:
        active_request = target.agent_request
        if active_request is None:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="missing_primary_request", backend=self.name)

        base_session_id = active_request.base_session_id
        turn_id = self._turn_registry.get_active_turn(base_session_id)
        if not turn_id or turn_id != request.expected_native_turn_id:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="stale_native_turn", backend=self.name)

        thread_id = self._session_mgr.get_thread_id(base_session_id)
        cwd = self._session_mgr.get_cwd(base_session_id) or active_request.working_path
        generation = self._generation_for_session(base_session_id)
        runtime = generation.runtime if generation is not None else None
        transport = runtime.transport if runtime is not None else None
        if not thread_id:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="missing_native_thread", backend=self.name)
        if transport is None or not transport.is_initialized:
            return steer_result(SteerOutcome.REFUSED, reason="runtime_unavailable", backend=self.name)
        self._remember_steer_reconciliation_target(
            request,
            target,
            thread_id=thread_id,
            cwd=cwd,
            runtime=runtime,
        )

        steer_params = {
            "threadId": thread_id,
            "expectedTurnId": request.expected_native_turn_id,
            "input": self._build_native_input(request.text, request.files, request.input_metadata),
        }
        if request.attempt_id:
            # Codex persists this opaque client id on the userMessage item. It
            # lets recovery prove whether an acknowledgement-ambiguous write
            # landed without matching by text or replaying the input.
            steer_params["clientUserMessageId"] = request.attempt_id

        try:
            response = await transport.send_request(
                "turn/steer",
                steer_params,
            )
        except RuntimeError as exc:
            diagnostic = str(exc)
            lowered = diagnostic.lower()
            if any(
                marker in lowered
                for marker in (
                    "no active turn to steer",
                    "thread not found",
                    "expected turn",
                    "expectedturnid",
                )
            ):
                return self._finish_steer_receipt(
                    request,
                    steer_result(
                        SteerOutcome.NOT_ACTIVE,
                        reason="native_turn_mismatch",
                        backend=self.name,
                        diagnostic=diagnostic,
                    ),
                )
            if "activeturnnotsteerable" in lowered or "not steerable" in lowered:
                return self._finish_steer_receipt(
                    request,
                    steer_result(
                        SteerOutcome.REFUSED,
                        reason="native_turn_not_steerable",
                        backend=self.name,
                        diagnostic=diagnostic,
                    ),
                )
            return self._finish_steer_receipt(
                request,
                steer_result(
                    SteerOutcome.REFUSED,
                    reason="backend_refused",
                    backend=self.name,
                    diagnostic=diagnostic,
                ),
            )
        except ConnectionError as exc:
            diagnostic = str(exc)
            if diagnostic in {
                "Codex app-server transport is not available",
                "Codex app-server stdin is not available",
            }:
                return self._finish_steer_receipt(
                    request,
                    steer_result(
                        SteerOutcome.REFUSED,
                        reason="runtime_unavailable",
                        backend=self.name,
                        diagnostic=diagnostic,
                    ),
                )
            self._touch_runtime(runtime)
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="acknowledgement_ambiguous",
                backend=self.name,
                diagnostic=diagnostic,
            )
        except TimeoutError as exc:
            self._touch_runtime(runtime)
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="acknowledgement_ambiguous",
                backend=self.name,
                diagnostic=str(exc),
            )

        self._touch_runtime(runtime)
        response_turn_id = str(response.get("turnId") or "").strip()
        if response_turn_id != request.expected_native_turn_id:
            return self._finish_steer_receipt(
                request,
                steer_result(
                    SteerOutcome.UNKNOWN,
                    reason="untrusted_acknowledgement",
                    backend=self.name,
                    response_turn_id=response_turn_id,
                ),
            )
        return self._finish_steer_receipt(
            request,
            steer_result(
                SteerOutcome.ACCEPTED,
                backend=self.name,
                thread_id=thread_id,
                turn_id=response_turn_id,
            ),
        )

    async def reconcile_steer_attempt(
        self,
        request: SteerReconcileRequest,
        target: ActiveSteerTarget,
    ) -> SteerResult:
        """Read Codex's persisted user-message id for one prior steer."""

        if not request.attempt_id:
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="missing_attempt_identity",
                backend=self.name,
            )

        active_request = target.agent_request
        base_session_id = (
            active_request.base_session_id
            if active_request is not None
            else request.target_session_id
        )
        targets = getattr(self, "_steer_reconciliation_targets", None) or {}
        retained = targets.get(request.attempt_id)
        transport: CodexTransport | None = None
        if retained is not None:
            thread_id = retained.thread_id
            cwd = retained.cwd
            if retained.runtime is not None:
                transport = retained.runtime.transport
        else:
            active_turn_id = self._turn_registry.get_active_turn(base_session_id)
            if active_turn_id == request.expected_native_turn_id:
                thread_id = self._session_mgr.get_thread_id(base_session_id)
                cwd = self._session_mgr.get_cwd(base_session_id)
                if not cwd and active_request is not None:
                    cwd = active_request.working_path
                transport = self.transport_for_session(base_session_id)
            else:
                durable = self._durable_reconciliation_binding(
                    request.target_session_id,
                    backend=self.name,
                )
                if durable is None:
                    return steer_result(
                        SteerOutcome.UNKNOWN,
                        reason="stale_native_turn",
                        backend=self.name,
                    )
                thread_id, cwd = durable

        if transport is not None and getattr(transport, "is_alive", True) is False:
            transport = None
        binding: RuntimeBinding[CodexLaunchSpec, _CodexRuntime] | None = None
        try:
            if transport is None and cwd:
                try:
                    # A timed-out request marks the transport uninitialized, but
                    # an alive reader can still answer this read-only evidence
                    # query, and ``thread/read`` works from any generation of
                    # the directory. Start one only when none is live; this
                    # never replays the original steer.
                    binding = await self._bind_any_generation(cwd)
                    transport = binding.generation.runtime.transport
                except Exception as exc:
                    return steer_result(
                        SteerOutcome.UNKNOWN,
                        reason="attempt_evidence_unavailable",
                        backend=self.name,
                        diagnostic=str(exc),
                    )
            if not thread_id or transport is None:
                return steer_result(
                    SteerOutcome.UNKNOWN,
                    reason="attempt_evidence_unavailable",
                    backend=self.name,
                )

            try:
                response = await transport.send_request(
                    "thread/read",
                    {
                        "threadId": thread_id,
                        "includeTurns": True,
                    },
                )
            except (ConnectionError, TimeoutError) as exc:
                return steer_result(
                    SteerOutcome.UNKNOWN,
                    reason="attempt_evidence_unavailable",
                    backend=self.name,
                    diagnostic=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - absence is not negative proof
                return steer_result(
                    SteerOutcome.UNKNOWN,
                    reason="attempt_evidence_unavailable",
                    backend=self.name,
                    diagnostic=str(exc),
                )
        finally:
            if binding is not None:
                await binding.release()

        thread = response.get("thread") if isinstance(response, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if not isinstance(turns, list):
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="attempt_evidence_unavailable",
                backend=self.name,
            )

        for turn in turns:
            if not isinstance(turn, dict) or str(turn.get("id") or "") != request.expected_native_turn_id:
                continue
            items = turn.get("items")
            if not isinstance(items, list):
                break
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "userMessage"
                    and str(item.get("clientId") or "") == request.attempt_id
                ):
                    self._forget_steer_reconciliation_target(request.attempt_id)
                    return steer_result(
                        SteerOutcome.ACCEPTED,
                        reason="native_attempt_client_id_found",
                        backend=self.name,
                        native_turn_id=request.expected_native_turn_id,
                        client_user_message_id=request.attempt_id,
                    )
            break

        return steer_result(
            SteerOutcome.UNKNOWN,
            reason="untrusted_attempt_evidence",
            backend=self.name,
        )

    async def handle_stop(self, request: AgentRequest) -> bool:
        """Gracefully interrupt the active turn."""
        thread_id = self._session_mgr.get_thread_id(request.base_session_id)
        turn_id = self._turn_registry.get_active_turn(request.base_session_id)

        if not thread_id or not turn_id:
            request.stop_failure_reason = "not_active"
            return False

        transport = self.transport_for_session(request.base_session_id)
        if not transport or not transport.is_alive:
            request.stop_failure_reason = "runtime_unavailable"
            return False

        # Recorded BEFORE the RPC. Codex answers an interrupt with a
        # ``turn/completed`` notification the event worker may process while this
        # call is still awaiting its response; that handler pops the turn and
        # clears its reaction, after which ``clear_pending`` here returns None.
        # Whichever side gets there first consumes the intent and owes the
        # receipt, so the race can no longer swallow it.
        self._user_stopped_turn_ids.add(turn_id)
        try:
            await transport.send_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
            )
            interrupted_request = self._event_handler.clear_pending(turn_id)
            stopped_by_user = self.consume_user_stop_intent(turn_id)
            if interrupted_request and stopped_by_user:
                await self._remove_ack_reaction(
                    interrupted_request,
                    terminal_emoji=STOPPED_REACTION_EMOJI,
                )
            elif interrupted_request is None and stopped_by_user:
                # A normal/failed completion won the race and popped the turn
                # without consuming the stop intent. Its own terminal output is
                # authoritative, so do not overwrite it with a silent cancel.
                logger.info("Codex turn %s completed before /stop claimed it", turn_id)
                return True
            # A user-initiated stop is terminal but intentional, so it carries NO
            # user-facing message: a single SILENT result settles the dot to idle +
            # releases the SSE waiter through the outbound chokepoint without a
            # bubble. The user already knows they stopped it (avibe shows the dot go
            # idle; IM shows the ⏹️ receipt stamped above). ``level="silent"`` is
            # the explicit visibility grade rather than faking it via empty text.
            # ``stop_output_for`` (not the terminal-turn default) keeps this empty body
            # out of the run's terminal state so the stop settles it ``canceled``
            # instead of ``succeeded`` — see its docstring.
            await self.controller.emit_agent_message(
                request.context,
                "result",
                "",
                level="silent",
                output=stop_output_for(request),
            )
            logger.info("Codex turn %s interrupted via /stop", turn_id)
            return True
        except Exception as e:
            request.stop_failure_reason = "interrupt_failed"
            logger.error("Failed to interrupt Codex turn: %s", e)
            return False
        finally:
            # A normal/failed completion does not consume stop intent, and the
            # caller itself may be cancelled while the RPC is in flight. Never
            # let either path leave a stale turn id in this long-lived agent.
            self._user_stopped_turn_ids.discard(turn_id)

    def consume_user_stop_intent(self, turn_id: str) -> bool:
        """Claim the /stop intent for ``turn_id``; True for the first claimer only.

        Both the interrupt RPC and the ``turn/completed`` notification it causes
        want to retire the same reaction, and either may run first. Claiming the
        intent makes the receipt exactly-once instead of dependent on that order.
        """

        if not turn_id or turn_id not in self._user_stopped_turn_ids:
            return False
        self._user_stopped_turn_ids.discard(turn_id)
        return True

    async def clear_sessions(self, session_key: str) -> int:
        """Clear sessions scoped to a specific session_key.

        Under each cleared Session's lock, its thread is released first, so a
        cleared conversation never stays loaded in a process that keeps
        serving other Sessions. A thread that stays loaded fails the clear
        before anything is forgotten, so the clear stays retryable.
        """
        while True:
            # Use session_key index (not _threads) so sessions with
            # invalidated threads are still cleaned up properly.
            to_clear = sorted(self._session_mgr.get_sessions_by_session_key(session_key))
            async with AsyncExitStack() as held:
                for bid in to_clear:
                    await held.enter_async_context(self.session_lifecycle(bid))
                for bid in to_clear:
                    await self.release_session_runtime(bid)
                if sorted(self._session_mgr.get_sessions_by_session_key(session_key)) != to_clear:
                    # A Session of this key started meanwhile, and the clear
                    # below covers the whole key: release that one too.
                    continue
                self.sessions.clear_agent_sessions(session_key, self.name)
                count = self._session_mgr.clear_by_session_key(session_key)
                for bid in to_clear:
                    self._turn_registry.clear_session(bid)
                    self._clear_thread_developer_instructions(bid)
                    self._session_locks.pop(bid, None)
                return count

    def runtime_turn_keys(self) -> set[str]:
        return {
            self._runtime_turn_key_for_base_session(base_session_id)
            for base_session_id in self._session_mgr.all_base_sessions()
        }

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        return {
            self._runtime_turn_key_for_base_session(base_session_id)
            for base_session_id in self._session_mgr.get_sessions_by_session_key(session_key)
        }

    def _runtime_turn_key_for_base_session(self, base_session_id: str) -> str:
        cwd = self._session_mgr.get_cwd(base_session_id)
        return f"{base_session_id}:{cwd}" if cwd else base_session_id

    async def retire_for_native_migration(self) -> None:
        """Retire every idle app-server generation or refuse credential mutation."""
        for unit in list(self._units.values()):
            # A teardown already in flight must answer before custody moves.
            await unit.settled()
            for generation in unit.generations:
                if not await self._generation_drained(generation):
                    raise RuntimeError("Codex runtime retirement was refused")
            # Re-check synchronously: work may have bound during the awaits above.
            generations = unit.generations
            if any(self._generation_has_live_work(generation) for generation in generations):
                raise RuntimeError("Codex runtime retirement was refused")
            # migration_guard interrupted the backend's work before custody
            # moves, and the drained check above refuses any owner it missed.
            await self._stop_generations_now(
                unit, generations, require_process_exit=True, settle_reason=None
            )
        await self._end_unattached_runtimes(require_process_exit=True)
        for base_session_id in self._session_mgr.all_base_sessions():
            self._forget_stale_session(base_session_id)
            self._turn_registry.clear_session(base_session_id)

    async def release_session_runtime(self, base_session_id: str) -> None:
        """Unload an ending Session's thread so no app-server keeps holding it.

        Codex lets one process at a time hold a thread; a thread left loaded in
        a shared app-server would block resuming that conversation elsewhere.
        The caller holds ``session_lifecycle(base_session_id)``.
        """
        generation = self._generation_for_session(base_session_id)
        if generation is None:
            self._forget_stale_session(base_session_id)
            return
        await self._release_session_thread(generation, base_session_id)

    async def end_session(self, base_session_id: str) -> dict[str, bool]:
        """End one Session's live runtime, for End in Running Agents.

        Under the Session's lifecycle, a Session that was the only user of its
        directory's app-servers takes them down: its own running turn and
        Activities settle through ``_end_bound_work`` first. Otherwise only
        its thread is released, which interrupts its own turn first and
        raises ``CodexThreadReleaseUnavailableError`` rather than forget a
        turn Codex did not stop. The Session's state is cleared only after
        that succeeded, so a failed End can be retried.
        """
        async with self.session_lifecycle(base_session_id):
            interrupted = bool(self._turn_registry.get_active_turn(base_session_id))
            generation = self._generation_for_session(base_session_id)
            unit = self._units.get(generation.runtime.cwd) if generation is not None else None
            process_killed = False
            if generation is None:
                self._forget_stale_session(base_session_id)
            elif (
                unit is not None
                and generation in unit.generations
                and not self._serves_another_session(unit, base_session_id)
            ):
                # End is the user's explicit request to kill, so what it ends
                # is settled as their stop.
                await self._stop_generations_now(
                    unit, unit.generations, require_process_exit=True, settle_reason=SETTLED_BY_STOPPED
                )
                process_killed = True
            else:
                # A process the reconciler already detached is not End's to
                # kill: release the thread there, which waits out its teardown.
                await self._release_session_thread(generation, base_session_id)
                process_killed = generation.runtime.ended
            self._turn_registry.clear_session(base_session_id)
            self._session_mgr.clear(base_session_id)
            self._session_last_activity.pop(base_session_id, None)
            self._clear_thread_developer_instructions(base_session_id)
            # End holds this lock; forgetting it is its last step.
            self._session_locks.pop(base_session_id, None)
        return {"interrupted": interrupted, "process_killed": process_killed}

    def _serves_another_session(
        self,
        unit: RuntimeGenerationSet[CodexLaunchSpec, _CodexRuntime],
        base_session_id: str,
    ) -> bool:
        """Whether any process of the directory still serves another Session."""
        return any(
            generation.bindings
            or any(other != base_session_id for other in generation.runtime.threads)
            for generation in unit.generations
        )

    async def refresh_auth_state(self) -> None:
        """Stop every app-server generation; used by exclusive credential cutovers."""
        base_session_ids = list(self._session_mgr.all_base_sessions())
        controller = getattr(self, "controller", None)
        turn_manager = getattr(controller, "session_turns", None)
        release_for_backend_refresh = getattr(turn_manager, "release_for_backend_refresh", None)
        if callable(release_for_backend_refresh):
            try:
                await release_for_backend_refresh(
                    backend=self.name,
                    base_session_ids=set(base_session_ids),
                )
            except Exception:
                logger.warning("Failed to release Workbench turns during Codex refresh", exc_info=True)
        self._session_last_activity.clear()
        stopped = 0
        for unit in list(self._units.values()):
            await unit.settled()
            generations = unit.generations
            try:
                # The exclusive cutover runs after the coordinator interrupted
                # this backend's turns and Activities.
                await self._stop_generations_now(unit, generations, settle_reason=None)
            except Exception as exc:
                logger.warning("Failed to stop Codex transport during auth refresh: %s", exc)
            stopped += sum(1 for generation in generations if generation.runtime.ended)
        try:
            await self._end_unattached_runtimes()
        except Exception as exc:
            logger.warning("Failed to stop Codex transport during auth refresh: %s", exc)

        for base_session_id in base_session_ids:
            # A Session whose process survived a failed stop stays bound to it.
            self._forget_stale_session(base_session_id)
            self._turn_registry.clear_session(base_session_id)

        logger.info("Refreshed Codex auth state across %d transport(s)", stopped)

    async def refresh_runtime_config(self, codex_config: Any) -> None:
        """Reload persisted runtime config and stop every app-server generation."""
        self.codex_config = codex_config
        self.controller.config.codex = codex_config
        await self.adopt_model_hub_catalog()
        await self.refresh_auth_state()

    async def renew_runtime(self, codex_config: Any, *, config_save: bool = False) -> None:
        """Adopt persisted runtime config; each cwd moves to it at its next turn.

        Nothing stops or waits here. The binary and extra arguments are launch
        spec inputs, and the other ``agents.codex`` fields are read live, so a
        plain config save needs no renewal. Every other caller (credential
        flows, manual Restart, installs) bumps the renewal epoch, which is part
        of every launch spec: a cwd's next turn then starts a new app-server
        generation while turns already running finish on theirs.
        """
        self.codex_config = codex_config
        self.controller.config.codex = codex_config
        if not config_save:
            self._runtime_epoch += 1

    async def adopt_model_hub_catalog(self) -> None:
        """Forget prepared catalogs; each Hub turn prepares from its own snapshot.

        A changed catalog changes the launch spec, so a cwd moves to a new
        generation at its next Hub turn. Running generations keep the catalog
        pinned for as long as they run.
        """
        catalogs = list(self._model_hub_catalogs.values())
        self._model_hub_catalogs.clear()
        for catalog in catalogs:
            catalog.close()

    async def prepare_model_hub_runtime(
        self,
        config: Any = None,
        *,
        inputs: _LaunchInputs | None = None,
    ) -> CodexHubCatalog:
        """Bind Hub metadata to this Agent's exact configured Codex binary.

        ``config`` is the turn's Model Hub snapshot; the catalog lists exactly
        the models that snapshot resolved against. ``inputs`` is the turn's
        launch snapshot, so the catalog comes from the binary its process runs.
        """
        from vibe import backend_model_catalog

        if inputs is None:
            binary = self.codex_config.binary
            binary_identity = self._binary_identity(binary, self._codex_runtime_environment())
        else:
            binary, binary_identity = inputs.binary, inputs.identity["binary"]
        configured_models = None
        if config is None:
            model_hub_service = getattr(self.controller, "model_hub_service", None)
            store = getattr(model_hub_service, "store", None)
            config = store.load() if store is not None else None
        if config is not None:
            configured_models = [model.to_payload() for model in config.agents["codex"].models]
        key = (
            json.dumps(binary_identity, sort_keys=True),
            hashlib.sha256(json.dumps(configured_models, sort_keys=True).encode()).hexdigest(),
        )
        async with self._model_hub_catalog_lock:
            cached = self._model_hub_catalogs.get(key)
            if cached is not None:
                self._model_hub_catalogs.move_to_end(key)
                return cached
            preparation = asyncio.create_task(
                asyncio.to_thread(
                    backend_model_catalog.prepare_codex_hub_catalog,
                    binary,
                    None,
                    configured_models,
                )
            )
            try:
                catalog = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                # The export runs in a thread and cannot be cancelled. Its
                # result must still release its pin after the caller leaves.
                preparation.add_done_callback(self._discard_model_hub_catalog)
                raise
            except Exception as exc:
                raise CodexModelHubCatalogUnavailableError(
                    "Codex Model Hub catalog preparation failed"
                ) from exc
            self._model_hub_catalogs[key] = catalog
            while len(self._model_hub_catalogs) > _CACHED_HUB_CATALOGS:
                _, evicted = self._model_hub_catalogs.popitem(last=False)
                evicted.close()
            return catalog

    @staticmethod
    def _discard_model_hub_catalog(preparation: asyncio.Task) -> None:
        try:
            preparation.result().close()
        except Exception:
            logger.warning("Cancelled Codex catalog preparation failed", exc_info=True)

    async def prepare_resume_binding(
        self,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        """Release the Session's current thread before it binds another one.

        Only this Session's thread is unloaded; the app-server keeps serving
        every other Session in the directory. Raises
        ``CodexThreadReleaseUnavailableError`` when the thread stays loaded.
        """
        # Serialized with turn admission. The caller rewrites the durable
        # mapping right after this returns, with no await in between.
        async with self.session_lifecycle(base_session_id):
            generation = self._generation_for_session(base_session_id)
            if generation is None:
                self._forget_stale_session(base_session_id)
            else:
                # A thread that stays loaded aborts the resume before its mapping
                # changes; otherwise the next turn would still reach the old thread.
                await self._release_session_thread(generation, base_session_id)
            self._turn_registry.clear_session(base_session_id)
        logger.info("Prepared Codex runtime for resumed session %s", base_session_id)

    async def shutdown_runtime(self, settle_reason: str | None = None) -> None:
        """Stop every app-server now: service shutdown, probe teardown, or disable.

        Disabling the backend is an explicit, user-visible stop: the core has
        removed this agent from routing and passes ``settle_reason``. Every
        forced stop then settles the turns and Activities bound to its
        generation with that reason before it kills the process, so this
        agent's work is settled even if the core's own interrupt failed, and
        no other agent's work is touched. Service shutdown and probe teardown
        pass none and show no notice. A turn that captured the agent before
        the routing change fails visibly and starts no process.

        Raises while any app-server survives: nothing else owns it once the
        agent leaves routing, so the caller keeps the teardown and retries
        it. A retry stops every survivor again and settles whatever is still
        bound to it.
        """
        await self.adopt_model_hub_catalog()
        self._session_last_activity.clear()
        self._shutting_down = True
        self._shutdown_settle_reason = settle_reason
        stopped = sum(len(runtimes) for runtimes in self._runtimes.values())
        for unit in list(self._units.values()):
            # A forced stop that fails leaves its generation attached; the
            # pass below retries every process still running, attached or not.
            await unit.stop_all(force=True)
        failure: Exception | None = None
        try:
            await self._end_unattached_runtimes(settle_reason=settle_reason)
        except Exception as exc:
            failure = exc

        # Shutdown ends the whole runtime: every process was just asked to end,
        # so all Session state goes with it, including any bindings. A Session
        # still bound to a survivor keeps its state, so a retry can settle it.
        for base_session_id in list(self._session_mgr.all_base_sessions()):
            if self._generation_for_session(base_session_id) is not None:
                continue
            session_key = self._session_mgr.get_session_key(base_session_id)
            if session_key:
                self.sessions.clear_agent_session_mapping(session_key, self.name, base_session_id)
            self._session_generations.pop(base_session_id, None)
            self._session_mgr.clear(base_session_id)
            self._turn_registry.clear_session(base_session_id)
            self._clear_thread_developer_instructions(base_session_id)

        self._session_locks.clear()
        survivors = sum(len(runtimes) for runtimes in self._runtimes.values())
        if failure is not None or survivors:
            raise RuntimeError(f"{survivors} Codex app-server(s) survived shutdown") from failure
        logger.info("Stopped Codex runtime across %d transport(s)", stopped)

    def _bind_runtime_agent_session_id(self, request: AgentRequest) -> None:
        setter = getattr(self._session_mgr, "set_agent_session_id", None)
        if not callable(setter):
            return
        payload = getattr(request.context, "platform_specific", None) or {}
        session_id = payload.get("agent_session_id") if isinstance(payload, dict) else None
        setter(request.base_session_id, session_id)

    def _resource_failure_for_transport(self, transport: CodexTransport | None):
        process = getattr(transport, "_process", None)
        if process is None or getattr(process, "returncode", None) is None:
            return None
        if getattr(transport, "_vibe_resource_failure_checked", False):
            return getattr(transport, "_vibe_resource_failure", None)
        failure = observe_agent_resource_pressure(self.controller)
        setattr(transport, "_vibe_resource_failure", failure)
        setattr(transport, "_vibe_resource_failure_checked", True)
        if failure is not None:
            logger.error(
                "Codex app-server exited while the shared Agent cgroup reported resource pressure: %s",
                failure.message,
            )
        return failure

    def _error_display_text(
        self,
        error: BaseException,
        *,
        resource_failure=None,
    ) -> str:
        if isinstance(error, CodexResponseTooLargeError):
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            message = i18n_t(
                "error.codexResponseTooLarge",
                language,
                limitMiB=error.limit // (1024 * 1024),
            )
        elif isinstance(error, CodexPromptRefreshUnavailableError):
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            message = i18n_t("error.codexPromptRefreshUnavailable", language)
        elif isinstance(error, RuntimeUnitStopping):
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            message = i18n_t("error.agentRuntimeRetired", language, agent=i18n_t("backend.codex", language))
        elif isinstance(error, CodexForkBoundaryUnavailableError):
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            message = i18n_t("error.codexForkBoundaryUnavailable", language)
        else:
            message = f"Codex error: {error}"

        if resource_failure is not None:
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            if getattr(resource_failure, "kind", None) == "pids":
                message = (
                    f"{message} "
                    f"{i18n_t('error.agentPidsLimit', language, **pids_failure_labels(resource_failure, language))}"
                )
            elif getattr(resource_failure, "kind", None) == "memory":
                message = f"{message} {i18n_t('error.agentMemoryLimit', language)}"
        return f"❌ {message}"

    def _runtime_ownership_target_for_generation(
        self,
        generation: _CodexGeneration,
    ) -> RuntimeResourceTarget | None:
        """Durable ownership scope of one generation.

        The current generation answers for every durable Session of its
        directory, including ones not yet bound in memory. A retiring one
        answers only for the Sessions whose threads it still holds; work that
        moved to a newer generation must not keep it alive.
        """
        runtime = generation.runtime
        cwd = runtime.cwd
        sessions_for_cwd = getattr(self._session_mgr, "sessions_for_cwd", None)
        all_base_sessions = getattr(self._session_mgr, "all_base_sessions", None)
        get_cwd = getattr(self._session_mgr, "get_cwd", None)
        get_session_key = getattr(self._session_mgr, "get_session_key", None)
        get_agent_session_id = getattr(
            self._session_mgr,
            "get_agent_session_id",
            None,
        )
        if not all(
            callable(method)
            for method in (
                sessions_for_cwd,
                all_base_sessions,
                get_cwd,
                get_session_key,
                get_agent_session_id,
            )
        ):
            return None

        current = not generation.retiring and not generation.stopped
        # A Session cleared from the manager no longer owns anything here.
        session_ids = (
            sessions_for_cwd(cwd)
            if current
            else [base_session_id for base_session_id in runtime.threads if get_cwd(base_session_id)]
        )
        bindings: list[RuntimeSessionBinding] = []
        for base_session_id in session_ids:
            session_key = str(get_session_key(base_session_id) or "").strip()
            agent_session_id = str(
                get_agent_session_id(base_session_id) or ""
            ).strip()
            bound_cwd = str(get_cwd(base_session_id) or "").strip()
            if not session_key or not agent_session_id or bound_cwd != cwd:
                return None
            bindings.append(
                RuntimeSessionBinding(
                    session_id=agent_session_id,
                    session_anchor=base_session_id,
                    workdir=cwd,
                    activity_runtime_keys=(f"{base_session_id}:{cwd}",),
                    fallback_route_keys=(session_key,),
                )
            )

        known_activity_keys = tuple(
            sorted(
                f"{base_session_id}:{bound_cwd}"
                for base_session_id in all_base_sessions()
                if (bound_cwd := str(get_cwd(base_session_id) or "").strip())
            )
        )
        known_route_keys = tuple(
            sorted(
                {
                    session_key
                    for base_session_id in all_base_sessions()
                    if (
                        session_key := str(
                            get_session_key(base_session_id) or ""
                        ).strip()
                    )
                }
            )
        )
        return RuntimeResourceTarget(
            backend="codex",
            resource_key=self._generation_resource_key(runtime),
            bindings=tuple(bindings),
            known_activity_runtime_keys=known_activity_keys,
            known_fallback_route_keys=known_route_keys,
            durable_session_workdir=cwd if current else None,
        )

    def _generation_resource_key(self, runtime: _CodexRuntime) -> str:
        """One generation's activation and ownership key.

        A disabled agent's retried teardown can hold its generations while a
        re-enabled agent numbers its own from 1 in the same directory, so the
        key names the agent instance as well.
        """
        return f"{runtime.cwd}#{self._instance_serial}.{runtime.serial}"

    async def _ownership_snapshots(
        self,
        generations: Sequence[_CodexGeneration],
    ) -> tuple[Any, ...] | None:
        """Read one backend snapshot batch without blocking the controller loop."""

        if not generations:
            return ()
        provider = getattr(getattr(self, "controller", None), "runtime_ownership", None)
        targets = tuple(
            self._runtime_ownership_target_for_generation(generation)
            for generation in generations
        )
        if any(target is None for target in targets):
            logger.error("Codex runtime ownership mapping unavailable")
            return None
        snapshot_many = getattr(provider, "snapshot_many", None)
        if callable(snapshot_many):
            snapshots = tuple(await asyncio.to_thread(snapshot_many, targets))
        else:
            snapshot = getattr(provider, "snapshot", None)
            if not callable(snapshot):
                logger.error("Codex runtime ownership provider unavailable")
                return None
            snapshots = tuple(
                await asyncio.gather(*(asyncio.to_thread(snapshot, target) for target in targets))
            )
        if any(item is None for item in snapshots):
            return None
        for item in snapshots:
            wake_runtime_ownership(self.controller, item)
        return snapshots

    async def _ownership_snapshot(self, generation: _CodexGeneration):
        snapshots = await self._ownership_snapshots((generation,))
        return snapshots[0] if snapshots else None

    async def runtime_ownership_snapshots(self) -> tuple[Any, ...] | None:
        return await self._ownership_snapshots(self._live_generations())

    def _attach_runtime_activation(
        self,
        runtime: _CodexRuntime,
    ) -> RuntimeActivationIdentity | None:
        if not getattr(self, "_registered_runtime", True):
            return None
        registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
        if registry is None:
            return None
        return registry.attach(self.name, self._generation_resource_key(runtime))

    def runtime_activation_identity_for_request(
        self,
        request: Any,
    ) -> RuntimeActivationIdentity | None:
        base_session_id = str(getattr(request, "base_session_id", "") or "").strip()
        if base_session_id:
            generation = self._generation_for_session(base_session_id)
            if generation is not None:
                return generation.runtime.activation
        cwd = str(getattr(request, "working_path", "") or "").strip()
        if not cwd:
            metadata = getattr(request, "metadata", None)
            if isinstance(metadata, dict):
                cwd = str(metadata.get("session_workdir") or "").strip()
        if cwd:
            generation = self._current_generation(cwd)
            return generation.runtime.activation if generation is not None else None

        session_key = str(getattr(request, "session_key", "") or "").strip()
        if not session_key:
            return None
        get_sessions = getattr(self._session_mgr, "get_sessions_by_session_key", None)
        get_cwd = getattr(self._session_mgr, "get_cwd", None)
        if not callable(get_sessions) or not callable(get_cwd):
            raise ValueError("Codex Session route mapping is unavailable")

        live_identities: dict[int, RuntimeActivationIdentity] = {}
        for base_session_id in get_sessions(session_key):
            generation = self._generation_for_session(base_session_id)
            if generation is None:
                mapped_cwd = str(get_cwd(base_session_id) or "").strip()
                generation = self._current_generation(mapped_cwd) if mapped_cwd else None
            if generation is None:
                continue
            identity = generation.runtime.activation
            if identity is None:
                raise ValueError("Codex runtime generation is not attached")
            live_identities[id(generation)] = identity

        if len(live_identities) > 1:
            raise ValueError("multiple live Codex runtime resources match Session route")
        return next(iter(live_identities.values()), None)

    def runtime_activation_identity_for_session_binding(
        self,
        *,
        session_anchor: str,
        workdir: str | None,
    ) -> RuntimeActivationIdentity | None:
        normalized_anchor = str(session_anchor or "").strip()
        normalized_workdir = str(workdir or "").strip()
        if not normalized_anchor or not normalized_workdir:
            return None
        bound_workdir = str(self._session_mgr.get_cwd(normalized_anchor) or "").strip()
        if bound_workdir and bound_workdir != normalized_workdir:
            raise ValueError("Codex Session binding changed workdir")
        generation = self._generation_for_session(normalized_anchor)
        if generation is None:
            generation = self._current_generation(normalized_workdir)
        if generation is None:
            return None
        identity = generation.runtime.activation
        if identity is None:
            raise ValueError("Codex runtime generation is not attached")
        return identity

    def record_runtime_turn_start(
        self,
        *,
        runtime_key: str,
        request: AgentRequest | None,
    ) -> None:
        if request is None:
            return
        generation = self._generation_for_session(request.base_session_id)
        if generation is not None:
            self._touch_runtime(generation.runtime)
        self._touch_session_activity(request.base_session_id)

    # ------------------------------------------------------------------
    # Runtime generations
    # ------------------------------------------------------------------

    def _unit(self, cwd: str) -> RuntimeGenerationSet[CodexLaunchSpec, _CodexRuntime]:
        unit = self._units.get(cwd)
        if unit is None:
            if self._shutting_down:
                # Every existing unit already refuses admission; a new
                # directory must not start a process nobody would stop.
                raise RuntimeUnitStopping("runtime unit is stopping")
            unit = RuntimeGenerationSet(
                start=self._start_generation,
                stop=self._stop_generation,
            )
            self._units[cwd] = unit
        return unit

    def _current_generation(self, cwd: str) -> _CodexGeneration | None:
        unit = self._units.get(cwd)
        return unit.current if unit is not None else None

    def _live_generations(self) -> tuple[_CodexGeneration, ...]:
        return tuple(
            generation
            for unit in self._units.values()
            for generation in unit.generations
        )

    def _generation_for_session(self, base_session_id: str) -> _CodexGeneration | None:
        """The live generation holding this Session's thread, if any."""
        generation = self._session_generations.get(base_session_id)
        if generation is not None and generation.runtime.ended:
            self._forget_stale_session(base_session_id)
            return None
        return generation

    def _generation_holding_thread(self, thread_id: str) -> _CodexGeneration | None:
        """The live generation that has ``thread_id`` loaded, if any."""
        for generation in self._session_generations.values():
            if not generation.runtime.ended and thread_id in generation.runtime.threads.values():
                return generation
        return None

    def transport_for_session(self, base_session_id: str) -> CodexTransport | None:
        """The app-server process serving this Session's thread, if one is live.

        A pure read: the Running Agents snapshot calls it from a worker thread.
        """
        generation = self._session_generations.get(base_session_id)
        if generation is None or generation.runtime.ended:
            return None
        return generation.runtime.transport

    def _generation_running(self, transport: CodexTransport) -> _CodexGeneration | None:
        """The attached generation whose process is ``transport``."""
        for unit in self._units.values():
            for generation in unit.generations:
                if generation.runtime.transport is transport:
                    return generation
        return None

    def _bind_session_thread(
        self,
        generation: _CodexGeneration,
        base_session_id: str,
        thread_id: str,
    ) -> None:
        previous = self._session_generations.get(base_session_id)
        if previous is not None and previous is not generation:
            previous.runtime.threads.pop(base_session_id, None)
        generation.runtime.threads[base_session_id] = thread_id
        self._session_generations[base_session_id] = generation

    def _unbind_session(self, base_session_id: str, runtime: _CodexRuntime) -> None:
        """Drop a Session's binding to ``runtime``; its next turn resumes the thread.

        Only two owners may drop a live binding: the process ending
        (``_forget_runtime_sessions``) and the thread being released
        (``_release_session_thread``). Codex lets one process at a time hold a
        thread, so forgetting a binding any other way would strand the thread's
        writer lock in a process Avibe no longer routes to.
        """
        runtime.threads.pop(base_session_id, None)
        generation = self._session_generations.get(base_session_id)
        if generation is not None and generation.runtime is not runtime:
            return
        self._session_generations.pop(base_session_id, None)
        self._session_mgr.invalidate_thread(base_session_id)
        self._clear_thread_developer_instructions(base_session_id)

    def _forget_stale_session(self, base_session_id: str) -> bool:
        """Forget a Session's thread id when no live process holds that thread.

        A Session still bound to a live process is left alone and False is
        returned: only that process ending or the thread's release unbinds it.
        """
        generation = self._session_generations.get(base_session_id)
        if generation is not None and not generation.runtime.ended:
            return False
        if generation is not None:
            self._unbind_session(base_session_id, generation.runtime)
            return True
        self._session_mgr.invalidate_thread(base_session_id)
        self._clear_thread_developer_instructions(base_session_id)
        return True

    def _session_has_turn(self, base_session_id: str) -> bool:
        if self._turn_registry.get_active_turn(base_session_id):
            return True
        has_pending_turn_start = getattr(self._turn_registry, "has_pending_turn_start", None)
        return bool(callable(has_pending_turn_start) and has_pending_turn_start(base_session_id))

    def _launch_inputs(self, cwd: str, *, hub_config: Any = None) -> _LaunchInputs:
        """Read every mutable launch input for ``cwd`` in one step, with no await.

        The directory is created first, so its inode is the one the process
        will run in.
        """
        os.makedirs(cwd, exist_ok=True)
        codex_config = self.codex_config
        binary = codex_config.binary
        env = dict(self._codex_runtime_environment())
        codex_home = env.get("CODEX_HOME") or os.path.join(
            env.get("HOME") or os.path.expanduser("~"),
            ".codex",
        )
        return _LaunchInputs(
            hub_config=hub_config,
            binary=binary,
            extra_args=tuple(codex_config.extra_args),
            env=env,
            identity={
                "epoch": self._runtime_epoch,
                "binary": self._binary_identity(binary, env),
                "credential": codex_credential_identity(Path(codex_home).expanduser()),
                # A directory deleted and re-created under the same path leaves
                # a running app-server in a dead inode (#561).
                "cwd": [cwd, self._cwd_inode(cwd)],
            },
        )

    @staticmethod
    def _launch_spec_digest(
        inputs: _LaunchInputs,
        *,
        args: Sequence[str],
        env: Mapping[str, str],
        catalog_path: str | None = None,
    ) -> str:
        identity = {
            **inputs.identity,
            "argv": [*args, *inputs.extra_args],
            "catalog": catalog_path,
            # Digest only: the environment carries the Hub gateway token.
            "env": hashlib.sha256(
                json.dumps(sorted(env.items()), separators=(",", ":")).encode()
            ).hexdigest(),
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _binary_identity(binary: str, env: Mapping[str, str]) -> dict[str, Any]:
        """The executable a launch would run; any reinstall changes its stat."""
        resolved = shutil.which(binary, path=env.get("PATH")) or binary
        real = os.path.realpath(resolved)
        try:
            st = os.stat(real)
        except OSError:
            return {"configured": binary, "resolved": real, "stat": None}
        return {
            "configured": binary,
            "resolved": real,
            "stat": [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns],
        }

    async def _launch_spec(
        self,
        cwd: str,
        launch: "ModelHubLaunch | None" = None,
        *,
        inputs: _LaunchInputs | None = None,
    ) -> CodexLaunchSpec:
        """Every process-level input an app-server for ``cwd`` needs, from one load.

        ``inputs`` is the turn's admission snapshot. A caller outside a turn
        passes none, and one is taken here before anything awaits.
        """
        if inputs is None:
            inputs = self._launch_inputs(cwd)
        env = dict(inputs.env)
        args: list[str] = []
        catalog: CodexHubCatalog | None = None
        if launch is not None and launch.channel == "hub":
            from modules.agents.model_hub import build_codex_hub_launch

            catalog = (await self.prepare_model_hub_runtime(inputs.hub_config, inputs=inputs)).retain()
            try:
                args, hub_env = build_codex_hub_launch(
                    [],
                    env,
                    launch,
                    model_catalog_path=catalog.path,
                )
            except BaseException:
                catalog.close()
                raise
            # A launch without Hub settings keeps the managed environment.
            if hub_env is not None:
                env = hub_env
        digest = self._launch_spec_digest(
            inputs,
            args=args,
            env=env,
            catalog_path=str(catalog.path) if catalog is not None else None,
        )
        return CodexLaunchSpec(
            digest=digest,
            cwd=cwd,
            binary=inputs.binary,
            args=tuple(args),
            extra_args=inputs.extra_args,
            env=env,
            hub=catalog is not None,
            catalog=catalog,
        )

    async def _acquire_generation(
        self,
        cwd: str,
        launch: "ModelHubLaunch | None" = None,
        *,
        inputs: _LaunchInputs | None = None,
    ) -> RuntimeBinding[CodexLaunchSpec, _CodexRuntime]:
        """Bind a new turn to the generation serving this turn's launch spec.

        A turn passes its admission snapshot as ``inputs``; work outside a
        turn passes none, and one is taken here before anything awaits.
        """
        if inputs is None:
            inputs = self._launch_inputs(cwd)
        unit = self._unit(cwd)
        await self._retire_unusable_generations(unit)
        spec = await self._launch_spec(cwd, launch, inputs=inputs)
        try:
            binding = await unit.acquire(spec)
            if not binding.generation.runtime.transport.is_initialized:
                # It became unusable while the spec was prepared.
                await binding.release()
                await self._retire_unusable_generations(unit)
                binding = await unit.acquire(spec)
            return binding
        finally:
            spec.close()

    async def _bind_any_generation(self, cwd: str) -> RuntimeBinding[CodexLaunchSpec, _CodexRuntime]:
        """Bind read-only work to any live generation, starting one only if none is."""
        unit = self._unit(cwd)
        for generation in reversed(unit.generations):
            if generation.runtime.transport.is_initialized:
                try:
                    return await unit.bind(generation)
                except RuntimeError:
                    continue
        return await self._acquire_generation(cwd)

    async def _retire_unusable_generations(
        self,
        unit: RuntimeGenerationSet[CodexLaunchSpec, _CodexRuntime],
    ) -> None:
        """New turns must not bind to a process that can no longer serve.

        Admission may promote a retiring generation whose spec matches, so
        every attached generation is checked, not only the current one.
        """
        for generation in unit.generations:
            if generation.closed or generation.runtime.transport.is_initialized:
                continue
            # Exited, or alive but unusable after a request timed out. New
            # turns get a fresh process; this one stops once nothing needs it,
            # which for an exited process is as soon as no durable owner
            # outlives it.
            logger.warning(
                "Retiring unusable Codex app-server generation %s for cwd=%s",
                generation.runtime.serial,
                generation.runtime.cwd,
            )
            await unit.retire(generation)

    async def _start_generation(self, spec: CodexLaunchSpec) -> _CodexRuntime:
        transport = CodexTransport(
            binary=spec.binary,
            cwd=spec.cwd,
            extra_args=list(spec.extra_args),
            runtime_args=list(spec.args),
            runtime_env=dict(spec.env),
            model_hub_catalog=spec.catalog,
        )
        runtime = _CodexRuntime(
            cwd=spec.cwd,
            serial=next(self._generation_serials),
            transport=transport,
            hub=spec.hub,
        )
        transport.on_notification(
            lambda method, params: self._on_notification(method, params, runtime=runtime)
        )
        transport.on_server_request(
            lambda req_id, method, params: self._on_server_request(
                runtime.cwd, req_id, method, params
            )
        )
        # Tracked from before the spawn, so a starting Hub child keeps the
        # directory's gateway credential and a failed start is still owned.
        self._runtimes.setdefault(runtime.cwd, set()).add(runtime)
        try:
            await transport.start()
            governor_from_controller(self.controller).apply_to_pid(
                getattr(transport, "pid", None),
                label="codex app-server",
            )
            runtime.activation = self._attach_runtime_activation(runtime)
        except BaseException:
            # No generation will hold this process, whatever failed: a start
            # whose own cleanup failed, or setup after a successful spawn.
            if self._process_exited(transport):
                runtime.ended = True
                self._runtimes.get(runtime.cwd, set()).discard(runtime)
                self._retire_hub_scope_after(runtime)
            else:
                # The sweep, or shutdown, stops it.
                runtime.orphaned = True
            raise
        logger.info(
            "Started Codex app-server generation %s for cwd=%s",
            runtime.serial,
            runtime.cwd,
        )
        return runtime

    async def _generation_drained(self, generation: _CodexGeneration) -> bool:
        """Whether no turn and no durable owner still needs this process."""
        runtime = generation.runtime
        # A turn registered on an exited process can no longer progress there.
        # A closed reader alone is not exit: the child may still be running it.
        dead = self._process_exited(runtime.transport)
        if not dead and any(
            self._session_has_turn(base_session_id) for base_session_id in runtime.threads
        ):
            return False
        if not dead:
            # An Activity this process started keeps it, though no Session
            # binding names it after its turn ended or its Session moved.
            service = getattr(getattr(self, "controller", None), "agent_service", None)
            holds = getattr(service, "activation_has_activities", None)
            if callable(holds) and holds(self.name, runtime.activation):
                return False
        ownership = await self._ownership_snapshot(generation)
        if ownership is None:
            return False
        if dead:
            return not ownership.blocks_dead_transport_replacement
        return not ownership.blocks_transport_replacement

    async def _stop_generation(self, generation: _CodexGeneration, force: bool) -> bool:
        """Stop one generation the shared core has detached from its unit.

        A graceful stop declines while a turn or a durable owner still needs
        the process; the core keeps it and asks again later. A forced stop
        settles that work with the runtime-update notice and ends the process.
        A failure propagates, so the core retries and the Sessions stay bound
        until the process is really gone.
        """
        runtime = generation.runtime
        # A child whose reader closed can never report its work again, so it
        # is ended like a forced stop instead of waiting for that work.
        broken = not self._process_exited(runtime.transport) and not getattr(runtime.transport, "is_alive", True)
        kill = force or broken
        if not kill and not await self._generation_drained(generation):
            return False
        # Every adapter-initiated kill settles its bound work through
        # ``_end_bound_work`` with the runtime-update notice. A shutdown
        # settles with its own reason: a disable's, or none at service
        # shutdown, whose restart recovery reports interrupted work.
        if not kill:
            settle_reason = None
        elif self._shutting_down:
            settle_reason = self._shutdown_settle_reason
        else:
            settle_reason = SETTLED_BY_BACKEND_REFRESH
        stopped = await self._stop_runtime(
            runtime,
            # The decision is repeated inside the fence, where no owner can
            # commit to this generation any longer.
            still_drained=None if kill else lambda: self._generation_drained(generation),
            settle_reason=settle_reason,
        )
        if not stopped:
            return False
        self._forget_runtime_sessions(runtime)
        logger.info(
            "Stopped Codex app-server generation %s for cwd=%s%s",
            runtime.serial,
            runtime.cwd,
            " (forced)" if force else "",
        )
        return True

    async def _end_bound_work(self, runtime: _CodexRuntime, reason: str) -> None:
        """Settle a force-stopped generation's work with ``reason``'s notice.

        Its bound Sessions' registered turns and every Activity started under
        its activation settle here. An Activity can outlive its foreground turn
        or its Session's thread move and still keep this process owned, so
        Activities are found by activation, never by Session. A turn still
        starting holds the unit's binding inside ``handle_message`` and fails
        or retries on its own once its process is gone. Only this agent's work
        settles: after a re-enable, another agent can run the same Session in
        the same directory. Settling again finds nothing left, so a retried
        teardown may call this freely.
        """
        sessions = set(runtime.threads)
        service = getattr(getattr(self, "controller", None), "agent_service", None)
        # A turn whose gate a later turn of its Session already took was settled
        # when that gate was released, for example by the liveness monitor after
        # the reader closed. Settling its Session now would cancel the later turn.
        still_owns_gate = getattr(service, "emit_matches_runtime_turn", lambda _context: True)
        busy = set()
        for base_session_id in sessions:
            turn_id = self._turn_registry.get_active_turn(base_session_id)
            request = self._turn_registry.get_request_for_turn(turn_id) if turn_id else None
            if turn_id and (request is None or still_owns_gate(request.context)):
                busy.add(base_session_id)
        logger.warning(
            "Force-stopping Codex app-server generation %s for cwd=%s with %d running turn(s)",
            runtime.serial,
            runtime.cwd,
            len(busy),
        )
        end_work = getattr(service, "force_end_runtime_work", None)
        if callable(end_work):
            await end_work(
                self.name,
                base_session_ids=busy,
                activation_identities={runtime.activation},
                reason=reason,
                agent=self,
            )

    async def _stop_runtime(
        self,
        runtime: _CodexRuntime,
        *,
        still_drained: Callable[[], Awaitable[bool]] | None = None,
        settle_reason: str | None = None,
        require_process_exit: bool = False,
    ) -> bool:
        """Tear one process down inside its activation fence.

        The retirement is reserved before anything awaits, so no durable owner
        commits to the generation while its final drained check runs, its bound
        work settles (with ``settle_reason``), or its process stops. A failed check or
        teardown aborts the reservation. Returns False when ``still_drained``
        declined.
        """
        registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
        reservation = (
            registry.reserve_retirement(runtime.activation)
            if registry is not None and runtime.activation is not None
            else None
        )
        transport = runtime.transport
        process = getattr(transport, "_process", None)
        # A Session moving off this process meanwhile waits for the teardown,
        # so its next turn cannot be among the work this teardown settles.
        teardown = runtime.teardown = asyncio.get_running_loop().create_future()
        try:
            if still_drained is not None and not await still_drained():
                if reservation is not None:
                    registry.finish_retirement(reservation, retire=False)
                return False
            if settle_reason is not None:
                await self._end_bound_work(runtime, settle_reason)
            await transport.stop()
            if require_process_exit and process is not None and process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=5)
                if process.returncode is None:
                    raise RuntimeError("Codex native process did not exit")
        except BaseException:
            if reservation is not None:
                registry.finish_retirement(reservation, retire=False)
            raise
        finally:
            runtime.teardown = None
            teardown.set_result(None)
        runtime.ended = True
        runtime.orphaned = False
        self._runtimes.get(runtime.cwd, set()).discard(runtime)
        if reservation is not None:
            registry.finish_retirement(reservation, retire=True)
        return True

    def _forget_runtime_sessions(self, runtime: _CodexRuntime) -> None:
        """Unbind every Session of a stopped generation and release its Hub scope."""
        for base_session_id in list(runtime.threads):
            self._unbind_session(base_session_id, runtime)
            self._turn_registry.clear_session(base_session_id)
        self._retire_hub_scope_after(runtime)

    def _retire_hub_scope_after(self, runtime: _CodexRuntime) -> None:
        """Revoke the directory's gateway credential once its last Hub process is gone.

        Every Hub generation of a directory, a starting one included, shares
        one request-scoped credential; only the last one may revoke it.
        """
        if runtime.hub and not any(other.hub for other in self._runtimes.get(runtime.cwd, ())):
            self._retire_model_hub_process_scope(runtime.cwd)

    def _generation_has_live_work(self, generation: _CodexGeneration) -> bool:
        runtime = generation.runtime
        # A turn registered on an exited process can no longer progress there.
        return bool(generation.bindings) or (
            not self._process_exited(runtime.transport)
            and any(self._session_has_turn(base_session_id) for base_session_id in runtime.threads)
        )

    async def _stop_generations_now(
        self,
        unit: RuntimeGenerationSet[CodexLaunchSpec, _CodexRuntime],
        generations: Sequence[_CodexGeneration],
        *,
        require_process_exit: bool = False,
        settle_reason: str | None,
    ) -> None:
        """Stop generations outside the core's own decisions.

        Every generation is detached before the first stop awaits, so a turn
        arriving meanwhile starts its own generation instead of binding to one
        about to be killed. With ``settle_reason``, each generation's bound
        work is settled through ``_end_bound_work`` inside its activation
        fence; callers pass None only when that work was already settled
        upstream.
        A process whose stop fails, or that a cancellation left unstopped, is
        adopted back as retiring with its Sessions still bound, so a later
        call or sweep can retry.
        """
        pending = list(generations)
        for generation in pending:
            await unit.discard(generation)  # synchronous bookkeeping
        failure: BaseException | None = None
        try:
            while pending:
                generation = pending[0]
                try:
                    await self._stop_runtime(
                        generation.runtime, settle_reason=settle_reason, require_process_exit=require_process_exit
                    )
                except Exception as exc:
                    failure = failure or exc
                    await self._readopt(unit, generation)
                else:
                    self._forget_runtime_sessions(generation.runtime)
                pending.pop(0)
        finally:
            for generation in pending:
                await self._readopt(unit, generation)
        if failure is not None:
            raise failure

    async def _readopt(
        self,
        unit: RuntimeGenerationSet[CodexLaunchSpec, _CodexRuntime],
        generation: _CodexGeneration,
    ) -> None:
        """Attach a detached generation whose process still runs again, as retiring."""
        runtime = generation.runtime
        if runtime.ended:
            return
        try:
            restored = await unit.adopt(generation.spec, runtime, current=False)
        except RuntimeUnitStopping:
            return  # Shutdown ends it through ``_runtimes``.
        for base_session_id, bound in list(self._session_generations.items()):
            if bound is generation:
                self._session_generations[base_session_id] = restored

    async def _end_unattached_runtimes(
        self,
        *,
        require_process_exit: bool = False,
        settle_reason: str | None = None,
    ) -> None:
        """End every process this Agent still owns that no unit holds any longer.

        Migration and the exclusive refresh have already settled the backend's
        work. A shutdown passes its own reason: a disable's, or none.
        """
        failure: BaseException | None = None
        for runtimes in list(self._runtimes.values()):
            for runtime in list(runtimes):
                try:
                    await self._stop_runtime(
                        runtime, require_process_exit=require_process_exit, settle_reason=settle_reason
                    )
                except Exception as exc:
                    failure = failure or exc
                    continue
                self._forget_runtime_sessions(runtime)
        if failure is not None:
            raise failure

    def _schedule_reap(self, cwd: str) -> None:
        """Stop the directory's drained retiring generations without blocking the caller."""
        unit = self._units.get(cwd)
        if unit is None or not any(generation.retiring for generation in unit.generations):
            return
        task = asyncio.create_task(unit.reap())
        self._reap_tasks.add(task)
        task.add_done_callback(self._reap_done)

    def _reap_done(self, task: asyncio.Task[None]) -> None:
        self._reap_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Codex runtime generation reap failed", exc_info=task.exception())

    async def reap_runtime_generations(self) -> None:
        """Stop every retiring generation whose bound work has drained.

        The sweep also retires processes that exited or stopped answering, so
        an idle directory's dead process and its Hub scope do not linger, and
        retries the stop of every process no unit holds.
        """
        for unit in list(self._units.values()):
            await self._retire_unusable_generations(unit)
            await unit.reap()
        for runtimes in list(self._runtimes.values()):
            for runtime in [runtime for runtime in runtimes if runtime.orphaned]:
                try:
                    await self._stop_runtime(runtime)
                except Exception:
                    logger.warning("Failed to stop a Codex app-server no generation holds", exc_info=True)
                    continue
                self._forget_runtime_sessions(runtime)

    async def _move_session_to(self, generation: _CodexGeneration, request: AgentRequest) -> None:
        """Make sure this Session's thread can be loaded in ``generation``."""
        base_session_id = request.base_session_id
        bound = self._generation_for_session(base_session_id)
        if bound is generation:
            return
        if bound is not None:
            logger.info(
                "Moving Codex session %s from app-server generation %s to %s for cwd=%s",
                base_session_id,
                bound.runtime.serial,
                generation.runtime.serial,
                generation.runtime.cwd,
            )
            await self._release_session_thread(bound, base_session_id)
        else:
            # Not loaded anywhere: forget any stale thread id so the next step resumes it.
            self._forget_stale_session(base_session_id)
        self._turn_registry.clear_session(base_session_id)

    async def _release_session_thread(self, generation: _CodexGeneration, base_session_id: str) -> None:
        """Have ``generation`` unload this Session's thread so another can resume it.

        Codex lets only one process at a time hold a thread's writer lock. This
        returns only on proof that the thread is released: ``thread/closed``
        from that process, Codex reporting it not loaded there, or the process
        having exited. Anything less raises
        ``CodexThreadReleaseUnavailableError`` and leaves the binding in place.
        """
        runtime = generation.runtime
        thread_id = runtime.threads.get(base_session_id) or self._session_mgr.get_thread_id(base_session_id)
        transport = runtime.transport
        teardown = runtime.teardown
        if teardown is not None:
            # A teardown is settling this process's work and then ends it;
            # its exit releases the thread.
            try:
                await asyncio.wait_for(asyncio.shield(teardown), _THREAD_RELEASE_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise CodexThreadReleaseUnavailableError(
                    f"Codex app-server generation {runtime.serial} is still stopping"
                ) from exc
            bound = self._generation_for_session(base_session_id)
            if bound is None or bound.runtime is not runtime:
                return  # The stop unbound it.
            # The teardown declined or failed, and adopting a survivor back
            # rebinds the Session to the same process: release it there.
        if thread_id and not runtime.ended and not self._process_exited(transport):
            active_turn = self._turn_registry.get_active_turn(base_session_id)
            if active_turn:
                await self._interrupt_turn_before_move(transport, base_session_id, thread_id, active_turn)
            released = asyncio.Event()
            runtime.released_threads[thread_id] = released
            try:
                await self._await_thread_release(transport, thread_id, released)
            finally:
                runtime.released_threads.pop(thread_id, None)
        self._unbind_session(base_session_id, runtime)
        self._schedule_reap(runtime.cwd)

    @staticmethod
    def _process_exited(transport: CodexTransport) -> bool:
        """Whether the app-server process is gone.

        A closed stdout reader is no proof: the child can outlive it and keep
        every thread's writer lock.
        """
        process = getattr(transport, "_process", None)
        if process is None:
            return not getattr(transport, "is_alive", True)
        return process.returncode is not None

    async def _await_thread_release(
        self,
        transport: CodexTransport,
        thread_id: str,
        released: asyncio.Event,
    ) -> None:
        unavailable = CodexThreadReleaseUnavailableError(f"Codex app-server did not release thread {thread_id}")
        try:
            response = await transport.send_request("thread/unsubscribe", {"threadId": thread_id})
        except ConnectionError:
            # The reader is gone, so only the process exit can release the thread.
            response = None
        except (CodexRPCError, TimeoutError) as exc:
            raise unavailable from exc
        if self._process_exited(transport) or (response is not None and response.get("status") == "notLoaded"):
            return
        process = getattr(transport, "_process", None)
        waits = {asyncio.create_task(released.wait())}
        if process is not None and process.returncode is None:
            waits.add(asyncio.create_task(process.wait()))
        try:
            await asyncio.wait(waits, timeout=_THREAD_RELEASE_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        if released.is_set() or self._process_exited(transport):
            return
        if response is None or not transport.is_alive:
            raise unavailable
        try:
            loaded = await transport.send_request("thread/loaded/list", {})
        except Exception as exc:  # noqa: BLE001 - unknown is not released
            if self._process_exited(transport):
                return
            raise unavailable from exc
        if thread_id in (loaded.get("data") or ()):
            raise unavailable

    async def _interrupt_turn_before_move(
        self,
        transport: CodexTransport,
        base_session_id: str,
        thread_id: str,
        active_turn: str,
    ) -> None:
        """Interrupt a Session's own running turn before its thread moves.

        Unsubscribing ends this connection's view of the turn, so the turn is
        settled here, and only once Codex accepted the interrupt or the turn or
        its process already ended. A refused interrupt changes nothing.
        """
        try:
            await transport.send_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": active_turn},
            )
        except Exception as exc:
            if self._turn_registry.get_active_turn(base_session_id) == active_turn and not self._process_exited(
                transport
            ):
                raise CodexThreadReleaseUnavailableError(
                    f"Codex app-server did not interrupt turn {active_turn} before moving thread {thread_id}"
                ) from exc
        interrupted_request = self._event_handler.clear_pending(active_turn)
        if interrupted_request:
            await self._remove_ack_reaction(interrupted_request)
            # Its completion never arrives here. Release is token-guarded, so
            # it cannot close the new turn.
            self._event_handler._release_stream_turn(interrupted_request.context)
        self._turn_registry.clear_session(base_session_id)

    async def _open_session_thread(
        self,
        generation: _CodexGeneration,
        request: AgentRequest,
        *,
        developer_instructions: Optional[str] = None,
    ) -> str:
        """Start, fork, or resume the Session's thread in ``generation``."""
        try:
            return await self._start_or_resume_thread(
                generation.runtime.transport,
                request,
                developer_instructions=developer_instructions,
            )
        finally:
            # A thread may be loaded even when a later step failed.
            thread_id = self._session_mgr.get_thread_id(request.base_session_id)
            if thread_id:
                self._bind_session_thread(generation, request.base_session_id, thread_id)

    def _stuck_active_sessions(
        self,
        runtime: _CodexRuntime,
        *,
        now: float,
        cap: float | None,
    ) -> list[str]:
        """The Sessions on ``runtime`` whose turn made no progress within ``cap``."""
        if cap is None:
            return []
        stuck = []
        for base_session_id in list(runtime.threads):
            if not self._turn_registry.get_active_turn(base_session_id):
                continue
            last_progress = self._session_last_activity.get(base_session_id)
            if last_progress is not None and now - last_progress >= cap:
                stuck.append(base_session_id)
        return stuck

    def _has_active_turns_on(self, runtime: _CodexRuntime) -> bool:
        return any(self._session_has_turn(base_session_id) for base_session_id in list(runtime.threads))

    async def _settle_stuck_sessions(self, generation: _CodexGeneration, stuck_sessions: Sequence[str]) -> bool:
        """Settle each stuck turn still held by ``generation``; True if any was.

        Only the exact stuck turn is forgotten: one that a new admission
        replaced meanwhile, or that moved to another generation, is left alone.
        """
        runtime = generation.runtime
        settled = False
        for base_session_id in stuck_sessions:
            turn_id = self._turn_registry.get_active_turn(base_session_id)
            if not turn_id or self._session_generations.get(base_session_id) is not generation:
                continue
            logger.warning(
                "Settling stuck-active Codex session %s for cwd=%s after exact progress timeout",
                base_session_id,
                runtime.cwd,
            )
            await self._settle_stuck_active_request(base_session_id)
            # Not held across the terminal emission, which may admit the next turn.
            async with self.session_lifecycle(base_session_id):
                if self._turn_registry.get_active_turn(base_session_id) == turn_id:
                    self._turn_registry.clear_session(base_session_id)
                    self._session_last_activity.pop(base_session_id, None)
                    settled = True
        return settled

    async def evict_idle_transports(self, idle_timeout: float) -> int:
        """Retire idle app-servers and repair stuck turns on every generation.

        Each generation is judged only by the Sessions whose threads it holds.
        A directory's current generation retires once it stays idle; a
        retiring one whose turn is stuck past the age backstop has that turn
        settled, so it can drain. Two exact ownership snapshots gate each
        decision, and the shared core stops a generation only if no turn bound
        to it meanwhile.
        """
        if idle_timeout <= 0:
            return 0
        stuck_active_cap = self._stuck_active_idle_eviction_cap(idle_timeout)
        now = time.monotonic()
        evicted = 0
        candidates = tuple(
            generation
            for unit in list(self._units.values())
            for generation in unit.generations
            if generation is unit.current
            or self._stuck_active_sessions(generation.runtime, now=now, cap=stuck_active_cap)
        )
        initial_snapshots = await self._ownership_snapshots(candidates)
        if initial_snapshots is None:
            return 0

        for generation, ownership in zip(candidates, initial_snapshots, strict=True):
            runtime = generation.runtime
            unit = self._units.get(runtime.cwd)
            if unit is None or generation not in unit.generations:
                continue
            current = unit.current is generation
            stuck_sessions = self._stuck_active_sessions(runtime, now=now, cap=stuck_active_cap)
            idle_for = now - runtime.last_activity
            ordinary_candidate = (
                current
                and not ownership.blocks_reclamation
                and not generation.bindings
                and not self._has_active_turns_on(runtime)
                and idle_for >= idle_timeout
            )
            stuck_candidate = bool(stuck_sessions) and not ownership.blocks_reclamation
            if not ordinary_candidate and not stuck_candidate:
                continue

            ownership = await self._ownership_snapshot(generation)
            moved = generation not in unit.generations or (unit.current is generation) != current
            if ownership is None or ownership.blocks_reclamation or moved:
                continue
            # Silence cannot revoke a durable Turn or Activity owner. The age
            # backstop only repairs stale adapter-local flags once durable
            # ownership independently allows reclamation.
            current_now = time.monotonic()
            stuck_sessions = self._stuck_active_sessions(runtime, now=current_now, cap=stuck_active_cap)
            if await self._settle_stuck_sessions(generation, stuck_sessions):
                # A settled turn may still run natively. It ends with its
                # process, which stops once nothing else needs it.
                await unit.retire(generation)
                await unit.reap()
                evicted += int(runtime.ended)
                continue
            if not current:
                continue
            idle_for = time.monotonic() - runtime.last_activity
            if (
                unit.current is not generation
                or self._has_active_turns_on(runtime)
                or idle_for < idle_timeout
            ):
                continue

            sessions = list(self._session_mgr.sessions_for_cwd(runtime.cwd))
            await unit.retire(generation)
            await unit.settled()
            if not runtime.ended:
                # Work bound meanwhile; the generation stops once it drains.
                continue
            logger.info(
                "Evicting idle Codex transport for cwd=%s after %.1fs idle",
                runtime.cwd,
                idle_for,
            )
            # The stop unbound every Session it served. One that bound to
            # another generation meanwhile keeps its state.
            for base_session_id in sessions:
                if self._session_generations.get(base_session_id) is None:
                    self._forget_session_lock(base_session_id)
                    self._session_last_activity.pop(base_session_id, None)
            evicted += 1

        return evicted

    def _hub_process_scope(self, cwd: str) -> str:
        """The gateway credential scope of this agent's Hub processes in ``cwd``.

        A disable whose teardown failed keeps the old agent's processes until
        a retry stops them, while a re-enabled agent already runs its own in
        the same directory. Each agent owns its scope, so the old one revoking
        it with its last Hub process never revokes the new one's credential.
        """
        return f"{cwd}#{self._instance_serial}"

    def _retire_model_hub_process_scope(self, cwd: str) -> None:
        if not getattr(self, "_registered_runtime", True):
            return
        controller = getattr(self, "controller", None)
        router = getattr(controller, "model_hub_runtime", None)
        retire = getattr(router, "retire_process_scope", None)
        if callable(retire):
            retire("codex", self._hub_process_scope(cwd))

    async def _settle_stuck_active_request(self, base_session_id: str) -> None:
        """Settle a turn we are about to force-reap.

        ``_start_turn`` marks the AgentService runtime turn started; it is
        normally settled by a terminal result, which also flips Workbench
        ``agent_status`` out of ``running``. The stuck-active force-eviction path
        has no backend terminal event, so emit a silent error result here. The
        terminal-result path is token-guarded by its owner, so a no-op
        (already-settled or no active turn) is safe.
        """
        get_active = getattr(self._turn_registry, "get_active_turn", None)
        active_turn = get_active(base_session_id) if callable(get_active) else None
        if not active_turn:
            return

        request = None
        get_for_turn = getattr(self._turn_registry, "get_request_for_turn", None)
        if callable(get_for_turn):
            request = get_for_turn(active_turn)
        if request is None:
            get_latest = getattr(self._turn_registry, "get_latest_request", None)
            if callable(get_latest):
                request = get_latest(base_session_id)

        context = getattr(request, "context", None)
        if context is None:
            return
        controller = getattr(self, "controller", None)
        emit = getattr(controller, "emit_agent_message", None)
        if callable(emit):
            try:
                await emit(
                    context,
                    "result",
                    "",
                    is_error=True,
                    level="silent",
                    output=terminal_output_for(request),
                )
                return
            except Exception:
                logger.warning(
                    "Failed to emit silent terminal result for force-evicted Codex turn %s",
                    active_turn,
                    exc_info=True,
                )

        # Best-effort fallback for narrow test doubles or partial controllers:
        # release the runtime gate even if the Workbench status path is absent.
        release = getattr(self._event_handler, "_release_stream_turn", None)
        if callable(release):
            release(context)

    def _stuck_active_idle_eviction_cap(self, idle_timeout: float) -> Optional[float]:
        """Age threshold for repairing an unowned adapter-local active flag.

        Returns ``None`` when the backstop is disabled (multiplier <= 0), in
        which case an active flag remains an absolute veto. Durable ownership
        always vetoes reclamation regardless of this threshold.
        """
        multiplier = DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER
        if multiplier <= 0:
            return None
        floor = max(0.0, float(DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS))
        return max(idle_timeout * multiplier, floor)

    def _is_recoverable_transport_error(self, error: Exception) -> bool:
        if isinstance(error, CodexResponseTooLargeError):
            return False
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True

        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "transport is not available",
                "stdout closed",
                "timed out after 120s",
                # codex resolves configuration against its process cwd at
                # thread/start; a cwd deleted out from under the app-server
                # surfaces as this RPC error (#561). A restart respawns the
                # process in the (re-created) directory.
                "failed to load configuration",
            )
        )

    async def _drop_generation_after_failure(
        self,
        generation: _CodexGeneration,
        request: AgentRequest,
        binding: RuntimeBinding[CodexLaunchSpec, _CodexRuntime],
    ) -> bool:
        """Replace a broken app-server generation when no other work still needs it.

        Retiring is atomic with admission, so no new turn binds to the broken
        process; it stops only through the core's drained check.
        """
        runtime = generation.runtime
        # Stopping the generation forgets every Session it held.
        forgotten_by_stop = request.base_session_id in runtime.threads and not runtime.ended
        if not runtime.ended:
            unit = self._units.get(runtime.cwd)
            await binding.release()  # this turn no longer needs the broken process
            if unit is not None:
                await unit.retire(generation)
                await unit.settled()
            if not runtime.ended:
                logger.warning(
                    "Codex failure recovery cannot replace an owned transport for cwd=%s",
                    runtime.cwd,
                )
                return False
        if not forgotten_by_stop:
            self._forget_stale_session(request.base_session_id)
            self._turn_registry.clear_session(request.base_session_id)
        return True

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------

    def _caller_env_for_request(self, request: AgentRequest) -> dict[str, str]:
        # The typed context carries the CREATION ORIGIN (platform, channel, thread,
        # user, message id) that a Harness definition created by ``vibe task add`` in
        # this turn's shell needs in order to record where it came from. This env is
        # the only hop it can travel: the CLI runs as a subprocess of the Codex shell.
        context = getattr(request, "context", None)
        env = caller_env_for_platform_payload(
            getattr(context, "platform_specific", None),
            message=context,
            # Defensively resolved: this is reached from payload-shaping helpers that
            # are exercised (and legitimately used) without a fully wired controller,
            # and a missing fallback costs an origin platform — never a raised turn.
            fallback_platform=getattr(
                getattr(getattr(self, "controller", None), "config", None), "platform", None
            ),
        )
        env.update(
            managed_skill_environment(
                getattr(request, "working_path", None),
                project_base=managed_skill_project_base(context),
                claude_cli_path=managed_skill_claude_cli_path(
                    getattr(getattr(self, "controller", None), "config", None)
                ),
            )
        )
        return env

    def _caller_env_script_path(self, request: AgentRequest) -> Path:
        caller_env = self._caller_env_for_request(request)
        session_key = (
            str(getattr(request, "base_session_id", "") or "").strip()
            or caller_env.get("AVIBE_SESSION_ID")
            or "session"
        )
        safe_session_id = "".join(
            ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
            for ch in session_key
        )
        return paths.get_runtime_dir() / CODEX_CALLER_ENV_DIR / f"{safe_session_id}.sh"

    def _write_caller_env_script(self, request: AgentRequest) -> Path | None:
        env = self._caller_env_for_request(request)
        if not env:
            return None
        script_path = self._caller_env_script_path(request)
        script_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Generated by Avibe. Sourced by Codex shell commands.\n"]
        for key, value in sorted(env.items()):
            lines.append(f"export {key}={shlex.quote(value)}\n")
        tmp_path = script_path.with_suffix(script_path.suffix + ".tmp")
        tmp_path.write_text("".join(lines), encoding="utf-8")
        tmp_path.replace(script_path)
        return script_path

    def _inject_caller_env_config(
        self,
        params: Dict[str, Any],
        request: AgentRequest,
        process_env: Mapping[str, str],
        *,
        force_path: bool = False,
    ) -> tuple[str, bool]:
        """Set the turn's shell environment on top of ``process_env``.

        ``process_env`` is the environment of the app-server that runs the
        turn, which its shell inherits; see ``_process_environment``.
        """
        from core.git_runtime import prepend_vendored_git_to_path

        env = self._caller_env_for_request(request)
        runtime_env = process_env
        config = dict(params.get("config") or {})
        config["skills.include_instructions"] = False
        params["config"] = config
        shell_policy = dict(config.get("shell_environment_policy") or {})
        set_env = dict(shell_policy.get("set") or {})
        had_path = "PATH" in set_env
        if env:
            env_script_path = self._caller_env_script_path(request)
            set_env.update({**env, "BASH_ENV": str(env_script_path)})
        git_path_changed = prepend_vendored_git_to_path(
            set_env,
            base_env=runtime_env,
            working_dir=getattr(request, "working_path", None),
        )
        git_path_state = set_env["PATH"] if "PATH" in set_env else runtime_env.get("PATH", "")
        path_managed = had_path or git_path_changed or force_path
        if force_path:
            set_env["PATH"] = git_path_state
        if not env and not path_managed:
            return git_path_state, False
        shell_policy["set"] = set_env
        config["shell_environment_policy"] = shell_policy
        params["config"] = config
        return git_path_state, path_managed

    def _git_path_state_for_request(self, request: AgentRequest, process_env: Mapping[str, str]) -> str:
        from core.git_runtime import prepend_vendored_git_to_path

        runtime_env = process_env
        env: dict[str, str] = {}
        prepend_vendored_git_to_path(
            env,
            base_env=runtime_env,
            working_dir=getattr(request, "working_path", None),
        )
        return env["PATH"] if "PATH" in env else runtime_env.get("PATH", "")

    @staticmethod
    def _process_environment(transport: CodexTransport) -> Mapping[str, str]:
        """The environment an app-server runs with, which its shell inherits.

        It comes from the launch the process was started from, never from the
        current configuration, so a save during a turn cannot split them.
        """
        env = getattr(transport, "runtime_env", None)
        return env if env is not None else os.environ

    def _codex_runtime_environment(self) -> Mapping[str, str]:
        base_env = os.environ
        binary = getattr(getattr(self, "codex_config", None), "binary", "codex")
        managed = desktop_backend_subprocess_environment(
            "codex",
            binary,
            base_env,
        )
        return managed if managed is not None else base_env

    async def _start_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        *,
        developer_instructions: Optional[str] = None,
    ) -> str:
        """Create a new Codex thread and return its threadId."""
        params: Dict[str, Any] = {
            "cwd": request.working_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        self.ensure_agent_session_id(request)
        if developer_instructions:
            params["developerInstructions"] = await self._native_thread_prompt(
                transport, request, developer_instructions
            )
        git_path_state, git_path_managed = self._inject_caller_env_config(
            params, request, self._process_environment(transport)
        )

        resp = await transport.send_request("thread/start", params)
        # thread/start returns Thread directly OR may nest under "thread"
        thread_id = resp.get("id", "")
        if not thread_id:
            thread_obj = resp.get("thread")
            if isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id", "")
        if not thread_id:
            raise RuntimeError("Codex thread/start returned no thread id")

        # Persisted first: a thread the durable mapping lacks must not serve a
        # turn, or that conversation would vanish from resume history. A failed
        # write leaves the new, still empty thread unused, and the retry starts
        # another.
        self.bind_agent_session_id(request, thread_id)
        self._session_mgr.set_thread_id(request.base_session_id, thread_id)
        if developer_instructions:
            from core.skill_observability import accept_catalog

            accept_catalog(
                self.controller,
                request.context,
                getattr(request, "skill_catalog_observation", None),
                backend="codex",
            )
            # Only a genuinely new thread can establish its first model-visible
            # prompt from native configuration alone. Resume/fork still carry
            # history, so they must not advance this delivery fingerprint.
            self._remember_thread_developer_instructions(
                request.base_session_id, thread_id, developer_instructions
            )
            self._remember_thread_prompt_strategy(
                request.base_session_id, thread_id, "injected_pending_persist"
            )
            if not hasattr(self, "_thread_unpersisted_prompts"):
                self._thread_unpersisted_prompts = {}
            self._thread_unpersisted_prompts[request.base_session_id] = (
                thread_id,
                developer_instructions,
                "fallback",
            )
            self._repair_unpersisted_prompt_strategy(
                request, thread_id, agent_session_id=self._prompt_state_agent_session_id(request)
            )
        self._remember_thread_model_settings_from_response(
            request.base_session_id,
            thread_id,
            resp,
        )
        self._remember_thread_caller_env_config(
            request.base_session_id,
            thread_id,
            self._caller_env_for_request(request),
        )
        self._remember_thread_git_path_config(
            request.base_session_id,
            thread_id,
            git_path_state,
            git_path_managed,
        )
        return thread_id

    async def _fork_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        fork: dict[str, Any],
        *,
        developer_instructions: Optional[str] = None,
    ) -> str:
        """Fork an existing Codex thread and bind the new thread id."""
        target_agent_session_id = self.ensure_agent_session_id(request)
        (
            source_prompt_strategy,
            source_prompt_sha256,
            source_prompt_instructions,
        ) = self._fork_source_prompt_state(fork)
        if source_prompt_strategy == "injected_pending_persist":
            source_session_id = str(fork.get("source_session_id") or "").strip()
            source_thread_id = str(fork.get("source_native_session_id") or "").strip()
            pending = getattr(self, "_thread_unpersisted_prompts", {}).get(
                source_session_id
            )
            if (
                pending
                and pending[0] == source_thread_id
                and pending[2] in {"fallback", "fallback_pending_clear"}
            ):
                _, source_prompt_instructions, source_prompt_strategy = pending
                source_prompt_sha256 = self._prompt_fingerprint(
                    source_prompt_instructions
                )
            else:
                # This process cannot prove which prompt bytes the source
                # injected, so a fork must not inherit an unrepairable state.
                source_prompt_strategy = "unavailable"
                source_prompt_sha256 = None
                source_prompt_instructions = None
        elif source_prompt_strategy == "fallback_pending_injection":
            # The source injection outcome is ambiguous. The fork already
            # carries whatever native history exists; never append it again.
            source_prompt_strategy = "unavailable"
            source_prompt_sha256 = None
            source_prompt_instructions = None
        elif source_prompt_strategy == "unavailable":
            source_prompt_sha256 = None
            source_prompt_instructions = None
        _, effective_model, _, _ = self._resolve_codex_agent_settings(request)
        source_thread_id = str(fork.get("source_native_session_id") or "").strip()
        params: Dict[str, Any] = {
            "threadId": source_thread_id,
            "excludeTurns": True,
            "cwd": request.working_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if developer_instructions:
            params["developerInstructions"] = await self._native_thread_prompt(
                transport, request, developer_instructions
            )
        elif source_prompt_strategy is None and callable(
            getattr(
                getattr(self, "sessions", None),
                "get_agent_session_runtime_marker",
                None,
            )
        ):
            # Threads created before Turn-bound prompt delivery stored Avibe's
            # prompt as native thread configuration. Do not copy that legacy
            # configuration into a fork alongside the new delivery strategy.
            params["developerInstructions"] = None
        if effective_model:
            params["model"] = effective_model
        git_path_state, git_path_managed = self._inject_caller_env_config(
            params, request, self._process_environment(transport)
        )

        self._mark_fork_correction_pending(request.base_session_id)
        try:
            should_trim = await self._should_trim_forked_running_turn(fork)
            if should_trim:
                source_still_running, last_completed_turn_id = (
                    await self._fork_source_last_completed_turn_id(transport, fork)
                )
                if not last_completed_turn_id:
                    raise CodexForkBoundaryUnavailableError(
                        "Cannot fork Codex thread while the source turn boundary "
                        "is unknown"
                    )
                # `lastTurnId` is the stable thread/fork boundary supported by
                # the Codex app-server versions Avibe supports. It is
                # inclusive: use the preceding terminal turn while the source
                # is active, or the source turn itself if it completed during
                # this race.
                params["lastTurnId"] = last_completed_turn_id
                if not source_still_running:
                    logger.debug(
                        "Codex source turn completed during fork boundary read; "
                        "preserving completed history"
                    )
            resp = await transport.send_request("thread/fork", params)
            thread_id = resp.get("id", "")
            if not thread_id:
                thread_obj = resp.get("thread")
                if isinstance(thread_obj, dict):
                    thread_id = thread_obj.get("id", "")
            if not thread_id:
                raise RuntimeError("Codex thread/fork returned no thread id")

            await self._inject_forked_session_correction(transport, request, thread_id)
        finally:
            self._clear_fork_correction_pending(request.base_session_id)
        # Persisted first, as for a new thread.
        target_agent_session_id = (
            self.bind_agent_session_id(request, thread_id) or target_agent_session_id
        )
        self._session_mgr.set_thread_id(request.base_session_id, thread_id)
        cache_source_prompt_strategy = True
        if source_prompt_instructions is not None:
            persisted_prompt_strategy = self._persist_prompt_strategy(
                request,
                thread_id,
                source_prompt_instructions,
                strategy=source_prompt_strategy,
                agent_session_id=target_agent_session_id,
            )
            if persisted_prompt_strategy:
                self._remember_thread_developer_instructions(
                    request.base_session_id,
                    thread_id,
                    source_prompt_instructions,
                )
        elif source_prompt_sha256 is not None and source_prompt_strategy in {
            "fallback",
            "fallback_pending_clear",
            "fallback_pending_clear_injection",
        }:
            persisted_prompt_strategy = self._persist_prompt_strategy(
                request,
                thread_id,
                None,
                strategy=source_prompt_strategy,
                agent_session_id=target_agent_session_id,
                prompt_sha256=source_prompt_sha256,
            )
            # Let the first target Turn read and compare the carried fingerprint.
            # Caching only the strategy would make unchanged bytes look changed.
            cache_source_prompt_strategy = False
        elif source_prompt_strategy in {
            "collaboration",
            "fallback_pending_clear",
            "unavailable",
        }:
            persisted_prompt_strategy = self._persist_prompt_strategy(
                request,
                thread_id,
                None,
                strategy=source_prompt_strategy,
                agent_session_id=target_agent_session_id,
            )
        else:
            persisted_prompt_strategy = True
        if not persisted_prompt_strategy:
            raise CodexPromptRefreshUnavailableError(
                "Could not persist the forked Codex prompt strategy"
            )
        if source_prompt_strategy and cache_source_prompt_strategy:
            self._remember_thread_prompt_strategy(
                request.base_session_id,
                thread_id,
                source_prompt_strategy,
            )
        self._remember_thread_model_settings_from_response(
            request.base_session_id,
            thread_id,
            resp,
        )
        self._remember_thread_caller_env_config(
            request.base_session_id,
            thread_id,
            self._caller_env_for_request(request),
        )
        self._remember_thread_git_path_config(
            request.base_session_id,
            thread_id,
            git_path_state,
            git_path_managed,
        )
        logger.info("Forked Codex thread %s from %s for session %s", thread_id, source_thread_id, request.base_session_id)
        return thread_id

    async def _should_trim_forked_running_turn(self, fork: dict[str, Any]) -> bool:
        """Trim only when the reserved fork boundary still targets this turn."""

        if not bool(fork.get("trim_latest_running_turn")):
            return False
        source_state = fork_source_state(fork)
        if source_state.anchor_is_terminal_agent_output:
            return False
        anchor_is_running_input = is_input_turn(
            getattr(source_state, "anchor_author", None),
            getattr(source_state, "anchor_type", None),
        )
        if getattr(source_state, "has_input_turn_after_anchor", False):
            return False
        if anchor_is_running_input:
            if source_state.has_messages_after_anchor:
                return True
            if bool(fork.get("native_turn_started")):
                return True
            return await self._fork_source_turn_now_started(fork)
        if source_state.has_messages_after_anchor:
            return not source_state.has_terminal_agent_output_after_anchor
        if bool(fork.get("native_turn_started")):
            return True
        return await self._fork_source_turn_now_started(fork)

    async def _fork_source_turn_now_started(self, fork: dict[str, Any]) -> bool:
        source_session_id = str(fork.get("source_session_id") or "").strip()
        if not source_session_id:
            return False
        from vibe import internal_client

        try:
            turn_result = await internal_client.turn_state(source_session_id)
        except (internal_client.InternalServerTimeout, internal_client.InternalServerUnavailable):
            return False
        body = turn_result.get("body") or {}
        return bool(body.get("in_flight") and body.get("native_turn_started"))

    async def _fork_source_native_turn_id(self, fork: dict[str, Any]) -> Optional[str]:
        """Return the active native turn at the source fork boundary."""

        source_session_id = str(fork.get("source_session_id") or "").strip()
        if not source_session_id:
            return None

        turn_registry = getattr(self, "_turn_registry", None)
        get_active_turn = getattr(turn_registry, "get_active_turn", None)
        if callable(get_active_turn):
            native_turn_id = str(get_active_turn(source_session_id) or "").strip()
            if native_turn_id:
                return native_turn_id

        source_message_id = str(fork.get("source_message_id") or "").strip()
        session_turns = getattr(getattr(self, "controller", None), "session_turns", None)
        get_for_initial_message = getattr(
            session_turns,
            "native_turn_id_for_initial_message",
            None,
        )
        if source_message_id and callable(get_for_initial_message):
            try:
                native_turn_id = str(
                    get_for_initial_message(source_session_id, source_message_id) or ""
                ).strip()
            except Exception:
                logger.debug(
                    "Could not read persisted Codex fork boundary for session %s",
                    source_session_id,
                    exc_info=True,
                )
            else:
                if native_turn_id:
                    return native_turn_id

        from vibe import internal_client

        try:
            turn_result = await internal_client.turn_state(source_session_id)
        except (internal_client.InternalServerTimeout, internal_client.InternalServerUnavailable):
            return None
        body = turn_result.get("body") or {}
        if not (body.get("in_flight") and body.get("native_turn_started")):
            return None
        return str(body.get("native_turn_id") or "").strip() or None

    async def _fork_source_last_completed_turn_id(
        self,
        transport: CodexTransport,
        fork: dict[str, Any],
    ) -> tuple[bool, Optional[str]]:
        """Resolve the inclusive fork boundary immediately before a live turn.

        ``thread/fork.lastTurnId`` cannot point at an in-progress turn. Page
        through the source turns in reverse chronological order and return the
        preceding terminal turn instead. If the reserved turn completed during
        this race, use that turn itself: ``lastTurnId`` is inclusive, so this
        still excludes any later turn that may have started while the fork
        request was being prepared.
        """

        active_turn_id = await self._fork_source_native_turn_id(fork)
        source_thread_id = str(fork.get("source_native_session_id") or "").strip()
        if not active_turn_id or not source_thread_id:
            return True, None
        # Another process reads a live turn as interrupted; only the generation
        # holding the source thread reports it in progress. ``source_session_id``
        # is the durable Session id, not a base session, so look up the thread.
        source_generation = self._generation_holding_thread(source_thread_id)
        if source_generation is not None and not self._process_exited(source_generation.runtime.transport):
            # Even one that stopped answering: reading it fails closed, while
            # another process would report the live turn as interrupted.
            transport = source_generation.runtime.transport

        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        active_turn_seen = False
        try:
            while True:
                params: dict[str, Any] = {
                    "threadId": source_thread_id,
                    "limit": 2,
                    "itemsView": "notLoaded",
                    "sortDirection": "desc",
                }
                if cursor:
                    params["cursor"] = cursor
                response = await transport.send_request("thread/turns/list", params)

                turns = response.get("data") if isinstance(response, dict) else None
                if not isinstance(turns, list):
                    return True, None

                for turn in turns:
                    if not isinstance(turn, dict):
                        return True, None
                    turn_id = str(turn.get("id") or "").strip()
                    status = str(turn.get("status") or "").strip()
                    if turn_id == active_turn_id:
                        if status in {"completed", "interrupted", "failed"}:
                            return False, active_turn_id
                        if status != "inProgress":
                            return True, None
                        active_turn_seen = True
                    elif active_turn_seen:
                        # Descending pages contain the predecessor after the
                        # active turn, possibly on the next page. Do not skip
                        # an unproven boundary and silently discard history.
                        if not turn_id or status not in {"completed", "interrupted", "failed"}:
                            return True, None
                        return True, turn_id

                next_cursor = response.get("nextCursor")
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor in seen_cursors
                ):
                    return True, None
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except (CodexRPCError, CodexResponseTooLargeError, ConnectionError, TimeoutError):
            logger.debug(
                "Could not read Codex fork boundary for source thread %s",
                source_thread_id,
                exc_info=True,
            )
            return True, None

    def _resolve_codex_agent_settings(
        self,
        request: AgentRequest,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        routing_agent, routing_model, routing_effort = self._get_codex_overrides(request)
        request_subagent = getattr(request, "subagent_name", None)
        request_model = getattr(request, "subagent_model", None)
        request_effort = getattr(request, "subagent_reasoning_effort", None)
        vibe_model = getattr(request, "vibe_agent_model", None)
        vibe_effort = getattr(request, "vibe_agent_reasoning_effort", None)
        vibe_model_explicit = bool(getattr(request, "vibe_agent_model_explicit", False))
        vibe_effort_explicit = bool(
            getattr(request, "vibe_agent_reasoning_effort_explicit", False)
        )
        vibe_instructions = getattr(request, "vibe_agent_system_prompt", None)

        effective_agent = request_subagent or routing_agent
        if request_model is not None:
            selected_model = request_model
            selected_model_is_explicit = True
        elif vibe_model_explicit:
            selected_model = vibe_model
            selected_model_is_explicit = True
        else:
            selected_model = vibe_model or routing_model
            selected_model_is_explicit = False
        if request_effort is not None:
            selected_effort = request_effort
            selected_effort_is_explicit = True
        elif vibe_effort_explicit:
            selected_effort = vibe_effort
            selected_effort_is_explicit = True
        else:
            selected_effort = vibe_effort or routing_effort
            selected_effort_is_explicit = False

        agent_definition: Optional[SubagentDefinition] = None
        if effective_agent:
            try:
                working_path = getattr(request, "working_path", None)
                project_root = Path(working_path) if working_path else None
                agent_definition = load_codex_subagent(effective_agent, project_root=project_root)
            except Exception as exc:
                logger.warning("Failed to load Codex subagent %s: %s", effective_agent, exc)

        effective_model = (
            selected_model
            if selected_model_is_explicit
            else selected_model or (agent_definition.model if agent_definition else None)
        )
        effective_effort = (
            selected_effort
            if selected_effort_is_explicit
            else selected_effort or (agent_definition.reasoning_effort if agent_definition else None)
        )
        if getattr(self.controller, "model_hub_runtime", None) is not None:
            from modules.agents.model_hub import launch_for_context

            launch = launch_for_context(getattr(request, "context", None))
            if launch is not None and launch.backend == "codex":
                effective_model = launch.runtime_model or effective_model
                if (
                    launch.channel != "direct"
                    and effective_effort not in launch.reasoning_efforts
                ):
                    effective_effort = None
        developer_instructions = vibe_instructions or (agent_definition.developer_instructions if agent_definition else None)

        return effective_agent, effective_model, effective_effort, developer_instructions

    def _get_codex_overrides(
        self,
        request: AgentRequest,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve scope routing through the controller's shared routing API."""
        controller = getattr(self, "controller", None)
        request_context = getattr(request, "context", None)
        getter = getattr(controller, "get_codex_overrides", None)
        if request_context is None or not callable(getter):
            return None, None, None
        try:
            return getter(request_context)
        except Exception as exc:
            logger.warning("Failed to resolve Codex routing overrides: %s", exc)
            return None, None, None

    async def _start_or_resume_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        *,
        developer_instructions: Optional[str] = None,
    ) -> str:
        """Try to resume a persisted thread, fall back to creating a new one."""
        # Resume the native thread bound to the RESERVED workbench row (by PK): the
        # bind WRITE is by-PK, so the resume READ must read it back from the row, not
        # the (session_key, anchor) projection which drifts for avibe and would fork
        # a fresh thread (context loss) after a restart. Skip it for ANY subagent —
        # explicit (its own thread, distinct base_session_id) OR a routing-default
        # subagent (the namespaced base also has its own thread) — else the first
        # subagent turn would resume the MAIN thread. Falls back to the projection
        # for IM/CLI turns (no reserved target).
        persisted = self.sessions.get_agent_session_id(
            request.session_key,
            request.base_session_id,
            self.name,
        )
        _ctx_spec = getattr(getattr(request, "context", None), "platform_specific", None) or {}
        if not getattr(request, "subagent_name", None) and not _ctx_spec.get("routing_subagent"):
            persisted = self._reserved_native_session_id(getattr(request, "context", None), self.name) or persisted
        if persisted:
            try:
                self.bind_agent_session_id(request, persisted)
                resume_params: Dict[str, Any] = {
                    "threadId": persisted,
                    "excludeTurns": True,
                }
                if developer_instructions:
                    resume_params["developerInstructions"] = await self._native_thread_prompt(
                        transport, request, developer_instructions
                    )
                marker_getter = getattr(
                    getattr(self, "sessions", None),
                    "get_agent_session_runtime_marker",
                    None,
                )
                if callable(marker_getter):
                    marker = self._read_persisted_prompt_strategy_marker(
                        persisted,
                        agent_session_id=self._prompt_state_agent_session_id(request),
                    )
                    if marker is None and not developer_instructions:
                        # Older Avibe releases persisted their prompt as thread
                        # configuration. Clear it before the first Turn selects
                        # one of the new prompt-delivery strategies.
                        resume_params["developerInstructions"] = None
                git_path_state, git_path_managed = self._inject_caller_env_config(
                    resume_params,
                    request,
                    self._process_environment(transport),
                )
                model_provider = await self._resolve_resume_model_provider_override(
                    transport,
                    request,
                    persisted,
                    rebind_same_provider=True,
                )
                if model_provider:
                    resume_params["modelProvider"] = model_provider
                holder = self._generation_running(transport)
                if holder is not None:
                    # A resume whose answer is lost may still load the thread
                    # there. Claim it first, so it is released there before any
                    # other process resumes it; releasing a thread that never
                    # loaded is a cheap ``notLoaded``.
                    self._bind_session_thread(holder, request.base_session_id, persisted)
                resp = await transport.send_request(
                    "thread/resume",
                    resume_params,
                )
                # thread/resume returns Thread directly OR may nest under "thread"
                thread_id = resp.get("id", "")
                if not thread_id:
                    thread_obj = resp.get("thread")
                    if isinstance(thread_obj, dict):
                        thread_id = thread_obj.get("id", "")
            except Exception as e:
                if isinstance(e, (CodexPromptRefreshUnavailableError, CodexResponseTooLargeError)):
                    raise
                if self._is_recoverable_transport_error(e):
                    # Transient: reconnect the SAME thread (handled by the outer
                    # retry) — not context loss, keep.
                    logger.warning("Failed to resume Codex thread %s due to transport failure: %s", persisted, e)
                    raise
                from core.agent_auth_service import classify_auth_error

                if classify_auth_error("codex", str(e)):
                    # Auth expired/invalid: preserve the ORIGINAL error so
                    # handle_message's auth-recovery classifier can surface the
                    # reset-OAuth button — don't mask it as a generic resume failure.
                    logger.warning("Codex auth error while resuming thread %s: %s", persisted, e)
                    raise
                # FAIL LOUD: an associated thread that won't resume (expired/gone) is
                # context loss — surface it rather than silently starting a fresh
                # thread (product decision: no silent fallbacks).
                logger.warning("Failed to resume Codex thread %s: %s", persisted, e)
                raise CodexResumeUnavailableError(persisted) from e
            if not thread_id:
                raise CodexResumeUnavailableError(persisted, detail="thread/resume returned no thread id")
            self._session_mgr.set_thread_id(request.base_session_id, thread_id)
            self._remember_thread_model_settings_from_response(
                request.base_session_id,
                thread_id,
                resp,
            )
            self._remember_thread_caller_env_config(
                request.base_session_id,
                thread_id,
                self._caller_env_for_request(request),
            )
            self._remember_thread_git_path_config(
                request.base_session_id,
                thread_id,
                git_path_state,
                git_path_managed,
            )
            logger.info("Resumed Codex thread %s for session %s", thread_id, request.base_session_id)
            return thread_id

        fork = pending_native_fork(request.context, self.name)
        if fork:
            return await self._fork_thread(
                transport, request, fork, developer_instructions=developer_instructions
            )

        # No associated thread yet (genuinely first turn) — start fresh.
        return await self._start_thread(
            transport, request, developer_instructions=developer_instructions
        )

    async def _native_thread_prompt(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        developer_instructions: str,
    ) -> str:
        """Keep user-configured native instructions before Avibe's baseline.

        Native configuration is reconstructed after compaction, whereas injected
        items are budgeted history. Never claim that an overlay updates this
        configuration, or advance the history fingerprint on resume/fork.
        """
        params: Dict[str, Any] = {"includeLayers": False}
        if getattr(request, "working_path", None):
            params["cwd"] = request.working_path
        response = await transport.send_request("config/read", params)
        config = response.get("config") if isinstance(response, dict) else None
        if not isinstance(config, dict):
            raise CodexPromptRefreshUnavailableError(
                "Could not read configured Codex instructions before setting the native baseline"
            )
        configured = config.get("developer_instructions")
        if configured is not None and not isinstance(configured, str):
            raise CodexPromptRefreshUnavailableError(
                "Configured Codex developer instructions must be text"
            )
        return f"{configured}\n\n{developer_instructions}" if configured else developer_instructions

    async def _resolve_resume_model_provider_override(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
        *,
        rebind_same_provider: bool = False,
    ) -> Optional[str]:
        """Return a provider override only when a persisted thread is stale.

        Codex preserves a thread's latest model / reasoning effort on resume
        unless the client sends a model/provider override. Vibe Remote only
        overrides transitions between managed providers. A persisted thread's
        first resume in a fresh app-server also rebinds Avibe-managed same-id
        providers, because OAuth and API-key/custom-base-URL configurations
        deliberately reuse ``openai-managed``.
        """
        current_provider = await self._read_effective_model_provider(transport, request)
        if not current_provider:
            return None

        try:
            resp = await transport.send_request(
                "thread/read",
                {
                    "threadId": thread_id,
                    "includeTurns": False,
                },
            )
        except Exception as exc:
            logger.warning("Failed to read Codex thread %s provider before resume: %s", thread_id, exc)
            return None

        thread_obj = resp.get("thread") if isinstance(resp, dict) else None
        if not isinstance(thread_obj, dict) and isinstance(resp, dict) and resp.get("id") == thread_id:
            thread_obj = resp
        stored_provider = thread_obj.get("modelProvider") if isinstance(thread_obj, dict) else None
        if not isinstance(stored_provider, str) or not stored_provider.strip():
            return None

        stored_provider = stored_provider.strip()
        if stored_provider == current_provider:
            if rebind_same_provider and current_provider in _CODEX_REBINDABLE_SAME_ID_PROVIDERS:
                return current_provider
            return None
        if not self._is_managed_provider_transition(stored_provider, current_provider):
            return None
        return current_provider

    @staticmethod
    def _is_managed_provider_transition(stored_provider: str, current_provider: str) -> bool:
        if _CODEX_MODEL_HUB_PROVIDER_ID in {stored_provider, current_provider}:
            return True
        return {stored_provider, current_provider}.issubset(_CODEX_MANAGED_PROVIDER_IDS)

    async def _read_effective_model_provider(
        self,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> Optional[str]:
        """Ask Codex app-server for the provider it resolves for this request."""
        params: Dict[str, Any] = {"includeLayers": False}
        working_path = getattr(request, "working_path", None)
        if working_path:
            params["cwd"] = working_path

        try:
            resp = await transport.send_request("config/read", params)
        except Exception as exc:
            logger.warning("Failed to read effective Codex model provider before resume: %s", exc)
            return None

        config_obj = resp.get("config") if isinstance(resp, dict) else None
        if not isinstance(config_obj, dict):
            return None
        model_provider = config_obj.get("model_provider")
        if isinstance(model_provider, str) and model_provider.strip():
            return model_provider.strip()
        # Codex omits the built-in provider from config/read when no explicit
        # model_provider is configured. Make that default concrete so a thread
        # created under the ephemeral Hub provider can resume in Direct mode.
        return _CODEX_DEFAULT_PROVIDER_ID

    async def _build_thread_developer_instructions(self, request: AgentRequest) -> Optional[str]:
        """Render the developer instructions applied at the next Turn boundary."""
        _, _, _, agent_instructions = self._resolve_codex_agent_settings(request)
        platform = (
            request.context.platform
            or (request.context.platform_specific or {}).get("platform")
            or self.controller.config.platform
        )

        skill_catalog_sink: list[dict] = []
        instructions = await asyncio.to_thread(
            build_system_prompt_injection,
            agent_instructions=agent_instructions or "",
            backend="codex",
            include_quick_replies=getattr(self.controller.config, "reply_enhancements", True)
            and platform != "wechat",
            include_codex_generated_images=True,
            context=request.context,
            fallback_platform=platform,
            enabled_agents=get_enabled_agents_for_prompt(self.controller),
            skills_cwd=getattr(request, "working_path", None),
            skills_project_base=managed_skill_project_base(request.context),
            skills_claude_cli_path=managed_skill_claude_cli_path(
                getattr(getattr(self, "controller", None), "config", None)
            ),
            skill_catalog_sink=skill_catalog_sink,
        )

        request.skill_catalog_observation = skill_catalog_sink[0] if skill_catalog_sink else None
        return instructions or None

    async def _inject_forked_session_correction(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
    ) -> None:
        """Append a fork correction as Codex model-visible developer history.

        Codex accepts ``developerInstructions`` on ``thread/fork``, but the fork
        also copies the source thread's previous developer messages. Appending a
        fresh developer item makes the target session id authoritative without
        creating a user turn.
        """
        correction = build_forked_session_correction_prompt(request.context)
        if not correction:
            return
        await transport.send_request(
            "thread/inject_items",
            {
                "threadId": thread_id,
                "items": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": correction}],
                    }
                ],
            },
        )

    async def _refresh_thread_developer_instructions_if_needed(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
    ) -> None:
        """Refresh mutable non-prompt thread config before starting a Turn."""
        self.ensure_agent_session_id(request)
        # Refresh caller environment and git path after prompt rendering,
        # without rendering the prompt a second time.
        caller_env = self._caller_env_for_request(request)
        git_path_state = self._git_path_state_for_request(request, self._process_environment(transport))

        if not hasattr(self, "_thread_caller_env_configs"):
            self._thread_caller_env_configs = {}
        if not hasattr(self, "_thread_git_path_configs"):
            self._thread_git_path_configs = {}

        cached_caller_env = self._thread_caller_env_configs.get(request.base_session_id)
        cached_git_path = self._thread_git_path_configs.get(request.base_session_id)
        git_path_changed = cached_git_path is None or cached_git_path[:2] != (
            thread_id,
            git_path_state,
        )
        git_path_managed = bool(cached_git_path and cached_git_path[0] == thread_id and cached_git_path[2])
        caller_env_changed = bool(caller_env) and cached_caller_env != (thread_id, caller_env)
        if not caller_env_changed and not git_path_changed:
            return

        resume_params: Dict[str, Any] = {
            "threadId": thread_id,
        }
        if caller_env_changed or git_path_changed:
            git_path_state, git_path_managed = self._inject_caller_env_config(
                resume_params,
                request,
                self._process_environment(transport),
                force_path=git_path_managed,
            )
        model_provider = await self._resolve_resume_model_provider_override(transport, request, thread_id)
        if model_provider:
            resume_params["modelProvider"] = model_provider

        if len(resume_params) == 1:
            return

        resume_params["excludeTurns"] = True
        await transport.send_request(
            "thread/resume",
            resume_params,
        )
        self._remember_thread_caller_env_config(request.base_session_id, thread_id, caller_env)
        self._remember_thread_git_path_config(
            request.base_session_id,
            thread_id,
            git_path_state,
            git_path_managed,
        )

    def _remember_thread_developer_instructions(
        self,
        base_session_id: str,
        thread_id: str,
        developer_instructions: Optional[str],
    ) -> None:
        if not developer_instructions:
            return
        if not hasattr(self, "_thread_developer_instructions"):
            self._thread_developer_instructions = {}
        self._thread_developer_instructions[base_session_id] = (thread_id, developer_instructions)

    def _remember_thread_prompt_strategy(
        self,
        base_session_id: str,
        thread_id: str,
        strategy: str,
    ) -> None:
        if not hasattr(self, "_thread_prompt_strategies"):
            self._thread_prompt_strategies = {}
        self._thread_prompt_strategies[base_session_id] = (thread_id, strategy)

    def _fork_source_prompt_state(
        self,
        fork: dict[str, Any],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        source_session_id = str(fork.get("source_session_id") or "").strip()
        source_thread_id = str(fork.get("source_native_session_id") or "").strip()
        if not source_session_id or not source_thread_id:
            return None, None, None

        cached_strategy = getattr(self, "_thread_prompt_strategies", {}).get(
            source_session_id
        )
        cached_instructions = getattr(
            self,
            "_thread_developer_instructions",
            {},
        ).get(source_session_id)
        instructions = (
            cached_instructions[1]
            if cached_instructions and cached_instructions[0] == source_thread_id
            else None
        )
        if cached_strategy and cached_strategy[0] == source_thread_id:
            strategy = cached_strategy[1]
            prompt_sha256 = (
                self._prompt_fingerprint(instructions) if instructions else None
            )
            if prompt_sha256 is None and strategy in {
                "fallback",
                "fallback_pending_clear",
                "fallback_pending_clear_injection",
            }:
                marker = self._read_persisted_prompt_strategy_marker(
                    source_thread_id,
                    agent_session_id=source_session_id,
                )
                if marker is not None and marker["strategy"] == strategy:
                    prompt_sha256 = marker.get("sha256")
            return strategy, prompt_sha256, instructions

        marker = self._read_persisted_prompt_strategy_marker(
            source_thread_id,
            agent_session_id=source_session_id,
        )
        if marker is None:
            return None, None, None
        return marker["strategy"], marker.get("sha256"), instructions

    def _remember_thread_model_settings_from_response(
        self,
        base_session_id: str,
        thread_id: str,
        response: Any,
    ) -> None:
        if not isinstance(response, dict):
            return
        model = response.get("model")
        if not isinstance(model, str) or not model.strip():
            return
        effort = response.get("reasoningEffort")
        if not isinstance(effort, str):
            effort = None
        if not hasattr(self, "_thread_model_settings"):
            self._thread_model_settings = {}
        self._thread_model_settings[base_session_id] = (
            thread_id,
            model.strip(),
            effort,
        )

    def _remember_thread_caller_env_config(
        self,
        base_session_id: str,
        thread_id: str,
        caller_env: dict[str, str],
    ) -> None:
        if not caller_env:
            return
        if not hasattr(self, "_thread_caller_env_configs"):
            self._thread_caller_env_configs = {}
        self._thread_caller_env_configs[base_session_id] = (thread_id, dict(caller_env))

    def _remember_thread_git_path_config(
        self,
        base_session_id: str,
        thread_id: str,
        git_path_state: str,
        path_managed: bool,
    ) -> None:
        if not hasattr(self, "_thread_git_path_configs"):
            self._thread_git_path_configs = {}
        self._thread_git_path_configs[base_session_id] = (
            thread_id,
            git_path_state,
            path_managed,
        )

    def _clear_thread_developer_instructions(self, base_session_id: str) -> None:
        if hasattr(self, "_thread_developer_instructions"):
            self._thread_developer_instructions.pop(base_session_id, None)
        if hasattr(self, "_thread_prompt_strategies"):
            self._thread_prompt_strategies.pop(base_session_id, None)
        if hasattr(self, "_thread_unpersisted_prompts"):
            self._thread_unpersisted_prompts.pop(base_session_id, None)
        if hasattr(self, "_thread_model_settings"):
            self._thread_model_settings.pop(base_session_id, None)
        if hasattr(self, "_thread_caller_env_configs"):
            self._thread_caller_env_configs.pop(base_session_id, None)
        if hasattr(self, "_thread_git_path_configs"):
            self._thread_git_path_configs.pop(base_session_id, None)

    def _fork_correction_pending_sessions(self) -> set[str]:
        if not hasattr(self, "_fork_correction_pending_base_sessions"):
            self._fork_correction_pending_base_sessions = set()
        return self._fork_correction_pending_base_sessions

    def _mark_fork_correction_pending(self, base_session_id: str) -> None:
        self._fork_correction_pending_sessions().add(base_session_id)

    def _clear_fork_correction_pending(self, base_session_id: str) -> None:
        self._fork_correction_pending_sessions().discard(base_session_id)

    def is_fork_correction_pending(self, base_session_id: str) -> bool:
        return base_session_id in self._fork_correction_pending_sessions()

    @staticmethod
    def _collaboration_mode_is_unsupported(error: BaseException) -> bool:
        message = str(error).casefold()
        names_field = "collaborationmode" in message or "collaboration_mode" in message
        unsupported = any(
            marker in message
            for marker in (
                "experimental api",
                "experimentalapi",
                "unknown field",
                "unsupported",
                "unrecognized",
            )
        )
        return names_field and unsupported

    @staticmethod
    def _render_developer_prompt_snapshot(developer_instructions: str) -> str:
        return (
            prompt_text("runtime-snapshot-open")
            + developer_instructions
            + prompt_text("runtime-snapshot-close")
        )

    async def _inject_thread_developer_instructions(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
        developer_instructions: str,
        *,
        agent_session_id: Optional[str] = None,
        strategy: str = "fallback",
    ) -> None:
        """Append model-visible instructions, with durable at-most-once recovery."""

        previous_marker = self._read_persisted_prompt_strategy_marker(
            thread_id,
            agent_session_id=agent_session_id,
        )
        pending_strategy = (
            "fallback_pending_clear_injection"
            if strategy == "fallback_pending_clear"
            else "fallback_pending_injection"
        )
        if not self._persist_prompt_strategy(
            request,
            thread_id,
            developer_instructions,
            strategy=pending_strategy,
            agent_session_id=agent_session_id,
        ):
            # No native mutation happened, so volatile strategy selection can
            # be discarded and resolved again on a later retry.
            getattr(self, "_thread_prompt_strategies", {}).pop(
                request.base_session_id,
                None,
            )
            raise CodexPromptRefreshUnavailableError(
                "Could not prepare the fallback prompt strategy before injection"
            )
        # Keep the write-ahead state in memory too. If the RPC outcome is
        # ambiguous, a same-process retry must follow the same at-most-once
        # recovery path as a process restart.
        self._remember_thread_prompt_strategy(
            request.base_session_id,
            thread_id,
            pending_strategy,
        )
        try:
            await transport.send_request(
                "thread/inject_items",
                {
                    "threadId": thread_id,
                    "items": [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": self._render_developer_prompt_snapshot(developer_instructions),
                                }
                            ],
                        }
                    ],
                },
            )
        except CodexRPCError as exc:
            if not exc.request_rejected:
                raise
            # The server rejected the request before dispatch, so restore the
            # pre-injection state instead of recording an unknowable mutation.
            previous_marker = previous_marker or {}
            if not self._persist_prompt_strategy(
                request,
                thread_id,
                None,
                strategy=previous_marker.get("strategy"),
                prompt_sha256=previous_marker.get("sha256"),
                agent_session_id=agent_session_id,
            ):
                raise CodexPromptRefreshUnavailableError(
                    "Could not restore the prompt strategy after rejected injection"
                ) from exc
            self._thread_prompt_strategies.pop(request.base_session_id, None)
            raise CodexPromptRefreshUnavailableError(
                "Codex rejected developer prompt injection; check app-server API compatibility"
            ) from exc
        from core.skill_observability import accept_catalog

        candidate = getattr(request, "skill_catalog_observation", None)
        if candidate is not None:
            accept_catalog(self.controller, request.context, candidate, backend="codex")
        if not self._persist_prompt_strategy(
            request,
            thread_id,
            developer_instructions,
            strategy=strategy,
            agent_session_id=agent_session_id,
        ):
            self._remember_thread_developer_instructions(
                request.base_session_id,
                thread_id,
                developer_instructions,
            )
            self._remember_thread_prompt_strategy(
                request.base_session_id,
                thread_id,
                "injected_pending_persist",
            )
            if not hasattr(self, "_thread_unpersisted_prompts"):
                self._thread_unpersisted_prompts = {}
            self._thread_unpersisted_prompts[request.base_session_id] = (
                thread_id,
                developer_instructions,
                strategy,
            )
            raise CodexPromptRefreshUnavailableError(
                "Could not persist the fallback prompt strategy after injection"
            )
        getattr(self, "_thread_unpersisted_prompts", {}).pop(
            request.base_session_id,
            None,
        )
        self._remember_thread_developer_instructions(
            request.base_session_id,
            thread_id,
            developer_instructions,
        )
        self._remember_thread_prompt_strategy(
            request.base_session_id,
            thread_id,
            strategy,
        )

    @classmethod
    def _prompt_fingerprint(cls, developer_instructions: str) -> str:
        snapshot = cls._render_developer_prompt_snapshot(developer_instructions)
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def _read_persisted_prompt_strategy_marker(
        self,
        thread_id: str,
        *,
        agent_session_id: Optional[str],
    ) -> Optional[dict[str, str]]:
        if not agent_session_id:
            return None
        getter = getattr(
            getattr(self, "sessions", None),
            "get_agent_session_runtime_marker",
            None,
        )
        if not callable(getter):
            return None
        try:
            marker = getter(
                agent_session_id,
                backend=self.name,
                native_session_id=thread_id,
                key=CODEX_PROMPT_STRATEGY_METADATA_KEY,
            )
        except Exception as exc:
            raise CodexPromptRefreshUnavailableError(
                "Could not resolve the Codex prompt strategy"
            ) from exc
        if marker is None:
            return None
        marker_thread_id = marker.get("thread_id") if isinstance(marker, dict) else None
        marker_strategy = marker.get("strategy") if isinstance(marker, dict) else None
        marker_sha256 = marker.get("sha256") if isinstance(marker, dict) else None
        marker_sha256_valid = bool(
            isinstance(marker_sha256, str)
            and len(marker_sha256) == 64
            and all(character in "0123456789abcdef" for character in marker_sha256)
        )
        marker_invalid = (
            marker_thread_id != thread_id
            or marker_strategy
            not in {
                "collaboration",
                "fallback",
                "fallback_pending_clear",
                "fallback_pending_injection",
                "fallback_pending_clear_injection",
                "unavailable",
            }
            or (marker_strategy == "fallback" and not marker_sha256_valid)
            or (
                marker_strategy
                in {
                    "fallback_pending_injection",
                    "fallback_pending_clear_injection",
                }
                and not marker_sha256_valid
            )
            or (
                marker_strategy
                in {"collaboration", "fallback_pending_clear"}
                and marker_sha256 is not None
                and not marker_sha256_valid
            )
            or (marker_strategy == "unavailable" and marker_sha256 is not None)
        )
        if marker_invalid:
            logger.warning(
                "Stored Codex prompt strategy marker is invalid for thread %s; "
                "continuing without prompt refresh",
                thread_id,
            )
            return {"thread_id": thread_id, "strategy": "unavailable"}
        if marker_strategy == "fallback_pending_injection":
            logger.warning(
                "Codex fallback prompt injection has an unknown outcome for thread %s; "
                "continuing without further prompt refresh",
                thread_id,
            )
            return {"thread_id": thread_id, "strategy": "unavailable"}
        resolved = {
            "thread_id": thread_id,
            "strategy": marker_strategy,
        }
        if marker_sha256_valid:
            resolved["sha256"] = marker_sha256
        return resolved

    def _prompt_state_agent_session_id(
        self,
        request: AgentRequest,
    ) -> Optional[str]:
        """Return the row that owns backend state, not necessarily visible output."""

        visible_session_id = self.ensure_agent_session_id(request)
        if not self._uses_namespaced_backend_session(
            request.context,
            subagent_name=getattr(request, "subagent_name", None),
        ):
            return visible_session_id

        getter = getattr(
            getattr(self, "sessions", None),
            "get_agent_session_row_id",
            None,
        )
        if not callable(getter):
            raise CodexPromptRefreshUnavailableError(
                "Could not resolve the Codex backend session binding"
            )
        try:
            backend_session_id = getter(
                request.session_key,
                request.base_session_id,
                self.name,
            )
        except Exception as exc:
            raise CodexPromptRefreshUnavailableError(
                "Could not resolve the Codex backend session binding"
            ) from exc
        if not backend_session_id:
            raise CodexPromptRefreshUnavailableError(
                "Could not resolve the Codex backend session binding"
            )
        return str(backend_session_id)

    def _persist_prompt_strategy(
        self,
        request: AgentRequest,
        thread_id: str,
        developer_instructions: Optional[str],
        *,
        strategy: Optional[str],
        agent_session_id: Optional[str],
        prompt_sha256: Optional[str] = None,
    ) -> bool:
        if strategy not in {
            None,
            "collaboration",
            "fallback",
            "fallback_pending_clear",
            "fallback_pending_injection",
            "fallback_pending_clear_injection",
            "unavailable",
        }:
            raise ValueError(f"Unsupported Codex prompt strategy: {strategy}")
        if prompt_sha256 is not None and (
            len(prompt_sha256) != 64
            or any(character not in "0123456789abcdef" for character in prompt_sha256)
        ):
            raise ValueError("Codex prompt fingerprint must be lowercase SHA-256")
        if strategy in {None, "unavailable"} and (
            developer_instructions or prompt_sha256 is not None
        ):
            raise ValueError("Absent or unavailable prompt strategy cannot carry prompt identity")
        if strategy == "fallback" and not developer_instructions and not prompt_sha256:
            raise ValueError("Fallback prompt strategy requires developer instructions")
        if not agent_session_id:
            return True
        setter = getattr(
            getattr(self, "sessions", None),
            "set_agent_session_runtime_marker",
            None,
        )
        if not callable(setter):
            return True
        marker = {
            "thread_id": thread_id,
            "strategy": strategy,
        }
        if developer_instructions:
            computed_sha256 = self._prompt_fingerprint(developer_instructions)
            if prompt_sha256 is not None and prompt_sha256 != computed_sha256:
                raise ValueError("Codex prompt fingerprint does not match prompt bytes")
            marker["sha256"] = computed_sha256
        elif prompt_sha256 is not None:
            marker["sha256"] = prompt_sha256

        def _set_marker(target_session_id: str) -> bool:
            return bool(
                setter(
                    target_session_id,
                    backend=self.name,
                    native_session_id=thread_id,
                    key=CODEX_PROMPT_STRATEGY_METADATA_KEY,
                    value=marker if strategy is not None else None,
                )
            )

        try:
            persisted = _set_marker(agent_session_id)
        except Exception:
            logger.warning("Failed to persist Codex prompt strategy", exc_info=True)
            return False
        if not persisted:
            # A native thread is cached before its durable Session bind. The
            # Workbench binder deliberately preserves the selected row when a
            # first bind fails, so retry that exact bind before treating the
            # marker as unavailable on every later Turn.
            try:
                rebound_session_id = self.bind_agent_session_id(request, thread_id)
                persisted = bool(rebound_session_id) and _set_marker(
                    str(rebound_session_id)
                )
            except Exception:
                logger.warning(
                    "Failed to rebind the Codex Session before prompt marker persistence",
                    exc_info=True,
                )
                persisted = False
        if not persisted:
            logger.warning(
                "Skipped Codex prompt strategy for stale Session binding %s",
                agent_session_id,
            )
            return False
        return True

    def _repair_unpersisted_prompt_strategy(
        self,
        request: AgentRequest,
        thread_id: str,
        *,
        agent_session_id: Optional[str],
    ) -> str:
        pending = getattr(self, "_thread_unpersisted_prompts", {}).get(
            request.base_session_id
        )
        if not pending or pending[0] != thread_id:
            raise CodexPromptRefreshUnavailableError(
                "The injected Codex prompt strategy cannot be recovered"
            )
        _, injected_instructions, target_strategy = pending
        if not self._persist_prompt_strategy(
            request,
            thread_id,
            injected_instructions,
            strategy=target_strategy,
            agent_session_id=agent_session_id,
        ):
            raise CodexPromptRefreshUnavailableError(
                "Could not persist the injected Codex prompt strategy"
            )
        self._remember_thread_prompt_strategy(
            request.base_session_id,
            thread_id,
            target_strategy,
        )
        self._thread_unpersisted_prompts.pop(request.base_session_id, None)
        return target_strategy

    @staticmethod
    async def _confirm_collaboration_mode_capability(transport: CodexTransport) -> None:
        if getattr(transport, "supports_turn_collaboration_mode", False):
            return
        try:
            await transport.send_request("collaborationMode/list", {})
        except Exception as exc:
            raise CodexPromptRefreshUnavailableError(
                "Cannot safely resume a collaboration-backed Codex thread because "
                "the current app-server did not confirm collaboration mode support"
            ) from exc
        transport.supports_turn_collaboration_mode = True

    async def _start_turn(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
        *,
        developer_instructions: Optional[str] = None,
    ) -> str:
        """Build input, configure overrides, and send turn/start to Codex."""
        agent_session_id = self._prompt_state_agent_session_id(request)
        _, effective_model, effective_effort, _ = self._resolve_codex_agent_settings(request)
        model_explicit = bool(getattr(request, "vibe_agent_model_explicit", False))
        effort_explicit = bool(
            getattr(request, "vibe_agent_reasoning_effort_explicit", False)
        )
        cached_model_settings = getattr(self, "_thread_model_settings", {}).get(request.base_session_id)
        if cached_model_settings and cached_model_settings[0] == thread_id:
            if effective_model is None and not model_explicit:
                effective_model = cached_model_settings[1]
                if effective_effort is None and not effort_explicit:
                    effective_effort = cached_model_settings[2]

        turn_params: Dict[str, Any] = {
            "threadId": thread_id,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
        from modules.agents.model_hub import launch_for_context

        launch = launch_for_context(getattr(request, "context", None))
        if (
            launch is not None and launch.backend == "codex" and launch.channel == "hub"
            and launch.gateway_request_metadata
        ):
            # Process authentication stays stable; native tool loops and retries
            # carry this turn's route instead of inheriting a peer's launch.
            turn_params["responsesapiClientMetadata"] = dict(launch.gateway_request_metadata)
        if effective_model is not None or model_explicit:
            turn_params["model"] = effective_model
        if effective_effort is not None or effort_explicit:
            turn_params["effort"] = effective_effort

        cached_instructions = getattr(self, "_thread_developer_instructions", {}).get(request.base_session_id)
        prompt_changed = cached_instructions != (thread_id, developer_instructions)
        cached_strategy = getattr(self, "_thread_prompt_strategies", {}).get(request.base_session_id)
        prompt_strategy = cached_strategy[1] if cached_strategy and cached_strategy[0] == thread_id else None
        if prompt_strategy == "injected_pending_persist":
            prompt_strategy = self._repair_unpersisted_prompt_strategy(
                request,
                thread_id,
                agent_session_id=agent_session_id,
            )
        if prompt_strategy == "fallback_pending_injection":
            # The native injection may already have succeeded. Never append it
            # again when only its RPC acknowledgement is unknown.
            prompt_strategy = "unavailable"
            self._remember_thread_prompt_strategy(
                request.base_session_id,
                thread_id,
                prompt_strategy,
            )
        persisted_prompt_marker = None
        if developer_instructions and prompt_strategy is None:
            persisted_prompt_marker = self._read_persisted_prompt_strategy_marker(
                thread_id,
                agent_session_id=agent_session_id,
            )
            if persisted_prompt_marker is not None:
                prompt_strategy = persisted_prompt_marker["strategy"]
            else:
                # Keep the durable name for existing threads. Collaboration
                # settings are not a prompt channel: model catalog messages
                # can override their developer_instructions entirely.
                prompt_strategy = "fallback"
            self._remember_thread_prompt_strategy(
                request.base_session_id,
                thread_id,
                prompt_strategy,
            )
        fallback_prompt_is_current = bool(
            developer_instructions
            and persisted_prompt_marker
            and persisted_prompt_marker["strategy"]
            in {"fallback", "fallback_pending_clear"}
            and persisted_prompt_marker.get("sha256")
            == self._prompt_fingerprint(developer_instructions)
        )
        if fallback_prompt_is_current:
            self._remember_thread_developer_instructions(
                request.base_session_id,
                thread_id,
                developer_instructions,
            )
            prompt_changed = False
        pending_clear_after_unknown_injection = (
            prompt_strategy == "fallback_pending_clear_injection"
        )
        if prompt_strategy in {
            "collaboration",
            "fallback_pending_clear",
            "fallback_pending_clear_injection",
        }:
            await self._confirm_collaboration_mode_capability(transport)
        collaboration_mode_is_known = bool(
            getattr(transport, "supports_turn_collaboration_mode", True)
        )
        clear_collaboration_mode = collaboration_mode_is_known and bool(
            prompt_strategy
            in {"collaboration", "fallback_pending_clear", "fallback_pending_clear_injection"}
            or (model_explicit and effective_model is None)
        )
        if clear_collaboration_mode:
            was_collaboration = prompt_strategy == "collaboration"
            turn_params["collaborationMode"] = None
            if developer_instructions and not pending_clear_after_unknown_injection:
                prompt_strategy = (
                    "fallback_pending_clear"
                    if was_collaboration
                    or prompt_strategy == "fallback_pending_clear"
                    else "fallback"
                )
                self._remember_thread_prompt_strategy(
                    request.base_session_id,
                    thread_id,
                    prompt_strategy,
                )
                if was_collaboration and not fallback_prompt_is_current:
                    prompt_changed = True
        if (
            developer_instructions
            and prompt_changed
            and prompt_strategy
            not in {"unavailable", "fallback_pending_clear_injection"}
        ):
            await self._inject_thread_developer_instructions(
                transport,
                request,
                thread_id,
                developer_instructions,
                agent_session_id=agent_session_id,
                strategy=(
                    "fallback_pending_clear"
                    if prompt_strategy == "fallback_pending_clear"
                    else "fallback"
                ),
            )

        self._write_caller_env_script(request)
        self._turn_registry.begin_turn_start(request, thread_id)
        event_handler = getattr(self, "_event_handler", None)
        snapshot_generated_images = getattr(
            event_handler,
            "snapshot_generated_images",
            None,
        )
        if callable(snapshot_generated_images):
            snapshot_generated_images(thread_id, request.base_session_id)
        turn_params["input"] = self._build_input(request)
        mark_backend_dispatch_attempted(request.context)
        try:
            resp = await transport.send_request("turn/start", turn_params)
        except Exception as exc:
            if prompt_strategy in {
                "fallback_pending_clear",
                "fallback_pending_clear_injection",
            }:
                raise CodexPromptRefreshUnavailableError(
                    "Could not confirm that Codex cleared the previous collaboration prompt"
                ) from exc
            if (
                "collaborationMode" not in turn_params
                or not self._collaboration_mode_is_unsupported(exc)
            ):
                raise
            logger.warning(
                "Codex turn collaboration mode is unavailable; retrying without the model reset field: %s",
                exc,
            )
            transport.supports_turn_collaboration_mode = False
            fallback_turn_params = dict(turn_params)
            fallback_turn_params.pop("collaborationMode", None)
            resp = await transport.send_request("turn/start", fallback_turn_params)

        if prompt_strategy == "fallback_pending_clear" and developer_instructions:
            if self._persist_prompt_strategy(
                request,
                thread_id,
                developer_instructions,
                strategy="fallback",
                agent_session_id=agent_session_id,
            ):
                prompt_strategy = "fallback"
                self._remember_thread_prompt_strategy(
                    request.base_session_id,
                    thread_id,
                    prompt_strategy,
                )
            else:
                logger.warning(
                    "Codex collaboration clear succeeded but its completed prompt strategy marker remains pending"
                )
        elif (
            prompt_strategy == "fallback_pending_clear_injection"
            and developer_instructions
        ):
            # The clear is confirmed, but the preceding injection outcome is
            # unknowable. Lock this thread against later prompt reinjection.
            if self._persist_prompt_strategy(
                request,
                thread_id,
                None,
                strategy="unavailable",
                agent_session_id=agent_session_id,
            ):
                prompt_strategy = "unavailable"
                self._remember_thread_prompt_strategy(
                    request.base_session_id,
                    thread_id,
                    prompt_strategy,
                )
            else:
                logger.warning(
                    "Codex collaboration clear succeeded but the unknown injection marker remains pending"
                )

        if effective_model:
            self._thread_model_settings = getattr(self, "_thread_model_settings", {})
            self._thread_model_settings[request.base_session_id] = (
                thread_id,
                effective_model,
                effective_effort,
            )
        elif model_explicit:
            getattr(self, "_thread_model_settings", {}).pop(request.base_session_id, None)

        turn_id = resp.get("id", "")
        if not turn_id:
            turn_obj = resp.get("turn")
            if isinstance(turn_obj, dict):
                turn_id = turn_obj.get("id", "")
        if not turn_id:
            turn_id = self._turn_registry.get_bootstrapped_turn_id(request.base_session_id, request) or ""
        if not turn_id:
            raise RuntimeError("Codex turn/start returned no turn id")

        turn_state = self._turn_registry.finalize_turn_start_response(turn_id, request)
        generation = self._generation_for_session(request.base_session_id)
        self._mark_runtime_turn_started(
            getattr(request, "context", None),
            activation_identity=(
                generation.runtime.activation
                if generation is not None and generation.runtime.transport is transport
                else None
            ),
        )
        bind_generated_image_snapshot = getattr(event_handler, "bind_generated_image_snapshot", None)
        if callable(bind_generated_image_snapshot):
            bind_generated_image_snapshot(thread_id, turn_id, request.base_session_id)
        logger.info(
            "Codex turn started: thread=%s turn=%s session=%s state=%s",
            thread_id,
            turn_id,
            request.composite_session_id,
            "registered" if turn_state else "already-finished",
        )
        return thread_id

    def _mark_runtime_turn_started(
        self,
        context: Any,
        *,
        activation_identity: RuntimeActivationIdentity | None = None,
    ) -> None:
        service = getattr(getattr(self, "controller", None), "agent_service", None)
        mark_started = getattr(service, "mark_runtime_turn_started", None)
        if callable(mark_started):
            mark_started(context, activation_identity=activation_identity)

    # ------------------------------------------------------------------
    # Input building
    # ------------------------------------------------------------------

    def _build_input(self, request: AgentRequest) -> list[Dict[str, Any]]:
        """Convert AgentRequest into Codex UserInput items."""
        return self._build_native_input(request.message, request.files, getattr(request, "input_metadata", None))

    def _build_native_input(
        self,
        message: str,
        files: Sequence[FileAttachment] | None,
        input_metadata: AgentInputMetadata | None,
    ) -> list[Dict[str, Any]]:
        """Build both turn/start and turn/steer input without dropping images."""
        items: list[Dict[str, Any]] = []

        # Text input
        if files:
            # Append file info like Claude agent does
            file_lines = ["", "[User Attachments]"]
            for attachment in files:
                if not attachment.local_path:
                    continue
                is_image = (attachment.mimetype or "").startswith("image/")
                if is_image:
                    # Send as localImage input
                    items.append(
                        {
                            "type": "localImage",
                            "path": attachment.local_path,
                        }
                    )
                else:
                    size_str = f", {attachment.size} bytes" if attachment.size else ""
                    file_lines.append(f"- File: {attachment.local_path} ({attachment.mimetype}{size_str})")
            if len(file_lines) > 2:
                message = f"{message}\n" + "\n".join(file_lines)

        message = self.render_input(message, input_metadata)
        if message:
            items.insert(0, {"type": "text", "text": message})

        return items

    # ------------------------------------------------------------------
    # Callback handlers (wired to transport)
    # ------------------------------------------------------------------

    async def _on_notification(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        runtime: _CodexRuntime | None = None,
    ) -> None:
        """Route a server notification from one app-server to the event handler."""
        if runtime is not None and method == "thread/closed":
            released = runtime.released_threads.get(self._extract_thread_id(params))
            if released is not None:
                released.set()
            return
        if self._handle_connection_probe_notification(method, params):
            return
        request = self._find_request_for_notification(method, params)
        if not request:
            thread_id = self._extract_thread_id(params)
            turn_id = self._extract_turn_id(params)
            logger.debug(
                "No active request for Codex notification %s (thread=%s turn=%s)",
                method,
                thread_id,
                turn_id,
            )
            return
        if runtime is not None and not self._extract_turn_id(params):
            # Turn ids are unique across processes, so only thread-scoped
            # events can reach the wrong turn: a Session that moved on must not
            # receive late events from the generation it left.
            generation = self._session_generations.get(request.base_session_id)
            if generation is not None and generation.runtime is not runtime:
                logger.debug(
                    "Dropping Codex notification %s from a previous generation of session %s",
                    method,
                    request.base_session_id,
                )
                return

        if self._notification_is_real_progress(method):
            if runtime is not None:
                self._touch_runtime(runtime)
            self._touch_session_activity(request.base_session_id)
        await self._event_handler.handle_notification(method, params, request)
        if runtime is not None and method == "turn/completed":
            # A turn that ends on a retiring generation may be its last work.
            self._schedule_reap(runtime.cwd)

    def _handle_connection_probe_notification(
        self,
        method: str,
        params: Dict[str, Any],
    ) -> bool:
        thread_id = self._extract_thread_id(params)
        turn_id = self._extract_turn_id(params)
        probe_turns = getattr(self, "_connection_probe_turns", {})
        if not thread_id and turn_id:
            thread_id = probe_turns.get(turn_id, "")
        state = getattr(self, "_connection_probes", {}).get(thread_id)
        if state is None:
            return False
        if turn_id:
            state.turn_id = turn_id
            probe_turns[turn_id] = thread_id

        if method == "item/completed":
            item = params.get("item") if isinstance(params, dict) else None
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = str(item.get("text") or "").strip()
                if text:
                    state.response_text = text
            return True
        if method == "error":
            error = params.get("error") if isinstance(params, dict) else params
            detail = (
                error.get("message")
                if isinstance(error, dict)
                else str(error or "Codex error")
            )
            state.record_diagnostic(str(detail or "Codex error"))
            if params.get("willRetry") is True:
                return True
            if not state.terminal.done():
                state.terminal.set_result(("error", str(detail or "Codex error")))
            return True
        if method != "turn/completed":
            return True

        turn = params.get("turn") if isinstance(params, dict) else None
        status = turn.get("status") if isinstance(turn, dict) else None
        if status == "completed":
            outcome = ("success", state.response_text)
        elif status == "interrupted":
            outcome = ("error", "Codex turn was interrupted")
        elif status == "failed":
            error = turn.get("error") if isinstance(turn, dict) else None
            detail = (
                error.get("message")
                if isinstance(error, dict)
                else str(error or "Codex turn failed")
            )
            state.record_diagnostic(str(detail or "Codex turn failed"))
            outcome = ("error", str(detail or "Codex turn failed"))
        else:
            outcome = ("error", f"Codex turn ended with status: {status or 'unknown'}")
        if not state.terminal.done():
            state.terminal.set_result(outcome)
        return True

    async def _on_server_request(
        self,
        cwd: str,
        req_id: int | str,
        method: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle server requests that Avibe opts into or auto-approves."""
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            logger.info("Auto-approving Codex %s (item=%s)", method, params.get("itemId"))
            return {"approved": True}

        if method == "item/tool/requestUserInput":
            # Avibe conversations collect user input through the next normal
            # message. An empty answer map is the app-server's valid
            # unsupported/cancelled response for this experimental request.
            logger.info(
                "Declining unsupported Codex requestUserInput (item=%s)",
                params.get("itemId"),
            )
            return {"answers": {}}

        if method == "currentTime/read":
            return {"currentTimeAt": int(time.time())}

        logger.warning("Unsupported Codex server request: %s", method)
        raise NotImplementedError(f"Unsupported Codex server request: {method}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_request_for_thread(self, thread_id: str) -> Optional[AgentRequest]:
        """Look up the active AgentRequest for a given Codex threadId."""
        base_session_id = self._session_mgr.find_base_session_id_for_thread(thread_id)
        if not base_session_id:
            return None
        return self._turn_registry.get_latest_request(base_session_id)

    def _find_request_for_notification(self, method: str, params: Dict[str, Any]) -> Optional[AgentRequest]:
        turn_id = self._extract_turn_id(params)
        if turn_id:
            request = self._turn_registry.get_request_for_turn(turn_id)
            if request:
                return request

            thread_id = self._extract_thread_id(params)
            if not thread_id:
                return None
            if method != "turn/started":
                return None
            base_session_id = self._session_mgr.find_base_session_id_for_thread(thread_id)
            if not base_session_id:
                return None

            bootstrap_state = self._turn_registry.bootstrap_turn(turn_id, base_session_id, thread_id)
            if bootstrap_state:
                logger.info(
                    "Bootstrapped Codex turn %s for notification %s on session %s",
                    turn_id,
                    method,
                    base_session_id,
                )
                return bootstrap_state.request
            return None

        thread_id = self._extract_thread_id(params)
        if thread_id:
            return self._find_request_for_thread(thread_id)
        return None

    def _extract_thread_id(self, params: Dict[str, Any]) -> str:
        thread_id = params.get("threadId", "")
        if not thread_id:
            thread_obj = params.get("thread")
            if isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id", "")
        return thread_id

    def _extract_turn_id(self, params: Dict[str, Any]) -> str:
        turn_id = params.get("turnId", "")
        if not turn_id:
            turn_obj = params.get("turn")
            if isinstance(turn_obj, dict):
                turn_id = turn_obj.get("id", "")
        return turn_id

    async def _delete_ack(self, request: AgentRequest) -> None:
        service = getattr(self.controller, "processing_indicator", None)
        if service is not None:
            await service.delete_ack_message(request)
            return
        ack_id = request.ack_message_id
        if ack_id and hasattr(self.im_client, "delete_message"):
            try:
                await self.im_client.delete_message(request.context.channel_id, ack_id)
            except Exception as err:
                logger.debug("Could not delete ack message: %s", err)
            finally:
                request.ack_message_id = None

    @staticmethod
    def _cwd_inode(cwd: str) -> Optional[int]:
        try:
            return os.stat(cwd).st_ino
        except OSError:
            return None

    @staticmethod
    def _touch_runtime(runtime: _CodexRuntime | None) -> None:
        if runtime is not None:
            runtime.last_activity = time.monotonic()

    def _touch_session_activity(self, base_session_id: str) -> None:
        if not hasattr(self, "_session_last_activity"):
            self._session_last_activity = {}
        if base_session_id:
            self._session_last_activity[base_session_id] = time.monotonic()

    @staticmethod
    def _notification_is_real_progress(method: str) -> bool:
        return method in {
            "item/completed",
            "item/agentMessage/delta",
            "item/commandExecution/outputDelta",
            "item/reasoning/summaryTextDelta",
        }

    def _has_active_turns_for_cwd(self, cwd: str) -> bool:
        return any(
            self._session_has_turn(base_session_id)
            for base_session_id in self._session_mgr.sessions_for_cwd(cwd)
        )
