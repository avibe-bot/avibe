"""Codex agent — persistent app-server mode with JSON-RPC 2.0 transport."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from config import paths
from config.v2_config import (
    DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS,
    DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER,
)
from core.avibe_cloud import avibe_cloud_url_available
from core.backend_failure import emit_backend_failure
from core.caller_context import caller_env_for_platform_payload
from core.message_output import stop_output_for, terminal_output_for
from core.managed_skills import (
    managed_skill_claude_cli_path,
    managed_skill_environment,
    managed_skill_project_base,
)
from core.native_dispatch_phase import mark_backend_dispatch_attempted
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerRequest,
    SteerResult,
    result as steer_result,
)
from core.services.session_fork import fork_source_state, pending_native_fork
from core.system_prompt_injection import (
    build_forked_session_correction_prompt,
    build_system_prompt_injection,
    get_enabled_agents_for_prompt,
    memory_cli_prompt_admitted,
)
from core.resource_governance import governor_from_controller
from core.runtime_activation import RuntimeActivationIdentity
from core.runtime_ownership import (
    RuntimeResourceTarget,
    RuntimeSessionBinding,
    SessionRuntimeDisposition,
    wake_runtime_ownership,
)
from modules.agents.base import AgentRequest, BaseAgent
from modules.agents.subagent_router import SubagentDefinition, load_codex_subagent
from modules.agents.codex.event_handler import CodexEventHandler
from modules.agents.codex.session import CodexSessionManager
from modules.agents.codex.transport import CodexTransport
from modules.agents.codex.turn_state import CodexTurnRegistry
from vibe.codex_config import LEGACY_MANAGED_PROVIDER_IDS, MANAGED_PROVIDER_ID
from vibe.i18n import t as i18n_t
from vibe.message_identity import is_input_turn

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from modules.agents.model_hub import ModelHubLaunch

_CODEX_MANAGED_PROVIDER_IDS = frozenset((MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS))
_CODEX_MODEL_HUB_PROVIDER_ID = "avibe_model_hub"
_CODEX_DEFAULT_PROVIDER_ID = "openai"
_CODEX_REBINDABLE_SAME_ID_PROVIDERS = _CODEX_MANAGED_PROVIDER_IDS | frozenset(
    (_CODEX_MODEL_HUB_PROVIDER_ID,)
)
CODEX_CALLER_ENV_DIR = "codex-caller-env"
CODEX_CONNECTION_PROBE_DIR = "codex-connection-probe"
CODEX_PROMPT_STRATEGY_METADATA_KEY = "codex_prompt_strategy"


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


class CodexModelHubCatalogUnavailableError(RuntimeError):
    """The configured Codex binary could not provide Hub launch metadata."""


class CodexPromptRefreshUnavailableError(RuntimeError):
    """The current app-server cannot safely refresh a persisted thread prompt."""


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
    """Codex CLI integration via persistent ``codex app-server`` subprocess.

    One transport (subprocess) is maintained per unique working directory.
    Multiple Slack threads in the same channel share a transport but each
    gets its own Codex thread.
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
        self._model_hub_catalog_path: Path | None = None
        self._model_hub_catalog_lock = asyncio.Lock()
        self._model_hub_catalog_generation = 0

        # cwd → CodexTransport (one persistent process per working dir)
        self._transports: Dict[str, CodexTransport] = {}
        self._transport_locks: Dict[str, asyncio.Lock] = {}
        self._transport_last_activity: Dict[str, float] = {}
        self._session_last_activity: Dict[str, float] = {}
        # cwd inode at app-server spawn time, keyed like ``_transports``. A
        # cached app-server whose directory was deleted (even if re-created
        # with the same path) sits in a dead inode and fails every
        # ``thread/start`` with a misleading "failed to load configuration:
        # No such file or directory" (#561); the inode comparison detects
        # that staleness BEFORE paying a failed RPC.
        self._transport_cwd_inodes: Dict[str, Optional[int]] = {}

        self._session_mgr = CodexSessionManager()
        self._turn_registry = CodexTurnRegistry()
        self._event_handler = CodexEventHandler(self)

        # base_session_id → asyncio.Lock (serialize turn lifecycle per session)
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # base_session_id → (thread_id, developer_instructions)
        self._thread_developer_instructions: Dict[str, tuple[str, str]] = {}
        # base_session_id → (thread_id, "collaboration" | "fallback")
        self._thread_prompt_strategies: Dict[str, tuple[str, str]] = {}
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
        self._connection_probe_cwds: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    def backend_alive(self, context) -> Optional[bool]:
        """Liveness via the CodexTransport for this turn's working directory.

        Resolves base_session_id → cwd → transport and returns transport.is_alive.
        Returns None (unknown) when anything can't be resolved, so the status
        bubble never false-alarms ⚠️."""
        payload = getattr(context, "platform_specific", None) or {}
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        if not base_session_id:
            return None
        cwd = self._session_mgr.get_cwd(base_session_id)
        if not cwd:
            return None
        transport = self._transports.get(cwd)
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
        cwd = self._session_mgr.get_cwd(base_session_id) if base_session_id else None
        transport = self._transports.get(cwd) if cwd else None
        if transport is None:
            return lambda: None
        return lambda: self._transport_alive(transport)

    def can_reuse_direct_connection_probe(self, cwd: str) -> bool:
        """Return whether a cached transport can test direct credentials."""

        transport = self._transports.get(cwd)
        return bool(
            transport is not None
            and getattr(transport, "runtime_fingerprint", "direct") == "direct"
            and os.path.isdir(cwd)
        )

    async def probe_connection(
        self,
        cwd: str,
        *,
        model: str | None = None,
        on_diagnostic: Callable[[str], None] | None = None,
    ) -> str:
        """Run a read-only ephemeral turn on the normal persistent app-server."""

        probe_cwd = paths.get_runtime_dir() / CODEX_CONNECTION_PROBE_DIR
        probe_cwd.mkdir(parents=True, exist_ok=True)
        transport: CodexTransport | None = None
        state: _CodexConnectionProbeState | None = None
        thread_id = ""
        closed_task: asyncio.Task[None] | None = None
        probe_cwds = self._connection_probe_cwds
        owns_probe_cwd = False
        try:
            if (
                getattr(self, "_registered_runtime", True)
                and not self.can_reuse_direct_connection_probe(cwd)
            ):
                raise CodexConnectionProbeRuntimeMismatchError(
                    "No cached direct Codex transport is available for the probe"
                )
            transport = await self._get_or_create_transport(
                cwd,
                allow_runtime_replacement=False,
            )
            probe_cwds[cwd] = probe_cwds.get(cwd, 0) + 1
            owns_probe_cwd = True

            thread_response = await transport.send_request(
                "thread/start",
                {
                    "cwd": str(probe_cwd),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
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
            }
            if isinstance(model, str) and model.strip():
                turn_params["model"] = model.strip()
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
            self._touch_transport_activity(cwd)
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
                if closed_task is not None:
                    closed_task.cancel()
                    await asyncio.gather(closed_task, return_exceptions=True)
                if owns_probe_cwd:
                    remaining = probe_cwds.get(cwd, 0) - 1
                    if remaining > 0:
                        probe_cwds[cwd] = remaining
                    else:
                        probe_cwds.pop(cwd, None)

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
        1. Get or create transport for the working directory
        2. Get or create a Codex thread for this Slack thread
        3. If a turn is active → interrupt it first
        4. Start a new turn with the user's message
        """
        # Serialize turn lifecycle per session
        if request.base_session_id not in self._session_locks:
            self._session_locks[request.base_session_id] = asyncio.Lock()

        async with self._session_locks[request.base_session_id]:
            launch = None
            try:
                if getattr(self.controller, "model_hub_runtime", None) is not None:
                    from modules.agents.model_hub import bind_launch, resolve_model_hub_launch

                    _, requested_model, _, _ = self._resolve_codex_agent_settings(request)
                    launch = await resolve_model_hub_launch(
                        self.controller,
                        "codex",
                        requested_model or "",
                        process_scope=request.working_path,
                    )
                    bind_launch(request.context, launch)
                    await self._interrupt_active_turn_before_runtime_change(request, launch)
                    transport = await self._get_or_create_transport(request.working_path, launch)
                else:
                    transport = await self._get_or_create_transport(request.working_path)
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
                logger.error("Failed to start Codex transport: %s", e, exc_info=True)
                await self._record_model_hub_native_failure(request.context, str(e))
                if isinstance(e, CodexModelHubCatalogUnavailableError):
                    language = str(
                        getattr(getattr(self.controller, "config", None), "language", "en")
                        or "en"
                    )
                    display_text = f"❌ {i18n_t('modelHub.errors.codex_catalog_unavailable', language)}"
                else:
                    display_text = f"❌ Failed to start Codex CLI: {e}"
                await emit_backend_failure(
                    self.controller,
                    request.context,
                    self.name,
                    str(e),
                    display_text=display_text,
                    request=request,
                )
                await self._remove_ack_reaction(request)
                self._event_handler._release_stream_turn(request.context)
                return

            # Resolve after queued turns, then bind this session to the runtime.
            self._session_mgr.set_session_key(request.base_session_id, request.session_key)
            self._session_mgr.set_cwd(request.base_session_id, request.working_path)
            self._touch_transport_activity(request.working_path)
            await self._delete_ack(request)

            self._turn_registry.remember_request(request)
            developer_instructions: Optional[str] = None
            try:
                # Get or create thread (with resume support)
                thread_id = self._session_mgr.get_thread_id(request.base_session_id)

                if not thread_id:
                    thread_id = await self._start_or_resume_thread(transport, request)

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
                # payload byte-stable, this avoids repeating Memory admission
                # side effects while the same request refreshes and starts.
                self.ensure_agent_session_id(request)
                developer_instructions = await self._build_thread_developer_instructions(request)
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
                # Safety net: if the thread is stale (e.g. Codex server-side
                # expiry, or the proactive invalidation in _get_or_create_transport
                # was bypassed by a race), invalidate and retry once.
                if self._is_recoverable_transport_error(e):
                    logger.warning(
                        "Recoverable Codex transport failure for session %s, restarting transport and retrying: %s",
                        request.base_session_id,
                        e,
                    )
                    await self._drop_transport_after_failure(request.working_path, transport, request)
                    try:
                        if launch is None:
                            transport = await self._get_or_create_transport(request.working_path)
                        else:
                            transport = await self._get_or_create_transport(request.working_path, launch)
                        self._touch_transport_activity(request.working_path)
                        thread_id = await self._start_or_resume_thread(transport, request)
                        if developer_instructions is None:
                            developer_instructions = await self._build_thread_developer_instructions(request)
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
                self._turn_registry.clear_pending_turn_start(request.base_session_id, request)
                logger.error("Error in Codex handle_message: %s", e, exc_info=True)
                await self._record_model_hub_native_failure(request.context, str(e))
                error_text = self._error_display_text(e)
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
        transport = self._transports.get(cwd)
        if not thread_id:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="missing_native_thread", backend=self.name)
        if transport is None or not transport.is_initialized:
            return steer_result(SteerOutcome.REFUSED, reason="runtime_unavailable", backend=self.name)

        try:
            response = await transport.send_request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": request.expected_native_turn_id,
                    "input": [{"type": "text", "text": request.text}],
                },
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
                return steer_result(
                    SteerOutcome.NOT_ACTIVE,
                    reason="native_turn_mismatch",
                    backend=self.name,
                    diagnostic=diagnostic,
                )
            if "activeturnnotsteerable" in lowered or "not steerable" in lowered:
                return steer_result(
                    SteerOutcome.REFUSED,
                    reason="native_turn_not_steerable",
                    backend=self.name,
                    diagnostic=diagnostic,
                )
            return steer_result(
                SteerOutcome.REFUSED,
                reason="backend_refused",
                backend=self.name,
                diagnostic=diagnostic,
            )
        except ConnectionError as exc:
            diagnostic = str(exc)
            if diagnostic in {
                "Codex app-server transport is not available",
                "Codex app-server stdin is not available",
            }:
                return steer_result(
                    SteerOutcome.REFUSED,
                    reason="runtime_unavailable",
                    backend=self.name,
                    diagnostic=diagnostic,
                )
            self._touch_transport_activity(cwd)
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="acknowledgement_ambiguous",
                backend=self.name,
                diagnostic=diagnostic,
            )
        except TimeoutError as exc:
            self._touch_transport_activity(cwd)
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="acknowledgement_ambiguous",
                backend=self.name,
                diagnostic=str(exc),
            )

        self._touch_transport_activity(cwd)
        response_turn_id = str(response.get("turnId") or "").strip()
        if response_turn_id != request.expected_native_turn_id:
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="untrusted_acknowledgement",
                backend=self.name,
                response_turn_id=response_turn_id,
            )
        return steer_result(
            SteerOutcome.ACCEPTED,
            backend=self.name,
            thread_id=thread_id,
            turn_id=response_turn_id,
        )

    async def handle_stop(self, request: AgentRequest) -> bool:
        """Gracefully interrupt the active turn."""
        thread_id = self._session_mgr.get_thread_id(request.base_session_id)
        turn_id = self._turn_registry.get_active_turn(request.base_session_id)

        if not thread_id or not turn_id:
            request.stop_failure_reason = "not_active"
            return False

        transport = self._transports.get(request.working_path)
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
        """Clear sessions scoped to a specific session_key."""
        self.sessions.clear_agent_sessions(session_key, self.name)

        # Use session_key index (not _threads) so sessions with
        # invalidated threads are still cleaned up properly.
        to_clear = self._session_mgr.get_sessions_by_session_key(session_key)

        count = self._session_mgr.clear_by_session_key(session_key)

        # Clean up in-memory turn state and session locks for cleared sessions
        for bid in to_clear:
            self._turn_registry.clear_session(bid)
            self._session_locks.pop(bid, None)
            self._clear_thread_developer_instructions(bid)

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

    async def refresh_auth_state(self) -> None:
        """Drop app-server runtime state so future turns pick up fresh auth."""
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if not hasattr(self, "_session_last_activity"):
            self._session_last_activity = {}
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
        if not hasattr(self, "_transport_locks"):
            self._transport_locks = {}
        transport_items = list(self._transports.items())
        self._session_last_activity.clear()
        stopped = 0
        for cwd, transport in transport_items:
            lock = self._transport_locks.setdefault(cwd, asyncio.Lock())
            async with lock:
                try:
                    detached = await self._stop_and_detach_transport_generation(
                        cwd,
                        transport,
                    )
                except Exception as exc:
                    logger.warning("Failed to stop Codex transport during auth refresh: %s", exc)
                    continue
                if not detached:
                    continue
                self._retire_model_hub_process_scope(cwd)
                stopped += 1

        for base_session_id in base_session_ids:
            self._session_mgr.invalidate_thread(base_session_id)
            self._turn_registry.clear_session(base_session_id)
            self._clear_thread_developer_instructions(base_session_id)

        logger.info("Refreshed Codex auth state across %d transport(s)", stopped)

    async def refresh_runtime_config(self, codex_config: Any) -> None:
        """Reload persisted runtime config before respawning app-server transports."""
        self.codex_config = codex_config
        self.controller.config.codex = codex_config
        self._model_hub_catalog_generation += 1
        self._model_hub_catalog_path = None
        await self.refresh_auth_state()

    async def invalidate_model_hub_runtime(self) -> None:
        """Make the next Hub launch rebuild its catalog without touching Direct transports."""
        self._model_hub_catalog_generation += 1
        self._model_hub_catalog_path = None

    async def prepare_model_hub_runtime(self) -> Path:
        """Bind Hub metadata to this Agent's exact configured Codex binary."""
        from vibe import backend_model_catalog

        async with self._model_hub_catalog_lock:
            if self._model_hub_catalog_path is not None:
                return self._model_hub_catalog_path
            generation = self._model_hub_catalog_generation
            binary = self.codex_config.binary
            configured_models = None
            model_hub_service = getattr(self.controller, "model_hub_service", None)
            store = getattr(model_hub_service, "store", None)
            if store is not None:
                configured_models = [
                    model.to_payload()
                    for model in store.load().agents["codex"].models
                ]
            try:
                path = await asyncio.to_thread(
                    backend_model_catalog.prepare_codex_hub_catalog,
                    binary,
                    None,
                    configured_models,
                )
            except Exception as exc:
                raise CodexModelHubCatalogUnavailableError(
                    "Codex Model Hub catalog preparation failed"
                ) from exc
            if self._model_hub_catalog_generation != generation:
                raise CodexModelHubCatalogUnavailableError(
                    "Codex Model Hub catalog generation changed during preparation"
                )
            self._model_hub_catalog_path = path
            return path

    async def prepare_resume_binding(
        self,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        """Restart a Codex transport only when the resumed session owns that cwd."""
        if not hasattr(self, "_transport_locks"):
            self._transport_locks = {}
        lock = self._transport_locks.setdefault(working_path, asyncio.Lock())
        async with lock:
            transport = self._transports.get(working_path)
            if transport is None:
                return

            affected_sessions = self._session_mgr.sessions_for_cwd(working_path)
            other_sessions = [session_id for session_id in affected_sessions if session_id != base_session_id]
            if other_sessions:
                logger.info(
                    "Skipping Codex resume preparation for %s; cwd=%s is shared by %d other session(s)",
                    base_session_id,
                    working_path,
                    len(other_sessions),
                )
                return
            try:
                detached = await self._stop_and_detach_transport_generation(
                    working_path,
                    transport,
                )
            except Exception as exc:
                logger.warning("Failed to stop Codex transport during resume preparation: %s", exc)
                return
            if not detached:
                logger.warning(
                    "Failed to retire Codex transport generation during resume preparation for cwd=%s",
                    working_path,
                )
                return
            self._retire_model_hub_process_scope(working_path)

        self._session_mgr.invalidate_thread(base_session_id)
        self._turn_registry.clear_session(base_session_id)
        self._clear_thread_developer_instructions(base_session_id)
        logger.info("Prepared Codex runtime for resumed session %s", base_session_id)

    async def shutdown_runtime(self) -> None:
        """Stop all app-server transports during vibe-remote shutdown."""
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if not hasattr(self, "_transport_locks"):
            self._transport_locks = {}
        if not hasattr(self, "_session_locks"):
            self._session_locks = {}
        if not hasattr(self, "_session_last_activity"):
            self._session_last_activity = {}
        transport_items = list(self._transports.items())
        self._session_last_activity.clear()
        stopped = 0
        for cwd, transport in transport_items:
            lock = self._transport_locks.setdefault(cwd, asyncio.Lock())
            async with lock:
                try:
                    detached = await self._stop_and_detach_transport_generation(
                        cwd,
                        transport,
                    )
                except Exception as exc:
                    logger.warning("Failed to stop Codex transport during shutdown: %s", exc)
                    continue
                if not detached:
                    continue
                self._retire_model_hub_process_scope(cwd)
                stopped += 1

        for base_session_id in list(self._session_mgr.all_base_sessions()):
            session_key = self._session_mgr.get_session_key(base_session_id)
            if session_key:
                self.sessions.clear_agent_session_mapping(session_key, self.name, base_session_id)
            self._session_mgr.clear(base_session_id)
            self._turn_registry.clear_session(base_session_id)
            self._clear_thread_developer_instructions(base_session_id)

        self._session_locks.clear()
        self._transport_locks.clear()
        logger.info("Stopped Codex runtime across %d transport(s)", stopped)

    def _bind_runtime_agent_session_id(self, request: AgentRequest) -> None:
        setter = getattr(self._session_mgr, "set_agent_session_id", None)
        if not callable(setter):
            return
        payload = getattr(request.context, "platform_specific", None) or {}
        session_id = payload.get("agent_session_id") if isinstance(payload, dict) else None
        setter(request.base_session_id, session_id)

    def _error_display_text(self, error: BaseException) -> str:
        if isinstance(error, CodexPromptRefreshUnavailableError):
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            return f"❌ {i18n_t('error.codexPromptRefreshUnavailable', language)}"
        return f"❌ Codex error: {error}"

    def _runtime_ownership_target_for_cwd(
        self,
        cwd: str,
    ) -> RuntimeResourceTarget | None:
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

        bindings: list[RuntimeSessionBinding] = []
        for base_session_id in sessions_for_cwd(cwd):
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
            resource_key=cwd,
            bindings=tuple(bindings),
            known_activity_runtime_keys=known_activity_keys,
            known_fallback_route_keys=known_route_keys,
            durable_session_workdir=cwd,
        )

    def _runtime_ownership_snapshot_for_cwd(self, cwd: str):
        target = self._runtime_ownership_target_for_cwd(cwd)
        provider = getattr(getattr(self, "controller", None), "runtime_ownership", None)
        snapshot = getattr(provider, "snapshot", None)
        if target is None or not callable(snapshot):
            logger.error(
                "Codex runtime ownership mapping unavailable for cwd=%s",
                cwd,
            )
            return None
        result = snapshot(target)
        wake_runtime_ownership(self.controller, result)
        return result

    async def _runtime_ownership_snapshots_for_cwds(
        self,
        cwds: tuple[str, ...],
    ) -> tuple[Any, ...] | None:
        """Read one backend snapshot batch without blocking the controller loop."""

        if not cwds:
            return ()
        provider = getattr(getattr(self, "controller", None), "runtime_ownership", None)
        snapshot_many = getattr(provider, "snapshot_many", None)
        targets = tuple(self._runtime_ownership_target_for_cwd(cwd) for cwd in cwds)
        if callable(snapshot_many) and all(target is not None for target in targets):
            snapshots = await asyncio.to_thread(snapshot_many, targets)
            for snapshot in snapshots:
                wake_runtime_ownership(self.controller, snapshot)
            return tuple(snapshots)

        # Tests and legacy embedders can still provide the older single-target
        # probe. Keep it off the event loop; production SQLite providers take the
        # batched path above.
        snapshots = await asyncio.gather(
            *(
                asyncio.to_thread(self._runtime_ownership_snapshot_for_cwd, cwd)
                for cwd in cwds
            )
        )
        if any(snapshot is None for snapshot in snapshots):
            return None
        return tuple(snapshots)

    async def _runtime_ownership_snapshot_for_cwd_async(self, cwd: str):
        snapshots = await self._runtime_ownership_snapshots_for_cwds((cwd,))
        return snapshots[0] if snapshots else None

    async def runtime_ownership_snapshots(self) -> tuple[Any, ...] | None:
        return await self._runtime_ownership_snapshots_for_cwds(tuple(self._transports))

    @staticmethod
    def _transport_activation_identity(
        transport: CodexTransport | None,
    ) -> RuntimeActivationIdentity | None:
        identity = getattr(transport, "_vibe_runtime_activation_identity", None)
        return identity if isinstance(identity, RuntimeActivationIdentity) else None

    def _attach_transport_activation(
        self,
        cwd: str,
        transport: CodexTransport,
    ) -> RuntimeActivationIdentity | None:
        if not getattr(self, "_registered_runtime", True):
            return None
        registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
        if registry is None:
            return None
        existing = self._transport_activation_identity(transport)
        if existing is not None and registry.is_current(existing):
            return existing
        identity = registry.attach(self.name, cwd)
        setattr(transport, "_vibe_runtime_activation_identity", identity)
        return identity

    def _reserve_transport_retirement(
        self,
        cwd: str,
        transport: CodexTransport,
    ) -> tuple[Any, Any] | None:
        if self._transports.get(cwd) is not transport:
            return None
        if not getattr(self, "_registered_runtime", True):
            return (None, None)
        registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
        if registry is None:
            return (None, None)
        identity = self._transport_activation_identity(transport)
        if identity is None:
            identity = self._attach_transport_activation(cwd, transport)
        if identity is None:
            return None
        reservation = registry.reserve_retirement(identity)
        return (registry, reservation) if reservation is not None else None

    @staticmethod
    def _finish_transport_retirement(
        reserved: tuple[Any, Any],
        *,
        retire: bool,
    ) -> bool:
        registry, reservation = reserved
        if registry is None:
            return True
        return bool(registry.finish_retirement(reservation, retire=retire))

    def _detach_transport_bookkeeping(
        self,
        cwd: str,
        transport: CodexTransport,
    ) -> bool:
        """Remove only the exact transport after its process has stopped."""
        if self._transports.get(cwd) is not transport:
            return False
        self._transports.pop(cwd, None)
        if hasattr(self, "_transport_last_activity"):
            self._transport_last_activity.pop(cwd, None)
        self._cwd_inodes().pop(cwd, None)
        return True

    async def _stop_and_detach_transport_generation(
        self,
        cwd: str,
        transport: CodexTransport,
        *,
        final_predicate: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        """Stop one exact generation, retaining it if validation or stop fails."""

        reserved = self._reserve_transport_retirement(cwd, transport)
        if reserved is None:
            return False
        try:
            if final_predicate is not None and not await final_predicate():
                self._finish_transport_retirement(reserved, retire=False)
                return False
            await transport.stop()
        except BaseException:
            self._finish_transport_retirement(reserved, retire=False)
            raise
        if not self._finish_transport_retirement(reserved, retire=True):
            raise RuntimeError(
                f"Codex transport retirement lost its exact generation for cwd={cwd}"
            )
        return self._detach_transport_bookkeeping(cwd, transport)

    def runtime_activation_identity_for_request(
        self,
        request: Any,
    ) -> RuntimeActivationIdentity | None:
        cwd = str(getattr(request, "working_path", "") or "").strip()
        if not cwd:
            metadata = getattr(request, "metadata", None)
            if isinstance(metadata, dict):
                cwd = str(metadata.get("session_workdir") or "").strip()
        if cwd:
            return self._transport_activation_identity(self._transports.get(cwd))

        session_key = str(getattr(request, "session_key", "") or "").strip()
        if not session_key:
            return None
        get_sessions = getattr(self._session_mgr, "get_sessions_by_session_key", None)
        get_cwd = getattr(self._session_mgr, "get_cwd", None)
        if not callable(get_sessions) or not callable(get_cwd):
            raise ValueError("Codex Session route mapping is unavailable")

        live_identities: dict[str, RuntimeActivationIdentity] = {}
        for base_session_id in get_sessions(session_key):
            mapped_cwd = str(get_cwd(base_session_id) or "").strip()
            if not mapped_cwd or mapped_cwd in live_identities:
                continue
            transport = self._transports.get(mapped_cwd)
            if transport is None:
                continue
            identity = self._transport_activation_identity(transport)
            if identity is None:
                raise ValueError("Codex runtime generation is not attached")
            live_identities[mapped_cwd] = identity

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
        transport = self._transports.get(normalized_workdir)
        if transport is None:
            return None
        identity = self._transport_activation_identity(transport)
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
        self._touch_transport_activity(request.working_path)
        self._touch_session_activity(request.base_session_id)

    def _stuck_active_sessions_for_cwd(
        self,
        cwd: str,
        *,
        now: float,
        cap: float | None,
    ) -> list[str]:
        if cap is None:
            return []
        if not hasattr(self, "_session_last_activity"):
            self._session_last_activity = {}
        stuck = []
        for base_session_id in self._session_mgr.sessions_for_cwd(cwd):
            if not self._turn_registry.get_active_turn(base_session_id):
                continue
            last_progress = self._session_last_activity.get(base_session_id)
            if last_progress is not None and now - last_progress >= cap:
                stuck.append(base_session_id)
        return stuck

    async def evict_idle_transports(self, idle_timeout: float) -> int:
        """Stop idle Codex transports after two exact ownership snapshots."""
        if idle_timeout <= 0:
            return 0
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if not hasattr(self, "_transport_locks"):
            self._transport_locks = {}
        if not hasattr(self, "_session_locks"):
            self._session_locks = {}
        if not hasattr(self, "_session_last_activity"):
            self._session_last_activity = {}

        stuck_active_cap = self._stuck_active_idle_eviction_cap(idle_timeout)
        now = time.monotonic()
        evicted = 0
        initial_cwds = tuple(self._transports)
        initial_snapshots = await self._runtime_ownership_snapshots_for_cwds(
            initial_cwds
        )
        if initial_snapshots is None:
            return 0
        ownership_by_cwd = dict(zip(initial_cwds, initial_snapshots, strict=True))

        for cwd, last_activity in list(self._transport_last_activity.items()):
            transport = self._transports.get(cwd)
            if transport is None:
                self._transport_last_activity.pop(cwd, None)
                continue
            ownership = ownership_by_cwd.get(cwd)
            if ownership is None:
                continue
            stuck_sessions = self._stuck_active_sessions_for_cwd(
                cwd,
                now=now,
                cap=stuck_active_cap,
            )
            has_active = self._has_active_turns_for_cwd(cwd)
            idle_for = now - last_activity
            ordinary_candidate = (
                not ownership.blocks_reclamation
                and not has_active
                and idle_for >= idle_timeout
            )
            stuck_candidate = bool(stuck_sessions) and ownership.disposition not in {
                SessionRuntimeDisposition.TRANSITIONING,
                SessionRuntimeDisposition.UNKNOWN,
            }
            if not ordinary_candidate and not stuck_candidate:
                continue

            lock = self._transport_locks.setdefault(cwd, asyncio.Lock())
            async with lock:
                current_transport = self._transports.get(cwd)
                current_last_activity = self._transport_last_activity.get(cwd)
                if current_transport is None or current_transport is not transport:
                    continue
                if current_last_activity is None:
                    continue
                ownership = await self._runtime_ownership_snapshot_for_cwd_async(cwd)
                if ownership is None:
                    continue
                current_now = time.monotonic()
                idle_for = current_now - current_last_activity
                stuck_sessions = self._stuck_active_sessions_for_cwd(
                    cwd,
                    now=current_now,
                    cap=stuck_active_cap,
                )
                if ownership.disposition in {
                    SessionRuntimeDisposition.TRANSITIONING,
                    SessionRuntimeDisposition.UNKNOWN,
                }:
                    continue
                if ownership.blocks_reclamation and not stuck_sessions:
                    continue

                settled_stuck_sessions: set[str] = set()
                for base_session_id in stuck_sessions:
                    logger.warning(
                        "Settling stuck-active Codex session %s for cwd=%s after exact progress timeout",
                        base_session_id,
                        cwd,
                    )
                    await self._settle_stuck_active_request(base_session_id)
                    self._turn_registry.clear_session(base_session_id)
                    settled_stuck_sessions.add(base_session_id)
                    self._session_locks.pop(base_session_id, None)
                    self._session_last_activity.pop(base_session_id, None)

                if stuck_sessions:
                    ownership = await self._runtime_ownership_snapshot_for_cwd_async(cwd)
                    if ownership is None or ownership.blocks_reclamation:
                        continue
                final: dict[str, float] = {}

                async def final_reclamation_predicate() -> bool:
                    if self._transports.get(cwd) is not transport:
                        return False
                    latest_activity = self._transport_last_activity.get(cwd)
                    if latest_activity is None:
                        return False
                    current_ownership = (
                        await self._runtime_ownership_snapshot_for_cwd_async(cwd)
                    )
                    if current_ownership is None or current_ownership.blocks_reclamation:
                        return False
                    current_idle_for = time.monotonic() - latest_activity
                    final["idle_for"] = current_idle_for
                    return bool(
                        not self._has_active_turns_for_cwd(cwd)
                        and current_idle_for >= idle_timeout
                    )

                try:
                    detached = await self._stop_and_detach_transport_generation(
                        cwd,
                        transport,
                        final_predicate=final_reclamation_predicate,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to stop idle Codex transport for cwd=%s: %s",
                        cwd,
                        exc,
                    )
                    continue
                if not detached:
                    continue
                idle_for = final.get("idle_for", idle_for)

                logger.info(
                    "Evicting idle Codex transport for cwd=%s after %.1fs idle",
                    cwd,
                    idle_for,
                )
                self._retire_model_hub_process_scope(cwd)

                for base_session_id in list(self._session_mgr.sessions_for_cwd(cwd)):
                    self._session_mgr.invalidate_thread(base_session_id)
                    if base_session_id not in settled_stuck_sessions:
                        self._turn_registry.clear_session(base_session_id)
                    self._session_locks.pop(base_session_id, None)
                    self._session_last_activity.pop(base_session_id, None)
                    self._clear_thread_developer_instructions(base_session_id)

                evicted += 1

        return evicted

    def _retire_model_hub_process_scope(self, cwd: str) -> None:
        if not getattr(self, "_registered_runtime", True):
            return
        controller = getattr(self, "controller", None)
        router = getattr(controller, "model_hub_runtime", None)
        retire = getattr(router, "retire_process_scope", None)
        if callable(retire):
            retire("codex", cwd)

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
        """Idle cap after which an *active* transport is force-evicted.

        Returns ``None`` when the backstop is disabled (multiplier <= 0), in
        which case an active turn remains an absolute veto. Otherwise a
        transport with an active turn is force-evicted once it has been idle for
        ``max(idle_timeout * multiplier, floor)`` — the floor keeps the window
        sane even when ``idle_timeout`` is configured very small.
        """
        multiplier = DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER
        if multiplier <= 0:
            return None
        floor = max(0.0, float(DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS))
        return max(idle_timeout * multiplier, floor)

    def _is_transport_evictable(
        self,
        *,
        has_active: bool,
        idle_for: float,
        idle_timeout: float,
        stuck_active_cap: Optional[float],
    ) -> bool:
        """Decide whether an idle transport is eligible for eviction.

        Pure decision (no lookups), so callers evaluate the active-turn flag
        exactly once. An idle transport with no active turn is evictable once it
        crosses the normal ``idle_timeout``. A transport with an active turn is
        normally vetoed, but is force-evictable once it crosses
        ``stuck_active_cap`` (the absolute-time backstop) — the only path that
        reaps a wedged app-server whose ``turn/completed`` never arrived.
        """
        if has_active:
            if stuck_active_cap is None:
                return False
            return idle_for >= stuck_active_cap
        return idle_for >= idle_timeout

    # ------------------------------------------------------------------
    # Transport management
    # ------------------------------------------------------------------

    def _is_recoverable_transport_error(self, error: Exception) -> bool:
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

    async def _drop_transport_after_failure(
        self,
        cwd: str,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> None:
        """Remove a broken app-server and clear stale in-memory request state."""
        lock = self._transport_locks.setdefault(cwd, asyncio.Lock())
        async with lock:
            current = self._transports.get(cwd)
            should_invalidate_cwd_sessions = current is None or current is transport
            if current is transport:
                try:
                    detached = await self._stop_and_detach_transport_generation(
                        cwd,
                        transport,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to stop broken Codex transport for cwd=%s: %s",
                        cwd,
                        exc,
                    )
                    return
                if not detached:
                    return
            elif current is None:
                identity = self._transport_activation_identity(transport)
                registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
                if identity is not None and registry is not None and registry.is_current(identity):
                    logger.error(
                        "Refusing to stop an untracked current Codex generation for cwd=%s",
                        cwd,
                    )
                    return
                try:
                    await transport.stop()
                except Exception as exc:
                    logger.warning(
                        "Failed to stop detached broken Codex transport for cwd=%s: %s",
                        cwd,
                        exc,
                    )
                    return
            else:
                identity = self._transport_activation_identity(transport)
                registry = getattr(
                    getattr(self, "controller", None),
                    "runtime_activation",
                    None,
                )
                if identity is not None and registry is not None and registry.is_current(
                    identity
                ):
                    logger.error(
                        "Refusing to stop a replaced but still-current Codex generation for cwd=%s",
                        cwd,
                    )
                    return
                try:
                    await transport.stop()
                except Exception as exc:
                    logger.warning(
                        "Failed to stop replaced broken Codex transport for cwd=%s: %s",
                        cwd,
                        exc,
                    )
                    return

            if should_invalidate_cwd_sessions:
                for base_session_id in list(self._session_mgr.sessions_for_cwd(cwd)):
                    if base_session_id == request.base_session_id:
                        continue
                    self._session_mgr.invalidate_thread(base_session_id)
                    self._clear_thread_developer_instructions(base_session_id)
                    self._turn_registry.clear_session(base_session_id)

        self._session_mgr.invalidate_thread(request.base_session_id)
        self._clear_thread_developer_instructions(request.base_session_id)
        self._turn_registry.clear_session(request.base_session_id)

    async def _get_or_create_transport(
        self,
        cwd: str,
        launch: "ModelHubLaunch | None" = None,
        *,
        allow_runtime_replacement: bool = True,
    ) -> CodexTransport:
        """Return an initialized transport for the given working directory."""
        # Serialize creation per cwd
        if cwd not in self._transport_locks:
            self._transport_locks[cwd] = asyncio.Lock()

        while True:
            wait_for_active_turns = False
            async with self._transport_locks[cwd]:
                # Double-check after acquiring lock
                existing = self._transports.get(cwd)
                existing_dead = bool(
                    existing is not None
                    and self._transport_alive(existing) is False
                )
                desired_fingerprint = launch.fingerprint if launch is not None else "direct"
                existing_fingerprint = getattr(existing, "runtime_fingerprint", "direct")
                runtime_changed = existing_fingerprint != desired_fingerprint
                if existing is not None and runtime_changed and not allow_runtime_replacement:
                    raise CodexConnectionProbeRuntimeMismatchError(
                        "The cached Codex transport does not use direct credentials"
                    )
                if existing and existing.is_initialized:
                    # Reuse only while the directory the app-server was spawned in
                    # is still the SAME directory (#561): after a delete (+ possible
                    # re-create) the cached process sits in a dead inode and every
                    # thread/start fails. Untracked legacy entries reuse as before.
                    spawned_ino = self._cwd_inodes().get(cwd)
                    stale_cwd = spawned_ino is not None and self._cwd_inode(cwd) != spawned_ino
                    if not stale_cwd and not runtime_changed:
                        self._attach_transport_activation(cwd, existing)
                        self._touch_transport_activity(cwd)
                        return existing
                    if runtime_changed and self._has_active_turns_for_cwd(cwd):
                        wait_for_active_turns = True
                    elif stale_cwd:
                        logger.warning(
                            "Codex transport cwd was replaced under the cached app-server; "
                            "restarting transport for cwd=%s",
                            cwd,
                        )
                    else:
                        logger.info("Restarting Codex transport after Model Hub channel change for cwd=%s", cwd)

                if wait_for_active_turns:
                    pass
                else:
                    runtime_args: list[str] = []
                    runtime_env: dict[str, str] | None = None
                    runtime_fingerprint = "direct"
                    if launch is not None:
                        from modules.agents.model_hub import build_codex_hub_launch

                        if (
                            launch.channel == "hub"
                            and self._model_hub_catalog_path is None
                        ):
                            await self.prepare_model_hub_runtime()
                        runtime_args, runtime_env = build_codex_hub_launch(
                            [],
                            os.environ.copy(),
                            launch,
                            model_catalog_path=self._model_hub_catalog_path,
                        )
                        runtime_fingerprint = launch.fingerprint

                    # Stop stale transport if any
                    if existing:
                        async def replacement_is_safe() -> bool:
                            ownership = (
                                await self._runtime_ownership_snapshot_for_cwd_async(cwd)
                            )
                            replacement_blocked = getattr(
                                ownership,
                                (
                                    "blocks_dead_transport_replacement"
                                    if existing_dead
                                    else "blocks_transport_replacement"
                                ),
                                True,
                            )
                            return bool(
                                ownership is not None
                                and not replacement_blocked
                                and (
                                    existing_dead
                                    or not self._has_active_turns_for_cwd(cwd)
                                )
                            )

                        detached = await self._stop_and_detach_transport_generation(
                            cwd,
                            existing,
                            final_predicate=replacement_is_safe,
                        )
                        if not detached:
                            raise RuntimeError(
                                "Codex transport replacement blocked by a durable owner "
                                "or changed generation"
                            )
                        if (
                            runtime_changed
                            and desired_fingerprint == "direct"
                            and existing_fingerprint.startswith("hub:")
                        ):
                            self._retire_model_hub_process_scope(cwd)
                        # The new app-server process won't know about threads/turns
                        # from the old process. Invalidate only sessions bound to
                        # this cwd so healthy sessions on other cwds are unaffected.
                        affected = self._session_mgr.sessions_for_cwd(cwd)
                        for bid in affected:
                            self._session_mgr.invalidate_thread(bid)
                            self._clear_thread_developer_instructions(bid)
                            self._turn_registry.clear_session(bid)
                        if affected:
                            logger.info(
                                "Invalidated %d stale Codex session(s) after transport restart for cwd=%s",
                                len(affected),
                                cwd,
                            )

                    transport = CodexTransport(
                        binary=self.codex_config.binary,
                        cwd=cwd,
                        extra_args=list(self.codex_config.extra_args),
                        runtime_args=runtime_args,
                        runtime_env=runtime_env,
                        runtime_fingerprint=runtime_fingerprint,
                    )

                    # Wire up callbacks
                    transport.on_notification(self._on_notification)
                    # Bind the cwd so any server request (e.g. an auto-approval)
                    # refreshes this transport's activity even without a turn id.
                    transport.on_server_request(
                        lambda req_id, method, params, _cwd=cwd: self._on_server_request(
                            _cwd, req_id, method, params
                        )
                    )

                    await transport.start()
                    governor_from_controller(self.controller).apply_to_pid(
                        getattr(transport, "pid", None),
                        label="codex app-server",
                    )
                    self._attach_transport_activation(cwd, transport)
                    self._transports[cwd] = transport
                    self._cwd_inodes()[cwd] = self._cwd_inode(cwd)
                    self._touch_transport_activity(cwd)
                    return transport
            if wait_for_active_turns:
                await asyncio.sleep(0.05)

    async def _interrupt_active_turn_before_runtime_change(
        self,
        request: AgentRequest,
        launch: "ModelHubLaunch",
    ) -> None:
        """Let a replacement prompt interrupt its own stale-runtime turn."""

        transport = self._transports.get(request.working_path)
        if transport is None or not transport.is_initialized:
            return
        if getattr(transport, "runtime_fingerprint", "direct") == launch.fingerprint:
            return
        thread_id = self._session_mgr.get_thread_id(request.base_session_id)
        active_turn = self._turn_registry.get_active_turn(request.base_session_id)
        if not thread_id or not active_turn:
            return
        try:
            await transport.send_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": active_turn},
            )
        except Exception:
            # A dead/wedged transport cannot acknowledge the interrupt. Hide
            # and release the old turn anyway so the runtime-change path can
            # replace that transport instead of waiting on it forever.
            logger.warning(
                "Codex turn interrupt failed before Model Hub runtime change; "
                "replacing stale transport for cwd=%s",
                request.working_path,
                exc_info=True,
            )
        interrupted_request = self._event_handler.clear_pending(active_turn)
        if interrupted_request:
            await self._remove_ack_reaction(interrupted_request)
            # The app-server may be replaced before its interrupted completion
            # notification arrives. Settle the old request now; release is
            # token-guarded, so it cannot close the replacement turn.
            release = getattr(self._event_handler, "_release_stream_turn", None)
            if callable(release):
                release(interrupted_request.context)

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
        *,
        force_path: bool = False,
    ) -> tuple[str, bool]:
        from core.git_runtime import prepend_vendored_git_to_path

        env = self._caller_env_for_request(request)
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
            base_env=os.environ,
            working_dir=getattr(request, "working_path", None),
        )
        git_path_state = set_env["PATH"] if "PATH" in set_env else os.environ.get("PATH", "")
        path_managed = had_path or git_path_changed or force_path
        if force_path:
            set_env["PATH"] = git_path_state
        if not env and not path_managed:
            return git_path_state, False
        shell_policy["set"] = set_env
        config["shell_environment_policy"] = shell_policy
        params["config"] = config
        return git_path_state, path_managed

    def _git_path_state_for_request(self, request: AgentRequest) -> str:
        from core.git_runtime import prepend_vendored_git_to_path

        env: dict[str, str] = {}
        prepend_vendored_git_to_path(
            env,
            base_env=os.environ,
            working_dir=getattr(request, "working_path", None),
        )
        return env["PATH"] if "PATH" in env else os.environ.get("PATH", "")

    async def _start_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> str:
        """Create a new Codex thread and return its threadId."""
        params: Dict[str, Any] = {
            "cwd": request.working_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        self.ensure_agent_session_id(request)
        git_path_state, git_path_managed = self._inject_caller_env_config(params, request)

        resp = await transport.send_request("thread/start", params)
        # thread/start returns Thread directly OR may nest under "thread"
        thread_id = resp.get("id", "")
        if not thread_id:
            thread_obj = resp.get("thread")
            if isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id", "")
        if not thread_id:
            raise RuntimeError("Codex thread/start returned no thread id")

        self._session_mgr.set_thread_id(request.base_session_id, thread_id)
        # Also persist for resume support
        self.bind_agent_session_id(request, thread_id)
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
    ) -> str:
        """Fork an existing Codex thread and bind the new thread id."""
        target_agent_session_id = self.ensure_agent_session_id(request)
        source_prompt_strategy = self._fork_source_prompt_strategy(fork)
        _, effective_model, _, _ = self._resolve_codex_agent_settings(request)
        source_thread_id = str(fork.get("source_native_session_id") or "").strip()
        params: Dict[str, Any] = {
            "threadId": source_thread_id,
            "cwd": request.working_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if effective_model:
            params["model"] = effective_model
        git_path_state, git_path_managed = self._inject_caller_env_config(params, request)

        self._mark_fork_correction_pending(request.base_session_id)
        try:
            should_trim = await self._should_rollback_forked_running_turn(fork)
            resp = await transport.send_request("thread/fork", params)
            thread_id = resp.get("id", "")
            if not thread_id:
                thread_obj = resp.get("thread")
                if isinstance(thread_obj, dict):
                    thread_id = thread_obj.get("id", "")
            if not thread_id:
                raise RuntimeError("Codex thread/fork returned no thread id")

            if should_trim:
                await self._rollback_forked_running_turn(transport, thread_id)
            await self._inject_forked_session_correction(transport, request, thread_id)
        finally:
            self._clear_fork_correction_pending(request.base_session_id)
        self._session_mgr.set_thread_id(request.base_session_id, thread_id)
        target_agent_session_id = (
            self.bind_agent_session_id(request, thread_id) or target_agent_session_id
        )
        if source_prompt_strategy == "collaboration" and not self._persist_prompt_strategy(
            request,
            thread_id,
            None,
            strategy="collaboration",
            agent_session_id=target_agent_session_id,
        ):
            raise RuntimeError("Could not persist the forked Codex prompt strategy")
        if source_prompt_strategy:
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

    async def _should_rollback_forked_running_turn(self, fork: dict[str, Any]) -> bool:
        """Rollback only when Codex's latest-turn rollback still targets the reserved turn."""

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

    async def _rollback_forked_running_turn(
        self,
        transport: CodexTransport,
        thread_id: str,
    ) -> None:
        """Remove the source's still-running latest turn from a forked thread."""

        await transport.send_request(
            "thread/rollback",
            {
                "threadId": thread_id,
                "numTurns": 1,
            },
        )

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
                }
                git_path_state, git_path_managed = self._inject_caller_env_config(
                    resume_params,
                    request,
                )
                model_provider = await self._resolve_resume_model_provider_override(
                    transport,
                    request,
                    persisted,
                    rebind_same_provider=True,
                )
                if model_provider:
                    resume_params["modelProvider"] = model_provider
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
            return await self._fork_thread(transport, request, fork)

        # No associated thread yet (genuinely first turn) — start fresh.
        return await self._start_thread(transport, request)

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

        instruction_parts: list[str] = []
        if agent_instructions:
            instruction_parts.append(agent_instructions)

        # Resolve admission once: it associates or clears this turn's Memory CLI
        # session scope as a side effect, so a second call per turn would repeat
        # that write.
        memory_cli_admitted = memory_cli_prompt_admitted(self.controller, request.context)

        instruction_parts.append(
            await asyncio.to_thread(
                build_system_prompt_injection,
                include_quick_replies=getattr(self.controller.config, "reply_enhancements", True)
                and platform != "wechat",
                include_show_pages=getattr(self.controller.config, "show_pages_prompt", True),
                include_codex_generated_images=True,
                include_memory_cli=memory_cli_admitted,
                avibe_cloud_connected=avibe_cloud_url_available(self.controller.config),
                context=request.context,
                fallback_platform=platform,
                enabled_agents=get_enabled_agents_for_prompt(self.controller),
                current_agent_backend="codex",
                skills_cwd=getattr(request, "working_path", None),
                skills_project_base=managed_skill_project_base(request.context),
                skills_claude_cli_path=managed_skill_claude_cli_path(
                    getattr(getattr(self, "controller", None), "config", None)
                ),
            )
        )

        return "\n\n".join(part for part in instruction_parts if part) or None

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
        # The caller invokes this after prompt rendering grants or revokes the
        # per-turn Memory CLI capability, so the environment observes that
        # decision without rendering the prompt a second time.
        caller_env = self._caller_env_for_request(request)
        git_path_state = self._git_path_state_for_request(request)

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
                force_path=git_path_managed,
            )
        model_provider = await self._resolve_resume_model_provider_override(transport, request, thread_id)
        if model_provider:
            resume_params["modelProvider"] = model_provider

        if len(resume_params) == 1:
            return

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

    def _fork_source_prompt_strategy(self, fork: dict[str, Any]) -> Optional[str]:
        source_session_id = str(fork.get("source_session_id") or "").strip()
        source_thread_id = str(fork.get("source_native_session_id") or "").strip()
        if not source_session_id or not source_thread_id:
            return None

        cached_strategy = getattr(self, "_thread_prompt_strategies", {}).get(
            source_session_id
        )
        if cached_strategy and cached_strategy[0] == source_thread_id:
            return cached_strategy[1]

        marker = self._read_persisted_prompt_strategy_marker(
            source_thread_id,
            agent_session_id=source_session_id,
        )
        return marker["strategy"] if marker is not None else None

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

    async def _inject_thread_developer_instructions(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
        developer_instructions: str,
        *,
        agent_session_id: Optional[str] = None,
    ) -> None:
        """Fallback for Codex builds without Turn collaboration settings."""

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
                                "text": developer_instructions,
                            }
                        ],
                    }
                ],
            },
        )
        self._remember_thread_developer_instructions(
            request.base_session_id,
            thread_id,
            developer_instructions,
        )
        self._remember_thread_prompt_strategy(
            request.base_session_id,
            thread_id,
            "fallback",
        )
        self._persist_prompt_strategy(
            request,
            thread_id,
            developer_instructions,
            strategy="fallback",
            agent_session_id=agent_session_id,
        )

    @staticmethod
    def _prompt_fingerprint(developer_instructions: str) -> str:
        return hashlib.sha256(developer_instructions.encode("utf-8")).hexdigest()

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
            raise RuntimeError("Could not resolve the Codex prompt strategy") from exc
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
        if (
            marker_thread_id != thread_id
            or marker_strategy not in {"collaboration", "fallback"}
            or (marker_strategy == "fallback" and not marker_sha256_valid)
            or (
                marker_strategy == "collaboration"
                and marker_sha256 is not None
                and not marker_sha256_valid
            )
        ):
            raise RuntimeError("Stored Codex prompt strategy marker is invalid")
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
            raise RuntimeError("Could not resolve the Codex backend session binding")
        try:
            backend_session_id = getter(
                request.session_key,
                request.base_session_id,
                self.name,
            )
        except Exception as exc:
            raise RuntimeError("Could not resolve the Codex backend session binding") from exc
        if not backend_session_id:
            raise RuntimeError("Could not resolve the Codex backend session binding")
        return str(backend_session_id)

    def _persist_prompt_strategy(
        self,
        request: AgentRequest,
        thread_id: str,
        developer_instructions: Optional[str],
        *,
        strategy: str,
        agent_session_id: Optional[str],
    ) -> bool:
        if strategy not in {"collaboration", "fallback"}:
            raise ValueError(f"Unsupported Codex prompt strategy: {strategy}")
        if strategy == "fallback" and not developer_instructions:
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
            marker["sha256"] = self._prompt_fingerprint(developer_instructions)
        try:
            persisted = setter(
                agent_session_id,
                backend=self.name,
                native_session_id=thread_id,
                key=CODEX_PROMPT_STRATEGY_METADATA_KEY,
                value=marker,
            )
        except Exception:
            logger.warning("Failed to persist Codex prompt strategy", exc_info=True)
            return False
        if not persisted:
            logger.warning(
                "Skipped Codex prompt strategy for stale Session binding %s",
                agent_session_id,
            )
            return False
        return True

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
        input_items = self._build_input(request)
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
            "input": input_items,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
        if effective_model is not None or model_explicit:
            turn_params["model"] = effective_model
        if effective_effort is not None or effort_explicit:
            turn_params["effort"] = effective_effort

        cached_instructions = getattr(self, "_thread_developer_instructions", {}).get(request.base_session_id)
        prompt_changed = cached_instructions != (thread_id, developer_instructions)
        cached_strategy = getattr(self, "_thread_prompt_strategies", {}).get(request.base_session_id)
        prompt_strategy = cached_strategy[1] if cached_strategy and cached_strategy[0] == thread_id else None
        persisted_prompt_marker = None
        if developer_instructions and prompt_strategy is None:
            persisted_prompt_marker = self._read_persisted_prompt_strategy_marker(
                thread_id,
                agent_session_id=agent_session_id,
            )
            if persisted_prompt_marker is not None:
                prompt_strategy = persisted_prompt_marker["strategy"]
            elif effective_model and getattr(transport, "supports_turn_collaboration_mode", True):
                prompt_strategy = (
                    "collaboration"
                    if self._persist_prompt_strategy(
                        request,
                        thread_id,
                        developer_instructions,
                        strategy="collaboration",
                        agent_session_id=agent_session_id,
                    )
                    else "fallback"
                )
            else:
                prompt_strategy = "fallback"
            self._remember_thread_prompt_strategy(
                request.base_session_id,
                thread_id,
                prompt_strategy,
            )
        fallback_prompt_is_current = bool(
            developer_instructions
            and persisted_prompt_marker
            and persisted_prompt_marker["strategy"] == "fallback"
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
        if prompt_strategy == "collaboration":
            await self._confirm_collaboration_mode_capability(transport)
        collaboration_mode_is_known = bool(
            getattr(transport, "supports_turn_collaboration_mode", True)
        )
        collaboration_strategy_has_no_model = bool(
            developer_instructions
            and prompt_strategy == "collaboration"
            and not effective_model
        )
        clear_collaboration_mode = collaboration_mode_is_known and bool(
            (model_explicit and effective_model is None)
            or collaboration_strategy_has_no_model
        )
        if clear_collaboration_mode:
            was_collaboration = prompt_strategy == "collaboration"
            turn_params["collaborationMode"] = None
            if developer_instructions:
                prompt_strategy = "fallback"
                self._remember_thread_prompt_strategy(
                    request.base_session_id,
                    thread_id,
                    prompt_strategy,
                )
                if was_collaboration and not fallback_prompt_is_current:
                    prompt_changed = True
        use_collaboration_mode = bool(
            developer_instructions
            and effective_model
            and prompt_strategy == "collaboration"
            and collaboration_mode_is_known
        )
        if use_collaboration_mode:
            # Codex keeps the collaboration world state across Turns and emits
            # a new developer fragment only when these exact bytes change. The
            # explicit model and effort preserve the route because the mode
            # takes precedence over the sibling turn fields.
            turn_params["collaborationMode"] = {
                "mode": "default",
                "settings": {
                    "model": effective_model,
                    "reasoning_effort": effective_effort,
                    "developer_instructions": developer_instructions,
                },
            }
        elif developer_instructions and prompt_changed:
            await self._inject_thread_developer_instructions(
                transport,
                request,
                thread_id,
                developer_instructions,
                agent_session_id=agent_session_id,
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
        mark_backend_dispatch_attempted(request.context)
        try:
            resp = await transport.send_request("turn/start", turn_params)
        except Exception as exc:
            if (
                "collaborationMode" not in turn_params
                or not self._collaboration_mode_is_unsupported(exc)
            ):
                raise
            logger.warning(
                "Codex turn collaboration mode is unavailable; falling back to developer item injection: %s",
                exc,
            )
            transport.supports_turn_collaboration_mode = False
            self._remember_thread_prompt_strategy(
                request.base_session_id,
                thread_id,
                "fallback",
            )
            if developer_instructions and not fallback_prompt_is_current:
                await self._inject_thread_developer_instructions(
                    transport,
                    request,
                    thread_id,
                    developer_instructions,
                    agent_session_id=agent_session_id,
                )
            fallback_turn_params = dict(turn_params)
            fallback_turn_params.pop("collaborationMode", None)
            resp = await transport.send_request("turn/start", fallback_turn_params)

        if use_collaboration_mode and developer_instructions:
            self._remember_thread_developer_instructions(
                request.base_session_id,
                thread_id,
                developer_instructions,
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
        self._mark_runtime_turn_started(
            getattr(request, "context", None),
            activation_identity=self._transport_activation_identity(transport),
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
        items: list[Dict[str, Any]] = []

        # Text input
        message = request.message
        if request.files:
            # Append file info like Claude agent does
            file_lines = ["", "[User Attachments]"]
            for attachment in request.files:
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

        if message:
            items.insert(0, {"type": "text", "text": message})

        return items

    # ------------------------------------------------------------------
    # Callback handlers (wired to transport)
    # ------------------------------------------------------------------

    async def _on_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Route a server notification to the event handler."""
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

        if self._notification_is_real_progress(method):
            self._touch_transport_activity(request.working_path)
            self._touch_session_activity(request.base_session_id)
        await self._event_handler.handle_notification(method, params, request)

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

        logger.warning("Unknown Codex server request: %s", method)
        return {"approved": True}

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

    def _cwd_inodes(self) -> Dict[str, Optional[int]]:
        if not hasattr(self, "_transport_cwd_inodes"):
            self._transport_cwd_inodes = {}
        return self._transport_cwd_inodes

    @staticmethod
    def _cwd_inode(cwd: str) -> Optional[int]:
        try:
            return os.stat(cwd).st_ino
        except OSError:
            return None

    def _touch_transport_activity(self, cwd: str) -> None:
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if cwd:
            self._transport_last_activity[cwd] = time.monotonic()

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
        if cwd in getattr(self, "_connection_probe_cwds", set()):
            return True
        for base_session_id in self._session_mgr.sessions_for_cwd(cwd):
            if self._turn_registry.get_active_turn(base_session_id):
                return True
            has_pending_turn_start = getattr(self._turn_registry, "has_pending_turn_start", None)
            if callable(has_pending_turn_start) and has_pending_turn_start(base_session_id):
                return True
        return False
