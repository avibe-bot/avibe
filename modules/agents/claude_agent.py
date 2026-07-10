import asyncio
import logging
import os
from typing import Callable, Optional

from core.agent_auth_service import classify_auth_error
from modules.claude_sdk_compat import TextBlock, ToolUseBlock, is_claude_sdk_buffer_error
from modules.agents.claude_process_reaper import (
    AVIBE_CLAUDE_SESSION_OWNER,
    register_claude_owned_process,
)

from modules.agents.base import (
    AGENT_RUNTIME_TURN_KEY,
    AGENT_RUNTIME_TURN_TOKEN,
    AGENT_TURN_TOKEN,
    AgentRequest,
    BaseAgent,
)

# NOTE: AskUserQuestion support is disabled because Claude Code SDK cannot
# respond to it programmatically. See: https://github.com/anthropics/claude-code/issues/10168
# Keeping the import for future use when SDK adds support.
# from modules.agents.claude_question_handler import ClaudeQuestionHandler
from modules.im import MessageContext

logger = logging.getLogger(__name__)


class ClaudeAgent(BaseAgent):
    """Existing Claude Code integration extracted into an agent backend."""

    name = "claude"

    # AskUserQuestion support is disabled - SDK cannot respond programmatically
    # Set to True when SDK adds support (see issue #10168)
    ENABLE_ASK_USER_QUESTION = False

    def __init__(self, controller):
        super().__init__(controller)
        self.session_handler = controller.session_handler
        self.session_manager = controller.session_manager
        self.receiver_tasks = controller.receiver_tasks
        self.claude_sessions = controller.claude_sessions
        self.claude_client = controller.claude_client
        self._last_assistant_text: dict[str, str] = {}
        self._pending_assistant_message: dict[str, str] = {}
        self._native_session_ids: dict[str, str] = {}
        self._suppressed_synthetic_results: set[str] = set()
        self._suppress_receiver_runtime_release: set[str] = set()
        # Store reaction info per runtime session for cleanup after terminal
        # result. Under the runtime turn gate there is normally one active entry;
        # the list shape remains for defensive cleanup of older queued state.
        self._pending_reactions: dict[str, list[tuple[str, str]]] = {}
        self._pending_requests: dict[str, list[AgentRequest]] = {}

        # Question handler for AskUserQuestion support (disabled)
        # NOTE: Uncomment when SDK adds AskUserQuestion support
        # self._question_handler = ClaudeQuestionHandler(
        #     agent=self,
        #     controller=controller,
        #     im_client=controller.im_client,
        #     settings_manager=controller.settings_manager,
        # )
        self._question_handler = None

    def _format_error_notify(self, error: Exception) -> str:
        """Return the durable notify text for Claude terminal errors."""
        if is_claude_sdk_buffer_error(error):
            translator = getattr(self.session_handler, "_t", None) or getattr(self.controller, "_t", None)
            if callable(translator):
                try:
                    return f"❌ {translator('error.sessionConnectionLost')}"
                except Exception:
                    logger.debug("claude: failed to translate buffer-error notify", exc_info=True)
            return "❌ Connection to Claude was lost. Please try your message again."
        return f"❌ Claude error: {error}"

    async def handle_message(self, request: AgentRequest) -> None:
        context = request.context
        runtime_base_session_id = request.base_session_id
        runtime_session_key = request.composite_session_id
        turn_registered = False

        # Question callback handling (disabled - SDK doesn't support AskUserQuestion response)
        # if self.ENABLE_ASK_USER_QUESTION and request.message.startswith("claude_question:"):
        #     await self._handle_question_callback(request)
        #     return

        try:
            client = await self.session_handler.get_or_create_claude_session(
                context,
                subagent_name=request.subagent_name,
                subagent_model=request.subagent_model or getattr(request, "vibe_agent_model", None),
                subagent_reasoning_effort=(
                    request.subagent_reasoning_effort
                    or getattr(request, "vibe_agent_reasoning_effort", None)
                ),
                agent_system_prompt=getattr(request, "vibe_agent_system_prompt", None),
            )
            runtime_base_session_id = getattr(client, "_vibe_runtime_base_session_id", runtime_base_session_id)
            runtime_session_key = getattr(client, "_vibe_runtime_session_key", runtime_session_key)
            mark_session_active = getattr(self.session_handler, "mark_session_active", None)
            if callable(mark_session_active):
                mark_session_active(runtime_session_key)

            # Queue reaction BEFORE sending query to avoid race condition where
            # a fast result arrives before the reaction is queued
            if request.ack_reaction_message_id and request.ack_reaction_emoji:
                if runtime_session_key not in self._pending_reactions:
                    self._pending_reactions[runtime_session_key] = []
                self._pending_reactions[runtime_session_key].append(
                    (request.ack_reaction_message_id, request.ack_reaction_emoji)
                )
            self._pending_requests.setdefault(runtime_session_key, []).append(request)

            # Prepare message with file attachment info if present
            message = self._prepare_message_with_files(request)

            await client.query(message, session_id=runtime_session_key)
            self.mark_runtime_turn_started(context)
            if (
                runtime_session_key not in self.receiver_tasks
                or self.receiver_tasks[runtime_session_key].done()
            ):
                self.receiver_tasks[runtime_session_key] = asyncio.create_task(
                    self._receive_messages(
                        client,
                        runtime_base_session_id,
                        request.working_path,
                        context,
                        composite_key=runtime_session_key,
                    )
                )
            turn_registered = True
            logger.info(f"Sent message to Claude for session {runtime_session_key}")

            await self._delete_ack(context, request)
        except asyncio.CancelledError:
            if not turn_registered:
                await self._remove_specific_pending_reaction(runtime_session_key, context, request)
                self._remove_pending_request(runtime_session_key, request)
                self._mark_session_idle_if_no_pending_requests(runtime_session_key)
                await self._delete_ack(context, request)
                self._release_service_runtime_turn(context)
            raise
        except Exception as e:
            logger.error(f"Error processing Claude message: {e}", exc_info=True)
            # Clean up the specific reaction for this request (not FIFO)
            await self._remove_specific_pending_reaction(runtime_session_key, context, request)
            self._remove_pending_request(runtime_session_key, request)
            self._mark_session_idle_if_no_pending_requests(runtime_session_key)
            await self._remove_ack_reaction(request)
            error_notify = self._format_error_notify(e)
            handled = await self.controller.agent_auth_service.maybe_emit_auth_recovery_message(
                context,
                "claude",
                error_notify,
            )
            if not handled:
                await self.session_handler.handle_session_error(runtime_session_key, context, e)
                # ``handle_session_error`` sends through the IM client, which doesn't
                # write to ``messages``, and the web Chat renders only durable
                # ``message.new`` rows — so persist a terminal notify or the avibe
                # user's prompt stops with no explanation (Codex P2). The HANDLED
                # (auth) branch instead persists the full recovery text centrally in
                # ``maybe_emit_auth_recovery_message``.
                try:
                    from core.message_mirror import persist_agent_message

                    persist_agent_message(context, "notify", error_notify)
                except Exception:
                    logger.debug("claude: failed to persist terminal error row", exc_info=True)
            # Synchronous failure (query/setup raised), no async receiver result
            # coming: settle the turn through the OUTBOUND status chokepoint. An
            # empty terminal error result turns the dot red AND releases the
            # web-Chat stream waiter (the visible error was sent + persisted
            # above), instead of waiting out the safety timeout. No-op off-workbench.
            try:
                await self.controller.emit_agent_message(context, "result", "", is_error=True)
            finally:
                self._release_service_runtime_turn(context)
        finally:
            await self._delete_ack(context, request)

    def _release_service_runtime_turn(self, context: MessageContext) -> None:
        service = getattr(self.controller, "agent_service", None)
        release = getattr(service, "release_runtime_turn", None)
        if callable(release):
            release(context)

    def backend_alive(self, context) -> Optional[bool]:
        """Liveness via the receiver task for this turn's runtime session.

        ``receiver_tasks`` is keyed by the Claude runtime session key. When that
        key matches the turn's ``AGENT_RUNTIME_TURN_KEY`` we know the answer; if
        it doesn't (e.g. a client-overridden runtime key) we return ``None`` so
        the caller never false-alarms."""
        payload = getattr(context, "platform_specific", None) or {}
        key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        if not key:
            return None
        task = self.receiver_tasks.get(key)
        if task is None:
            return None
        return not task.done()

    async def _handle_question_callback(self, request: AgentRequest) -> None:
        """Handle question-related callbacks (button clicks, modal submissions).

        NOTE: This method is disabled because Claude Code SDK cannot respond to
        AskUserQuestion programmatically. See: https://github.com/anthropics/claude-code/issues/10168
        """
        # AskUserQuestion support disabled
        await self.controller.emit_agent_message(
            request.context,
            "notify",
            "AskUserQuestion support is currently disabled. Claude Code SDK does not support programmatic responses to this tool.",
        )
        return

    async def clear_sessions(self, session_key: str) -> int:
        """Clear Claude sessions scoped to the provided session key."""
        agent_map = self.sessions.list_agent_sessions(session_key, self.name)
        session_bases_to_clear = set(agent_map.keys())

        self.sessions.clear_agent_sessions(session_key, self.name)

        sessions_to_clear = []
        for composite_id in list(self.claude_sessions.keys()):
            if self._runtime_key_matches_session_base(composite_id, session_bases_to_clear):
                sessions_to_clear.append(composite_id)

        for composite_id in sessions_to_clear:
            await self._cleanup_runtime_session(composite_id)

        # Legacy session manager cleanup (best-effort)
        await self.session_manager.clear_session(session_key)

        return len(sessions_to_clear) or len(session_bases_to_clear)

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        agent_map = self.sessions.list_agent_sessions(session_key, self.name)
        session_bases_to_clear = set(agent_map.keys())
        runtime_keys = set()
        for composite_id in self.runtime_turn_keys():
            if self._runtime_key_matches_session_base(composite_id, session_bases_to_clear):
                runtime_keys.add(composite_id)
        return runtime_keys

    def runtime_turn_keys(self) -> set[str]:
        return set(self.claude_sessions.keys()) | set(self.receiver_tasks.keys())

    @staticmethod
    def _runtime_key_matches_session_base(runtime_key: str, session_bases: set[str]) -> bool:
        for session_base in session_bases:
            if runtime_key == session_base or runtime_key.startswith(f"{session_base}:"):
                return True
        return False

    async def refresh_auth_state(self) -> None:
        """Reconnect Claude runtime so future requests load fresh auth."""
        session_ids = self.runtime_turn_keys()

        for composite_id in session_ids:
            await self._cleanup_runtime_session(composite_id)

        logger.info("Refreshed Claude auth state across %d runtime session(s)", len(session_ids))

    async def refresh_runtime_config(self, claude_config) -> None:
        """Reload persisted runtime config before reconnecting Claude sessions."""
        self.config.claude = claude_config
        self.controller.config.claude = claude_config
        session_handler = getattr(self, "session_handler", None)
        if session_handler is not None:
            session_handler.config = self.controller.config
        await self.refresh_auth_state()

    async def prepare_resume_binding(
        self,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        """Drop only the target Claude runtime session before rebinding it."""
        composite_key = f"{base_session_id}:{working_path}"
        if composite_key not in self.claude_sessions and composite_key not in self.receiver_tasks:
            return

        await self._cleanup_runtime_session(composite_key)
        logger.info("Prepared Claude runtime for resumed session %s", composite_key)

    async def _disconnect_client(self, client, composite_key: str) -> None:
        try:
            if hasattr(client, "disconnect"):
                await client.disconnect()
            elif hasattr(client, "close"):
                await client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error disconnecting Claude session %s: %s", composite_key, exc)

    def _disconnect_client_after_receiver(
        self,
        client,
        composite_key: str,
        receiver_task: asyncio.Task | None,
    ) -> None:
        async def _run() -> None:
            if receiver_task is not None:
                try:
                    await receiver_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Claude receiver ended with error before deferred disconnect: %s", exc)
            await self._disconnect_client(client, composite_key)

        asyncio.create_task(_run())

    @staticmethod
    def _drain_receiver_task_exception(receiver_task: asyncio.Task) -> None:
        try:
            exc = receiver_task.exception()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error reading Claude receiver cleanup result: %s", exc)
            return
        if exc is not None:
            logger.warning("Claude receiver ended with error during cleanup: %s", exc)

    async def _stop_receiver_task(self, receiver_task: asyncio.Task | None) -> None:
        if receiver_task is None:
            return
        if receiver_task.done():
            self._drain_receiver_task_exception(receiver_task)
            return
        receiver_result_retrieved = False
        try:
            await asyncio.wait_for(asyncio.shield(receiver_task), timeout=0.1)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            receiver_result_retrieved = True
            logger.warning("Claude receiver ended with error during cleanup: %s", exc)
        if receiver_task.done():
            if not receiver_result_retrieved:
                self._drain_receiver_task_exception(receiver_task)
            return
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error stopping Claude receiver during cleanup: %s", exc)

    async def _cleanup_runtime_session(
        self,
        composite_key: str,
        *,
        current_receiver_task: asyncio.Task | None = None,
        preserve_pending_request_state: bool = False,
    ) -> None:
        """Drop Claude runtime state without canceling the current receiver task."""

        self._last_assistant_text.pop(composite_key, None)
        self._pending_assistant_message.pop(composite_key, None)
        self._native_session_ids.pop(composite_key, None)
        self._suppressed_synthetic_results.discard(composite_key)
        if not preserve_pending_request_state:
            self._pending_reactions.pop(composite_key, None)
            self._pending_requests.pop(composite_key, None)
        cleanup = getattr(self.session_handler, "cleanup_session", None)
        if callable(cleanup):
            await cleanup(composite_key, current_receiver_task=current_receiver_task)
            return
        receiver_task = self.receiver_tasks.pop(composite_key, None)
        client = self.claude_sessions.pop(composite_key, None)
        cleanup_from_receiver = receiver_task is not None and receiver_task is current_receiver_task
        clear_tracking = getattr(self.session_handler, "clear_session_tracking", None)
        if callable(clear_tracking):
            clear_tracking(composite_key)
        try:
            if client is not None:
                if cleanup_from_receiver:
                    self._disconnect_client_after_receiver(client, composite_key, receiver_task)
                else:
                    await self._disconnect_client(client, composite_key)
        finally:
            if not cleanup_from_receiver:
                await self._stop_receiver_task(receiver_task)

    async def force_cleanup_stuck_active_session(self, composite_key: str) -> None:
        """Settle and drop a Claude session whose active flag is stale.

        SessionHandler owns the idle timer, but ClaudeAgent owns the pending
        request FIFO and Workbench turn tokens. Force cleanup must retire the
        failed turn here before removing the SDK client, otherwise a later
        result can adopt the stale request/token.
        """
        pending_request = self._pop_pending_request(composite_key)
        context = getattr(pending_request, "context", None)
        if context is not None:
            self._adopt_pending_turn_token(context, pending_request)
            await self._remove_result_pending_reaction(composite_key, context, pending_request)
        else:
            self._pending_reactions.pop(composite_key, None)
        self._last_assistant_text.pop(composite_key, None)
        self._pending_assistant_message.pop(composite_key, None)

        self._suppress_receiver_runtime_release.add(composite_key)
        try:
            try:
                await self._cleanup_runtime_session(
                    composite_key,
                    preserve_pending_request_state=True,
                )
            except Exception:
                if context is not None:
                    self._release_service_runtime_turn(context)
                raise
        finally:
            self._suppress_receiver_runtime_release.discard(composite_key)

        if context is not None:
            try:
                await self.controller.emit_agent_message(
                    context,
                    "result",
                    "",
                    is_error=True,
                    level="silent",
                )
            except Exception:
                logger.debug(
                    "Failed to emit terminal result while force-cleaning Claude session %s",
                    composite_key,
                    exc_info=True,
                )
                self._release_service_runtime_turn(context)

    async def handle_stop(self, request: AgentRequest) -> bool:
        composite_key = request.composite_session_id
        if composite_key not in self.claude_sessions:
            request.stop_failure_reason = "not_active"
            return False

        client = self.claude_sessions[composite_key]
        if not hasattr(client, "interrupt"):
            request.stop_failure_reason = "unsupported"
            await self.controller.emit_agent_message(
                request.context,
                "notify",
                "⚠️ This Claude session cannot be interrupted; consider /new.",
            )
            return False

        try:
            await client.interrupt()
        except Exception as err:
            request.stop_failure_reason = "interrupt_failed"
            logger.error(f"Failed to interrupt Claude session {composite_key}: {err}")
            await self.controller.emit_agent_message(
                request.context,
                "notify",
                "⚠️ Failed to interrupt Claude session. Please try /new.",
            )
            return False

        stopped_request = self._pop_pending_request(composite_key)
        self._adopt_pending_turn_token(request.context, stopped_request)
        if stopped_request is not None:
            try:
                await self._remove_specific_pending_reaction(composite_key, request.context, stopped_request)
                await self._remove_ack_reaction(stopped_request)
            except Exception:
                logger.debug("Failed to clear Claude stop processing indicator", exc_info=True)

        self._suppress_receiver_runtime_release.add(composite_key)
        try:
            self._mark_session_idle_if_no_pending_requests(composite_key)
            await self._cleanup_runtime_session(composite_key)
        except Exception as err:
            logger.error("Failed to clean up stopped Claude session %s: %s", composite_key, err, exc_info=True)
            self._release_service_runtime_turn(request.context)
            raise
        finally:
            self._suppress_receiver_runtime_release.discard(composite_key)

        try:
            # A user-initiated stop is terminal but intentional, so it carries
            # NO user-facing message: a single SILENT result settles the dot to
            # idle + releases the SSE waiter through the outbound chokepoint
            # WITHOUT a bubble. Emit only after cleanup so the next turn cannot
            # acquire the gate and reuse a client that this stop is still
            # disconnecting.
            await self.controller.emit_agent_message(request.context, "result", "", level="silent")
        except Exception as err:
            logger.error("Failed to emit Claude stop result for session %s: %s", composite_key, err, exc_info=True)
            self._release_service_runtime_turn(request.context)

        return True

    async def _receive_messages(
        self,
        client,
        base_session_id: str,
        working_path: str,
        context: MessageContext,
        *,
        composite_key: str | None = None,
    ):
        """Receive messages from Claude SDK client."""
        try:
            session_key = self.controller._get_session_key(context)
            composite_key = composite_key or f"{base_session_id}:{working_path}"

            # Build a request object for question handler
            request = AgentRequest(
                context=context,
                message="",
                working_path=working_path,
                base_session_id=base_session_id,
                composite_session_id=composite_key,
                session_key=session_key,
            )

            async for message in client.receive_messages():
                try:
                    touch_session_activity = getattr(self.session_handler, "touch_session_activity", None)
                    if callable(touch_session_activity):
                        touch_session_activity(composite_key)
                    claude_session_id = self._maybe_capture_session_id(
                        message,
                        base_session_id,
                        session_key,
                        context,
                        working_path=working_path,
                    )
                    if claude_session_id:
                        self._native_session_ids[composite_key] = claude_session_id
                        runtime_client = self.claude_sessions.get(composite_key)
                        if runtime_client is client:
                            setattr(runtime_client, "_vibe_native_session_id", claude_session_id)
                            register_claude_owned_process(
                                runtime_client,
                                native_session_id=claude_session_id,
                                owner=AVIBE_CLAUDE_SESSION_OWNER,
                            )
                        logger.info(f"Captured Claude session id {claude_session_id} for {base_session_id}")

                    if self.claude_client._is_skip_message(message):
                        continue

                    message_type = self._detect_message_type(message)
                    formatter = self._get_formatter(context)
                    is_model_refusal_fallback = self._is_model_refusal_fallback_message(message)
                    model_refusal_fallback_notice = (
                        self._parse_model_refusal_fallback_notice(message)
                        if is_model_refusal_fallback
                        else None
                    )

                    # Unsolicited backend output (a background-task completion or
                    # a ScheduleWakeup re-invoked the agent inside this SDK
                    # process) reaches this long-lived receiver with no Avibe turn
                    # open. Open an agent-initiated turn so the reply is persisted
                    # + delivered + notified instead of dropped by the outbound
                    # active-turn guard. An actionable refusal-fallback frame is
                    # the one user-facing system exception: it belongs to the
                    # replacement run and must use that run's turn.
                    if message_type in ("assistant", "result") or model_refusal_fallback_notice is not None:
                        await self._maybe_begin_agent_initiated_turn(
                            context,
                            composite_key,
                            base_session_id,
                            working_path,
                            session_key,
                        )

                    if message_type == "assistant":
                        toolcalls = []
                        text_parts = []
                        # AskUserQuestion detection disabled - SDK cannot respond
                        # ask_user_question_block = None

                        for block in getattr(message, "content", []) or []:
                            if isinstance(block, ToolUseBlock):
                                # AskUserQuestion handling disabled - tool is disallowed via ClaudeAgentOptions
                                # if self.ENABLE_ASK_USER_QUESTION and self._question_handler:
                                #     if self._question_handler.is_ask_user_question(block):
                                #         ask_user_question_block = block
                                #         continue

                                toolcalls.append(
                                    (
                                        formatter.format_toolcall(
                                            block.name,
                                            block.input,
                                            get_relative_path=lambda path: self.get_relative_path(path, context),
                                        ),
                                        formatter.format_toolcall_label(
                                            block.name,
                                            block.input,
                                            get_relative_path=lambda path: self.get_relative_path(path, context),
                                        ),
                                    )
                                )
                            elif isinstance(block, TextBlock):
                                text = block.text.strip() if block.text else ""
                                if text:
                                    text_parts.append(text)

                        # Update the status footer's session token figure to this
                        # request's context-window occupancy. SET (not add): the
                        # latest assistant message reflects current context size,
                        # so the figure tracks it live and drops after a /compact.
                        context_tokens = self._extract_context_tokens(message)
                        if context_tokens:
                            self.controller.note_session_tokens(context, total=context_tokens)

                        auth_failure_assistant = self._is_auth_failure_assistant_message(message)
                        assistant_text = self._extract_text_blocks(message, context)
                        auth_failure_text = assistant_text or "OAuth authentication failed."
                        if await self._handle_auth_failure_result(
                            context,
                            composite_key,
                            "error" if auth_failure_assistant else "",
                            auth_failure_text if auth_failure_assistant else assistant_text,
                        ):
                            mark_session_idle = getattr(self.session_handler, "mark_session_idle", None)
                            if callable(mark_session_idle):
                                mark_session_idle(composite_key)
                            await self._clear_pending_reactions(composite_key, context)
                            self._last_assistant_text.pop(composite_key, None)
                            self._pending_assistant_message.pop(composite_key, None)
                            return
                        if await self._handle_synthetic_api_error_message(
                            context,
                            composite_key,
                            message,
                            assistant_text,
                        ):
                            continue
                        if assistant_text:
                            self._last_assistant_text[composite_key] = assistant_text

                        pending_requests = self._pending_requests.get(composite_key) or []
                        pending_request = pending_requests[0] if pending_requests else None
                        self._adopt_pending_turn_token(context, pending_request)

                        pending = self._pending_assistant_message.pop(composite_key, None)
                        if pending:
                            await self.controller.emit_agent_message(
                                context,
                                "assistant",
                                pending,
                                parse_mode="markdown",
                            )

                        for toolcall_text, toolcall_label in toolcalls:
                            # Persisted/verbose text stays the original
                            # ``format_toolcall`` output; ``status_label`` carries
                            # the clean claude-pipe label for the concise bubble only.
                            await self.controller.emit_agent_message(
                                context,
                                "toolcall",
                                toolcall_text,
                                parse_mode="markdown",
                                status_label=toolcall_label,
                            )

                        if text_parts:
                            formatted_assistant = formatter.format_assistant_message(text_parts)
                            self._pending_assistant_message[composite_key] = formatted_assistant

                        # AskUserQuestion handling disabled - SDK cannot respond programmatically
                        # See: https://github.com/anthropics/claude-code/issues/10168
                        # if self.ENABLE_ASK_USER_QUESTION and ask_user_question_block:
                        #     logger.info(
                        #         "Detected AskUserQuestion for session %s",
                        #         base_session_id,
                        #     )
                        #     answered = await self._question_handler.handle_ask_user_question(
                        #         request=request,
                        #         tool_use_block=ask_user_question_block,
                        #         client=client,
                        #         composite_session_id=composite_key,
                        #     )
                        #     if not answered:
                        #         logger.warning(
                        #             "AskUserQuestion timed out for session %s",
                        #             base_session_id,
                        #         )
                        #         return
                        #     logger.info(
                        #         "AskUserQuestion answered for session %s, continuing",
                        #         base_session_id,
                        #     )

                        continue

                    # Match the structured subtype instead of relying only on the
                    # current SDK's generic SystemMessage class. A future SDK may
                    # promote this event to a dedicated typed message.
                    if is_model_refusal_fallback:
                        if model_refusal_fallback_notice is None:
                            logger.debug("Ignoring non-actionable Claude refusal fallback for %s", composite_key)
                            continue
                        if not self._has_pending_requests(composite_key):
                            logger.info(
                                "Dropping Claude refusal fallback for %s without an active turn",
                                composite_key,
                            )
                            continue
                        await self._handle_model_refusal_fallback(
                            context,
                            composite_key,
                            model_refusal_fallback_notice,
                        )
                        continue

                    if message_type == "system":
                        formatted_message = self.claude_client.format_message(
                            message,
                            get_relative_path=lambda path: self.get_relative_path(path, context),
                            formatter=formatter,
                        )
                        if await self._handle_auth_failure_result(
                            context,
                            composite_key,
                            getattr(message, "subtype", "") or "",
                            formatted_message,
                        ):
                            # Retire the failed request from the FIFO (else the next
                            # successful turn adopts its stale token). Codex P2.
                            self._retire_failed_auth_turn(composite_key, context)
                            mark_session_idle = getattr(self.session_handler, "mark_session_idle", None)
                            if callable(mark_session_idle):
                                mark_session_idle(composite_key)
                            await self._clear_pending_reactions(composite_key, context)
                            return
                        continue

                    if message_type == "result":
                        self._pending_assistant_message.pop(composite_key, None)
                        result_text = getattr(message, "result", None)
                        if self._consume_suppressed_synthetic_result(composite_key, message, result_text):
                            self._last_assistant_text.pop(composite_key, None)
                            continue
                        if not result_text:
                            # ResultMessage had no text; use the last assistant
                            # text as a fallback so the user still sees output.
                            fallback = self._last_assistant_text.get(composite_key)
                            if fallback:
                                result_text = fallback

                        if await self._handle_auth_failure_result(
                            context,
                            composite_key,
                            getattr(message, "subtype", "") or "",
                            result_text,
                        ):
                            self._retire_failed_auth_turn(composite_key, context)
                            mark_session_idle = getattr(self.session_handler, "mark_session_idle", None)
                            if callable(mark_session_idle):
                                mark_session_idle(composite_key)
                            await self._clear_pending_reactions(composite_key, context)
                            self._last_assistant_text.pop(composite_key, None)
                            self._pending_assistant_message.pop(composite_key, None)
                            return

                        # NOTE: The pending assistant message is intentionally
                        # NOT emitted here.  ResultMessage.result already
                        # contains the same text as the last AssistantMessage,
                        # so sending both would duplicate the content.

                        pending_request = self._pop_pending_request(composite_key)

                        # The receiver is long-lived and reused across a session's
                        # turns, so ``context`` still carries the FIRST turn's
                        # ``turn_token``. Adopt the token of the turn THIS result
                        # belongs to (the FIFO-matched pending request) so the
                        # streaming completion guard in ``_stream_chunk`` correlates
                        # the result to the live sink instead of rejecting it as a
                        # stale straggler. No-op for fresh sessions / absent tokens.
                        self._adopt_pending_turn_token(context, pending_request)

                        # A terminal result settles the turn. The pending request
                        # was already popped above, so the idle transition must run
                        # even if emitting the result or backfilling the title
                        # raises — otherwise the inner ``except … continue`` below
                        # would swallow the error, skip the mark-idle, and leave the
                        # session pinned ``active`` (exempt from idle eviction until
                        # the next service restart). Run it in a ``finally``.
                        emit_failed = False
                        try:
                            await self.emit_result_message(
                                context,
                                result_text,
                                subtype=getattr(message, "subtype", "") or "",
                                duration_ms=getattr(message, "duration_ms", 0),
                                parse_mode="markdown",
                                request=pending_request,
                            )
                            native_session_id = self._native_session_ids.get(composite_key) or self._reserved_native_session_id(
                                context,
                                self.name,
                            ) or self._reserved_native_session_id(
                                getattr(pending_request, "context", None),
                                self.name,
                            )
                            if pending_request is not None and native_session_id:
                                self._maybe_backfill_session_title(pending_request, native_session_id)
                        except Exception:
                            emit_failed = True
                            raise
                        finally:
                            # Settle the turn regardless of an emit/backfill
                            # failure: clear the loading reaction and the stale
                            # assistant-text fallback, then release the active
                            # flag so the session can be idle-evicted.
                            await self._remove_result_pending_reaction(
                                composite_key,
                                context,
                                pending_request,
                            )
                            self._last_assistant_text.pop(composite_key, None)
                            is_idle = self._mark_session_idle_if_no_pending_requests(composite_key)
                            try:
                                session = await self.session_manager.get_or_create_session(
                                    context.user_id, context.channel_id
                                )
                                if session and is_idle:
                                    session.session_active[composite_key] = False
                            except Exception:
                                logger.debug(
                                    "claude: failed to update session_active after result for %s",
                                    composite_key,
                                    exc_info=True,
                                )
                            if emit_failed:
                                self._release_service_runtime_turn(context)
                        continue

                    # Ignore UserMessage/tool results; toolcalls are emitted from ToolUseBlock.
                    continue
                except Exception as e:
                    logger.error(f"Error processing message from Claude: {e}", exc_info=True)
                    continue
            await self._handle_receiver_eof(composite_key, context)
        except asyncio.CancelledError:
            # Receiver task was explicitly cancelled (e.g. /stop, /clear,
            # or a new message replacing the session).  Clean up reactions
            # because this receiver will never process another result.
            composite_key = composite_key or f"{base_session_id}:{working_path}"
            mark_session_idle = getattr(self.session_handler, "mark_session_idle", None)
            if callable(mark_session_idle):
                mark_session_idle(composite_key)
            logger.info("Claude receiver cancelled for session %s", composite_key)
            await self._clear_pending_reactions(composite_key, context)
            if composite_key not in self._suppress_receiver_runtime_release:
                self._release_service_runtime_turn(context)
            raise
        except Exception as e:
            composite_key = composite_key or f"{base_session_id}:{working_path}"
            mark_session_idle = getattr(self.session_handler, "mark_session_idle", None)
            if callable(mark_session_idle):
                mark_session_idle(composite_key)
            logger.error(
                f"Error in Claude receiver for session {composite_key}: {e}",
                exc_info=True,
            )
            # The reused receiver context still carries the FIRST turn's token, so a
            # 2nd-or-later turn's crash would emit its terminal error under a stale
            # token — the outbound active-turn guard treats it as superseded and drops
            # BOTH the failed-status write and the completion signal, hanging Chat to
            # the 600s timeout. Adopt the CURRENT (FIFO-head) turn's token onto the
            # context BEFORE clearing the FIFO (and before either the auth-recovery or
            # the non-auth emit below), mirroring the in-loop auth-failure paths (Codex P2).
            _pending = self._pending_requests.get(composite_key) or []
            self._adopt_pending_turn_token(context, _pending[0] if _pending else None)
            # Clean up all pending reactions for this session on error —
            # the receiver is dead and won't process any more results.
            await self._clear_pending_reactions(composite_key, context)
            error_notify = self._format_error_notify(e)
            handled = await self.controller.agent_auth_service.maybe_emit_auth_recovery_message(
                context,
                "claude",
                error_notify,
            )
            if not handled:
                await self.session_handler.handle_session_error(composite_key, context, e)
                # ``handle_session_error`` sends via the IM client, which doesn't
                # write to ``messages``; the web Chat renders only durable rows, so
                # persist a terminal notify or the avibe user's turn stops with no
                # explanation (mirrors the synchronous query-failure path above).
                try:
                    from core.message_mirror import persist_agent_message

                    persist_agent_message(context, "notify", error_notify)
                except Exception:
                    logger.debug("claude: failed to persist terminal receiver-error row", exc_info=True)
                # A dead receiver is terminal. The HANDLED (auth) branch already
                # settled the turn via ``maybe_emit_auth_recovery_message``; the
                # non-auth branch is settled by NOTHING, so route it through the
                # OUTBOUND status chokepoint here (empty error result → dot red +
                # releases the SSE waiter) instead of letting the avibe Chat hang to
                # the 600s stream timeout and then settle idle. No-op off-workbench
                # (Codex P2).
                await self.controller.emit_agent_message(context, "result", "", is_error=True)
            self._release_service_runtime_turn(context)
        # NOTE: no `finally` cleanup of pending reactions here.
        # When the receiver ends normally (stream exhausted after a result),
        # new messages may have already queued their reactions via
        # handle_message().  Blindly clearing them here would remove the
        # 👀 for an in-flight request that hasn't produced a result yet.
        # The except blocks above handle the cancel/error cases; the
        # normal-result case is handled by _remove_pending_reaction()
        # inside the loop.
        finally:
            self._suppressed_synthetic_results.discard(composite_key)

    async def _handle_receiver_eof(self, composite_key: str, context: MessageContext) -> None:
        """Settle a Claude receiver that ended without a ResultMessage."""
        pending_request = self._pop_pending_request(composite_key)
        if pending_request is None:
            return

        pending_token = str(
            (getattr(getattr(pending_request, "context", None), "platform_specific", None) or {}).get(
                AGENT_RUNTIME_TURN_TOKEN
            )
            or ""
        )
        if not pending_token:
            self._pending_requests.setdefault(composite_key, []).insert(0, pending_request)
            return
        logger.warning("Claude receiver ended without a result for session %s", composite_key)
        self._adopt_pending_turn_token(context, pending_request)
        await self._remove_specific_pending_reaction(composite_key, context, pending_request)
        await self._remove_ack_reaction(pending_request)
        self._last_assistant_text.pop(composite_key, None)
        self._pending_assistant_message.pop(composite_key, None)
        self._mark_session_idle_if_no_pending_requests(composite_key)

        await self._cleanup_runtime_session(
            composite_key,
            current_receiver_task=asyncio.current_task(),
            preserve_pending_request_state=True,
        )
        try:
            await self.controller.emit_agent_message(context, "result", "", is_error=True)
        finally:
            self._release_service_runtime_turn(context)

    async def _handle_synthetic_api_error_message(
        self,
        context: MessageContext,
        composite_key: str,
        message,
        text: str,
    ) -> bool:
        """Settle Claude Code synthetic API errors without publishing them as chat."""

        if not self._is_synthetic_api_error_message(message, text):
            return False

        pending_request = self._pop_pending_request(composite_key)
        self._adopt_pending_turn_token(context, pending_request)
        logger.warning(
            "Claude malformed tool-use synthetic API error for session %s suppressed from user-visible transcript: %s",
            composite_key,
            text or "<empty>",
        )
        await self.controller.emit_agent_message(context, "result", "", is_error=True)
        if pending_request is not None:
            await self._remove_ack_reaction(pending_request)
        self._last_assistant_text.pop(composite_key, None)
        self._pending_assistant_message.pop(composite_key, None)
        self._suppressed_synthetic_results.add(composite_key)
        self._discard_pending_reaction(composite_key)
        self._mark_session_idle_if_no_pending_requests(composite_key)
        return True

    def _consume_suppressed_synthetic_result(self, composite_key: str, message, text: Optional[str]) -> bool:
        if composite_key not in self._suppressed_synthetic_results:
            return False

        if not self._is_malformed_tool_call_retry_failure_result(message, text):
            self._suppressed_synthetic_results.discard(composite_key)
            return False

        self._suppressed_synthetic_results.discard(composite_key)
        logger.warning(
            "Claude paired malformed tool-use synthetic ResultMessage for session %s suppressed: %s",
            composite_key,
            text or "<empty>",
        )
        return True

    async def _delete_ack(self, context: MessageContext, request: AgentRequest):
        service = getattr(self.controller, "processing_indicator", None)
        if service is not None:
            await service.delete_ack_message(request, channel_id=context.channel_id)
            return
        ack_id = request.ack_message_id
        if ack_id and hasattr(self.im_client, "delete_message"):
            try:
                await self.im_client.delete_message(context.channel_id, ack_id)
            except Exception as err:
                logger.debug("Could not delete ack message: %s", err)
            finally:
                request.ack_message_id = None

    async def _remove_pending_reaction(self, composite_key: str, context: MessageContext) -> None:
        """Remove the oldest stored reaction for a session after result is sent.

        The backend turn gate normally leaves at most one pending reaction, but
        keep FIFO cleanup for defensive compatibility with older queued state.
        """
        reactions = self._pending_reactions.get(composite_key)
        if reactions:
            # Pop the oldest reaction (FIFO)
            message_id, emoji = reactions.pop(0)
            # Clean up empty list
            if not reactions:
                self._pending_reactions.pop(composite_key, None)
            try:
                await self.im_client.remove_reaction(context, message_id, emoji)
            except Exception as err:
                logger.debug(f"Failed to remove reaction ack: {err}")

    def _discard_pending_reaction(self, composite_key: str) -> None:
        reactions = self._pending_reactions.get(composite_key)
        if not reactions:
            return
        reactions.pop(0)
        if not reactions:
            self._pending_reactions.pop(composite_key, None)

    async def _remove_result_pending_reaction(
        self,
        composite_key: str,
        context: MessageContext,
        request: Optional[AgentRequest],
    ) -> None:
        reactions_before = len(self._pending_reactions.get(composite_key) or [])
        if request is not None:
            await self._remove_specific_pending_reaction(composite_key, context, request)
            await self._remove_ack_reaction(request)
        reactions_after = len(self._pending_reactions.get(composite_key) or [])
        if reactions_before and reactions_after == reactions_before:
            self._discard_pending_reaction(composite_key)

    def _pop_pending_request(self, composite_key: str) -> Optional[AgentRequest]:
        requests = self._pending_requests.get(composite_key)
        if not requests:
            return None
        request = requests.pop(0)
        if not requests:
            self._pending_requests.pop(composite_key, None)
        return request

    @staticmethod
    def _adopt_pending_turn_token(context: MessageContext, pending_request: Optional[AgentRequest]) -> None:
        """Copy the pending turn tokens onto the reused receiver context.

        Claude runs one long-lived receiver per runtime session, so the context
        captured when it started can carry an older turn's tokens. The web stream
        guard uses ``turn_token`` and the shared backend runtime gate uses
        ``agent_runtime_turn_token``; both must follow the FIFO-matched pending
        request before any assistant/tool/result emit.
        """
        if pending_request is None:
            return
        src = getattr(pending_request, "context", None)
        src_payload = (getattr(src, "platform_specific", None) or {}) if src is not None else {}
        token = src_payload.get(AGENT_TURN_TOKEN)
        runtime_key = src_payload.get(AGENT_RUNTIME_TURN_KEY)
        runtime_token = src_payload.get(AGENT_RUNTIME_TURN_TOKEN)
        if not (token or runtime_key or runtime_token):
            return
        if context.platform_specific is None:
            context.platform_specific = {}
        if token:
            context.platform_specific[AGENT_TURN_TOKEN] = token
        if runtime_key:
            context.platform_specific[AGENT_RUNTIME_TURN_KEY] = runtime_key
        if runtime_token:
            context.platform_specific[AGENT_RUNTIME_TURN_TOKEN] = runtime_token

    def _retire_failed_auth_turn(self, composite_key: str, context: MessageContext) -> None:
        """Retire a terminal auth-failure turn from the pending FIFO.

        The auth error IS this turn's (failed) result, so pop its pending request:
        leaving the failed entry would make the next result adopt the old
        ``turn_token`` — then ``_stream_chunk`` rejects the live turn's completion
        and Stop sticks until the safety timeout. Adopt the failed turn's own token
        and release its Chat stream now. Called from auth-failure terminal paths
        after the recovery notify has been persisted."""
        failed_request = self._pop_pending_request(composite_key)
        self._adopt_pending_turn_token(context, failed_request)
        _mark = getattr(self.controller, "mark_turn_complete", None)
        if callable(_mark):
            _mark(context)

    def _has_pending_requests(self, composite_key: str) -> bool:
        return bool(self._pending_requests.get(composite_key))

    async def _maybe_begin_agent_initiated_turn(
        self,
        context: MessageContext,
        composite_key: str,
        base_session_id: str,
        working_path: str,
        session_key: str,
    ) -> None:
        """Open a turn for UNSOLICITED backend output (no Avibe query behind it).

        Claude Code re-invokes the agent loop inside this same SDK process when a
        background task completes or a ScheduleWakeup fires, streaming a fresh
        assistant/result run onto this long-lived receiver. The previous turn's
        terminal result already released the runtime gate and this receiver's
        reused context still carries that turn's (now stale) token, so the
        outbound active-turn guard would drop the whole reply — it would never be
        persisted, delivered, or pushed.

        When output arrives with NO pending request, open an agent-initiated turn
        and synthesize a pending ``AgentRequest`` for it. The existing
        assistant/result handlers then treat it exactly like a normal turn (token
        adoption keeps the gate token live across the assistant→result sequence,
        the result pops the synthetic request and the dispatcher's result
        ``finally`` releases the gate, and an EOF/error/cancel settles it through
        the same paths). No-op when a turn is already in flight for this session
        (a real user turn, or an agent-initiated turn already opened for an
        earlier message of this same run).
        """
        if self._has_pending_requests(composite_key):
            return
        # A malformed-tool-use synthetic API error pops the turn's real pending
        # request and arms ``_suppressed_synthetic_results`` for the PAIRED
        # ResultMessage, which the result branch then skips (``continue``) with NO
        # terminal emit. That paired result reaches this hook with an empty FIFO +
        # free gate, so opening an agent-initiated turn for it would leak the gate /
        # pending request / active flag until EOF — and the NEXT user message for
        # this Claude session would block behind the leaked gate (Codex P1). Skip
        # while a suppressed synthetic result is pending; the set is cleared by
        # ``_consume_suppressed_synthetic_result`` / cleanup, so real later turns
        # open normally.
        if composite_key in self._suppressed_synthetic_results:
            return
        service = getattr(self.controller, "agent_service", None)
        begin = getattr(service, "begin_agent_initiated_turn", None)
        if not callable(begin):
            return
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip() or composite_key
        token = await begin(self.name, context, runtime_key)
        if not token:
            # The gate is CONTENDED — a user turn holds it, or one is queued on it
            # and we must not block the receiver behind that waiter (deadlock; see
            # begin_agent_initiated_turn). If a turn actually holds the gate the
            # output rides that turn's machinery; in the narrow free-but-queued
            # window the outbound guard drops it. Log so that (rare, sub-tick) loss
            # is visible instead of silent — full preservation across a queued user
            # turn is a tracked follow-up.
            logger.info(
                "Agent-initiated output for %s not opened: runtime gate contended "
                "(a user turn holds or is queued on it)",
                composite_key,
            )
            return
        # Mark the session ACTIVE so this in-flight agent-initiated turn gets the
        # SAME idle-eviction exemption a user turn gets in ``handle_message``.
        # Without it the turn is protected only by per-message activity touches and
        # could be reclaimed mid-turn if it goes quiet past the idle timeout (e.g.
        # it kicks off its own long background work). The terminal result / EOF /
        # error / cancel paths all mark the session idle again, so this stays
        # balanced; the stuck-active backstop still reclaims a wedged turn. (The
        # SILENT wait BEFORE this first output is still bounded by the idle timeout
        # — once the session is evicted the receiver is gone and no agent-initiated
        # turn can open. See docs/plans/agent-initiated-turn-outbox.md.)
        mark_active = getattr(self.session_handler, "mark_session_active", None)
        if callable(mark_active):
            mark_active(composite_key)
        request = AgentRequest(
            context=context,
            message="",
            working_path=working_path,
            base_session_id=base_session_id,
            composite_session_id=composite_key,
            session_key=session_key,
        )
        self._pending_requests.setdefault(composite_key, []).append(request)
        logger.info(
            "Opened agent-initiated turn for session %s (unsolicited backend output)",
            composite_key,
        )

    def _mark_session_idle_if_no_pending_requests(self, composite_key: str) -> bool:
        if self._has_pending_requests(composite_key):
            return False
        mark_session_idle = getattr(self.session_handler, "mark_session_idle", None)
        if callable(mark_session_idle):
            mark_session_idle(composite_key)
        return True

    def _remove_pending_request(self, composite_key: str, request: AgentRequest) -> None:
        requests = self._pending_requests.get(composite_key)
        if not requests:
            return
        for index, pending_request in enumerate(requests):
            if pending_request is request:
                requests.pop(index)
                break
        if not requests:
            self._pending_requests.pop(composite_key, None)

    async def _remove_specific_pending_reaction(
        self, composite_key: str, context: MessageContext, request: AgentRequest
    ) -> None:
        """Remove a specific reaction from the queue by matching message_id.

        Used on error paths to remove the current request's reaction instead of FIFO.
        """
        target_id = getattr(request, "ack_reaction_message_id", None)
        target_emoji = getattr(request, "ack_reaction_emoji", None)
        if not target_id:
            return
        reactions = self._pending_reactions.get(composite_key)
        if not reactions:
            return
        # Find and remove the matching reaction
        for i, (msg_id, emoji) in enumerate(reactions):
            if msg_id == target_id and emoji == target_emoji:
                reactions.pop(i)
                if not reactions:
                    self._pending_reactions.pop(composite_key, None)
                try:
                    await self.im_client.remove_reaction(context, msg_id, emoji)
                except Exception as err:
                    logger.debug(f"Failed to remove reaction ack: {err}")
                return

    async def _clear_pending_reactions(self, composite_key: str, context: MessageContext) -> None:
        """Clear all pending reactions for a session (for error cleanup)."""
        reactions = self._pending_reactions.pop(composite_key, None)
        requests = self._pending_requests.pop(composite_key, None)
        if reactions:
            for message_id, emoji in reactions:
                try:
                    await self.im_client.remove_reaction(context, message_id, emoji)
                except Exception as err:
                    logger.debug(f"Failed to remove reaction ack: {err}")
        if requests:
            for request in requests:
                await self._remove_ack_reaction(request)

    def get_relative_path(self, abs_path: str, context: Optional[MessageContext] = None) -> str:
        """Convert absolute path to relative path from working directory."""
        try:
            cwd = self.session_handler.get_working_path(context)
            abs_path = os.path.abspath(os.path.expanduser(abs_path))
            rel_path = os.path.relpath(abs_path, cwd)
            if rel_path.startswith("../.."):
                return abs_path
            return rel_path
        except Exception:
            return abs_path

    def _get_target_context(self, context: MessageContext) -> MessageContext:
        """Return context for sending messages (respect Slack thread replies)."""
        if self.im_client.should_use_thread_for_reply() and context.thread_id:
            return MessageContext(
                user_id=context.user_id,
                channel_id=context.channel_id,
                thread_id=context.thread_id,
                message_id=context.message_id,
                platform_specific=context.platform_specific,
            )
        return context

    def _maybe_capture_session_id(
        self,
        message,
        base_session_id: str,
        session_key: str,
        context: MessageContext,
        *,
        working_path: Optional[str] = None,
    ) -> Optional[str]:
        """Capture session id from system init messages."""
        if (
            hasattr(message, "__class__")
            and message.__class__.__name__ == "SystemMessage"
            and getattr(message, "subtype", None) == "init"
            and getattr(message, "data", None)
        ):
            session_id = message.data.get("session_id")
            if session_id:
                # avibe: bind the native id to the RESERVED workbench session row so
                # the reply publishes under the open Chat session, not a freshly
                # minted hidden row (Codex P1). Target subagents are the exception:
                # their native id is persisted under the namespaced anchor, then the
                # publish target is pinned back to the open Chat session.
                reserved_id = self._reserved_agent_session_id(context)
                uses_backend_anchor = self._uses_namespaced_backend_session(context)
                reserved = self._bind_reserved_workbench_session(context, session_id)
                if reserved:
                    return session_id
                binder = getattr(self.session_handler, "bind_agent_session_id", None)
                if callable(binder):
                    agent_session_id = binder(
                        session_key=session_key,
                        agent_name=self.name,
                        session_anchor=base_session_id,
                        native_session_id=session_id,
                        working_path=working_path,
                    )
                else:
                    agent_session_id = self.session_handler.capture_session_id(
                        base_session_id,
                        session_id,
                        session_key,
                        working_path=working_path,
                    )
                if agent_session_id:
                    payload = dict(context.platform_specific or {})
                    payload["agent_session_id"] = (
                        reserved_id if uses_backend_anchor and reserved_id else agent_session_id
                    )
                    context.platform_specific = payload
                return session_id
        return None

    def _extract_text_blocks(self, message, context: MessageContext) -> str:
        """Extract text-only content blocks for result fallbacks."""
        parts = []
        for block in getattr(message, "content", []) or []:
            if isinstance(block, TextBlock):
                text = block.text.strip() if block.text else ""
                if text:
                    parts.append(self._get_formatter(context).escape_special_chars(text))
        return "\n\n".join(parts).strip()

    @staticmethod
    def _is_model_refusal_fallback_message(message) -> bool:
        return (getattr(message, "subtype", "") or "").strip().lower() == "model_refusal_fallback"

    @staticmethod
    def _parse_model_refusal_fallback_notice(message) -> Optional[tuple[str, str, str]]:
        data = getattr(message, "data", None)
        if not isinstance(data, dict):
            return None

        direction = str(data.get("direction") or "").strip().lower()
        if direction and direction != "retry":
            return None

        original_value = data.get("original_model") or data.get("originalModel") or ""
        fallback_value = data.get("fallback_model") or data.get("fallbackModel") or ""
        original_model = " ".join(str(original_value).split())[:160]
        fallback_model = " ".join(str(fallback_value).split())[:160]
        provider_content = str(data.get("content") or "").strip()
        if not provider_content and not (original_model and fallback_model):
            return None
        return original_model, fallback_model, provider_content

    async def _handle_model_refusal_fallback(
        self,
        context: MessageContext,
        composite_key: str,
        notice: tuple[str, str, str],
    ) -> None:
        """Surface Claude's structured safety fallback without ending the turn."""
        # Claude Code can write this marker after the fallback assistant row. Do
        # not infer retraction order from the marker alone: the result path may
        # still need that assistant text when ResultMessage.result is empty.
        pending_requests = self._pending_requests.get(composite_key) or []
        self._adopt_pending_turn_token(context, pending_requests[0] if pending_requests else None)

        original_model, fallback_model, provider_content = notice

        text = ""
        translator = getattr(self.session_handler, "_t", None) or getattr(self.controller, "_t", None)
        if callable(translator) and original_model and fallback_model:
            text = translator(
                "status.claudeRefusalFallback",
                originalModel=original_model,
                fallbackModel=fallback_model,
            )
        if not text:
            text = provider_content
        if not text:
            logger.warning("Claude refusal fallback for %s had no displayable content", composite_key)
            return

        logger.info(
            "Claude safety fallback for session %s: %s -> %s",
            composite_key,
            original_model or "<unknown>",
            fallback_model or "<unknown>",
        )
        await self.controller.emit_agent_message(
            context,
            "notify",
            text,
            parse_mode="markdown",
        )

    async def _handle_auth_failure_result(
        self,
        context: MessageContext,
        composite_key: str,
        subtype: str,
        text: Optional[str],
    ) -> bool:
        if not text or not text.strip():
            return False

        normalized_subtype = (subtype or "").strip().lower()
        if normalized_subtype not in {"error", "failed"}:
            return False

        if not classify_auth_error("claude", text):
            return False

        # The reused receiver still carries an EARLIER turn's ``turn_token``; adopt
        # THIS turn's token (the FIFO head — the request this result belongs to, per
        # the success path below) BEFORE the auth-recovery emit. The recovery helper
        # now settles the failed dot through the outbound chokepoint, whose
        # active-turn guard would otherwise treat the stale token as a superseded
        # turn and skip the failed-status write for 2nd-or-later Claude auth failures.
        pending = self._pending_requests.get(composite_key) or []
        self._adopt_pending_turn_token(context, pending[0] if pending else None)

        handled = await self.controller.agent_auth_service.maybe_emit_auth_recovery_message(
            context,
            "claude",
            f"❌ Claude error: {text}",
        )
        if handled:
            await self._cleanup_runtime_session(
                composite_key,
                current_receiver_task=asyncio.current_task(),
                preserve_pending_request_state=True,
            )
        return handled

    def _is_auth_failure_assistant_message(self, message) -> bool:
        error_kind = (getattr(message, "error", "") or "").strip().lower()
        return error_kind == "authentication_failed"

    @staticmethod
    def _is_synthetic_api_error_message(message, text: Optional[str] = None) -> bool:
        if not getattr(message, "isApiErrorMessage", False):
            model = str(getattr(message, "model", "") or "").strip().lower()
            if model != "<synthetic>":
                return False
        return ClaudeAgent._is_malformed_tool_call_retry_failure_text(text)

    @staticmethod
    def _is_malformed_tool_call_retry_failure_text(text: Optional[str]) -> bool:
        normalized = " ".join((text or "").strip().lower().split())
        if not normalized:
            return False
        return (
            "tool call" in normalized
            and "could not be parsed" in normalized
            and "retry also failed" in normalized
        )

    @staticmethod
    def _is_malformed_tool_call_retry_failure_result(message, text: Optional[str]) -> bool:
        subtype = (getattr(message, "subtype", "") or "").strip().lower()
        if subtype != "error" and not getattr(message, "is_error", False):
            return False

        candidates = [text or ""]
        errors = getattr(message, "errors", None) or []
        candidates.extend(str(error) for error in errors)
        normalized = " ".join(" ".join(candidates).strip().lower().split())
        if not normalized:
            return False
        if ClaudeAgent._is_malformed_tool_call_retry_failure_text(normalized):
            return True
        has_tool = "tool call" in normalized or "tool-call" in normalized or "tool-use" in normalized
        has_parse = "parse" in normalized or "parsing" in normalized or "malformed" in normalized
        has_retry = "retry" in normalized or "retried" in normalized
        return has_tool and has_parse and has_retry

    @staticmethod
    def _extract_context_tokens(message) -> int:
        """Current context-window occupancy from an AssistantMessage's ``usage``.

        Each AssistantMessage carries the RAW per-request Anthropic usage block
        (``data["message"]["usage"]``), so its ``input_tokens +
        cache_read_input_tokens + cache_creation_input_tokens`` is the full prompt
        sent for that request — i.e. the whole context at that moment — and
        ``output_tokens`` is the response that joins the context for the next
        request. The LATEST assistant message therefore reflects the current
        occupancy, which the dispatcher SETs (not accumulates) so the figure
        tracks the live context size and drops after a /compact.

        NB: ``ResultMessage.usage`` is the CLI's CUMULATIVE turn usage (summed
        across requests), so it overstates occupancy and must NOT be used here.

        Defensive: ``usage`` may be a dict or object, or absent — returns 0 rather
        than raising so a missing field never breaks the turn."""
        usage = getattr(message, "usage", None)
        if usage is None:
            return 0

        def _field(name: str) -> int:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            try:
                return int(value) if value and int(value) > 0 else 0
            except (TypeError, ValueError):
                return 0

        return (
            _field("input_tokens")
            + _field("cache_read_input_tokens")
            + _field("cache_creation_input_tokens")
            + _field("output_tokens")
        )

    def _detect_message_type(self, message) -> Optional[str]:
        """Infer message type name from Claude SDK class."""
        if not hasattr(message, "__class__"):
            return None
        class_name = message.__class__.__name__
        mapping = {
            "SystemMessage": "system",
            "UserMessage": "user",
            "AssistantMessage": "assistant",
            "ResultMessage": "result",
        }
        return mapping.get(class_name)

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
