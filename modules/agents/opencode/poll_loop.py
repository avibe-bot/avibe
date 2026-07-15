"""Unified polling loop for OpenCode sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from config.v2_config import DEFAULT_OPENCODE_ERROR_RETRY_LIMIT
from core.backend_failure import emit_backend_failure
from core.message_context import build_context_session_key
from core.message_output import terminal_output_for, terminal_turn_output
from modules.agents.base import AgentRequest
from modules.im import MessageContext
from vibe.i18n import t as i18n_t

from .message_processor import is_empty_terminal_opencode_message
from .server import OpenCodeServerManager

logger = logging.getLogger(__name__)


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

    def _build_restored_handle(self, poll_info):
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
        return self._agent.controller.processing_indicator.handle_from_snapshot(snapshot)

    def _build_restored_context(self, poll_info):
        return self._build_restored_handle(poll_info).context

    def _build_restored_ack_request(self, poll_info) -> AgentRequest:
        handle = self._build_restored_handle(poll_info)
        context = handle.context
        session_key = restored_session_key_from_poll_info(poll_info, context=context)
        return AgentRequest(
            context=context,
            message="",
            working_path=poll_info.working_path,
            base_session_id=poll_info.base_session_id,
            composite_session_id=f"{poll_info.base_session_id}:{poll_info.working_path}",
            session_key=session_key,
            processing_indicator=handle,
            ack_message_id=handle.ack_message_id,
            ack_reaction_message_id=handle.ack_reaction_message_id,
            ack_reaction_emoji=handle.ack_reaction_emoji,
            typing_indicator_active=handle.typing_indicator_active,
        )

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
        poll_interval_seconds = 2.0
        final_text: Optional[str] = None

        error_retry_count = 0
        error_retry_limit = getattr(
            self._agent.opencode_config,
            "error_retry_limit",
            DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
        )
        last_error_message_id: Optional[str] = None

        def _relative_path(path: str) -> str:
            return self._agent._to_relative_path(path, request.working_path)

        poll_iter = 0
        while True:
            poll_iter += 1
            try:
                messages = await server.list_messages(
                    session_id=session_id,
                    directory=request.working_path,
                )
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
            except Exception as poll_err:
                logger.warning(f"Failed to poll OpenCode messages: {poll_err}")
                await asyncio.sleep(poll_interval_seconds)
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
                last_message = messages[-1]
                last_info = last_message.get("info", {})
                last_id = last_info.get("id")

                if (
                    last_id
                    and last_id not in baseline_message_ids
                    and last_info.get("role") == "assistant"
                    and last_info.get("time", {}).get("completed")
                ):
                    msg_error = last_info.get("error")
                    if msg_error and last_id != last_error_message_id:
                        last_error_message_id = last_id
                        error_name = msg_error.get("name", "UnknownError")
                        error_data = msg_error.get("data", {})
                        error_msg = error_data.get("message", "") if isinstance(error_data, dict) else str(error_data)

                        logger.warning(
                            "OpenCode message error detected for %s: %s - %s (retry %d/%d)",
                            session_id,
                            error_name,
                            error_msg[:200],
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
                                    tools={"question": False},
                                )
                                await asyncio.sleep(poll_interval_seconds)
                                continue
                            except Exception as retry_err:
                                logger.error(
                                    "Failed to send retry 'continue' for %s: %s",
                                    session_id,
                                    retry_err,
                                )

                        diagnostic = f"{error_name} - {error_msg[:500]}".strip(" -")
                        message = f"OpenCode error: {diagnostic}"
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

            await asyncio.sleep(poll_interval_seconds)

        return final_text, True

    async def run_restored_poll_loop(self, poll_info) -> None:
        """Continue a poll loop that was interrupted by restart."""

        session_id = poll_info.opencode_session_id
        restored_request = self._build_restored_ack_request(poll_info)
        context = restored_request.context

        await self._agent.controller.emit_agent_message(
            context,
            "notify",
            "Resuming interrupted OpenCode session after restart...",
        )

        server = await self._agent._get_server()
        baseline_message_ids = set(poll_info.baseline_message_ids)
        seen_tool_calls = set(poll_info.seen_tool_calls)
        emitted_assistant_messages = set(poll_info.emitted_assistant_messages)
        poll_interval_seconds = 2.0
        final_text: Optional[str] = None

        error_retry_count = 0
        error_retry_limit = getattr(
            self._agent.opencode_config,
            "error_retry_limit",
            DEFAULT_OPENCODE_ERROR_RETRY_LIMIT,
        )
        last_error_message_id: Optional[str] = None

        started_at = time.monotonic()

        def _relative_path(path: str) -> str:
            return self._agent._to_relative_path(path, poll_info.working_path)

        try:
            poll_iter = 0
            while True:
                poll_iter += 1
                try:
                    messages = await server.list_messages(
                        session_id=session_id,
                        directory=poll_info.working_path,
                    )
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
                except Exception as poll_err:
                    logger.warning(f"Failed to poll OpenCode messages (restored): {poll_err}")
                    await asyncio.sleep(poll_interval_seconds)
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
                            self._agent.sessions.remove_active_poll(session_id)
                            await self.remove_restored_ack(poll_info)
                            return

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
                    last_message = messages[-1]
                    last_info = last_message.get("info", {})
                    if last_info.get("role") == "assistant":
                        time_info = last_info.get("time") or {}
                        if time_info.get("completed"):
                            msg_error = last_info.get("error")
                            if msg_error:
                                error_text = str(msg_error)
                                if last_info.get("id") != last_error_message_id:
                                    error_retry_count = 0
                                    last_error_message_id = last_info.get("id")
                                error_retry_count += 1
                                if error_retry_count > error_retry_limit:
                                    message = f"OpenCode error: {error_text}"
                                    await emit_backend_failure(
                                        self._agent.controller,
                                        context,
                                        "opencode",
                                        error_text,
                                        display_text=message,
                                        request=restored_request,
                                        failure_id=str(last_info.get("id") or ""),
                                    )
                                    self._agent.sessions.remove_active_poll(session_id)
                                    await self.remove_restored_ack(poll_info)
                                    return

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

                await asyncio.sleep(poll_interval_seconds)

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
            self._agent.sessions.remove_active_poll(session_id)

        except asyncio.CancelledError:
            logger.info(f"Restored OpenCode poll cancelled for {poll_info.base_session_id}")
            await self.remove_restored_ack(poll_info)
            self._agent.sessions.remove_active_poll(session_id)
            raise
        except Exception as e:
            error_name = type(e).__name__
            error_details = str(e).strip()
            error_text = f"{error_name}: {error_details}" if error_details else error_name

            logger.error(f"Restored OpenCode poll failed: {error_text}", exc_info=True)
            try:
                await server.abort_session(session_id, poll_info.working_path)
            except Exception as abort_err:
                logger.warning(f"Failed to abort OpenCode session after error: {abort_err}")

            self._agent.sessions.remove_active_poll(session_id)
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
