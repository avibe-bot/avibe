"""OpenCode agent implementation (coordinator).

Most heavy lifting lives in:
- server.py: OpenCodeServerManager
- poll_loop.py: unified poll loop
- session.py: session mapping + concurrency guards
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import aiohttp

from core.avibe_cloud import avibe_cloud_url_available
from core.backend_failure import emit_backend_failure
from core.message_output import stop_output_for, terminal_output_for
from core.native_dispatch_phase import mark_backend_dispatch_attempted
from core.resource_governance import governor_from_controller
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    SteerResult,
    result as steer_result,
)
from core.system_prompt_injection import (
    build_system_prompt_injection,
    get_enabled_agents_for_prompt,
    memory_cli_prompt_admitted,
)
from modules.agents.base import AgentRequest, BaseAgent
from modules.agents.model_hub import (
    ModelHubLaunch,
    OpenCodeOverlay,
    bind_launch,
    bind_turn_mode,
    opencode_model_for_overlay,
    persisted_launch_identity,
    resolve_opencode_overlay_launch,
)
from vibe.i18n import t as i18n_t

from .caller_context import bind_session as bind_caller_context_session
from .client_manager import OpenCodeClientManager
from .message_processor import OpenCodeMessageProcessorMixin
from .poll_loop import OpenCodePollLoop, restored_platform_from_poll_info, restored_session_key_from_poll_info
from .server import OpenCodePromptRejectedError, OpenCodeServerManager
from .session import OpenCodeResumeUnavailableError, OpenCodeSessionManager
from .utils import resolve_opencode_model_id, resolve_opencode_reasoning_effort

logger = logging.getLogger(__name__)
_STEERING_SNAPSHOT_KEY = "opencode_native_steering"
_STATUS_RECONCILIATION_FAILURE_LIMIT = 3
_RESTORED_IM_REGISTRATION_RETRY_DELAY_SECONDS = 0.25
_RESTORED_IM_PLATFORMS = {"slack", "discord", "telegram", "lark", "wechat"}


def _task_is_stopping(task: asyncio.Task) -> bool:
    cancelling = getattr(task, "cancelling", None)
    return task.done() or bool(cancelling and cancelling())


@dataclass
class _OpenCodeSteerState:
    task: asyncio.Task
    base_session_id: str
    target_session_id: str
    logical_turn_id: str
    native_session_id: str
    directory: str
    agent: Optional[str]
    model: Optional[Dict[str, str]]
    reasoning_effort: Optional[str]
    system: Optional[str]
    baseline_message_ids: set[str]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    awaiting_after_message_ids: set[str] | None = None
    awaiting_user_text: str | None = None
    idle_reconciliation_message: str = ""
    restored: bool = False
    reconcile_initial_status: bool = False
    status_reconciliation_failures: int = 0
    terminal_status_failure_messages: list[Dict[str, Any]] | None = None
    terminal_status_failure_generation: int = 0

    @property
    def native_turn_id(self) -> str:
        return f"opencode:{self.native_session_id}:{id(self.task)}"


class _SteeringAwareOpenCodeServer:
    """Keep the primary poll owner alive across an accepted async prompt."""

    def __init__(
        self,
        server: OpenCodeServerManager,
        state: _OpenCodeSteerState,
    ) -> None:
        self._server = server
        self._state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    @staticmethod
    def _message_ids(messages: list[Dict[str, Any]]) -> set[str]:
        return {
            str(message.get("info", {}).get("id"))
            for message in messages
            if message.get("info", {}).get("id")
        }

    def _is_final_assistant_snapshot(self, messages: list[Dict[str, Any]]) -> bool:
        if not messages:
            return False
        info = messages[-1].get("info", {})
        return bool(
            info.get("id") not in self._state.baseline_message_ids
            and info.get("role") == "assistant"
            and info.get("time", {}).get("completed")
            and info.get("finish") != "tool-calls"
            and not info.get("error")
        )

    @staticmethod
    def _message_text(message: Dict[str, Any]) -> str:
        return "".join(
            str(part.get("text") or "")
            for part in (message.get("parts") or [])
            if part.get("type") == "text"
        )

    @classmethod
    def _inserted_user_index(
        cls,
        messages: list[Dict[str, Any]],
        excluded_message_ids: set[str],
        inserted_user_text: str,
    ) -> int:
        return next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("info", {}).get("role") == "user"
                and message.get("info", {}).get("id") not in excluded_message_ids
                and cls._message_text(message) == inserted_user_text
            ),
            -1,
        )

    @classmethod
    def _has_final_assistant_after(
        cls,
        messages: list[Dict[str, Any]],
        excluded_message_ids: set[str],
        *,
        inserted_user_text: str | None = None,
    ) -> bool:
        inserted_user_index = -1
        if inserted_user_text is not None:
            inserted_user_index = cls._inserted_user_index(
                messages,
                excluded_message_ids,
                inserted_user_text,
            )
            if inserted_user_index < 0:
                return False
        return any(
            index > inserted_user_index
            and message.get("info", {}).get("role") == "assistant"
            and message.get("info", {}).get("id") not in excluded_message_ids
            and message.get("info", {}).get("time", {}).get("completed")
            and message.get("info", {}).get("finish") != "tool-calls"
            and not message.get("info", {}).get("error")
            for index, message in enumerate(messages)
        )

    def _has_pending_question_tool(self, messages: list[Dict[str, Any]]) -> bool:
        return any(
            message.get("info", {}).get("id") not in self._state.baseline_message_ids
            and part.get("type") == "tool"
            and part.get("tool") == "question"
            and (part.get("state") or {}).get("status") != "completed"
            for message in messages
            for part in (message.get("parts") or [])
        )

    def _completed_assistant_boundary(
        self,
        messages: list[Dict[str, Any]],
    ) -> set[str]:
        last_completed_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("info", {}).get("role") == "assistant"
                and messages[index].get("info", {}).get("time", {}).get("completed")
                and messages[index].get("info", {}).get("id")
                not in self._state.baseline_message_ids
            ),
            -1,
        )
        return self._message_ids(messages[: last_completed_index + 1])

    def _idle_reconciliation_error_text(self) -> str:
        if self._state.idle_reconciliation_message:
            return self._state.idle_reconciliation_message
        default_label = str(i18n_t("common.default", "en"))
        return str(
            i18n_t(
                "error.opencodeEmptyResponse",
                "en",
                provider=default_label,
                model=default_label,
                variant=default_label,
            )
        )

    def _terminal_reconciliation_failure(
        self,
        session_id: str,
        messages: list[Dict[str, Any]],
        *,
        name: str,
        message: str,
    ) -> list[Dict[str, Any]]:
        self._state.closing = True
        failure = {
            "info": {
                "id": f"avibe-status-reconciliation:{session_id}",
                "role": "assistant",
                "time": {"completed": time.time()},
                # Keep the restored poll in its bounded error path until it
                # emits the terminal failure through the existing Result owner.
                "finish": "tool-calls",
                "error": {
                    "name": name,
                    "data": {"message": message},
                },
            },
            "parts": [],
        }
        self._state.terminal_status_failure_messages = [*messages, failure]
        return self._state.terminal_status_failure_messages

    async def list_messages(self, session_id: str, directory: str) -> list[Dict[str, Any]]:
        while True:
            wait_for_insert = False
            async with self._state.lock:
                if self._state.terminal_status_failure_messages is not None:
                    return self._state.terminal_status_failure_messages
                messages = await self._server.list_messages(session_id, directory)
                if self._has_pending_question_tool(messages):
                    self._state.closing = True
                    return messages
                awaiting = self._state.awaiting_after_message_ids
                inserted_user_text = self._state.awaiting_user_text
                final_snapshot = self._is_final_assistant_snapshot(messages)
                reconcile_initial_status = self._state.reconcile_initial_status
                reconcile_insert = awaiting is not None
                if reconcile_insert or final_snapshot or reconcile_initial_status:
                    try:
                        status = await self._server.get_session_status(session_id, directory)
                    except Exception as exc:
                        self._state.status_reconciliation_failures += 1
                        if (
                            self._state.status_reconciliation_failures
                            >= _STATUS_RECONCILIATION_FAILURE_LIMIT
                        ):
                            evidence_boundary = (
                                awaiting
                                if awaiting is not None
                                else self._state.baseline_message_ids
                            )
                            last_message_id = (
                                messages[-1].get("info", {}).get("id")
                                if messages
                                else None
                            )
                            boundary_answer_is_latest = (
                                self._state.restored
                                and inserted_user_text is None
                                and last_message_id in evidence_boundary
                            )
                            if final_snapshot and (
                                boundary_answer_is_latest
                                or self._has_final_assistant_after(
                                    messages,
                                    evidence_boundary,
                                    inserted_user_text=inserted_user_text,
                                )
                            ):
                                self._state.awaiting_after_message_ids = None
                                self._state.awaiting_user_text = None
                                self._state.reconcile_initial_status = False
                                self._state.closing = True
                                return messages
                            return self._terminal_reconciliation_failure(
                                session_id,
                                messages,
                                name="StatusReconciliationError",
                                message=str(exc),
                            )
                        wait_for_insert = True
                    else:
                        self._state.status_reconciliation_failures = 0
                        if status is not None and status.get("type") in {"busy", "retry"}:
                            if reconcile_initial_status and awaiting is None:
                                self._state.awaiting_after_message_ids = (
                                    self._completed_assistant_boundary(messages)
                                )
                            wait_for_insert = True
                        else:
                            self._state.reconcile_initial_status = False
                            if reconcile_insert:
                                inserted_user_missing = (
                                    inserted_user_text is None
                                    or (
                                        self._inserted_user_index(
                                            messages,
                                            awaiting,
                                            inserted_user_text,
                                        )
                                        < 0
                                    )
                                )
                                last_message_id = (
                                    messages[-1].get("info", {}).get("id")
                                    if messages
                                    else None
                                )
                                if (
                                    inserted_user_missing
                                    and final_snapshot
                                    and last_message_id in awaiting
                                ):
                                    self._state.awaiting_after_message_ids = None
                                    self._state.awaiting_user_text = None
                                    self._state.closing = True
                                    return messages
                                has_final_insert_result = (
                                    final_snapshot
                                    and self._has_final_assistant_after(
                                        messages,
                                        awaiting,
                                        inserted_user_text=inserted_user_text,
                                    )
                                )
                                self._state.awaiting_after_message_ids = None
                                self._state.awaiting_user_text = None
                                if has_final_insert_result:
                                    self._state.closing = True
                                    return messages
                                return self._terminal_reconciliation_failure(
                                    session_id,
                                    messages,
                                    name="NativeSessionEndedBeforeResult",
                                    message=self._idle_reconciliation_error_text(),
                                )
                            self._state.awaiting_after_message_ids = None
                            self._state.awaiting_user_text = None
                            if final_snapshot:
                                self._state.closing = True
                            elif (
                                reconcile_initial_status
                                and not self._has_final_assistant_after(
                                    messages,
                                    self._state.baseline_message_ids,
                                )
                            ):
                                return self._terminal_reconciliation_failure(
                                    session_id,
                                    messages,
                                    name="NativeSessionEndedBeforeResult",
                                    message=self._idle_reconciliation_error_text(),
                                )
                            return messages
                else:
                    self._state.awaiting_after_message_ids = None
                    self._state.awaiting_user_text = None
                    return messages
            if wait_for_insert:
                await asyncio.sleep(0.1)

    async def prompt_async(self, *args, **kwargs) -> None:
        async with self._state.lock:
            terminal_messages = self._state.terminal_status_failure_messages
            if terminal_messages is not None:
                # The normal poll loop retries a first native error with
                # "continue". Do not dispatch new work after reconciliation
                # already proved terminal; advance only the synthetic evidence
                # ID so its existing retry budget reaches terminal delivery.
                self._state.terminal_status_failure_generation += 1
                failure = terminal_messages[-1]
                info = dict(failure.get("info", {}))
                info["id"] = (
                    f"avibe-status-reconciliation:{self._state.native_session_id}:"
                    f"{self._state.terminal_status_failure_generation}"
                )
                self._state.terminal_status_failure_messages = [
                    *terminal_messages[:-1],
                    {**failure, "info": info},
                ]
                return
            await self._server.prompt_async(*args, **kwargs)

    async def abort_session(self, *args, **kwargs) -> bool:
        async with self._state.lock:
            self._state.closing = True
            return await self._server.abort_session(*args, **kwargs)


def resolve_opencode_model_dict(model_str: str | None, default_provider: str | None) -> dict[str, str] | None:
    if not model_str:
        return None
    parts = model_str.split("/", 1)
    if len(parts) == 2:
        return {"providerID": parts[0], "modelID": parts[1]}
    if isinstance(default_provider, str) and default_provider.strip():
        return {"providerID": default_provider.strip(), "modelID": model_str}
    return None


def _raw_settings_key_from_session_key(session_key: str) -> str:
    parts = str(session_key or "").split("::")
    if len(parts) >= 3 and parts[1] in {"user", "channel", "platform", "project"}:
        return "::".join(parts[2:])
    if len(parts) >= 2:
        return "::".join(parts[1:])
    return str(session_key or "")


def _target_agent_session_id(request: AgentRequest) -> str:
    payload = request.context.platform_specific or {}
    target = payload.get("agent_session_target") if isinstance(payload, dict) else None
    if isinstance(target, dict) and target.get("id"):
        return str(target["id"])
    return str(payload.get("agent_session_id") or "") if isinstance(payload, dict) else ""


class OpenCodeAgent(OpenCodeMessageProcessorMixin, BaseAgent):
    """OpenCode Server API integration via HTTP."""

    name = "opencode"

    def __init__(self, controller, opencode_config):
        super().__init__(controller)
        self.opencode_config = opencode_config

        self._client_manager = OpenCodeClientManager(opencode_config)
        self._client_manager.set_resource_governor(governor_from_controller(controller))
        self._session_manager = OpenCodeSessionManager(self.settings_manager, self.name)

        self._poll_loop = OpenCodePollLoop(self)

        self._active_requests: Dict[str, asyncio.Task] = {}
        self._steering_states: Dict[str, _OpenCodeSteerState] = {}
        self._restored_poll_servers: Dict[asyncio.Task, _SteeringAwareOpenCodeServer] = {}

    async def _get_server(self) -> OpenCodeServerManager:
        current_task = asyncio.current_task()
        if current_task is not None:
            restored_server = self._restored_poll_servers.get(current_task)
            if restored_server is not None:
                return restored_server
        return await self._client_manager.get_server()

    def _idle_reconciliation_message(
        self,
        model: Optional[Dict[str, str]],
        reasoning_effort: Optional[str],
    ) -> str:
        controller_translate = getattr(self.controller, "_t", None)
        lang = getattr(getattr(self.controller, "config", None), "language", "en")

        def translate(key: str, **kwargs: Any) -> str:
            if callable(controller_translate):
                return str(controller_translate(key, **kwargs))
            return str(i18n_t(key, lang, **kwargs))

        default_label = translate("common.default")
        return translate(
            "error.opencodeEmptyResponse",
            provider=(model or {}).get("providerID") or default_label,
            model=(model or {}).get("modelID") or default_label,
            variant=reasoning_effort or default_label,
        )

    async def prepare_runtime_restart(self) -> None:
        """Adopt persisted server state before the shared drain snapshot."""
        await self._get_server()

    def runtime_has_active_turns(self) -> bool:
        if any(not task.done() for task in self._active_requests.values()):
            return True
        server = self._client_manager._server_manager
        return bool(server is not None and server.runtime_has_active_turns())

    async def refresh_runtime_config(self, opencode_config, *, force: bool = False) -> None:
        """Reload runtime config and refresh the shared server.

        OpenCode caches opencode.json provider/model config in the serve
        process. Prefer OpenCode's own global-config reload endpoint so
        Settings writes take effect without terminating active serve
        processes; fall back to restart for older OpenCode versions.
        """
        previous_server = await self._client_manager.reset_config(opencode_config)
        if previous_server is None:
            previous_server = await OpenCodeServerManager.get_instance_if_managed_server_exists(
                binary=self.opencode_config.binary,
                port=self.opencode_config.port,
                request_timeout_seconds=self.opencode_config.request_timeout_seconds,
                resource_governor=governor_from_controller(self.controller),
            )
        self.opencode_config = opencode_config
        self.controller.config.opencode = opencode_config
        if previous_server is not None:
            refreshed = False
            runtime_unchanged = (
                previous_server.binary == opencode_config.binary
                and previous_server.port == opencode_config.port
                and previous_server.request_timeout_seconds == opencode_config.request_timeout_seconds
            )
            refresh_global_config = getattr(previous_server, "refresh_global_config", None)
            if not force and runtime_unchanged and callable(refresh_global_config):
                try:
                    refreshed = bool(await refresh_global_config())
                except Exception:
                    logger.warning("OpenCode global config refresh failed; falling back to restart", exc_info=True)
                    refreshed = False
            if not refreshed:
                detach = getattr(previous_server, "detach_after_deferred_refresh", None)
                if callable(detach):
                    if force:
                        await detach(force=True)
                    else:
                        await detach()
                elif hasattr(previous_server, "restart_for_auth_refresh"):
                    if force:
                        await previous_server.restart_for_auth_refresh(force=True)
                    else:
                        await previous_server.restart_for_auth_refresh()
            reload_config = getattr(previous_server, "reload_runtime_config", None)
            if callable(reload_config):
                await reload_config(
                    binary=opencode_config.binary,
                    port=opencode_config.port,
                    request_timeout_seconds=opencode_config.request_timeout_seconds,
                )

    async def handle_message(self, request: AgentRequest) -> None:
        lock = self._session_manager.get_session_lock(request.base_session_id)
        task: Optional[asyncio.Task] = None

        async with lock:
            existing_task = self._active_requests.get(request.base_session_id)
            if existing_task and not existing_task.done():
                logger.info(
                    "OpenCode session %s already running; cancelling before new request",
                    request.base_session_id,
                )
                req_info = self._session_manager.get_request_session(request.base_session_id)
                if req_info:
                    server = await self._get_server()
                    await self._abort_active_request(
                        request.base_session_id,
                        existing_task,
                        req_info,
                    )
                    await self._session_manager.wait_for_session_idle(server, req_info[0], req_info[1])

                existing_task.cancel()
                try:
                    await existing_task
                except asyncio.CancelledError:
                    pass

                logger.info(
                    "OpenCode session %s cancelled; continuing with new request",
                    request.base_session_id,
                )

            task = asyncio.create_task(self._process_message(request))
            self._active_requests[request.base_session_id] = task

        if not task:
            return

        try:
            await task
        except asyncio.CancelledError:
            logger.debug(f"OpenCode task cancelled for {request.base_session_id}")
        finally:
            if self._active_requests.get(request.base_session_id) is task:
                self._active_requests.pop(request.base_session_id, None)
                self._session_manager.pop_request_session(request.base_session_id)
            # The poll loop ran to completion above (handle_message awaits the
            # task), so the turn is fully settled here. Release any web-Chat
            # stream waiter: a no-result failure (only a notify was emitted)
            # ends the spinner now instead of waiting out the safety timeout.
            # Token-guarded + no-op for IM/CLI; success already released via the
            # result emit during the poll. Defensive: tolerate controllers
            # without streaming completion support.
            _mark = getattr(self.controller, "mark_turn_complete", None)
            if callable(_mark):
                _mark(request.context)

    async def _process_message(self, request: AgentRequest) -> None:
        run_registered = False
        steer_state: _OpenCodeSteerState | None = None
        poll_server: _SteeringAwareOpenCodeServer | None = None
        model_hub_overlay: OpenCodeOverlay | None = None
        model_hub_launch: ModelHubLaunch | None = None
        # Bind early: get_or_create_session_id (below) can raise BEFORE assigning
        # session_id (a transient server error now that get_session raises on
        # non-404), and the error-cleanup paths reference session_id — keep it
        # defined so they can't trip UnboundLocalError (Codex P2).
        session_id = None
        logical_turn_id = ""
        start_attempt_id = ""
        native_start_phase = "before_write"
        try:
            model_hub_runtime = getattr(self.controller, "model_hub_runtime", None)
            turn_mode = getattr(model_hub_runtime, "turn_mode", None)
            if callable(turn_mode):
                bind_turn_mode(
                    request.context,
                    turn_mode("opencode"),
                )
            prepare_overlay = getattr(model_hub_runtime, "prepare_opencode_overlay", None)
            if callable(prepare_overlay):
                model_hub_overlay = await prepare_overlay()
            server = await self._get_server()
            configure_overlay = getattr(server, "configure_model_hub_overlay", None)
            if callable(configure_overlay):
                await configure_overlay(model_hub_overlay)
            await server.ensure_running()
        except Exception as e:
            logger.error(f"Failed to start OpenCode server: {e}", exc_info=True)
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                str(e),
                display_text=f"Failed to start OpenCode server: {e}",
                request=request,
            )
            await self._remove_ack_reaction(request)
            return

        await self._delete_ack(request)
        await self._session_manager.ensure_working_dir(request.working_path)

        try:
            session_id = await self._session_manager.get_or_create_session_id(request, server)
        except OpenCodeResumeUnavailableError as e:
            # The previous session is gone server-side — surface it as a terminal
            # ERROR result (outbound chokepoint turns the dot red), don't silently
            # fork a fresh session and lose context.
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                str(e),
                display_text=f"❌ {e}",
                request=request,
            )
            await self._remove_ack_reaction(request)
            return
        except Exception as e:
            # A transient/transport/auth failure while acquiring the session
            # (get_session now raises on non-404, Codex P2): surface it as a
            # terminal error result instead of letting it propagate unhandled or be
            # mislabeled as expiry. Route auth errors through the reset-OAuth flow
            # (which settles the dot itself); otherwise emit the error result here.
            logger.error(f"OpenCode session acquisition failed: {e}", exc_info=True)
            message = f"OpenCode error: {type(e).__name__}: {e}".strip()
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                str(e),
                display_text=message,
                request=request,
            )
            await self._remove_ack_reaction(request)
            return
        if not session_id:
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                "Failed to obtain OpenCode session ID",
                display_text="Failed to obtain OpenCode session ID",
                request=request,
            )
            await self._remove_ack_reaction(request)
            return

        self._session_manager.set_request_session(
            request.base_session_id,
            session_id,
            request.working_path,
            request.session_key,
        )

        self._session_manager.mark_initialized(session_id)

        try:
            override_agent, override_model, override_reasoning = self.controller.get_opencode_overrides(request.context)
            override_model = request.vibe_agent_model or override_model
            override_reasoning = request.vibe_agent_reasoning_effort or override_reasoning

            override_agent = request.subagent_name or override_agent
            if request.subagent_name:
                override_model = request.subagent_model
                override_reasoning = request.subagent_reasoning_effort

            if request.subagent_name and not override_model:
                override_model = server.get_agent_model_from_config(request.subagent_name)
            if request.subagent_name and not override_reasoning:
                override_reasoning = server.get_agent_reasoning_effort_from_config(request.subagent_name)

            agent_to_use = override_agent
            if not agent_to_use:
                agent_to_use = server.get_default_agent_from_config()

            model_dict = None
            model_str = override_model
            if not model_str:
                model_str = server.get_agent_model_from_config(agent_to_use)
            opencode_cfg = getattr(self.controller.config, "opencode", None)
            model_str = opencode_model_for_overlay(model_str, model_hub_overlay)
            if model_hub_runtime is not None and model_str:
                model_hub_launch = await resolve_opencode_overlay_launch(
                    self.controller,
                    model_str,
                    model_hub_overlay,
                )
                bind_launch(request.context, model_hub_launch)
            # Bare model id (no ``provider/`` prefix): only inject ``providerID``
            # when the user has explicitly chosen a default provider in Settings.
            # Otherwise leave ``model_dict`` unset so OpenCode keeps using its own
            # routing for legacy installs.
            default_provider = getattr(opencode_cfg, "default_provider", None)
            model_dict = resolve_opencode_model_dict(model_str, default_provider)

            reasoning_effort = override_reasoning
            if not reasoning_effort:
                reasoning_effort = server.get_agent_reasoning_effort_from_config(agent_to_use)
            if not reasoning_effort:
                reasoning_effort = getattr(opencode_cfg, "default_reasoning_effort", None)
            if model_dict:
                try:
                    model_catalog = await server.get_available_models(request.working_path)
                    resolved_model_id = resolve_opencode_model_id(
                        model_catalog,
                        model_dict.get("providerID"),
                        model_dict.get("modelID"),
                    )
                    if resolved_model_id and resolved_model_id != model_dict.get("modelID"):
                        model_dict = {**model_dict, "modelID": resolved_model_id}
                    reasoning_effort = resolve_opencode_reasoning_effort(
                        model_dict,
                        reasoning_effort,
                        model_catalog,
                    )
                except Exception as err:
                    logger.debug("Failed to resolve OpenCode model variant support: %s", err)

            baseline_message_ids: set[str] = set()
            try:
                baseline_messages = await server.list_messages(
                    session_id=session_id,
                    directory=request.working_path,
                )
                for message in baseline_messages:
                    message_id = message.get("info", {}).get("id")
                    if message_id:
                        baseline_message_ids.add(message_id)
            except Exception as err:
                logger.debug(f"Failed to snapshot OpenCode messages before prompt: {err}")

            # Prepare message with file attachment info if present
            prompt_text = self._prepare_message_with_files(request)
            platform = (
                request.context.platform
                or (request.context.platform_specific or {}).get("platform")
                or self.controller.config.platform
            )

            system_prompt_injection = build_system_prompt_injection(
                include_quick_replies=getattr(self.controller.config, "reply_enhancements", True)
                and platform != "wechat",
                include_show_pages=getattr(self.controller.config, "show_pages_prompt", True),
                include_memory_cli=memory_cli_prompt_admitted(self.controller, request.context),
                avibe_cloud_connected=avibe_cloud_url_available(self.controller.config),
                context=request.context,
                fallback_platform=platform,
                enabled_agents=get_enabled_agents_for_prompt(self.controller),
                current_agent_backend="opencode",
            )
            if request.vibe_agent_system_prompt:
                system_prompt_injection = f"{request.vibe_agent_system_prompt}\n\n{system_prompt_injection}"

            try:
                bind_caller_context_session(
                    session_id,
                    request.context.platform_specific or {},
                    base_env=os.environ,
                    working_dir=request.working_path,
                    # The creation origin travels with the identity: an OpenCode shell
                    # command running ``vibe task add`` sources this binding, and it is
                    # the only place the conversation behind the definition is visible.
                    message=request.context,
                    fallback_platform=platform,
                )
            except Exception:
                logger.warning("Failed to bind OpenCode caller context for session %s", session_id, exc_info=True)

            raw_settings_key = _raw_settings_key_from_session_key(request.session_key)
            platform_payload = request.context.platform_specific or {}
            logical_turn_id = str(platform_payload.get("turn_token") or "").strip()
            start_attempt_id = str(
                platform_payload.get("delivery_start_attempt_id") or ""
            ).strip()
            processing_indicator = self.controller.processing_indicator.snapshot_request(
                request
            )
            target_session_id = _target_agent_session_id(request)
            if target_session_id and logical_turn_id:
                processing_indicator[_STEERING_SNAPSHOT_KEY] = {
                    "target_session_id": target_session_id,
                    "logical_turn_id": logical_turn_id,
                    "agent": agent_to_use,
                    "system": system_prompt_injection,
                }
            if start_attempt_id:
                processing_indicator["delivery_start_attempt_id"] = start_attempt_id
            launch_identity = persisted_launch_identity(model_hub_launch)
            if launch_identity is not None:
                processing_indicator["model_hub_launch"] = launch_identity

            await server.mark_run_active(session_id)
            run_registered = True
            # Persist the complete recovery address before the first native write.
            # A crash after OpenCode accepts the exact message ID can now rebuild the
            # poll and Turn owner even if no post-prompt Python statement ran.
            self.sessions.add_active_poll(
                opencode_session_id=session_id,
                base_session_id=request.base_session_id,
                channel_id=request.context.channel_id,
                thread_id=request.context.thread_id,
                settings_key=raw_settings_key,
                working_path=request.working_path,
                baseline_message_ids=list(baseline_message_ids),
                ack_reaction_message_id=request.ack_reaction_message_id,
                ack_reaction_emoji=request.ack_reaction_emoji,
                typing_indicator_active=request.typing_indicator_active,
                context_token=str(platform_payload.get("context_token") or ""),
                processing_indicator=processing_indicator,
                user_id=request.context.user_id or "",
                platform=request.context.platform or platform_payload.get("platform") or "",
                prompt_started_at=None,
                model_dict=model_dict,
                reasoning_effort=reasoning_effort,
                session_key=request.session_key,
            )
            mark_backend_dispatch_attempted(request.context)
            native_start_phase = "may_have_written"
            await server.prompt_async(
                session_id=session_id,
                directory=request.working_path,
                text=prompt_text,
                message_id=str(
                    (request.context.platform_specific or {}).get(
                        "delivery_start_attempt_id"
                    )
                    or ""
                )
                or None,
                agent=agent_to_use,
                model=model_dict,
                reasoning_effort=reasoning_effort,
                system=system_prompt_injection,
                tools={"question": False},
            )
            current_task = asyncio.current_task()
            if current_task is None:
                raise RuntimeError("OpenCode runner task is unavailable")
            steer_state = _OpenCodeSteerState(
                task=current_task,
                base_session_id=request.base_session_id,
                target_session_id=target_session_id,
                logical_turn_id=logical_turn_id,
                native_session_id=session_id,
                directory=request.working_path,
                agent=agent_to_use,
                model=model_dict,
                reasoning_effort=reasoning_effort,
                system=system_prompt_injection,
                baseline_message_ids=set(baseline_message_ids),
                awaiting_after_message_ids=set(baseline_message_ids),
                idle_reconciliation_message=self._idle_reconciliation_message(
                    model_dict,
                    reasoning_effort,
                ),
            )
            self._steering_states[request.base_session_id] = steer_state
            self.mark_runtime_turn_started(request.context)
            native_start_phase = "accepted"

            logger.info(
                "Starting OpenCode poll loop for %s (thread=%s, cwd=%s)",
                session_id,
                request.base_session_id,
                request.working_path,
            )

            poll_server = _SteeringAwareOpenCodeServer(
                server,
                steer_state,
            )
            final_text, should_emit = await self._poll_loop.run_prompt_poll(
                request,
                poll_server,
                session_id,
                agent_to_use=agent_to_use,
                model_dict=model_dict,
                reasoning_effort=reasoning_effort,
                baseline_message_ids=baseline_message_ids,
            )

            if not should_emit:
                self.sessions.remove_active_poll(session_id)
                await self._remove_ack_reaction(request)
                return

            if final_text:
                await self.emit_result_message(
                    request.context,
                    final_text,
                    subtype="success",
                    started_at=request.started_at,
                    parse_mode="markdown",
                    request=request,
                )
            else:
                await self.emit_result_message(
                    request.context,
                    "(No response from OpenCode)",
                    subtype="warning",
                    started_at=request.started_at,
                    request=request,
                )

            self._maybe_backfill_session_title(request, session_id, retry_delay_seconds=3.0)
            self.sessions.remove_active_poll(session_id)

        except asyncio.CancelledError:
            logger.info(f"OpenCode request cancelled for {request.base_session_id}")
            await self._remove_ack_reaction(request)
            if session_id:
                self.sessions.remove_active_poll(session_id)
            raise
        except OpenCodePromptRejectedError as e:
            error_text = f"{type(e).__name__}: {e}"
            logger.error("OpenCode prompt was definitively rejected: %s", e)

            poll_can_be_removed = not (logical_turn_id and start_attempt_id)
            if logical_turn_id and start_attempt_id:
                reconcile = getattr(
                    getattr(self.controller, "session_turns", None),
                    "reconcile_start_attempt_not_written",
                    None,
                )
                if callable(reconcile):
                    try:
                        poll_can_be_removed = bool(
                            reconcile(
                                logical_turn_id,
                                start_attempt_id,
                                backend=self.name,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist definitive rejected OpenCode start "
                            "for Turn=%s; preserving its active poll",
                            logical_turn_id,
                        )

            await self._remove_ack_reaction(request)
            if session_id and poll_can_be_removed:
                self.sessions.remove_active_poll(session_id)

            await self.record_model_hub_native_failure(request.context, error_text)
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                error_text,
                display_text=f"OpenCode request failed: {error_text}",
                request=request,
            )
        except Exception as e:
            error_name = type(e).__name__
            error_details = str(e).strip()
            error_text = f"{error_name}: {error_details}" if error_details else error_name

            logger.error(f"OpenCode request failed: {error_text}", exc_info=True)
            await self.record_model_hub_native_failure(request.context, error_text)
            try:
                abort_server = poll_server or server
                await abort_server.abort_session(session_id, request.working_path)
            except Exception as abort_err:
                logger.warning(f"Failed to abort OpenCode session after error: {abort_err}")

            await self._remove_ack_reaction(request)
            if session_id and native_start_phase != "may_have_written":
                self.sessions.remove_active_poll(session_id)
            elif session_id:
                logger.warning(
                    "Preserving OpenCode active poll after ambiguous native start "
                    "failure for Turn=%s attempt=%s",
                    logical_turn_id,
                    start_attempt_id,
                )

            message = f"OpenCode request failed: {error_text}"
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                error_text,
                display_text=message,
                request=request,
            )
        finally:
            if steer_state is not None:
                async with steer_state.lock:
                    steer_state.closing = True
                if self._steering_states.get(request.base_session_id) is steer_state:
                    self._steering_states.pop(request.base_session_id, None)
            if run_registered:
                await server.mark_run_inactive(session_id)

    def additional_steer_targets(self, session_id: str) -> list[ActiveSteerTarget]:
        """Expose restored poll owners that did not pass through AgentService."""

        targets: list[ActiveSteerTarget] = []
        for state in list(self._steering_states.values()):
            if not state.restored or state.target_session_id != session_id or not state.logical_turn_id:
                continue
            targets.append(
                ActiveSteerTarget(
                    runtime_key=state.base_session_id,
                    logical_turn_id=state.logical_turn_id,
                    context=None,
                    agent_request=None,
                    agent=self,
                )
            )
        return targets

    def steering_native_turn_id(self, target: ActiveSteerTarget) -> Optional[str]:
        active_request = target.agent_request
        base_session_id = active_request.base_session_id if active_request is not None else target.runtime_key
        task = self._active_requests.get(base_session_id)
        state = self._steering_states.get(base_session_id)
        if (
            task is None
            or _task_is_stopping(task)
            or state is None
            or state.task is not task
            or state.closing
        ):
            return None
        return state.native_turn_id

    async def steer_active_turn(
        self,
        request: SteerRequest,
        target: ActiveSteerTarget,
    ) -> SteerResult:
        active_request = target.agent_request
        base_session_id = active_request.base_session_id if active_request is not None else target.runtime_key
        task = self._active_requests.get(base_session_id)
        request_session = self._session_manager.get_request_session(base_session_id)
        state = self._steering_states.get(base_session_id)
        if task is None or _task_is_stopping(task) or request_session is None or state is None:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="no_active_native_run", backend=self.name)

        native_session_id, directory, _session_key = request_session
        native_turn_id = state.native_turn_id
        if native_turn_id != request.expected_native_turn_id:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="stale_native_turn", backend=self.name)

        server = self._client_manager._server_manager
        if server is None:
            return steer_result(SteerOutcome.REFUSED, reason="runtime_unavailable", backend=self.name)

        try:
            async with state.lock:
                if (
                    state.closing
                    or state.task is not task
                    or self._active_requests.get(base_session_id) is not task
                    or _task_is_stopping(task)
                    or self._session_manager.get_request_session(base_session_id) != request_session
                ):
                    return steer_result(
                        SteerOutcome.NOT_ACTIVE,
                        reason="no_active_native_run",
                        backend=self.name,
                    )
                try:
                    messages = await server.list_messages(native_session_id, directory)
                    status = await server.get_session_status(native_session_id, directory)
                except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientConnectionError) as exc:
                    return steer_result(
                        SteerOutcome.REFUSED,
                        reason="preflight_unavailable",
                        backend=self.name,
                        diagnostic=str(exc),
                    )
                except Exception as exc:  # noqa: BLE001 - no input write was attempted
                    return steer_result(
                        SteerOutcome.REFUSED,
                        reason="preflight_failed",
                        backend=self.name,
                        diagnostic=str(exc),
                    )

                if status is None or status.get("type") not in {"busy", "retry"}:
                    return steer_result(
                        SteerOutcome.NOT_ACTIVE,
                        reason="native_session_idle",
                        backend=self.name,
                    )
                before_insert = _SteeringAwareOpenCodeServer._message_ids(messages)
                try:
                    prompt_kwargs = {
                        "session_id": native_session_id,
                        "directory": directory,
                        "text": request.text,
                        "agent": state.agent,
                        "model": state.model,
                        "reasoning_effort": state.reasoning_effort,
                        "system": state.system,
                        "tools": {"question": False},
                    }
                    if request.attempt_id:
                        prompt_kwargs["message_id"] = request.attempt_id
                    await server.prompt_async(
                        **prompt_kwargs,
                    )
                except aiohttp.ClientConnectorError:
                    raise
                except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientConnectionError):
                    state.awaiting_after_message_ids = before_insert
                    state.awaiting_user_text = request.text
                    raise
                state.awaiting_after_message_ids = before_insert
                state.awaiting_user_text = request.text
        except OpenCodePromptRejectedError as exc:
            outcome = SteerOutcome.NOT_ACTIVE if exc.status == 404 else SteerOutcome.REFUSED
            reason = "native_session_missing" if exc.status == 404 else "backend_refused"
            return steer_result(
                outcome,
                reason=reason,
                backend=self.name,
                status=exc.status,
                diagnostic=exc.response_text,
            )
        except aiohttp.ClientConnectorError as exc:
            return steer_result(
                SteerOutcome.REFUSED,
                reason="runtime_unavailable",
                backend=self.name,
                diagnostic=str(exc),
            )
        except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientConnectionError) as exc:
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="acknowledgement_ambiguous",
                backend=self.name,
                diagnostic=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a response is a definitive rejection
            return steer_result(
                SteerOutcome.REFUSED,
                reason="backend_refused",
                backend=self.name,
                diagnostic=str(exc),
            )

        if self._active_requests.get(base_session_id) is not task or _task_is_stopping(task):
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="runner_generation_changed",
                backend=self.name,
            )
        return steer_result(
            SteerOutcome.ACCEPTED,
            backend=self.name,
            native_session_id=native_session_id,
            runner_generation=native_turn_id,
        )

    async def reconcile_steer_attempt(
        self,
        request: SteerReconcileRequest,
        target: ActiveSteerTarget,
    ) -> SteerResult:
        """Resolve one prior OpenCode write by its native message identity."""

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
            else target.runtime_key
        )
        request_session = self._session_manager.get_request_session(base_session_id)
        if request_session is None:
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="native_session_unavailable",
                backend=self.name,
            )
        native_session_id, directory, _session_key = request_session
        if not request.expected_native_turn_id.startswith(f"opencode:{native_session_id}:"):
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="stale_native_session",
                backend=self.name,
            )
        server = self._client_manager._server_manager
        if server is None:
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="runtime_unavailable",
                backend=self.name,
            )
        try:
            message = await server.get_message(
                native_session_id,
                request.attempt_id,
                directory,
            )
        except Exception as exc:  # noqa: BLE001 - absence is not negative proof
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="attempt_evidence_unavailable",
                backend=self.name,
                diagnostic=str(exc),
            )
        info = message.get("info") if isinstance(message, dict) else None
        if (
            isinstance(info, dict)
            and str(info.get("id") or "") == request.attempt_id
            and info.get("role") == "user"
        ):
            return steer_result(
                SteerOutcome.ACCEPTED,
                reason="native_message_found",
                backend=self.name,
                native_message_id=request.attempt_id,
            )
        return steer_result(
            SteerOutcome.UNKNOWN,
            reason="untrusted_attempt_evidence",
            backend=self.name,
        )

    async def handle_stop(self, request: AgentRequest) -> bool:
        task = self._active_requests.get(request.base_session_id)
        if not task or task.done():
            request.stop_failure_reason = "not_active"
            return False

        req_info = self._session_manager.get_request_session(request.base_session_id)
        opencode_session_id = None
        if req_info:
            opencode_session_id = req_info[0]
        try:
            await self._abort_active_request(request.base_session_id, task, req_info)
        except Exception as e:
            logger.warning(f"Failed to abort OpenCode session: {e}")

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        if opencode_session_id:
            self.sessions.remove_active_poll(opencode_session_id)

        # A user-initiated stop is terminal but intentional, so it carries NO
        # user-facing message: a single SILENT result settles the dot to idle +
        # releases the SSE waiter through the outbound chokepoint without a bubble
        # (``level="silent"`` makes that explicit rather than faking it via empty text).
        # ``stop_output_for`` (not the terminal-turn default) keeps this empty body out
        # of the run's terminal state so the stop settles it ``canceled`` instead of
        # ``succeeded`` — see its docstring.
        await self.controller.emit_agent_message(
            request.context,
            "result",
            "",
            level="silent",
            output=stop_output_for(request),
        )
        logger.info(f"OpenCode session {request.base_session_id} terminated via /stop")
        return True

    async def clear_sessions(self, session_key: str) -> int:
        self.sessions.clear_agent_sessions(session_key, self.name)
        terminated = 0
        for base_id, task in list(self._active_requests.items()):
            req_info = self._session_manager.get_request_session(base_id)
            if req_info and len(req_info) >= 3 and req_info[2] == session_key:
                opencode_session_id = req_info[0]
                if not task.done():
                    try:
                        await self._abort_active_request(base_id, task, req_info)
                    except Exception:
                        pass
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    terminated += 1
                self.sessions.remove_active_poll(opencode_session_id)
        return terminated

    async def _abort_active_request(
        self,
        base_session_id: str,
        task: asyncio.Task,
        request_session: tuple[str, str, str] | None,
    ) -> None:
        state = self._steering_states.get(base_session_id)
        if state is not None and state.task is task:
            async with state.lock:
                state.closing = True
                if request_session:
                    server = await self._get_server()
                    await server.abort_session(request_session[0], request_session[1])
            return
        if request_session:
            server = await self._get_server()
            await server.abort_session(request_session[0], request_session[1])

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        return {
            f"{base_id}:{req_info[1]}"
            for base_id, req_info in self._session_manager.list_for_session_key(session_key).items()
        }

    def runtime_turn_keys(self) -> set[str]:
        return {
            f"{base_id}:{req_info[1]}"
            for base_id, req_info in self._session_manager.list_all().items()
        }

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

    # _remove_ack_reaction is inherited from BaseAgent

    @staticmethod
    def _workbench_session_id_for_poll(poll_info) -> Optional[str]:
        """Resolve the avibe workbench session id a restored poll belongs to, or
        ``None`` for an IM poll.

        Mirrors how the inbound chokepoint resolves it
        (``Controller._session_id_from_context`` reads
        ``platform_specific["agent_session_id"]``): for an avibe turn the dispatch
        stamps ``agent_session_id`` = the workbench session PK, which equals the
        session's anchor and therefore the OpenCode ``base_session_id`` the poll
        ran under (see ``internal_server._build_session_context`` +
        ``SessionHandler.get_base_session_id``). The persisted poll snapshot does
        not carry ``agent_session_id``, so we recover it from ``base_session_id``
        for avibe polls only — IM polls return ``None`` and get no status dot.
        """
        if restored_platform_from_poll_info(poll_info) != "avibe":
            return None
        base_session_id = poll_info.base_session_id or ""
        return base_session_id or None

    async def restore_active_polls(self, platforms: set[str] | None = None) -> int:
        """Restore active poll loops that were interrupted by vibe-remote restart."""

        active_polls = self.sessions.get_all_active_polls()
        if not active_polls:
            logger.debug("No active polls to restore")
            return 0

        restored_count = 0
        stale_poll_ids = []
        restoration_results: list[
            tuple[asyncio.Future[bool], asyncio.Event, Any]
        ] = []

        for session_id, poll_info in active_polls.items():
            poll_platform = restored_platform_from_poll_info(poll_info)
            if platforms is not None and poll_platform not in platforms:
                continue
            existing_task = self._active_requests.get(poll_info.base_session_id)
            if existing_task is not None and not existing_task.done():
                continue
            processing_snapshot = (
                poll_info.processing_indicator
                if isinstance(poll_info.processing_indicator, dict)
                else {}
            )
            start_attempt_id = str(
                processing_snapshot.get("delivery_start_attempt_id") or ""
            ).strip()
            steering_snapshot = processing_snapshot.get(_STEERING_SNAPSHOT_KEY)
            logical_turn_id = (
                str(steering_snapshot.get("logical_turn_id") or "").strip()
                if isinstance(steering_snapshot, dict)
                else ""
            )
            verification_unknown = False
            try:
                server = await self._get_server()
                messages = await server.list_messages(
                    session_id=poll_info.opencode_session_id,
                    directory=poll_info.working_path,
                )
            except Exception as err:
                logger.warning(f"Failed to verify OpenCode session {session_id} for restoration: {err}")
                messages = []
                verification_unknown = True

            baseline_message_ids = set(poll_info.baseline_message_ids)
            start_attempt_found = any(
                start_attempt_id
                and message.get("info", {}).get("role") == "user"
                and str(message.get("info", {}).get("id") or "")
                == start_attempt_id
                for message in messages
            )
            has_in_progress = False
            last_assistant_finish = None
            last_completed_assistant_index = -1
            for index, message in enumerate(messages):
                info = message.get("info", {})
                if info.get("role") != "assistant":
                    continue
                time_info = info.get("time") or {}
                if not time_info.get("completed"):
                    has_in_progress = True
                    continue
                if info.get("id") in baseline_message_ids:
                    continue
                last_completed_assistant_index = index
                last_assistant_finish = info.get("finish")

            has_post_assistant_user = any(
                last_completed_assistant_index >= 0
                and index > last_completed_assistant_index
                and message.get("info", {}).get("role") == "user"
                and message.get("info", {}).get("id") not in baseline_message_ids
                for index, message in enumerate(messages)
            )
            reconcile_after_message_ids = (
                {
                    str(message.get("info", {}).get("id"))
                    for message in messages[: last_completed_assistant_index + 1]
                    if message.get("info", {}).get("id")
                }
                if has_post_assistant_user
                else None
            )
            status_unknown = verification_unknown
            native_status = None
            if not verification_unknown:
                try:
                    native_status = await server.get_session_status(
                        poll_info.opencode_session_id,
                        poll_info.working_path,
                    )
                except Exception as err:
                    logger.debug("Failed to read OpenCode status while restoring %s: %s", session_id, err)
                    status_unknown = True

            native_status_is_active = (
                native_status is not None
                and native_status.get("type") in {"busy", "retry"}
            )
            if native_status_is_active and reconcile_after_message_ids is None:
                reconcile_after_message_ids = {
                    str(message.get("info", {}).get("id"))
                    for message in messages[: last_completed_assistant_index + 1]
                    if message.get("info", {}).get("id")
                }
            session_still_active = (
                status_unknown
                or native_status_is_active
                or has_in_progress
                or last_assistant_finish == "tool-calls"
                or has_post_assistant_user
                or start_attempt_found
            )
            if not session_still_active:
                if start_attempt_id and logical_turn_id:
                    try:
                        self.controller.session_turns.reconcile_start_attempt_not_written(
                            logical_turn_id,
                            start_attempt_id,
                            backend="opencode",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist definitive missing OpenCode start "
                            "attempt for Turn=%s",
                            logical_turn_id,
                        )
                        continue
                logger.info(f"OpenCode session {session_id} has completed, removing from active polls")
                await self._poll_loop.remove_restored_ack(poll_info)
                stale_poll_ids.append(session_id)
                continue

            logger.info(
                f"Restoring poll loop for OpenCode session {session_id} "
                f"(thread={poll_info.base_session_id}, cwd={poll_info.working_path})"
            )

            restoration_ready = asyncio.get_running_loop().create_future()
            restoration_published = asyncio.Event()
            task = asyncio.create_task(
                self._run_restored_poll_loop_with_tracking(
                    poll_info,
                    reconcile_initial_status=status_unknown,
                    reconcile_after_message_ids=reconcile_after_message_ids,
                    restoration_ready=restoration_ready,
                    restoration_published=restoration_published,
                )
            )
            restoration_results.append(
                (restoration_ready, restoration_published, poll_info)
            )
            self._active_requests[poll_info.base_session_id] = task
            self._session_manager.set_request_session(
                poll_info.base_session_id,
                poll_info.opencode_session_id,
                poll_info.working_path,
                restored_session_key_from_poll_info(poll_info),
            )
        for session_id in stale_poll_ids:
            self.sessions.remove_active_poll(session_id)

        if restoration_results:
            registered = await asyncio.gather(
                *(future for future, _, _ in restoration_results)
            )
            for is_registered, (_, published, poll_info) in zip(
                registered,
                restoration_results,
            ):
                try:
                    if not is_registered:
                        continue
                    workbench_session_id = self._workbench_session_id_for_poll(poll_info)
                    if workbench_session_id:
                        self.controller.session_turns.restore_running(workbench_session_id)
                    restored_count += 1
                finally:
                    published.set()

        if restored_count > 0:
            logger.info(f"Restored {restored_count} active poll loop(s)")
        if stale_poll_ids:
            logger.info(f"Removed {len(stale_poll_ids)} stale active poll(s)")

        return restored_count

    async def _run_restored_poll_loop_with_tracking(
        self,
        poll_info,
        *,
        reconcile_initial_status: bool = False,
        reconcile_after_message_ids: set[str] | None = None,
        restoration_ready: asyncio.Future[bool] | None = None,
        restoration_published: asyncio.Event | None = None,
    ) -> None:
        current_task = asyncio.current_task()
        steer_state = None
        server = None
        restoration_registered = False
        try:
            poll_platform = restored_platform_from_poll_info(poll_info)
            registration_attempts = 2 if poll_platform in _RESTORED_IM_PLATFORMS else 1
            for attempt in range(registration_attempts):
                try:
                    server = await self._get_server()
                    await server.mark_run_active(poll_info.opencode_session_id)
                    break
                except Exception:
                    if attempt + 1 >= registration_attempts:
                        raise
                    logger.warning(
                        "Retrying restored OpenCode IM poll registration for session=%s",
                        poll_info.opencode_session_id,
                        exc_info=True,
                    )
                    await asyncio.sleep(_RESTORED_IM_REGISTRATION_RETRY_DELAY_SECONDS)
            steering_snapshot = (
                poll_info.processing_indicator.get(_STEERING_SNAPSHOT_KEY)
                if isinstance(poll_info.processing_indicator, dict)
                else None
            )
            target_session_id = (
                str(steering_snapshot.get("target_session_id") or "")
                if isinstance(steering_snapshot, dict)
                else ""
            )
            logical_turn_id = (
                str(steering_snapshot.get("logical_turn_id") or "")
                if isinstance(steering_snapshot, dict)
                else ""
            )
            has_steering_identity = bool(target_session_id and logical_turn_id)
            if current_task is not None and (
                has_steering_identity
                or reconcile_initial_status
                or reconcile_after_message_ids is not None
            ):
                steer_state = _OpenCodeSteerState(
                    task=current_task,
                    base_session_id=poll_info.base_session_id,
                    target_session_id=target_session_id,
                    logical_turn_id=logical_turn_id,
                    native_session_id=poll_info.opencode_session_id,
                    directory=poll_info.working_path,
                    agent=(
                        steering_snapshot.get("agent")
                        if isinstance(steering_snapshot, dict)
                        and isinstance(steering_snapshot.get("agent"), str)
                        else None
                    ),
                    model=poll_info.model_dict,
                    reasoning_effort=poll_info.reasoning_effort,
                    system=(
                        steering_snapshot.get("system")
                        if isinstance(steering_snapshot, dict)
                        and isinstance(steering_snapshot.get("system"), str)
                        else None
                    ),
                    baseline_message_ids=set(poll_info.baseline_message_ids),
                    awaiting_after_message_ids=(
                        set(reconcile_after_message_ids)
                        if reconcile_after_message_ids is not None
                        else None
                    ),
                    idle_reconciliation_message=self._idle_reconciliation_message(
                        poll_info.model_dict,
                        poll_info.reasoning_effort,
                    ),
                    restored=True,
                    reconcile_initial_status=reconcile_initial_status,
                )
                if has_steering_identity:
                    self._steering_states[poll_info.base_session_id] = steer_state
                self._restored_poll_servers[current_task] = _SteeringAwareOpenCodeServer(
                    server,
                    steer_state,
                )
            restoration_registered = True
            if restoration_ready is not None and not restoration_ready.done():
                restoration_ready.set_result(True)
            if restoration_published is not None:
                await restoration_published.wait()
            delivery_recovery_complete = getattr(
                self.controller,
                "_delivery_recovery_complete",
                None,
            )
            if delivery_recovery_complete is not None:
                await delivery_recovery_complete.wait()
            await self._poll_loop.run_restored_poll_loop(poll_info)
        except Exception as err:
            if restoration_registered:
                raise
            else:
                steering_snapshot = (
                    poll_info.processing_indicator.get(_STEERING_SNAPSHOT_KEY)
                    if isinstance(poll_info.processing_indicator, dict)
                    else None
                )
                logical_turn_id = (
                    str(steering_snapshot.get("logical_turn_id") or "").strip()
                    if isinstance(steering_snapshot, dict)
                    else ""
                )
                terminal_persisted = False
                if logical_turn_id:
                    try:
                        terminal_persisted = bool(
                            self.controller.session_turns.fail_restored_backend_turn(
                                logical_turn_id,
                                backend="opencode",
                                reason="poll_registration_failed",
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Failed to terminalize OpenCode Turn=%s after poll registration error",
                            logical_turn_id,
                        )
                if terminal_persisted:
                    self.sessions.remove_active_poll(poll_info.opencode_session_id)
                logger.error(
                    "OpenCode poll registration failed for session=%s: %s",
                    poll_info.opencode_session_id,
                    err,
                )
        finally:
            if current_task is not None:
                self._restored_poll_servers.pop(current_task, None)
            if steer_state is not None:
                async with steer_state.lock:
                    steer_state.closing = True
                if self._steering_states.get(poll_info.base_session_id) is steer_state:
                    self._steering_states.pop(poll_info.base_session_id, None)
            if server is not None:
                try:
                    await server.mark_run_inactive(poll_info.opencode_session_id)
                except Exception:
                    logger.exception(
                        "Failed to clear restored OpenCode run marker for session=%s",
                        poll_info.opencode_session_id,
                    )
            if self._active_requests.get(poll_info.base_session_id) is current_task:
                self._active_requests.pop(poll_info.base_session_id, None)
                self._session_manager.pop_request_session(poll_info.base_session_id)
            if restoration_ready is not None and not restoration_ready.done():
                restoration_ready.set_result(False)

    def _prepare_message_with_files(self, request: AgentRequest) -> str:
        """Prepare message with file attachment information.

        If there are file attachments, append file info to the message
        so the agent knows what files are available to read.
        Files are stored in ~/.vibe_remote/attachments/{channel_id}/.

        Args:
            request: The agent request containing message and files

        Returns:
            Message string, potentially with file info appended
        """
        if not request.files:
            return request.message

        # Build file info section
        images = []
        other_files = []

        for attachment in request.files:
            if not attachment.local_path:
                continue

            is_image = (attachment.mimetype or "").startswith("image/")
            if is_image:
                images.append(attachment)
            else:
                other_files.append(attachment)

        if not images and not other_files:
            return request.message

        # Format file info as a clear block at the end
        file_lines = ["", "[User Attachments]"]

        for img in images:
            size_str = f", {img.size} bytes" if img.size else ""
            file_lines.append(f"- Image: {img.local_path} ({img.mimetype}{size_str})")

        for f in other_files:
            size_str = f", {f.size} bytes" if f.size else ""
            file_lines.append(f"- File: {f.local_path} ({f.mimetype}{size_str})")

        file_info = "\n".join(file_lines)

        # If there's no text message, just use file info (without leading newline)
        if not request.message or not request.message.strip():
            return file_info.lstrip()

        # Append file info to message
        return f"{request.message}{file_info}"
