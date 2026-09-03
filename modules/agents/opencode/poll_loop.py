"""Unified polling loop for OpenCode sessions."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional, Union

from config.v2_config import (
    DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS,
    DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
)
from core.backend_failure import emit_backend_failure
from core.message_context import build_context_session_key
from core.message_output import terminal_output_for, terminal_turn_output
from core.processing_indicator import STOPPED_REACTION_EMOJI
from modules.agents.base import AgentRequest
from modules.agents.model_hub import bind_persisted_launch
from modules.im import MessageContext
from vibe.i18n import t as i18n_t

from .message_processor import is_empty_terminal_opencode_message
from .server import OpenCodeServerManager

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0
_TIMEOUT_ABORT_GRACE_SECONDS = 10.0
# The optional wall-clock deadline must not be the only bound on a dead runtime:
# with the cap disabled, a persistent transport outage (daemon down, non-200
# responses) would otherwise retry forever and leave the accepted turn
# unsettled with no message able to carry an error. Consecutive failures —
# never total duration — drive this settlement, so an intermittent blip that
# recovers between polls resets the count and never trips it.
_POLL_FAILURE_SETTLE_LIMIT = 10
# OpenCode can report idle after accepting ``continue`` / a steer, before the
# replacement assistant is visible. Keep that inject pending for this window.
_POST_INJECT_CONFIRMATION_SECONDS = 5.0


def _opencode_error_text(error: object) -> str:
    if not isinstance(error, dict):
        return str(error)
    error_name = error.get("name", "UnknownError")
    error_data = error.get("data", {})
    error_message = error_data.get("message", "") if isinstance(error_data, dict) else str(error_data)
    return f"{error_name} - {error_message[:500]}".strip(" -")


def _message_info(message: Dict[str, Any]) -> Dict[str, Any]:
    info = message.get("info")
    return info if isinstance(info, dict) else {}


def _native_session_is_live(
    status: Optional[Dict[str, Any]],
    *,
    status_known: bool = True,
) -> bool:
    """True when OpenCode still owns the turn (busy/retry).

    A successful ``/session/status`` that omits this session is idle
    (``get_session_status`` returns ``None``). An unread or failed status
    (``status_known=False``) is treated as live so an accepted steer is not
    closed during the window where the user message exists and the assistant
    does not.
    """

    if not status_known:
        return True
    if not isinstance(status, dict):
        return False
    return status.get("type") in {"busy", "retry"}


def _has_post_boundary_assistant(
    messages: list[Dict[str, Any]],
    boundary_ids: set[str],
) -> bool:
    return any(
        (info := _message_info(message)).get("id")
        and info.get("id") not in boundary_ids
        and info.get("role") == "assistant"
        for message in messages
    )


def _snapshot_needs_native_liveness(
    messages: list[Dict[str, Any]],
    baseline_message_ids: set[str],
    awaiting_after_ids: Optional[set[str]] = None,
) -> bool:
    """True only when idle vs busy can change which completed assistant settles."""

    if awaiting_after_ids and not _has_post_boundary_assistant(
        messages, awaiting_after_ids
    ):
        return False
    latest_new: Optional[Dict[str, Any]] = None
    for message in reversed(messages):
        info = _message_info(message)
        message_id = info.get("id")
        if not message_id or message_id in baseline_message_ids:
            continue
        latest_new = message
        break
    if latest_new is None:
        return False
    info = _message_info(latest_new)
    if info.get("role") == "assistant" and not info.get("time", {}).get("completed"):
        return False
    return info.get("role") == "user"


def _settlement_assistant_message(
    messages: list[Dict[str, Any]],
    baseline_message_ids: set[str],
    *,
    native_live: bool,
    awaiting_after_ids: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """The assistant message that owns this poll's settlement.

    Trailing user injects are not the turn. While the native session is live,
    or an awaiting inject boundary has no post-boundary assistant yet, keep
    the previous completed assistant pending. When idle and the inject has
    produced no new assistant, settle the completed owner behind it.
    """

    if awaiting_after_ids and not _has_post_boundary_assistant(
        messages, awaiting_after_ids
    ):
        return None

    skipped_user = False
    for message in reversed(messages):
        info = _message_info(message)
        message_id = info.get("id")
        if not message_id or message_id in baseline_message_ids:
            continue
        if info.get("role") != "assistant":
            if info.get("role") == "user":
                skipped_user = True
            continue
        if not info.get("time", {}).get("completed"):
            return None
        if skipped_user and native_live:
            return None
        return message
    return None


def restored_platform_from_poll_info(poll_info) -> str:
    snapshot = poll_info.processing_indicator if isinstance(poll_info.processing_indicator, dict) else {}
    platform = str(snapshot.get("platform") or poll_info.platform or "")
    if platform:
        return platform
    session_key = str(getattr(poll_info, "session_key", "") or "").strip()
    return session_key.split("::", 1)[0] if "::" in session_key else ""


def restored_context_from_poll_info(poll_info) -> MessageContext:
    snapshot = poll_info.processing_indicator if isinstance(poll_info.processing_indicator, dict) else {}
    platform = restored_platform_from_poll_info(poll_info)
    user_id = str(snapshot.get("user_id") or poll_info.user_id or "")
    channel_id = str(snapshot.get("channel_id") or poll_info.channel_id or "")
    context_token = str(snapshot.get("context_token") or getattr(poll_info, "context_token", "") or "")
    platform_specific: dict[str, Any] = {}
    if platform:
        platform_specific["platform"] = platform
    if snapshot.get("is_dm") is not None:
        platform_specific["is_dm"] = bool(snapshot.get("is_dm"))
    elif platform in {"telegram", "wechat"} and user_id and user_id == channel_id:
        platform_specific["is_dm"] = True
    if context_token:
        platform_specific["context_token"] = context_token
    return MessageContext(
        user_id=user_id,
        channel_id=channel_id,
        platform=platform or None,
        thread_id=snapshot.get("thread_id") or poll_info.thread_id or None,
        message_id=snapshot.get("message_id") or None,
        platform_specific=platform_specific or None,
    )


def restored_session_key_from_poll_info(poll_info, *, context: Optional[MessageContext] = None) -> str:
    session_key = str(getattr(poll_info, "session_key", "") or "").strip()
    if session_key:
        return session_key
    restored_context = context or restored_context_from_poll_info(poll_info)
    return build_context_session_key(
        restored_context,
        platform=poll_info.platform or restored_context.platform,
        settings_key=poll_info.settings_key,
    )


def restored_request_from_poll_info(agent, poll_info) -> AgentRequest:
    """Rebuild the request that owns a restored poll's indicator lifecycle."""

    snapshot = poll_info.processing_indicator or {
        "platform": poll_info.platform,
        "user_id": poll_info.user_id,
        "channel_id": poll_info.channel_id,
        "thread_id": poll_info.thread_id,
        "context_token": getattr(poll_info, "context_token", ""),
        "ack_reaction_message_id": poll_info.ack_reaction_message_id,
        "ack_reaction_emoji": poll_info.ack_reaction_emoji,
        "typing_indicator_active": bool(getattr(poll_info, "typing_indicator_active", False)),
    }
    handle = agent.controller.processing_indicator.handle_from_snapshot(snapshot)
    context = handle.context
    return AgentRequest(
        context=context,
        message="",
        user_message="",
        working_path=poll_info.working_path,
        base_session_id=poll_info.base_session_id,
        composite_session_id=f"{poll_info.base_session_id}:{poll_info.working_path}",
        session_key=restored_session_key_from_poll_info(poll_info, context=context),
        processing_indicator=handle,
        ack_message_id=handle.ack_message_id,
        ack_reaction_message_id=handle.ack_reaction_message_id,
        ack_reaction_emoji=handle.ack_reaction_emoji,
        terminal_reaction_message_id=handle.terminal_reaction_message_id,
        typing_indicator_active=handle.typing_indicator_active,
    )


class OpenCodePollLoop:
    def __init__(self, agent):
        self._agent = agent

    def _t(self, key: str, **kwargs) -> str:
        controller = getattr(self._agent, "controller", None)
        translate = getattr(controller, "_t", None)
        if callable(translate):
            return str(translate(key, **kwargs))
        config = getattr(controller, "config", None)
        lang = getattr(config, "language", "en")
        return str(i18n_t(key, lang, **kwargs))

    async def _record_model_hub_failure(self, context: MessageContext, diagnostic: str) -> None:
        record_failure = getattr(self._agent, "record_model_hub_native_failure", None)
        if callable(record_failure):
            await record_failure(context, diagnostic)

    def _active_turn_timeout_seconds(self) -> float:
        """The configured wall-clock bound, or ``0.0`` when it is disabled.

        Non-positive, missing, or non-finite values all read as disabled. The
        upstream runtime bounds its own retries and surfaces the exhausted
        error on the message, which the poll loop's error path settles, so the
        wall-clock cap is an explicit opt-in — an unset or invalid value must
        not silently re-enable one.
        """

        raw_timeout = getattr(
            self._agent.opencode_config,
            "active_turn_timeout_seconds",
            DEFAULT_OPENCODE_ACTIVE_TURN_TIMEOUT_SECONDS,
        )
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(timeout) or timeout <= 0:
            return 0.0
        return timeout

    @staticmethod
    def _deadline_from_persisted_start(timeout_seconds: float, started_at: object) -> float:
        if timeout_seconds <= 0:
            return math.inf
        try:
            wall_started_at = float(started_at)
        except (TypeError, ValueError):
            wall_started_at = 0.0
        elapsed = max(0.0, time.time() - wall_started_at) if wall_started_at > 0 else 0.0
        return time.monotonic() + max(0.0, timeout_seconds - elapsed)

    @staticmethod
    def _wait_timeout(remaining: float) -> Union[float, None]:
        """Map an infinite remaining budget to ``wait_for``'s no-timeout form."""

        return None if math.isinf(remaining) else remaining

    async def _native_session_is_live(
        self,
        server: OpenCodeServerManager,
        session_id: str,
        directory: str,
        *,
        remaining: float,
        pending_inject_until: float = 0.0,
    ) -> bool:
        if pending_inject_until and time.monotonic() < pending_inject_until:
            return True
        snapshot_live = getattr(server, "last_list_native_live", None)
        if isinstance(snapshot_live, bool):
            return snapshot_live
        reader = getattr(server, "get_session_status", None)
        if not callable(reader):
            return True
        try:
            status = await asyncio.wait_for(
                reader(session_id, directory),
                timeout=self._wait_timeout(remaining),
            )
        except Exception as err:
            logger.debug(
                "OpenCode session status unavailable for %s: %s",
                session_id,
                err,
            )
            return True
        return _native_session_is_live(status)

    @staticmethod
    async def _sleep_with_deadline(deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    async def _settle_active_turn_timeout(
        self,
        *,
        request: AgentRequest,
        server: OpenCodeServerManager,
        session_id: str,
        working_path: str,
        timeout_seconds: float,
    ) -> None:
        seconds = f"{timeout_seconds:g}"
        diagnostic = (
            "OpenCode active turn exceeded the configured "
            f"{seconds}-second wall-clock limit"
        )
        logger.warning(
            "OpenCode active turn timed out: session=%s limit_seconds=%s; aborting native turn",
            session_id,
            seconds,
        )
        try:
            await asyncio.wait_for(
                server.abort_session(session_id, working_path),
                timeout=_TIMEOUT_ABORT_GRACE_SECONDS,
            )
        except Exception as abort_err:
            logger.error(
                "Failed to abort timed-out OpenCode session %s within %.0fs: %s",
                session_id,
                _TIMEOUT_ABORT_GRACE_SECONDS,
                abort_err,
            )
        await self._record_model_hub_failure(request.context, diagnostic)
        await emit_backend_failure(
            self._agent.controller,
            request.context,
            "opencode",
            diagnostic,
            display_text=self._t(
                "error.opencodeActiveTurnTimeout",
                seconds=seconds,
            ),
            request=request,
        )

    async def _settle_poll_transport_failure(
        self,
        *,
        request: AgentRequest,
        server: OpenCodeServerManager,
        session_id: str,
        working_path: str,
        failures: int,
    ) -> None:
        diagnostic = (
            f"OpenCode poll failed {failures} consecutive times; "
            "runtime is unreachable"
        )
        logger.warning(
            "OpenCode poll transport failures settled: session=%s failures=%s; aborting native turn",
            session_id,
            failures,
        )
        try:
            await asyncio.wait_for(
                server.abort_session(session_id, working_path),
                timeout=_TIMEOUT_ABORT_GRACE_SECONDS,
            )
        except Exception as abort_err:
            logger.error(
                "Failed to abort unreachable OpenCode session %s within %.0fs: %s",
                session_id,
                _TIMEOUT_ABORT_GRACE_SECONDS,
                abort_err,
            )
        await self._record_model_hub_failure(request.context, diagnostic)
        await emit_backend_failure(
            self._agent.controller,
            request.context,
            "opencode",
            diagnostic,
            display_text=self._t(
                "error.opencodePollTransportFailure",
                count=failures,
            ),
            request=request,
        )

    def _build_restored_handle(self, poll_info):
        return restored_request_from_poll_info(self._agent, poll_info).processing_indicator

    def _build_restored_context(self, poll_info):
        return self._build_restored_handle(poll_info).context

    def _build_restored_ack_request(self, poll_info) -> AgentRequest:
        return restored_request_from_poll_info(self._agent, poll_info)

    async def remove_restored_ack(self, poll_info) -> None:
        await self._agent._remove_ack_reaction(self._build_restored_ack_request(poll_info))

    def _fallback_extract_text(
        self,
        messages: list[Dict[str, Any]],
        baseline_message_ids: set[str],
        last_message_id: Optional[str] = None,
        emitted_message_ids: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Walk backward through messages to find response text.

        When the last completed message has no text parts (e.g. it only
        contains tool calls or step markers), search earlier messages for the
        actual assistant response text. Messages in *emitted_message_ids* are
        skipped so text already sent to the user is not re-sent as the final
        result.
        """
        skip_ids: set[str] = set()
        if last_message_id:
            skip_ids.add(last_message_id)
        if emitted_message_ids:
            skip_ids.update(emitted_message_ids)

        for message in reversed(messages):
            info = message.get("info", {})
            msg_id = info.get("id")
            if not msg_id or msg_id in baseline_message_ids:
                continue
            if info.get("role") != "assistant":
                continue
            if msg_id in skip_ids:
                continue
            text = self._agent._extract_response_text(message)
            if text:
                logger.info(
                    "Fallback: found response text in message %s instead of last message %s",
                    msg_id,
                    last_message_id,
                )
                return text
        return None

    async def run_prompt_poll(
        self,
        request: AgentRequest,
        server: OpenCodeServerManager,
        session_id: str,
        *,
        agent_to_use: Optional[str],
        model_dict: Optional[Dict[str, str]],
        reasoning_effort: Optional[str],
        baseline_message_ids: set[str],
    ) -> tuple[Optional[str], bool]:
        """Poll messages for a prompt.

        Returns:
            (final_text, should_emit_final_result)

        If `should_emit_final_result` is False, the caller should exit without
        emitting a final result message.
        """

        seen_tool_calls: set[str] = set()
        emitted_assistant_messages: set[str] = set()
        final_text: Optional[str] = None
        timeout_seconds = self._active_turn_timeout_seconds()
        deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds > 0 else math.inf
        )

        error_retry_count = 0
        error_retry_limit = getattr(
            self._agent.opencode_config,
            "error_retry_limit",
            DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
        )
        last_error_message_id: Optional[str] = None
        poll_failures = 0
        pending_inject_until = 0.0

        def _relative_path(path: str) -> str:
            return self._agent._to_relative_path(path, request.working_path)

        poll_iter = 0
        while True:
            poll_iter += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self._settle_active_turn_timeout(
                    request=request,
                    server=server,
                    session_id=session_id,
                    working_path=request.working_path,
                    timeout_seconds=timeout_seconds,
                )
                return None, False
            try:
                messages = await asyncio.wait_for(
                    server.list_messages(
                        session_id=session_id,
                        directory=request.working_path,
                    ),
                    timeout=self._wait_timeout(remaining),
                )
                poll_failures = 0
                if poll_iter % 5 == 0:
                    last_info = messages[-1].get("info", {}) if messages else {}
                    logger.info(
                        "OpenCode poll heartbeat %s iter=%s last=%s role=%s completed=%s finish=%s error=%s",
                        session_id,
                        poll_iter,
                        last_info.get("id"),
                        last_info.get("role"),
                        bool(last_info.get("time", {}).get("completed")),
                        last_info.get("finish"),
                        bool(last_info.get("error")),
                    )
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    await self._settle_active_turn_timeout(
                        request=request,
                        server=server,
                        session_id=session_id,
                        working_path=request.working_path,
                        timeout_seconds=timeout_seconds,
                    )
                    return None, False
                poll_failures += 1
                if poll_failures >= _POLL_FAILURE_SETTLE_LIMIT:
                    await self._settle_poll_transport_failure(
                        request=request,
                        server=server,
                        session_id=session_id,
                        working_path=request.working_path,
                        failures=poll_failures,
                    )
                    return None, False
                logger.warning("Timed out polling OpenCode messages before the active-turn deadline")
                await self._sleep_with_deadline(deadline)
                continue
            except Exception as poll_err:
                poll_failures += 1
                if poll_failures >= _POLL_FAILURE_SETTLE_LIMIT:
                    await self._settle_poll_transport_failure(
                        request=request,
                        server=server,
                        session_id=session_id,
                        working_path=request.working_path,
                        failures=poll_failures,
                    )
                    return None, False
                logger.warning(f"Failed to poll OpenCode messages: {poll_err}")
                await self._sleep_with_deadline(deadline)
                continue

            for message in messages:
                info = message.get("info", {})
                message_id = info.get("id")
                if not message_id or message_id in baseline_message_ids:
                    continue
                if info.get("role") != "assistant":
                    continue

                for part in message.get("parts", []) or []:
                    if part.get("type") != "tool":
                        continue
                    call_key = part.get("callID") or part.get("id")
                    if not call_key or call_key in seen_tool_calls:
                        continue
                    tool_name = part.get("tool") or "tool"
                    tool_state = part.get("state") or {}
                    tool_input = tool_state.get("input") or {}

                    if tool_name == "question" and tool_state.get("status") != "completed":
                        message = self._t("error.opencodeQuestionToolDisabled")
                        logger.warning("Aborting OpenCode session %s after disabled question tool call", session_id)
                        # Terminal abort → error RESULT so the outbound chokepoint
                        # turns the dot red (not a bare notify that never settles it).
                        await self._agent.controller.emit_agent_message(
                            request.context,
                            "result",
                            message,
                            is_error=True,
                            output=terminal_output_for(request),
                        )
                        try:
                            await server.abort_session(session_id, request.working_path)
                        except Exception as abort_err:
                            logger.warning("Failed to abort disabled question session %s: %s", session_id, abort_err)
                        return None, False

                    toolcall = self._agent._get_formatter(request.context).format_toolcall(
                        tool_name,
                        tool_input,
                        get_relative_path=_relative_path,
                    )
                    await self._agent.controller.emit_agent_message(
                        request.context,
                        "toolcall",
                        toolcall,
                        parse_mode="markdown",
                    )
                    seen_tool_calls.add(call_key)

                if (
                    info.get("time", {}).get("completed")
                    and message_id not in emitted_assistant_messages
                    and info.get("finish") == "tool-calls"
                ):
                    text = self._agent._extract_response_text(message)
                    if text:
                        await self._agent.controller.emit_agent_message(
                            request.context,
                            "assistant",
                            text,
                            parse_mode="markdown",
                        )
                    emitted_assistant_messages.add(message_id)

            if messages:
                remaining = deadline - time.monotonic()
                awaiting_after_ids = getattr(
                    getattr(server, "_state", None),
                    "awaiting_after_message_ids",
                    None,
                )
                native_live = False
                if _snapshot_needs_native_liveness(
                    messages, baseline_message_ids, awaiting_after_ids
                ):
                    native_live = await self._native_session_is_live(
                        server,
                        session_id,
                        request.working_path,
                        remaining=remaining,
                        pending_inject_until=pending_inject_until,
                    )
                last_message = _settlement_assistant_message(
                    messages,
                    baseline_message_ids,
                    native_live=native_live,
                    awaiting_after_ids=awaiting_after_ids,
                )
                last_info = _message_info(last_message) if last_message else {}
                last_id = last_info.get("id")

                if (
                    last_message is not None
                    and last_id
                    and last_info.get("time", {}).get("completed")
                ):
                    msg_error = last_info.get("error")
                    if msg_error and last_id != last_error_message_id:
                        last_error_message_id = last_id
                        diagnostic = _opencode_error_text(msg_error)

                        logger.warning(
                            "OpenCode message error detected for %s: %s (retry %d/%d)",
                            session_id,
                            diagnostic[:200],
                            error_retry_count,
                            error_retry_limit,
                        )

                        if error_retry_count < error_retry_limit:
                            error_retry_count += 1
                            logger.info(
                                "Auto-retrying OpenCode session %s with 'continue' (attempt %d/%d)",
                                session_id,
                                error_retry_count,
                                error_retry_limit,
                            )

                            try:
                                await server.prompt_async(
                                    session_id=session_id,
                                    directory=request.working_path,
                                    text="continue",
                                    agent=agent_to_use,
                                    model=model_dict,
                                    reasoning_effort=reasoning_effort,
                                    tools={"question": False, "skill": False},
                                    awaiting_after_ids={
                                        str(_message_info(item).get("id"))
                                        for item in messages
                                        if _message_info(item).get("id")
                                    },
                                )
                                pending_inject_until = (
                                    time.monotonic() + _POST_INJECT_CONFIRMATION_SECONDS
                                )
                                await self._sleep_with_deadline(deadline)
                                continue
                            except Exception as retry_err:
                                logger.error(
                                    "Failed to send retry 'continue' for %s: %s",
                                    session_id,
                                    retry_err,
                                )

                        await self._record_model_hub_failure(request.context, diagnostic)
                        message = self._t("error.opencodeBackendError", error=diagnostic)
                        await emit_backend_failure(
                            self._agent.controller,
                            request.context,
                            "opencode",
                            diagnostic,
                            display_text=message,
                            request=request,
                            failure_id=str(last_id or ""),
                        )
                        # Terminal: stop polling AND signal the caller NOT to emit the
                        # "(No response from OpenCode)" warning result — that warning is
                        # idle and would reset the dot we (or the auth-recovery path)
                        # just settled to failed. Mirrors the question-tool abort's
                        # ``return None, False`` rather than ``break`` (→ should_emit
                        # True → the idle warning) (Codex P2).
                        return None, False

                    if last_info.get("finish") != "tool-calls":
                        if not msg_error:
                            error_retry_count = 0
                        final_text = self._agent._extract_response_text(last_message)
                        if not final_text and not msg_error:
                            logger.warning(
                                "Last message %s has no text parts (finish=%s); "
                                "searching earlier messages for response text",
                                last_id,
                                last_info.get("finish"),
                            )
                            final_text = self._fallback_extract_text(
                                messages,
                                baseline_message_ids,
                                last_message_id=last_id,
                                emitted_message_ids=emitted_assistant_messages,
                            )
                        if not final_text and not msg_error and is_empty_terminal_opencode_message(last_message):
                            logger.warning(
                                "OpenCode session %s completed without text/error (provider=%s model=%s variant=%s)",
                                session_id,
                                (model_dict or {}).get("providerID"),
                                (model_dict or {}).get("modelID"),
                                reasoning_effort,
                            )
                            break
                        break

            await self._sleep_with_deadline(deadline)

        return final_text, True

    async def run_restored_poll_loop(self, poll_info) -> bool:
        """Continue a poll loop that was interrupted by restart."""

        session_id = poll_info.opencode_session_id
        restored_request = self._build_restored_ack_request(poll_info)
        context = restored_request.context
        processing_snapshot = (
            poll_info.processing_indicator if isinstance(poll_info.processing_indicator, dict) else {}
        )
        bind_persisted_launch(context, processing_snapshot.get("model_hub_launch"))

        await self._agent.controller.emit_agent_message(
            context,
            "notify",
            "Resuming interrupted OpenCode session after restart...",
        )

        server = await self._agent._get_server()
        baseline_message_ids = set(poll_info.baseline_message_ids)
        seen_tool_calls = set(poll_info.seen_tool_calls)
        emitted_assistant_messages = set(poll_info.emitted_assistant_messages)
        final_text: Optional[str] = None
        timeout_seconds = self._active_turn_timeout_seconds()
        persisted_started_at = poll_info.prompt_started_at or poll_info.started_at
        deadline = self._deadline_from_persisted_start(
            timeout_seconds,
            persisted_started_at,
        )

        error_retry_count = 0
        error_retry_limit = getattr(
            self._agent.opencode_config,
            "error_retry_limit",
            DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
        )
        last_error_message_id: Optional[str] = None
        pending_inject_until = 0.0

        started_at = time.monotonic()

        def _relative_path(path: str) -> str:
            return self._agent._to_relative_path(path, poll_info.working_path)

        try:
            poll_iter = 0
            poll_failures = 0
            while True:
                poll_iter += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._settle_active_turn_timeout(
                        request=restored_request,
                        server=server,
                        session_id=session_id,
                        working_path=poll_info.working_path,
                        timeout_seconds=timeout_seconds,
                    )
                    await self.remove_restored_ack(poll_info)
                    return True
                try:
                    messages = await asyncio.wait_for(
                        server.list_messages(
                            session_id=session_id,
                            directory=poll_info.working_path,
                        ),
                        timeout=self._wait_timeout(remaining),
                    )
                    poll_failures = 0
                    if poll_iter % 5 == 0:
                        last_info = messages[-1].get("info", {}) if messages else {}
                        logger.info(
                            "OpenCode restored poll heartbeat %s iter=%s last=%s role=%s completed=%s finish=%s error=%s",
                            session_id,
                            poll_iter,
                            last_info.get("id"),
                            last_info.get("role"),
                            bool(last_info.get("time", {}).get("completed")),
                            last_info.get("finish"),
                            bool(last_info.get("error")),
                        )
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        await self._settle_active_turn_timeout(
                            request=restored_request,
                            server=server,
                            session_id=session_id,
                            working_path=poll_info.working_path,
                            timeout_seconds=timeout_seconds,
                        )
                        await self.remove_restored_ack(poll_info)
                        return True
                    poll_failures += 1
                    if poll_failures >= _POLL_FAILURE_SETTLE_LIMIT:
                        await self._settle_poll_transport_failure(
                            request=restored_request,
                            server=server,
                            session_id=session_id,
                            working_path=poll_info.working_path,
                            failures=poll_failures,
                        )
                        await self.remove_restored_ack(poll_info)
                        return True
                    logger.warning(
                        "Timed out polling restored OpenCode messages before the active-turn deadline"
                    )
                    await self._sleep_with_deadline(deadline)
                    continue
                except Exception as poll_err:
                    poll_failures += 1
                    if poll_failures >= _POLL_FAILURE_SETTLE_LIMIT:
                        await self._settle_poll_transport_failure(
                            request=restored_request,
                            server=server,
                            session_id=session_id,
                            working_path=poll_info.working_path,
                            failures=poll_failures,
                        )
                        await self.remove_restored_ack(poll_info)
                        return True
                    logger.warning(f"Failed to poll OpenCode messages (restored): {poll_err}")
                    await self._sleep_with_deadline(deadline)
                    continue

                for message in messages:
                    info = message.get("info", {})
                    message_id = info.get("id")
                    if not message_id or message_id in baseline_message_ids:
                        continue
                    if info.get("role") != "assistant":
                        continue

                    for part in message.get("parts", []) or []:
                        if part.get("type") != "tool":
                            continue
                        call_key = part.get("callID") or part.get("id")
                        if not call_key or call_key in seen_tool_calls:
                            continue
                        tool_name = part.get("tool") or "tool"
                        tool_state = part.get("state") or {}
                        tool_input = tool_state.get("input") or {}

                        if tool_name == "question" and tool_state.get("status") != "completed":
                            message = self._t("error.opencodeQuestionToolDisabledRestored")
                            logger.warning(
                                "Aborting restored OpenCode session %s after disabled question tool call",
                                session_id,
                            )
                            # Terminal abort → error RESULT (settles the dot red).
                            await self._agent.controller.emit_agent_message(
                                context,
                                "result",
                                message,
                                is_error=True,
                                output=terminal_turn_output(),
                            )
                            try:
                                await server.abort_session(session_id, poll_info.working_path)
                            except Exception as abort_err:
                                logger.warning("Failed to abort disabled question session %s: %s", session_id, abort_err)
                            await self.remove_restored_ack(poll_info)
                            return True

                        seen_tool_calls.add(call_key)

                        poll_info.seen_tool_calls = list(seen_tool_calls)
                        self._agent.sessions.update_active_poll_state(
                            session_id, seen_tool_calls=poll_info.seen_tool_calls
                        )

                        if tool_name in (
                            "read",
                            "write",
                            "edit",
                            "bash",
                            "glob",
                            "grep",
                        ):
                            tool_summary = f"`{tool_name}`"
                            if tool_name == "bash":
                                cmd = tool_input.get("command", "")
                                if cmd:
                                    cmd_preview = cmd[:50] + "..." if len(cmd) > 50 else cmd
                                    tool_summary = f"`bash`: `{cmd_preview}`"
                            elif tool_name in ("read", "write", "edit"):
                                path = tool_input.get("file_path") or tool_input.get("path", "")
                                if path:
                                    tool_summary = f"`{tool_name}`: `{_relative_path(path)}`"

                            await self._agent.controller.emit_agent_message(context, "tool_call", tool_summary)

                if messages:
                    remaining = deadline - time.monotonic()
                    awaiting_after_ids = getattr(
                        getattr(server, "_state", None),
                        "awaiting_after_message_ids",
                        None,
                    )
                    native_live = False
                    if _snapshot_needs_native_liveness(
                        messages, baseline_message_ids, awaiting_after_ids
                    ):
                        native_live = await self._native_session_is_live(
                            server,
                            session_id,
                            poll_info.working_path,
                            remaining=remaining,
                            pending_inject_until=pending_inject_until,
                        )
                    last_message = _settlement_assistant_message(
                        messages,
                        baseline_message_ids,
                        native_live=native_live,
                        awaiting_after_ids=awaiting_after_ids,
                    )
                    last_info = _message_info(last_message) if last_message else {}
                    if last_info.get("id"):
                        time_info = last_info.get("time") or {}
                        if time_info.get("completed"):
                            msg_error = last_info.get("error")
                            if msg_error:
                                error_text = _opencode_error_text(msg_error)
                                if last_info.get("id") != last_error_message_id:
                                    error_retry_count = 0
                                    last_error_message_id = last_info.get("id")
                                error_retry_count += 1
                                if error_retry_count > error_retry_limit:
                                    await self._record_model_hub_failure(context, error_text)
                                    message = self._t("error.opencodeBackendError", error=error_text)
                                    await emit_backend_failure(
                                        self._agent.controller,
                                        context,
                                        "opencode",
                                        error_text,
                                        display_text=message,
                                        request=restored_request,
                                        failure_id=str(last_info.get("id") or ""),
                                    )
                                    await self.remove_restored_ack(poll_info)
                                    return True
                                await self._sleep_with_deadline(deadline)
                                continue

                            if last_info.get("finish") != "tool-calls":
                                if not msg_error:
                                    error_retry_count = 0
                                final_text = self._agent._extract_response_text(last_message)
                                if not final_text and not msg_error:
                                    logger.warning(
                                        "Restored poll: last message %s has no text parts (finish=%s); "
                                        "searching earlier messages for response text",
                                        last_info.get("id"),
                                        last_info.get("finish"),
                                    )
                                    final_text = self._fallback_extract_text(
                                        messages,
                                        baseline_message_ids,
                                        last_message_id=last_info.get("id"),
                                        emitted_message_ids=emitted_assistant_messages,
                                    )
                                if not final_text and not msg_error and is_empty_terminal_opencode_message(last_message):
                                    logger.warning(
                                        "Restored OpenCode session %s completed without text/error",
                                        session_id,
                                    )
                                    break
                                break

                await self._sleep_with_deadline(deadline)

            if final_text:
                await self._agent.emit_result_message(
                    context,
                    final_text,
                    subtype="success",
                    started_at=started_at,
                    parse_mode="markdown",
                )
            else:
                await self._agent.emit_result_message(
                    context,
                    "(No response from OpenCode)",
                    subtype="warning",
                    started_at=started_at,
                )

            # Clean up ack reaction after result is sent
            await self.remove_restored_ack(poll_info)
            return True

        except asyncio.CancelledError:
            logger.info(f"Restored OpenCode poll cancelled for {poll_info.base_session_id}")
            stopped_by_user = self._agent.consume_user_stop_intent(poll_info.base_session_id)
            await self._agent._remove_ack_reaction(
                restored_request,
                terminal_emoji=STOPPED_REACTION_EMOJI if stopped_by_user else None,
            )
            raise
        except Exception as e:
            error_name = type(e).__name__
            error_details = str(e).strip()
            error_text = f"{error_name}: {error_details}" if error_details else error_name

            logger.error(f"Restored OpenCode poll failed: {error_text}", exc_info=True)
            await self._record_model_hub_failure(context, error_text)
            try:
                await server.abort_session(session_id, poll_info.working_path)
            except Exception as abort_err:
                logger.warning(f"Failed to abort OpenCode session after error: {abort_err}")

            await self.remove_restored_ack(poll_info)

            message = f"Restored OpenCode session failed: {error_text}"
            await emit_backend_failure(
                self._agent.controller,
                context,
                "opencode",
                error_text,
                display_text=message,
                request=restored_request,
                failure_id=session_id,
            )
            return True
