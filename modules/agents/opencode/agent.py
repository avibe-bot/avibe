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
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import aiohttp

from core.backend_failure import emit_backend_failure
from core.caller_context import (
    AVIBE_CALLER_PLATFORM_ENV,
    AVIBE_CALLER_REMOTE_ENV,
    AVIBE_CALLER_USER_ID_ENV,
    AVIBE_SESSION_ID_ENV,
    caller_env_for_platform_payload,
    validated_caller_env_snapshot,
)
from core.message_output import stop_output_for, terminal_output_for
from core.memory_cli_access import configure_memory_cli_access
from core.managed_skills import (
    BUILTIN_SKILLS_ROOT_ENV,
    BUILTIN_SKILLS_SNAPSHOT_ENV,
    managed_skill_claude_cli_path,
    managed_skill_environment,
    managed_skill_project_base,
)
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.native_dispatch_phase import (
    mark_backend_dispatch_attempted,
    prewrite_user_stop_requested,
)
from core.resource_governance import governor_from_controller
from core.runtime_activation import RuntimeActivationIdentity
from core.runtime_ownership import (
    RuntimeResourceTarget,
    RuntimeSessionBinding,
    wake_runtime_ownership,
)
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
)
from modules.agents.base import AgentRequest, BaseAgent
from modules.agents.model_hub import (
    ModelHubLaunch,
    OpenCodeOverlay,
    bind_launch,
    bind_turn_mode,
    opencode_model_for_overlay,
    opencode_model_catalog_for_overlay,
    opencode_requested_model_for_overlay,
    persisted_launch_identity,
    resolve_opencode_overlay_launch,
)
from vibe.i18n import t as i18n_t

from .caller_context import (
    bind_session as bind_caller_context_session,
    refresh_session as refresh_caller_context_session,
    unbind_session as unbind_caller_context_session,
)
from .client_manager import OpenCodeClientManager
from .message_processor import OpenCodeMessageProcessorMixin
from .poll_loop import (
    OpenCodePollLoop,
    restored_context_from_poll_info,
    restored_platform_from_poll_info,
    restored_session_key_from_poll_info,
)
from .server import (
    OpenCodeManagedPolicyRefreshPendingError,
    OpenCodeModelHubOverlayRequiredError,
    OpenCodePromptRejectedError,
    OpenCodeRuntimeConfigInvalidError,
    OpenCodeServerManager,
    native_part_id_for_attempt,
)
from .session import (
    OpenCodeResumeUnavailableError,
    OpenCodeSessionManager,
    requires_message_order_repair,
)
from .utils import resolve_opencode_model_id, resolve_opencode_reasoning_effort

logger = logging.getLogger(__name__)
_STEERING_SNAPSHOT_KEY = "opencode_native_steering"
_MODEL_HUB_DISPLAY_MODEL_KEY = "model_hub_display_model"
_CALLER_CONTEXT_ENV_SNAPSHOT_KEY = "opencode_caller_context_env"
_MANAGED_SKILL_PROJECT_BASE_SNAPSHOT_KEY = "opencode_managed_skill_project_base"
_MANAGED_SKILL_BUILTIN_SNAPSHOT_KEY = "opencode_managed_skill_builtin_snapshot"
_STATUS_RECONCILIATION_FAILURE_LIMIT = 3
# A successful prompt_async response transfers ownership to a forked worker,
# but does not prove that worker will publish its first message. Keep this
# handoff bounded independently of the optional whole-turn timeout.
_ASYNC_PROMPT_ACCEPTED_ACTIVITY_TIMEOUT_SECONDS = 120.0
# OpenCode can report idle just before the completed assistant message becomes
# visible through the message-list endpoint.
_ASYNC_PROMPT_RESULT_CONFIRMATION_TIMEOUT_SECONDS = 5.0
# A prompt accepted at the end of the previous native turn can race its final
# status transition. Let that transition settle before trusting the write as a
# same-turn steer.
_STEER_POST_WRITE_STATUS_SETTLE_SECONDS = 0.1
_RESTORED_IM_REGISTRATION_RETRY_DELAY_SECONDS = 0.25
_RESTORED_IM_PLATFORMS = {"slack", "discord", "telegram", "lark", "wechat"}
_CALLER_CONTEXT_BINDING_REFRESH_SECONDS = 60 * 60
_CALLER_CONTEXT_BINDING_RETRY_SECONDS = 1.0


def _caller_context_path_for_server(server: object) -> str | None:
    resolver = getattr(server, "caller_context_binding_path", None)
    if not callable(resolver):
        return None
    try:
        value = os.fspath(resolver())
    except (TypeError, OSError):
        return None
    return value or None


def _binding_path_kwargs(path: str | None) -> dict[str, str]:
    return {"path": path} if path else {}


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
    awaiting_prompt_accepted: bool = False
    awaiting_prompt_activity_deadline: float | None = None
    awaiting_active_status_observed: bool = False
    awaiting_result_confirmation_deadline: float | None = None
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
        self.last_list_native_live: bool | None = None

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

    @staticmethod
    def _last_is_completed_error(messages: list[Dict[str, Any]]) -> bool:
        if not messages:
            return False
        info = messages[-1].get("info", {}) or {}
        return bool(
            info.get("role") == "assistant"
            and info.get("time", {}).get("completed")
            and info.get("error")
        )

    @staticmethod
    def _has_post_boundary_activity(
        messages: list[Dict[str, Any]],
        boundary_ids: set[str],
        baseline_ids: set[str],
    ) -> bool:
        # Evidence means a message the restored poll loop would treat as new:
        # outside the reconciliation boundary AND outside the pre-prompt
        # baseline. The baseline term matters when the sampled boundary is
        # empty (only baseline messages existed when it was sampled).
        for message in messages:
            message_id = (message.get("info", {}) or {}).get("id")
            if not message_id:
                continue
            if message_id not in boundary_ids and message_id not in baseline_ids:
                return True
        return False

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

    @classmethod
    def _has_completed_error_after(
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
            and message.get("info", {}).get("error")
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

    def _clear_awaiting_reconciliation(self) -> None:
        self._state.awaiting_after_message_ids = None
        self._state.awaiting_user_text = None
        self._state.awaiting_prompt_accepted = False
        self._state.awaiting_prompt_activity_deadline = None
        self._state.awaiting_active_status_observed = False
        self._state.awaiting_result_confirmation_deadline = None

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
                self.last_list_native_live = None
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
                        from modules.agents.opencode.poll_loop import _native_session_is_live

                        self.last_list_native_live = _native_session_is_live(status)
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
                                self._clear_awaiting_reconciliation()
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
                            if reconcile_insert and (
                                inserted_user_text is None
                                or self._inserted_user_index(
                                    messages,
                                    awaiting,
                                    inserted_user_text,
                                )
                                >= 0
                            ):
                                # Start confirmed: the inserted user message is
                                # visible while the native session reports
                                # activity (or a restored poll sampled its
                                # boundary during activity). Hand the in-progress
                                # snapshot back to the poll loop so live tool
                                # activity keeps streaming; the result-confirmation
                                # window still guards the idle boundary below.
                                if inserted_user_text is not None:
                                    self._state.awaiting_active_status_observed = True
                                    self._state.awaiting_result_confirmation_deadline = None
                                    if final_snapshot or self._has_final_assistant_after(
                                        messages,
                                        awaiting,
                                        inserted_user_text=inserted_user_text,
                                    ):
                                        # The final assistant already exists even
                                        # though the session still reports busy
                                        # (status settles after the message lands):
                                        # settle like the idle path does.
                                        self._clear_awaiting_reconciliation()
                                        self._state.closing = True
                                if self._last_is_completed_error(messages):
                                    # Keep terminal error snapshots gated while the
                                    # native runtime has not settled: the poll loop
                                    # would emit its own terminal failure (or a
                                    # competing "continue") while OpenCode's native
                                    # retry is still active.
                                    wait_for_insert = True
                                elif inserted_user_text is None and not self._has_post_boundary_activity(
                                    messages,
                                    awaiting,
                                    self._state.baseline_message_ids,
                                ):
                                    # Restored boundary-sampled polls: a busy
                                    # snapshot holding only boundary messages
                                    # (ending in a pre-restore final answer) must
                                    # not be handed back, or the restored poll
                                    # loop would re-emit the old answer and drop
                                    # the still-running poll. Wait for post-boundary
                                    # or in-progress evidence.
                                    wait_for_insert = True
                                else:
                                    return messages
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
                                if not inserted_user_missing:
                                    self._state.awaiting_active_status_observed = True
                                last_message_id = (
                                    messages[-1].get("info", {}).get("id")
                                    if messages
                                    else None
                                )
                                if (
                                    inserted_user_missing
                                    and final_snapshot
                                    and last_message_id in awaiting
                                    and not self._state.awaiting_prompt_accepted
                                ):
                                    self._clear_awaiting_reconciliation()
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
                                has_post_boundary_error = self._has_completed_error_after(
                                    messages,
                                    awaiting,
                                    inserted_user_text=inserted_user_text,
                                )
                                if has_final_insert_result or has_post_boundary_error:
                                    self._clear_awaiting_reconciliation()
                                    if has_final_insert_result:
                                        self._state.closing = True
                                    return messages
                                if self._state.awaiting_active_status_observed:
                                    result_deadline = (
                                        self._state.awaiting_result_confirmation_deadline
                                    )
                                    if result_deadline is None:
                                        self._state.awaiting_result_confirmation_deadline = (
                                            time.monotonic()
                                            + _ASYNC_PROMPT_RESULT_CONFIRMATION_TIMEOUT_SECONDS
                                        )
                                        wait_for_insert = True
                                    elif time.monotonic() < result_deadline:
                                        wait_for_insert = True
                                    else:
                                        self._clear_awaiting_reconciliation()
                                        return self._terminal_reconciliation_failure(
                                            session_id,
                                            messages,
                                            name="NativeSessionEndedBeforeResult",
                                            message=self._idle_reconciliation_error_text(),
                                        )
                                elif self._state.awaiting_prompt_accepted:
                                    # A successful prompt_async response transfers
                                    # ownership before OpenCode must publish a
                                    # message or mark the session busy.
                                    activity_deadline = (
                                        self._state.awaiting_prompt_activity_deadline
                                    )
                                    if activity_deadline is None:
                                        activity_deadline = (
                                            time.monotonic()
                                            + _ASYNC_PROMPT_ACCEPTED_ACTIVITY_TIMEOUT_SECONDS
                                        )
                                        self._state.awaiting_prompt_activity_deadline = (
                                            activity_deadline
                                        )
                                    if time.monotonic() < activity_deadline:
                                        wait_for_insert = True
                                    else:
                                        self._clear_awaiting_reconciliation()
                                        return self._terminal_reconciliation_failure(
                                            session_id,
                                            messages,
                                            name="NativeSessionEndedBeforeResult",
                                            message=self._idle_reconciliation_error_text(),
                                        )
                                else:
                                    self._clear_awaiting_reconciliation()
                                    return self._terminal_reconciliation_failure(
                                        session_id,
                                        messages,
                                        name="NativeSessionEndedBeforeResult",
                                        message=self._idle_reconciliation_error_text(),
                                    )
                            else:
                                self._clear_awaiting_reconciliation()
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
                    self._clear_awaiting_reconciliation()
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
            kwargs_map = dict(kwargs)
            prompt_text = str(kwargs_map.get("text") or "")
            snapshot_ids = kwargs_map.pop("awaiting_after_ids", None)
            if snapshot_ids is None:
                session_id = str(kwargs_map.get("session_id") or "")
                directory = str(kwargs_map.get("directory") or "")
                if session_id and directory:
                    try:
                        current = await self._server.list_messages(session_id, directory)
                    except Exception:
                        current = []
                    snapshot_ids = self._message_ids(current)
            if snapshot_ids is not None:
                self._state.awaiting_after_message_ids = set(snapshot_ids)
                self._state.awaiting_user_text = prompt_text or None
                self._state.awaiting_prompt_accepted = False
                self._state.awaiting_prompt_activity_deadline = None
                self._state.awaiting_active_status_observed = False
                self._state.awaiting_result_confirmation_deadline = None
            await self._server.prompt_async(*args, **{k: v for k, v in kwargs.items() if k != "awaiting_after_ids"})
            if snapshot_ids is not None:
                self._state.awaiting_prompt_accepted = True
                self._state.awaiting_prompt_activity_deadline = (
                    time.monotonic()
                    + _ASYNC_PROMPT_ACCEPTED_ACTIVITY_TIMEOUT_SECONDS
                )

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
        model_hub_runtime = getattr(controller, "model_hub_runtime", None)
        prepare_overlay = getattr(
            model_hub_runtime,
            "prepare_opencode_overlay",
            None,
        )
        if callable(prepare_overlay):
            self._client_manager.set_model_hub_overlay_preparer(prepare_overlay)
        self._session_manager = OpenCodeSessionManager(self.settings_manager, self.name)

        self._poll_loop = OpenCodePollLoop(self)

        self._active_requests: Dict[str, asyncio.Task] = {}
        # Sessions whose in-flight task is being cancelled BY THE USER. The
        # cancellation lands in the request coroutine, which cannot otherwise
        # tell a /stop from any other teardown; see ``handle_stop``.
        self._user_stopped_sessions: set[str] = set()
        self._session_last_activity: Dict[str, float] = {}
        self._steering_states: Dict[str, _OpenCodeSteerState] = {}
        self._restored_poll_servers: Dict[asyncio.Task, _SteeringAwareOpenCodeServer] = {}

    async def _get_server(self) -> OpenCodeServerManager:
        current_task = asyncio.current_task()
        if current_task is not None:
            restored_server = self._restored_poll_servers.get(current_task)
            if restored_server is not None:
                return restored_server
        server = await self._client_manager.get_server()
        server.set_active_poll_session_ids_provider(
            lambda: set(self.sessions.get_all_active_polls())
        )
        self._attach_server_activation(server)
        return server

    async def _maintain_caller_context_binding(
        self,
        session_id: str,
        binding_token: str,
        binding_path: str | None,
        *,
        payload: dict[str, Any] | None,
        working_directory: str,
        extra_env: dict[str, str],
        initially_bound: bool,
        message: Any = None,
        fallback_platform: str | None = None,
    ) -> None:
        """Keep one Turn binding published and renewed for its full lifetime."""

        bound = initially_bound
        while True:
            await asyncio.sleep(
                _CALLER_CONTEXT_BINDING_REFRESH_SECONDS
                if bound
                else _CALLER_CONTEXT_BINDING_RETRY_SECONDS
            )
            if bound:
                try:
                    refreshed = bool(
                        await asyncio.to_thread(
                            refresh_caller_context_session,
                            session_id,
                            binding_token=binding_token,
                            **_binding_path_kwargs(binding_path),
                        )
                    )
                except Exception:
                    logger.warning(
                        "Failed to renew OpenCode caller context for session %s",
                        session_id,
                        exc_info=True,
                    )
                    bound = False
                else:
                    if not refreshed:
                        return
                    bound = True
                    continue
            try:
                bound = bool(
                    await asyncio.to_thread(
                        bind_caller_context_session,
                        session_id,
                        payload,
                        base_env=os.environ,
                        working_dir=working_directory,
                        extra_env=extra_env,
                        binding_token=binding_token,
                        replace_existing=False,
                        **_binding_path_kwargs(binding_path),
                        message=message,
                        fallback_platform=fallback_platform,
                    )
                )
                if not bound:
                    return
            except Exception:
                logger.warning(
                    "Failed to republish OpenCode caller context for session %s",
                    session_id,
                    exc_info=True,
                )
                bound = False

    @staticmethod
    async def _stop_caller_context_binding_renewal(
        task: asyncio.Task[None] | None,
    ) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _active_poll_is_persisted(self, session_id: str) -> bool:
        try:
            return session_id in self.sessions.get_all_active_polls()
        except Exception:
            logger.warning(
                "Could not verify OpenCode active poll retention for session %s",
                session_id,
                exc_info=True,
            )
            return True

    @staticmethod
    def _server_activation_identity(
        server: OpenCodeServerManager | None,
    ) -> RuntimeActivationIdentity | None:
        identity = getattr(server, "_vibe_runtime_activation_identity", None)
        return identity if isinstance(identity, RuntimeActivationIdentity) else None

    def _attach_server_activation(
        self,
        server: OpenCodeServerManager,
    ) -> RuntimeActivationIdentity | None:
        registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
        if registry is None:
            return None
        existing = self._server_activation_identity(server)
        if existing is not None and registry.is_current(existing):
            return existing
        identity = registry.attach(self.name, server.base_url)
        setattr(server, "_vibe_runtime_activation_identity", identity)
        set_retire = getattr(server, "set_runtime_activation_retire", None)
        if callable(set_retire):
            set_retire(
                lambda force=False, native_turns_drained=False: self._retire_server_activation(
                    server,
                    force=force,
                    native_turns_drained=native_turns_drained,
                )
            )
        return identity

    def _retire_server_activation(
        self,
        server: OpenCodeServerManager,
        *,
        force: bool = False,
        native_turns_drained: bool = False,
    ) -> bool:
        registry = getattr(getattr(self, "controller", None), "runtime_activation", None)
        identity = self._server_activation_identity(server)
        if registry is None or identity is None:
            return True
        if not registry.is_current(identity):
            return True

        def final_predicate() -> bool:
            if force:
                return True
            snapshots = self.runtime_ownership_snapshots()
            return bool(
                snapshots is not None
                and all(
                    not (
                        snapshot.blocks_transport_replacement_after_turn_drain
                        if native_turns_drained
                        else snapshot.blocks_transport_replacement
                    )
                    for snapshot in snapshots
                )
            )

        return bool(registry.retire_if_current(identity, final_predicate))

    def runtime_activation_identity_for_request(
        self,
        request: Any,
    ) -> RuntimeActivationIdentity | None:
        del request
        server = self._client_manager._server_manager
        return self._attach_server_activation(server) if server is not None else None

    def runtime_activation_identity_for_session_binding(
        self,
        *,
        session_anchor: str,
        workdir: str | None,
    ) -> RuntimeActivationIdentity | None:
        normalized_anchor = str(session_anchor or "").strip()
        normalized_workdir = str(workdir or "").strip()
        if not normalized_anchor:
            raise ValueError("OpenCode Session binding has no anchor")
        active = self._session_manager.get_request_session(normalized_anchor)
        if active is not None and str(active[1] or "").strip() != normalized_workdir:
            raise ValueError("OpenCode Session binding changed workdir")
        server = self._client_manager._server_manager
        return self._attach_server_activation(server) if server is not None else None

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

    def _server_start_error_display_text(self, error: BaseException) -> str:
        localized_key = None
        if isinstance(error, OpenCodeModelHubOverlayRequiredError):
            localized_key = "error.opencodeModelHubOverlayRequired"
        elif isinstance(error, OpenCodeManagedPolicyRefreshPendingError):
            localized_key = "error.opencodePolicyRefreshPending"
        elif isinstance(error, OpenCodeRuntimeConfigInvalidError):
            localized_key = "error.opencodeRuntimeConfigInvalid"
        if localized_key is not None:
            language = str(
                getattr(getattr(self.controller, "config", None), "language", "en")
                or "en"
            )
            return f"❌ {i18n_t(localized_key, language)}"
        return f"Failed to start OpenCode server: {error}"

    async def prepare_runtime_restart(self) -> None:
        """Adopt persisted server state before the shared drain snapshot."""
        await self._get_server()

    def runtime_has_active_turns(self) -> bool:
        if any(not task.done() for task in self._active_requests.values()):
            return True
        server = self._client_manager._server_manager
        return bool(server is not None and server.runtime_has_active_turns())

    def runtime_ownership_snapshots(self) -> tuple[Any, ...] | None:
        """Map the one shared OpenCode server to its exact durable identities."""

        server = self._client_manager._server_manager
        if server is None:
            return ()
        request_sessions = self._session_manager.list_all()
        get_agent_session_id = getattr(
            self._session_manager,
            "get_agent_session_id",
            None,
        )
        if request_sessions and not callable(get_agent_session_id):
            logger.error("OpenCode durable Session ownership mapping is unavailable")
            return None
        bindings: list[RuntimeSessionBinding] = []
        for base_session_id, (
            _native_session_id,
            working_path,
            session_key,
        ) in request_sessions.items():
            agent_session_id = str(
                get_agent_session_id(base_session_id) or ""
            ).strip()
            if not agent_session_id or not working_path or not session_key:
                logger.error("OpenCode runtime ownership mapping is incomplete")
                return None
            bindings.append(
                RuntimeSessionBinding(
                    session_id=agent_session_id,
                    session_anchor=base_session_id,
                    workdir=working_path,
                    activity_runtime_keys=(f"{base_session_id}:{working_path}",),
                    fallback_route_keys=(session_key,),
                )
            )
        target = RuntimeResourceTarget(
            backend="opencode",
            resource_key=server.base_url,
            bindings=tuple(bindings),
            known_activity_runtime_keys=tuple(sorted(self.runtime_turn_keys())),
            known_fallback_route_keys=tuple(
                sorted(
                    session_key
                    for (
                        _native_session_id,
                        _working_path,
                        session_key,
                    ) in request_sessions.values()
                )
            ),
            include_all_backend_sessions=True,
            maps_all_backend_activities=True,
            maps_all_backend_fallback_runs=True,
        )
        provider = getattr(self.controller, "runtime_ownership", None)
        snapshot = getattr(provider, "snapshot", None)
        if not callable(snapshot):
            logger.error("OpenCode runtime ownership provider is unavailable")
            return None
        result = snapshot(target)
        wake_runtime_ownership(self.controller, result)
        return (result,)

    def record_runtime_turn_start(
        self,
        *,
        runtime_key: str,
        request: AgentRequest | None,
    ) -> None:
        del runtime_key
        if request is not None:
            self._session_last_activity[request.base_session_id] = time.monotonic()

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
            # Stop holds this same lock until the native abort has settled. Keep
            # the old request's Turn/session ownership until then, so a queued
            # successor cannot reuse the native session under an in-flight abort.
            async with lock:
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
        model_hub_turn_mode: str | None = None
        model_hub_launch: ModelHubLaunch | None = None
        model_hub_overlay_reservation: object | None = None
        server = None
        # Bind early: get_or_create_session_id (below) can raise BEFORE assigning
        # session_id (a transient server error now that get_session raises on
        # non-404), and the error-cleanup paths reference session_id — keep it
        # defined so they can't trip UnboundLocalError (Codex P2).
        session_id = None
        logical_turn_id = ""
        start_attempt_id = ""
        native_start_phase = "before_write"
        activation_identity: RuntimeActivationIdentity | None = None
        caller_context_binding_session_id: str | None = None
        caller_context_binding_token: str | None = None
        caller_context_binding_path: str | None = None
        caller_context_binding_renewal: asyncio.Task[None] | None = None
        active_poll_persisted = False
        active_poll_removal_pending = False

        def remove_active_poll() -> None:
            nonlocal active_poll_persisted, active_poll_removal_pending
            if session_id:
                active_poll_persisted = False
                active_poll_removal_pending = True
        try:
            model_hub_runtime = getattr(self.controller, "model_hub_runtime", None)
            turn_mode = getattr(model_hub_runtime, "turn_mode", None)
            if callable(turn_mode):
                model_hub_turn_mode = turn_mode("opencode")
                bind_turn_mode(
                    request.context,
                    model_hub_turn_mode,
                )
            prepare_overlay = getattr(model_hub_runtime, "prepare_opencode_overlay", None)
            if callable(prepare_overlay):
                model_hub_overlay = await prepare_overlay()
            server = await self._get_server()
            configure_overlay = getattr(server, "configure_model_hub_overlay", None)
            if callable(configure_overlay):
                model_hub_overlay_reservation = await configure_overlay(
                    model_hub_overlay
                )
            await server.ensure_running()
            caller_context_binding_path = _caller_context_path_for_server(server)
            activation_identity = self._attach_server_activation(server)
        except asyncio.CancelledError:
            await self._finish_prestart_cancellation(
                request,
                server,
                model_hub_overlay_reservation,
            )
            raise
        except Exception as e:
            await self._release_model_hub_overlay_reservation(
                server,
                model_hub_overlay_reservation,
            )
            model_hub_overlay_reservation = None
            logger.error(f"Failed to start OpenCode server: {e}", exc_info=True)
            await emit_backend_failure(
                self.controller,
                request.context,
                self.name,
                str(e),
                display_text=self._server_start_error_display_text(e),
                request=request,
            )
            await self._remove_ack_reaction(request)
            return

        try:
            await self._delete_ack(request)
            await self._session_manager.ensure_working_dir(request.working_path)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                await self._finish_prestart_cancellation(
                    request,
                    server,
                    model_hub_overlay_reservation,
                )
            else:
                await self._release_model_hub_overlay_reservation(
                    server,
                    model_hub_overlay_reservation,
                )
            model_hub_overlay_reservation = None
            raise

        try:
            session_id = await self._session_manager.get_or_create_session_id(request, server)
        except asyncio.CancelledError:
            await self._finish_prestart_cancellation(
                request,
                server,
                model_hub_overlay_reservation,
            )
            raise
        except OpenCodeResumeUnavailableError as e:
            await self._release_model_hub_overlay_reservation(
                server,
                model_hub_overlay_reservation,
            )
            model_hub_overlay_reservation = None
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
            await self._release_model_hub_overlay_reservation(
                server,
                model_hub_overlay_reservation,
            )
            model_hub_overlay_reservation = None
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
            await self._release_model_hub_overlay_reservation(
                server,
                model_hub_overlay_reservation,
            )
            model_hub_overlay_reservation = None
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

        try:
            self._session_manager.set_request_session(
                request.base_session_id,
                session_id,
                request.working_path,
                request.session_key,
            )
            self._session_manager.set_agent_session_id(
                request.base_session_id,
                _target_agent_session_id(request),
            )
            self._session_manager.mark_initialized(session_id)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                await self._finish_prestart_cancellation(
                    request,
                    server,
                    model_hub_overlay_reservation,
                )
            else:
                await self._release_model_hub_overlay_reservation(
                    server,
                    model_hub_overlay_reservation,
                )
            model_hub_overlay_reservation = None
            raise

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
            requested_model_str = opencode_requested_model_for_overlay(
                model_str,
                model_hub_overlay,
            )
            if model_hub_runtime is not None and requested_model_str:
                model_hub_launch = await resolve_opencode_overlay_launch(
                    self.controller,
                    requested_model_str,
                    model_hub_overlay,
                )
                bind_launch(request.context, model_hub_launch)
            model_str = opencode_model_for_overlay(
                requested_model_str,
                model_hub_overlay,
            )
            # Bare model id (no ``provider/`` prefix): only inject ``providerID``
            # when the user has explicitly chosen a default provider in Settings.
            # Otherwise leave ``model_dict`` unset so OpenCode keeps using its own
            # routing for legacy installs.
            default_provider = (
                None
                if model_hub_overlay is not None
                else getattr(opencode_cfg, "default_provider", None)
            )
            model_dict = resolve_opencode_model_dict(model_str, default_provider)
            display_model_dict = resolve_opencode_model_dict(
                requested_model_str,
                default_provider,
            )

            reasoning_effort = override_reasoning
            if not reasoning_effort:
                reasoning_effort = server.get_agent_reasoning_effort_from_config(agent_to_use)
            if not reasoning_effort:
                reasoning_effort = getattr(opencode_cfg, "default_reasoning_effort", None)
            if model_dict:
                try:
                    model_catalog = (
                        opencode_model_catalog_for_overlay(model_hub_overlay)
                        if model_hub_overlay is not None
                        else await server.get_available_models(request.working_path)
                    )
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
            baseline_messages = None
            try:
                baseline_messages = await server.list_messages(
                    session_id=session_id,
                    directory=request.working_path,
                )
            except Exception as err:
                logger.debug(f"Failed to snapshot OpenCode messages before prompt: {err}")

            if baseline_messages is not None:
                if requires_message_order_repair(baseline_messages):
                    session_id = await self._session_manager.repair_message_order(
                        request,
                        server,
                        session_id,
                        baseline_messages,
                    )
                    self._session_manager.set_request_session(
                        request.base_session_id,
                        session_id,
                        request.working_path,
                        request.session_key,
                    )
                    self._session_manager.mark_initialized(session_id)
                    baseline_messages = await server.list_messages(
                        session_id=session_id,
                        directory=request.working_path,
                    )
                for message in baseline_messages:
                    message_id = message.get("info", {}).get("id")
                    if message_id:
                        baseline_message_ids.add(message_id)

            # Prepare message with file attachment info if present
            prompt_text = self._prepare_message_with_files(request)
            platform = (
                request.context.platform
                or (request.context.platform_specific or {}).get("platform")
                or self.controller.config.platform
            )

            # Resolve admission once: it associates or clears this turn's Memory
            # CLI session scope as a side effect, so a second call per turn would
            # repeat that write.
            configure_memory_cli_access(self.controller, request.context)
            caller_context_env = caller_env_for_platform_payload(
                request.context.platform_specific or {},
                message=request.context,
                fallback_platform=platform,
            )

            project_base = managed_skill_project_base(request.context)
            managed_skills_env = managed_skill_environment(
                request.working_path,
                project_base=project_base,
                claude_cli_path=managed_skill_claude_cli_path(
                    getattr(getattr(self, "controller", None), "config", None)
                ),
            )
            binding_token = secrets.token_hex(16)
            binding_payload = request.context.platform_specific or {}
            binding_bound = False
            try:
                binding_bound = await asyncio.to_thread(
                    bind_caller_context_session,
                    session_id,
                    binding_payload,
                    base_env=os.environ,
                    working_dir=request.working_path,
                    extra_env=managed_skills_env,
                    binding_token=binding_token,
                    **_binding_path_kwargs(caller_context_binding_path),
                    # The creation origin travels with the identity: an OpenCode shell
                    # command running ``vibe task add`` sources this binding, and it is
                    # the only place the conversation behind the definition is visible.
                    message=request.context,
                    fallback_platform=platform,
                )
            except Exception:
                logger.warning(
                    "Starting OpenCode turn without caller context for session %s",
                    session_id,
                    exc_info=True,
                )
            caller_context_binding_session_id = session_id
            caller_context_binding_token = binding_token
            caller_context_binding_renewal = asyncio.create_task(
                self._maintain_caller_context_binding(
                    session_id,
                    binding_token,
                    caller_context_binding_path,
                    payload=binding_payload,
                    working_directory=request.working_path,
                    extra_env=managed_skills_env,
                    initially_bound=bool(binding_bound),
                    message=request.context,
                    fallback_platform=platform,
                )
            )

            system_prompt_injection = await asyncio.to_thread(
                build_system_prompt_injection,
                include_quick_replies=getattr(self.controller.config, "reply_enhancements", True)
                and platform != "wechat",
                include_show_pages=getattr(self.controller.config, "show_pages_prompt", True),
                memory_enabled=bool(
                    getattr(getattr(self.controller.config, "memory", None), "enabled", False)
                ),
                context=request.context,
                fallback_platform=platform,
                enabled_agents=get_enabled_agents_for_prompt(self.controller),
                skills_cwd=request.working_path if binding_bound else None,
                skills_project_base=project_base,
                skills_claude_cli_path=managed_skill_claude_cli_path(
                    getattr(getattr(self, "controller", None), "config", None)
                ),
            )
            if request.vibe_agent_system_prompt:
                system_prompt_injection = f"{request.vibe_agent_system_prompt}\n\n{system_prompt_injection}"

            raw_settings_key = _raw_settings_key_from_session_key(request.session_key)
            platform_payload = request.context.platform_specific or {}
            logical_turn_id = str(platform_payload.get("turn_token") or "").strip()
            start_attempt_id = str(
                platform_payload.get("delivery_start_attempt_id") or ""
            ).strip()
            processing_indicator = self.controller.processing_indicator.snapshot_request(
                request
            )
            if caller_context_env:
                processing_indicator[_CALLER_CONTEXT_ENV_SNAPSHOT_KEY] = caller_context_env
            if project_base:
                processing_indicator[_MANAGED_SKILL_PROJECT_BASE_SNAPSHOT_KEY] = project_base
            if BUILTIN_SKILLS_SNAPSHOT_ENV in managed_skills_env:
                processing_indicator[_MANAGED_SKILL_BUILTIN_SNAPSHOT_KEY] = {
                    "id": managed_skills_env[BUILTIN_SKILLS_SNAPSHOT_ENV],
                    "root": managed_skills_env[BUILTIN_SKILLS_ROOT_ENV],
                }
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
            if display_model_dict is not None:
                processing_indicator[_MODEL_HUB_DISPLAY_MODEL_KEY] = display_model_dict

            if model_hub_overlay_reservation is None:
                await server.mark_run_active(session_id)
            else:
                await server.mark_run_active(
                    session_id,
                    overlay_reservation=model_hub_overlay_reservation,
                )
                model_hub_overlay_reservation = None
            run_registered = True
            # Persist the complete recovery address before the first native write.
            # A crash after OpenCode accepts the exact attempt part can now rebuild
            # the poll and Turn owner even if no post-prompt Python statement ran.
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
            active_poll_persisted = True
            mark_backend_dispatch_attempted(request.context)
            native_start_phase = "may_have_written"
            await server.prompt_async(
                session_id=session_id,
                directory=request.working_path,
                text=prompt_text,
                attempt_id=start_attempt_id or None,
                agent=agent_to_use,
                model=model_dict,
                reasoning_effort=reasoning_effort,
                system=system_prompt_injection,
                tools={"question": False, "skill": False},
            )
            try:
                read_prompt_started_at = getattr(server, "get_last_prompt_started_at", None)
                prompt_started_at = (
                    read_prompt_started_at(session_id)
                    if callable(read_prompt_started_at)
                    else None
                ) or time.time()
                update_active_poll = getattr(
                    self.sessions,
                    "update_active_poll_state",
                    None,
                )
                if callable(update_active_poll):
                    update_active_poll(
                        session_id,
                        prompt_started_at=prompt_started_at,
                    )
            except Exception:
                # The pre-write active-poll record remains a safe recovery fallback:
                # its started_at is only moments older than native acceptance. Do not
                # fail a live accepted Turn because this timestamp refinement failed.
                logger.warning(
                    "Failed to persist OpenCode prompt start time for %s",
                    session_id,
                    exc_info=True,
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
                awaiting_user_text=prompt_text,
                awaiting_prompt_accepted=True,
                awaiting_prompt_activity_deadline=(
                    time.monotonic()
                    + _ASYNC_PROMPT_ACCEPTED_ACTIVITY_TIMEOUT_SECONDS
                ),
                idle_reconciliation_message=self._idle_reconciliation_message(
                    display_model_dict,
                    reasoning_effort,
                ),
            )
            self._steering_states[request.base_session_id] = steer_state
            self.mark_runtime_turn_started(
                request.context,
                activation_identity=activation_identity,
            )
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
                remove_active_poll()
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
            remove_active_poll()

        except asyncio.CancelledError:
            logger.info(f"OpenCode request cancelled for {request.base_session_id}")
            # A /stop is silent by design, so the ⏹️ replacing the 👀 is the only
            # thing that tells the user this turn ended on their command. Every
            # other cancellation (shutdown, clear_sessions, supersede) leaves the
            # plain removal, because none of those are the user's doing.
            await self._remove_ack_reaction(
                request,
                terminal_emoji=(
                    STOPPED_REACTION_EMOJI
                    if self._claim_user_stop_receipt(request)
                    else None
                ),
            )
            if session_id:
                remove_active_poll()
            raise
        except OpenCodePromptRejectedError as e:
            error_text = f"{type(e).__name__}: {e}"
            logger.error("OpenCode prompt was definitively rejected: %s", e)

            poll_can_be_removed = not (logical_turn_id and start_attempt_id)
            if logical_turn_id and start_attempt_id:
                session_turns = getattr(self.controller, "session_turns", None)
                try:
                    if e.is_permanent_input_rejection:
                        settle_invalid = getattr(
                            session_turns,
                            "settle_start_attempt_invalid_input",
                            None,
                        )
                        if callable(settle_invalid):
                            poll_can_be_removed = bool(
                                settle_invalid(
                                    logical_turn_id,
                                    start_attempt_id,
                                    backend=self.name,
                                )
                            )
                    else:
                        reconcile = getattr(
                            session_turns,
                            "reconcile_start_attempt_not_written",
                            None,
                        )
                        if callable(reconcile):
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
                remove_active_poll()

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
                remove_active_poll()
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
            await self._stop_caller_context_binding_renewal(
                caller_context_binding_renewal
            )
            if steer_state is not None:
                async with steer_state.lock:
                    steer_state.closing = True
                if self._steering_states.get(request.base_session_id) is steer_state:
                    self._steering_states.pop(request.base_session_id, None)
            if active_poll_removal_pending and run_registered:
                active_poll_persisted = not await self._retire_active_poll(
                    server,
                    session_id,
                )
            elif active_poll_removal_pending and session_id:
                self.sessions.remove_active_poll(session_id)
                active_poll_persisted = False
            if (
                caller_context_binding_session_id
                and caller_context_binding_token
                and not active_poll_persisted
            ):
                try:
                    await asyncio.to_thread(
                        unbind_caller_context_session,
                        caller_context_binding_session_id,
                        binding_token=caller_context_binding_token,
                        **_binding_path_kwargs(caller_context_binding_path),
                    )
                except Exception:
                    logger.warning(
                        "Failed to unbind OpenCode caller context for session %s",
                        caller_context_binding_session_id,
                        exc_info=True,
                    )
            await self._release_model_hub_overlay_reservation(
                server,
                model_hub_overlay_reservation,
            )

    @staticmethod
    async def _release_model_hub_overlay_reservation(
        server: Any,
        reservation: object | None,
    ) -> None:
        if server is None or reservation is None:
            return
        release = getattr(server, "release_model_hub_overlay_reservation", None)
        if not callable(release):
            return
        try:
            await release(reservation)
        except Exception:
            logger.warning(
                "Failed to release OpenCode Model Hub overlay reservation",
                exc_info=True,
            )

    async def _retire_active_poll(
        self,
        server: Any,
        session_id: str,
    ) -> bool:
        try:
            await server.mark_run_inactive(session_id)
        except Exception:
            logger.exception(
                "Failed to clear OpenCode run marker for session=%s; preserving its active poll",
                session_id,
            )
            return False
        self.sessions.remove_active_poll(session_id)
        return True

    async def _finish_prestart_cancellation(
        self,
        request: AgentRequest,
        server: Any,
        reservation: object | None,
    ) -> None:
        await self._release_model_hub_overlay_reservation(server, reservation)
        await self._remove_ack_reaction(
            request,
            terminal_emoji=(
                STOPPED_REACTION_EMOJI
                if self._claim_user_stop_receipt(request)
                else None
            ),
        )

    def _claim_user_stop_receipt(self, request: AgentRequest) -> bool:
        """Recognize adapter-local and shared prewrite Stop ownership."""

        return self.consume_user_stop_intent(
            request.base_session_id
        ) or prewrite_user_stop_requested(request.context)

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
                        "tools": {"question": False, "skill": False},
                    }
                    if request.attempt_id:
                        prompt_kwargs["attempt_id"] = request.attempt_id
                    await server.prompt_async(
                        **prompt_kwargs,
                    )
                except aiohttp.ClientConnectorError:
                    raise
                except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientConnectionError):
                    state.awaiting_after_message_ids = before_insert
                    state.awaiting_user_text = request.text
                    state.awaiting_prompt_accepted = False
                    state.awaiting_prompt_activity_deadline = None
                    state.awaiting_active_status_observed = False
                    raise
                state.awaiting_after_message_ids = before_insert
                state.awaiting_user_text = request.text
                state.awaiting_prompt_accepted = True
                state.awaiting_prompt_activity_deadline = (
                    time.monotonic()
                    + _ASYNC_PROMPT_ACCEPTED_ACTIVITY_TIMEOUT_SECONDS
                )
                state.awaiting_active_status_observed = False
                await asyncio.sleep(_STEER_POST_WRITE_STATUS_SETTLE_SECONDS)
                try:
                    status_after_write = await server.get_session_status(
                        native_session_id,
                        directory,
                    )
                except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientConnectionError) as exc:
                    # The native write may have been accepted, so leave the
                    # reconciliation evidence armed for the poll owner.
                    return steer_result(
                        SteerOutcome.UNKNOWN,
                        reason="post_write_status_unknown",
                        backend=self.name,
                        diagnostic=str(exc),
                    )
                except Exception as exc:  # noqa: BLE001 - write already happened
                    return steer_result(
                        SteerOutcome.UNKNOWN,
                        reason="post_write_status_unknown",
                        backend=self.name,
                        diagnostic=str(exc),
                    )
                if (
                    status_after_write is None
                    or status_after_write.get("type") not in {"busy", "retry"}
                ):
                    # The prompt write is durable, but OpenCode can take longer
                    # than this first status sample to register the continuation as
                    # busy. Keep the five-second reconciliation evidence armed so
                    # the poll owner can observe either start confirmation or the
                    # exact native attempt before any retry is admitted.
                    return steer_result(
                        SteerOutcome.UNKNOWN,
                        reason="native_turn_start_pending",
                        backend=self.name,
                    )
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
        """Resolve one prior OpenCode write by its exact native part identity."""

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
        native_part_id = native_part_id_for_attempt(request.attempt_id)
        try:
            messages = await server.list_messages(
                native_session_id,
                directory,
            )
        except Exception as exc:  # noqa: BLE001 - absence is not negative proof
            return steer_result(
                SteerOutcome.UNKNOWN,
                reason="attempt_evidence_unavailable",
                backend=self.name,
                diagnostic=str(exc),
            )
        for message in messages:
            info = message.get("info") if isinstance(message, dict) else None
            parts = message.get("parts") if isinstance(message, dict) else None
            if (
                isinstance(info, dict)
                and info.get("role") == "user"
                and isinstance(parts, list)
                and any(
                    isinstance(part, dict)
                    and str(part.get("id") or "") == native_part_id
                    for part in parts
                )
            ):
                return steer_result(
                    SteerOutcome.ACCEPTED,
                    reason="native_attempt_part_found",
                    backend=self.name,
                    native_message_id=str(info.get("id") or ""),
                    native_part_id=native_part_id,
                )
        return steer_result(
            SteerOutcome.UNKNOWN,
            reason="untrusted_attempt_evidence",
            backend=self.name,
        )

    def consume_user_stop_intent(self, base_session_id: str) -> bool:
        """Let the cancelled request coroutine claim its /stop receipt once."""

        if not base_session_id or base_session_id not in self._user_stopped_sessions:
            return False
        self._user_stopped_sessions.discard(base_session_id)
        return True

    async def handle_stop(self, request: AgentRequest) -> bool:
        lock = self._session_manager.get_session_lock(request.base_session_id)
        async with lock:
            # Read the task only after claiming the same ownership boundary used
            # to start and retire requests. A result that completed while Stop
            # waited for the lock remains authoritative.
            task = self._active_requests.get(request.base_session_id)
            if not task or task.done():
                request.stop_failure_reason = "not_active"
                return False

            req_info = self._session_manager.get_request_session(request.base_session_id)
            opencode_session_id = None
            if req_info:
                opencode_session_id = req_info[0]
            # Claimed BEFORE the abort: the request coroutine is what owns the 👀,
            # and its cancellation handler is the only place that trades it for the
            # ⏹️ receipt. The helper waits for any already-started steering write,
            # then cancels under that same lock before awaiting the native abort.
            self._user_stopped_sessions.add(request.base_session_id)
            cancellation_claimed = False
            try:
                try:
                    cancellation_claimed = await self._abort_active_request(
                        request.base_session_id,
                        task,
                        req_info,
                        cancel_before_abort=True,
                    )
                except Exception as e:
                    logger.warning(f"Failed to abort OpenCode session: {e}")
                    cancellation_claimed = task.cancelling() > 0 or task.cancelled()

                if not cancellation_claimed:
                    # The runner completed while Stop was waiting for a steering
                    # write. Its result/error is authoritative; do not append a
                    # stopped receipt or a silent cancellation settlement.
                    logger.info(
                        "OpenCode session %s completed before /stop claimed it",
                        request.base_session_id,
                    )
                    return True

                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                # Only reached with the intent still set if something above raised
                # past the handlers; a claimed intent is already gone.
                self._user_stopped_sessions.discard(request.base_session_id)

            if opencode_session_id and self._active_poll_is_persisted(
                opencode_session_id
            ):
                server = await self._get_server()
                await self._retire_active_poll(server, opencode_session_id)

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
                if self._active_poll_is_persisted(opencode_session_id):
                    server = await self._get_server()
                    await self._retire_active_poll(server, opencode_session_id)
        return terminated

    async def _abort_active_request(
        self,
        base_session_id: str,
        task: asyncio.Task,
        request_session: tuple[str, str, str] | None,
        *,
        cancel_before_abort: bool = False,
    ) -> bool:
        state = self._steering_states.get(base_session_id)
        if state is not None and state.task is task:
            async with state.lock:
                state.closing = True
                if cancel_before_abort:
                    if task.done():
                        return False
                    task.cancel()
                if request_session:
                    server = await self._get_server()
                    await server.abort_session(request_session[0], request_session[1])
            return True
        if cancel_before_abort:
            if task.done():
                return False
            task.cancel()
        if request_session:
            server = await self._get_server()
            await server.abort_session(request_session[0], request_session[1])
        return True

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
            start_attempt_part_id = (
                native_part_id_for_attempt(start_attempt_id)
                if start_attempt_id
                else ""
            )
            start_attempt_found = any(
                start_attempt_part_id
                and message.get("info", {}).get("role") == "user"
                and any(
                    isinstance(part, dict)
                    and str(part.get("id") or "") == start_attempt_part_id
                    for part in (message.get("parts") or [])
                )
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
                try:
                    await server.mark_run_inactive(poll_info.opencode_session_id)
                except Exception:
                    logger.exception(
                        "Failed to clear OpenCode run marker for session=%s; "
                        "preserving its active poll and continuing restoration",
                        poll_info.opencode_session_id,
                    )
                    continue
                await self._poll_loop.remove_restored_ack(poll_info)
                stale_poll_ids.append(session_id)
                continue

            logger.info(
                f"Restoring poll loop for OpenCode session {session_id} "
                f"(thread={poll_info.base_session_id}, cwd={poll_info.working_path})"
            )

            restored_binding_token = secrets.token_hex(16)
            restored_binding_path = _caller_context_path_for_server(server)
            restored_caller_env = validated_caller_env_snapshot(
                processing_snapshot.get(_CALLER_CONTEXT_ENV_SNAPSHOT_KEY)
            )
            restored_context = restored_context_from_poll_info(poll_info)
            if (
                poll_platform == "avibe"
                and str(restored_context.user_id or "").startswith("remote:")
            ):
                # Legacy persisted polls do not carry the authorization snapshot.
                # Keep them remote-but-unprivileged instead of silently treating an
                # absent snapshot as a local caller.
                restored_caller_env.setdefault(AVIBE_SESSION_ID_ENV, poll_info.base_session_id)
                restored_caller_env.setdefault(AVIBE_CALLER_PLATFORM_ENV, "avibe")
                restored_caller_env.setdefault(
                    AVIBE_CALLER_USER_ID_ENV,
                    str(restored_context.user_id),
                )
                restored_caller_env.setdefault(AVIBE_CALLER_REMOTE_ENV, "1")
            restored_project_base = processing_snapshot.get(
                _MANAGED_SKILL_PROJECT_BASE_SNAPSHOT_KEY
            )
            restored_project_base = (
                restored_project_base
                if isinstance(restored_project_base, str) and restored_project_base
                else None
            )
            restored_snapshot_kwargs: dict[str, str] = {}
            if _MANAGED_SKILL_BUILTIN_SNAPSHOT_KEY in processing_snapshot:
                restored_snapshot = processing_snapshot.get(
                    _MANAGED_SKILL_BUILTIN_SNAPSHOT_KEY
                )
                restored_snapshot_kwargs = {
                    "builtin_snapshot_id": (
                        restored_snapshot.get("id", "")
                        if isinstance(restored_snapshot, dict)
                        and isinstance(restored_snapshot.get("id"), str)
                        else ""
                    ),
                    "builtin_snapshot_root": (
                        restored_snapshot.get("root", "")
                        if isinstance(restored_snapshot, dict)
                        and isinstance(restored_snapshot.get("root"), str)
                        else ""
                    ),
                }
            restored_managed_skills_env = managed_skill_environment(
                poll_info.working_path,
                project_base=restored_project_base,
                claude_cli_path=managed_skill_claude_cli_path(
                    getattr(getattr(self, "controller", None), "config", None)
                ),
                **restored_snapshot_kwargs,
            )
            restored_bound = False
            for attempt in range(3):
                try:
                    restored_bound = await asyncio.to_thread(
                        bind_caller_context_session,
                        poll_info.opencode_session_id,
                        None,
                        base_env=os.environ,
                        working_dir=poll_info.working_path,
                        extra_env={
                            **restored_caller_env,
                            **restored_managed_skills_env,
                        },
                        binding_token=restored_binding_token,
                        **_binding_path_kwargs(restored_binding_path),
                    )
                except Exception:
                    logger.warning(
                        "Failed to restore OpenCode caller context for session=%s (attempt %s/3)",
                        poll_info.opencode_session_id,
                        attempt + 1,
                        exc_info=True,
                    )
                if restored_bound:
                    break
                if attempt < 2:
                    await asyncio.sleep(0)
            if not restored_bound:
                logger.error(
                    "Restoring OpenCode poll without caller context for session=%s",
                    poll_info.opencode_session_id,
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
                    caller_context_binding_token=(
                        restored_binding_token
                    ),
                    caller_context_binding_path=restored_binding_path,
                    caller_context_binding_extra_env={
                        **restored_caller_env,
                        **restored_managed_skills_env,
                    },
                    caller_context_binding_initially_bound=restored_bound,
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
            set_agent_session_id = getattr(
                self._session_manager,
                "set_agent_session_id",
                None,
            )
            if callable(set_agent_session_id):
                set_agent_session_id(
                    poll_info.base_session_id,
                    self._workbench_session_id_for_poll(poll_info),
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
        caller_context_binding_token: str | None = None,
        caller_context_binding_path: str | None = None,
        caller_context_binding_extra_env: dict[str, str] | None = None,
        caller_context_binding_initially_bound: bool = False,
    ) -> None:
        current_task = asyncio.current_task()
        steer_state = None
        server = None
        restoration_registered = False
        terminal_poll_cleanup = False
        caller_context_binding_renewal = (
            asyncio.create_task(
                self._maintain_caller_context_binding(
                    poll_info.opencode_session_id,
                    caller_context_binding_token,
                    caller_context_binding_path,
                    payload=None,
                    working_directory=poll_info.working_path,
                    extra_env=caller_context_binding_extra_env or {},
                    initially_bound=caller_context_binding_initially_bound,
                )
            )
            if caller_context_binding_token
            else None
        )
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
            display_model_dict = (
                poll_info.processing_indicator.get(_MODEL_HUB_DISPLAY_MODEL_KEY)
                if isinstance(poll_info.processing_indicator, dict)
                else None
            )
            if not isinstance(display_model_dict, dict):
                display_model_dict = poll_info.model_dict
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
                        display_model_dict,
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
            terminal_poll_cleanup = bool(
                await self._poll_loop.run_restored_poll_loop(poll_info)
            )
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
                    terminal_poll_cleanup = True
                logger.error(
                    "OpenCode poll registration failed for session=%s: %s",
                    poll_info.opencode_session_id,
                    err,
                )
        finally:
            await self._stop_caller_context_binding_renewal(
                caller_context_binding_renewal
            )
            if current_task is not None:
                self._restored_poll_servers.pop(current_task, None)
            if steer_state is not None:
                async with steer_state.lock:
                    steer_state.closing = True
                if self._steering_states.get(poll_info.base_session_id) is steer_state:
                    self._steering_states.pop(poll_info.base_session_id, None)
            if terminal_poll_cleanup and server is not None:
                await self._retire_active_poll(
                    server,
                    poll_info.opencode_session_id,
                )
            if caller_context_binding_token and not self._active_poll_is_persisted(
                poll_info.opencode_session_id
            ):
                try:
                    await asyncio.to_thread(
                        unbind_caller_context_session,
                        poll_info.opencode_session_id,
                        binding_token=caller_context_binding_token,
                        **_binding_path_kwargs(caller_context_binding_path),
                    )
                except Exception:
                    logger.warning(
                        "Failed to clear restored OpenCode caller context for session %s",
                        poll_info.opencode_session_id,
                        exc_info=True,
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
