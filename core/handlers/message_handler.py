"""Message routing and Agent communication handlers"""

import logging
import inspect
from datetime import datetime
from typing import Any, List, Optional, Tuple

from core.audio_asr import (
    AUDIO_SIGNATURE_SAMPLE_BYTES,
    AudioTranscript,
    append_audio_transcripts_to_message,
    detect_audio_mime_from_sample,
    format_audio_transcript_echo,
)
from core.message_output import terminal_output_for, terminal_turn_output
from core.message_context import resolve_context_thread_id
from modules.agents.base import AgentRequest
from modules.agents.catalog import display_name_for_backend, is_agent_backend
from modules.im import MessageContext
from modules.im.base import FileAttachment

from .base import BaseHandler

logger = logging.getLogger(__name__)

SUBAGENT_REACTION_EMOJI = "🤖"


def _target_agent_variant(value: Any, backend: Optional[str], agent_name: Optional[str] = None) -> Optional[str]:
    if value is None:
        return None
    variant = str(value).strip()
    if not variant:
        return None
    sentinel_values = {"default", "claude", "codex", "opencode"}
    if backend:
        sentinel_values.add(str(backend).strip())
    if agent_name:
        sentinel_values.add(str(agent_name).strip())
    return None if variant in sentinel_values else variant


class MessageHandler(BaseHandler):
    """Handles message routing and Claude communication"""

    TURN_SOURCE_HUMAN = "human"
    TURN_SOURCE_SCHEDULED = "scheduled"

    def __init__(self, controller):
        """Initialize with reference to main controller"""
        super().__init__(controller)
        self.session_manager = controller.session_manager
        self.session_handler = None  # Will be set after creation
        self.receiver_tasks = controller.receiver_tasks

    def set_session_handler(self, session_handler):
        """Set reference to session handler"""
        self.session_handler = session_handler

    async def handle_user_message(self, context: MessageContext, message: str):
        """Process regular human-originated messages and route to configured agent."""
        await self._handle_turn(context, message, source=self.TURN_SOURCE_HUMAN)

    async def handle_scheduled_message(self, context: MessageContext, message: str, parsed_session_key=None):
        """Process a scheduler-originated turn through the shared turn pipeline."""
        if parsed_session_key is not None:
            payload = dict(context.platform_specific or {})
            payload["parsed_session_key"] = parsed_session_key
            context.platform_specific = payload
        return await self._handle_turn(context, message, source=self.TURN_SOURCE_SCHEDULED)

    async def _prepare_turn_context(self, context: MessageContext, source: str) -> MessageContext:
        payload = dict(context.platform_specific or {})
        payload["turn_source"] = source
        context.platform_specific = payload
        prepared = await self._get_im_client(context).prepare_turn_context(context, source)
        prepared_payload = dict(prepared.platform_specific or {})
        prepared_payload["turn_source"] = source
        prepared.platform_specific = prepared_payload
        return prepared

    async def _handle_turn(self, context: MessageContext, message: str, *, source: str) -> Optional[str]:
        """Shared turn-processing pipeline used by both human and scheduled turns."""
        processing_indicator = None
        request: AgentRequest | None = None
        # Tracks whether we actually dispatched an agent turn (whose reply
        # streams in asynchronously). If we leave this method WITHOUT having
        # dispatched — early returns, missing/disabled backend, errors — no
        # async result is coming, so the ``finally`` releases any streaming SSE
        # waiter for this turn instead of leaving it open until the timeout.
        agent_dispatched = False
        try:
            is_human = source == self.TURN_SOURCE_HUMAN
            control_message = self._get_control_message(context, message) if is_human else message

            # Record user activity for auto-update idle detection
            if is_human and hasattr(self.controller, "update_checker"):
                self.controller.update_checker.record_activity()

            # If message is empty AND no files attached (e.g., user just @mentioned bot without text),
            # trigger the /start command instead of sending empty message to agent
            has_files = bool(context.files)
            if (not message or not message.strip()) and not has_files:
                if is_human:
                    await self.controller.command_handler.handle_start(context, "")
                return None

            if is_human:
                # Claim the message before processing so duplicate IM deliveries or
                # parallel runtime instances cannot start separate agent turns.
                message_ts = context.message_id
                thread_ts = context.thread_id or context.message_id
                if message_ts and thread_ts:
                    try_record = getattr(self.sessions, "try_record_processed_message", None)
                    if callable(try_record):
                        recorded = try_record(context.channel_id, thread_ts, message_ts)
                    else:
                        recorded = not self.sessions.is_message_already_processed(
                            context.channel_id,
                            thread_ts,
                            message_ts,
                        )
                        if recorded:
                            self.sessions.record_processed_message(context.channel_id, thread_ts, message_ts)
                    if not recorded:
                        logger.info(
                            f"Skipping already processed message: channel={context.channel_id}, "
                            f"thread={thread_ts}, message={message_ts}"
                        )
                        return None

            if is_human and not has_files:
                maybe_consume_setup_reply = getattr(self.controller.agent_auth_service, "maybe_consume_setup_reply", None)
                if callable(maybe_consume_setup_reply):
                    consumed = await maybe_consume_setup_reply(context, control_message)
                    if consumed:
                        return None

            # Skip automatic cleanup; receiver tasks are retained until shutdown

            # Allow "stop" shortcut inside Slack threads
            active_thread_id = resolve_context_thread_id(context) or context.thread_id
            if is_human and active_thread_id and control_message.strip().lower() in ["stop", "/stop"]:
                if await self._handle_inline_stop(context):
                    return None

            if not self.session_handler:
                raise RuntimeError("Session handler not initialized")

            context = await self._prepare_turn_context(context, source)

            # Mirror the originating prompt into the workbench messages table
            # before we kick off the agent, so the transcript shows the turn
            # that produced the reply. Human turns are source='user'; harness
            # turns (scheduled task / watch / webhook) use a first-class harness
            # author/type so they cannot be mistaken for human input. Wrapped in
            # try/except inside the helper so a mirror failure can't break the turn.
            if source == self.TURN_SOURCE_HUMAN:
                from core.message_mirror import mirror_inbound

                mirror_inbound(context, control_message)
            else:
                # Harness turns retain a complete local transcript even when their
                # session is background-only; visibility gates outward delivery.
                from core.message_mirror import mirror_harness_inbound

                mirror_harness_inbound(context, message)

            base_session_id, working_path, composite_key = self.session_handler.get_session_info(context, source=source)
            payload = dict(context.platform_specific or {})
            payload["turn_source"] = source
            payload["turn_base_session_id"] = base_session_id
            payload["scheduled_anchor_required"] = self.session_handler.should_allocate_scheduled_anchor(
                context, source=source
            )
            context.platform_specific = payload

            reply_anchor_base_session_id = payload.get("reply_anchor_base_session_id")
            if reply_anchor_base_session_id and reply_anchor_base_session_id != base_session_id:
                self.session_handler.alias_session_base(
                    context,
                    source_base_session_id=reply_anchor_base_session_id,
                    alias_base_session_id=base_session_id,
                    clear_source=False,
                )
            settings_key = self._get_settings_key(context)
            session_key = self._get_session_key(context)

            # NOTE: claiming the thread's current message_id (the dispatcher's
            # per-turn bubble trigger) and posting the "starting" status bubble
            # now happen in AgentService.handle_message, AFTER the runtime gate
            # is acquired — i.e. when this turn actually STARTS rather than while
            # it is queued behind an in-flight turn. This keeps a queued turn
            # from hijacking the running turn's bubble key or posting a premature
            # bubble.

            platform_payload = context.platform_specific or {}
            resolved_target = platform_payload.get("agent_run_target")
            resolved_target = resolved_target if isinstance(resolved_target, dict) else {}
            platform_name = context.platform or platform_payload.get("platform")
            routing = (
                None
                if platform_name == "avibe" and resolved_target
                else self._get_settings_manager(context).get_channel_routing(settings_key)
            )
            requested_vibe_agent = platform_payload.get("vibe_agent_name")
            session_target = platform_payload.get("agent_session_target")
            if not requested_vibe_agent and isinstance(session_target, dict):
                requested_vibe_agent = session_target.get("agent_name")
            if not requested_vibe_agent:
                requested_vibe_agent = resolved_target.get("agent_name")
            session_agent_backend = (
                str(session_target["agent_backend"])
                if isinstance(session_target, dict) and session_target.get("agent_backend")
                else None
            )
            if not session_agent_backend and resolved_target.get("agent_backend"):
                session_agent_backend = str(resolved_target["agent_backend"])
            if resolved_target.get("agent_session_id"):
                # The concrete persisted target owns outward delivery. Resolve
                # this before the backend guard below: existing sessions already
                # carry a backend, so the fallback anchor lookup is intentionally
                # skipped for them.
                platform_payload["suppress_delivery"] = (
                    resolved_target.get("visibility") == "background"
                )
                context.platform_specific = platform_payload
            # Pin an EXISTING thread to its OWN backend. avibe carries the session
            # row in ``agent_session_target``; IM/CLI turns don't, so look up the
            # thread's (scope, anchor) row and adopt its agent/backend. A thread
            # keeps its backend for life — a scope-level backend change only affects
            # NEWLY created threads, never established ones. Falls through to channel
            # routing when no row exists yet (a new thread).
            if not isinstance(session_target, dict) and not requested_vibe_agent and not session_agent_backend:
                finder = getattr(self.sessions, "find_session_for_anchor", None)
                existing_thread = None
                if callable(finder):
                    try:
                        existing_thread = finder(session_key, base_session_id)
                    except Exception:
                        logger.debug("find_session_for_anchor failed; falling back to routing", exc_info=True)
                if existing_thread:
                    requested_vibe_agent = existing_thread.get("agent_name") or requested_vibe_agent
                    session_agent_backend = existing_thread.get("agent_backend") or session_agent_backend
                    # Scope is only placement. A persisted session's visibility is
                    # the single outward-delivery gate, including ordinary IM turns
                    # after an agent promotes or backgrounds the session via CLI/API.
                    platform_payload["suppress_delivery"] = (
                        existing_thread.get("visibility") == "background"
                    )
                    context.platform_specific = platform_payload
            resolve_vibe_agent = getattr(self.controller, "resolve_vibe_agent_for_context", None)
            vibe_agent = None
            if requested_vibe_agent and callable(resolve_vibe_agent):
                vibe_agent = resolve_vibe_agent(
                    context,
                    override_agent_name=requested_vibe_agent,
                    required=False,
                )
            elif callable(resolve_vibe_agent) and not session_agent_backend:
                vibe_agent = resolve_vibe_agent(context, required=False)
            if vibe_agent:
                agent_name = vibe_agent.backend
            elif session_agent_backend:
                agent_name = session_agent_backend
            else:
                agent_name = self.controller.resolve_agent_for_context(context)

            # Check for routing-based agent to maintain session key consistency
            # This ensures session IDs match between MessageHandler and SessionHandler
            routing_agent = None
            if routing:
                if agent_name == "opencode":
                    routing_agent = getattr(routing, "opencode_agent", None)
                elif agent_name == "claude":
                    routing_agent = getattr(routing, "claude_agent", None)
                elif agent_name == "codex":
                    routing_agent = getattr(routing, "codex_agent", None)
            if not routing_agent and agent_name in {"opencode", "claude", "codex"}:
                routing_agent = _target_agent_variant(
                    resolved_target.get("agent_variant"),
                    agent_name,
                    resolved_target.get("agent_name"),
                )

            from config.v2_settings import routing_model_for_backend, routing_reasoning_effort_for_backend

            has_session_target = isinstance(session_target, dict) or bool(resolved_target.get("agent_session_id"))
            scope_model_override = (
                None if has_session_target else routing_model_for_backend(routing, agent_name)
            )
            scope_reasoning_override = (
                None if has_session_target else routing_reasoning_effort_for_backend(routing, agent_name)
            )

            # A workbench Chat session carries the user's explicit per-session
            # agent / model / effort picks in ``agent_session_target`` (the Chat
            # header cascade writes them onto the session row, and the dispatch
            # layer copies the row here). Those are the highest-precedence
            # override for this turn — above channel-routing scope overrides and
            # the VibeAgent's own defaults — otherwise the header's model /
            # effort picker would be cosmetic: persisted and displayed but never
            # actually routed to the backend.
            session_target_model = (
                session_target.get("model") if isinstance(session_target, dict) else None
            ) or resolved_target.get("model")
            session_target_reasoning = (
                session_target.get("reasoning_effort") if isinstance(session_target, dict) else None
            ) or resolved_target.get("reasoning_effort")
            # The model / effort this turn ACTUALLY runs with — the same
            # precedence the request is built on below.
            effective_model = session_target_model or scope_model_override or (
                vibe_agent.model if vibe_agent else None
            )
            effective_reasoning_effort = session_target_reasoning or scope_reasoning_override or (
                vibe_agent.reasoning_effort if vibe_agent else None
            )
            # Materialize the resolved route into EMPTY workbench session
            # columns NOW, at turn start. A session created on an inherited
            # default carries NULLs (dispatch resolves the live Agent default);
            # without pinning, the chat header shows an agent with no model /
            # effort after the first message. Pinning at turn START — not at
            # native bind — means any later explicit header pick in this turn
            # (including an explicit clear to NULL) lands after this write and
            # is never undone by it. IM (scope/anchor) rows never carry
            # ``agent_session_target`` and are untouched: their model semantics
            # stay with channel routing.
            if isinstance(session_target, dict) and session_target.get("id") and (
                (effective_model and not session_target.get("model"))
                or (effective_reasoning_effort and not session_target.get("reasoning_effort"))
            ):
                materialize = getattr(self.sessions, "materialize_agent_session_route", None)
                if callable(materialize):
                    try:
                        materialize(
                            str(session_target["id"]),
                            model=effective_model,
                            reasoning_effort=effective_reasoning_effort,
                        )
                    except Exception:
                        logger.debug("Session route materialization failed; dispatch continues", exc_info=True)

            matched_prefix = None
            subagent_message = None
            subagent_name = None
            subagent_model = None
            subagent_reasoning_effort = None

            if agent_name in ["opencode", "claude", "codex"]:
                from modules.agents.subagent_router import (
                    load_codex_subagent,
                    load_claude_subagent,
                    normalize_subagent_name,
                    parse_subagent_prefix,
                )

                parsed = parse_subagent_prefix(control_message)
                if parsed:
                    normalized = normalize_subagent_name(parsed.name)
                    if agent_name == "opencode":
                        try:
                            opencode_agent = self.controller.agent_service.agents.get("opencode")
                            if opencode_agent and hasattr(opencode_agent, "_get_server"):
                                server = await opencode_agent._get_server()
                                await server.ensure_running()
                                opencode_agents = await server.get_available_agents(self.controller.get_cwd(context))
                                name_map = {
                                    normalize_subagent_name(a.get("name", "")): a
                                    for a in opencode_agents
                                    if a.get("name")
                                }
                                match = name_map.get(normalized)
                                if match:
                                    subagent_name = match.get("name")
                        except Exception as err:
                            logger.warning(f"Failed to resolve OpenCode subagent: {err}")
                    elif agent_name == "claude":
                        try:
                            from pathlib import Path

                            subagent_def = load_claude_subagent(
                                normalized,
                                project_root=Path(working_path),
                            )
                            if subagent_def:
                                subagent_name = subagent_def.name
                                subagent_model = subagent_def.model
                                subagent_reasoning_effort = subagent_def.reasoning_effort
                        except Exception as err:
                            logger.warning(f"Failed to resolve Claude subagent: {err}")
                    else:
                        try:
                            from pathlib import Path

                            subagent_def = load_codex_subagent(
                                normalized,
                                project_root=Path(working_path),
                            )
                            if subagent_def:
                                subagent_name = subagent_def.name
                                subagent_model = subagent_def.model
                                subagent_reasoning_effort = subagent_def.reasoning_effort
                        except Exception as err:
                            logger.warning(f"Failed to resolve Codex subagent: {err}")

                    if subagent_name:
                        matched_prefix = parsed.name
                        subagent_message = parsed.message

            if subagent_name and subagent_message:
                message = subagent_message
                if agent_name in {"claude", "codex"}:
                    base_session_id = f"{base_session_id}:{subagent_name}"
                    composite_key = f"{base_session_id}:{working_path}"
            elif agent_name in {"claude", "codex"} and routing_agent and not subagent_name:
                # Update session IDs for routing-based agent to match SessionHandler
                base_session_id = f"{base_session_id}:{routing_agent}"
                composite_key = f"{base_session_id}:{working_path}"
                subagent_name = routing_agent
                # Flag the routing-default subagent so the backends' reserved-native
                # resume shortcut treats it like an explicit subagent: this namespaced
                # base has its OWN thread, so resuming the MAIN session's reserved
                # native here would wrongly replay the main transcript under the
                # subagent on the first turn after the subagent is enabled (Codex P2).
                spec = dict(context.platform_specific or {})
                spec["routing_subagent"] = routing_agent
                context.platform_specific = spec

            if agent_name in {"claude", "codex"} and subagent_name:
                spec = dict(context.platform_specific or {})
                spec["backend_base_session_id"] = base_session_id
                spec["backend_composite_session_id"] = composite_key
                context.platform_specific = spec

            if is_human:
                # The concise status bubble (footer-only at turn start) is now
                # posted by AgentService.handle_message after the runtime gate is acquired,
                # so it only appears once this turn truly starts (not while it is
                # queued). See _begin_turn_status there.
                processing_indicator = await self.controller.processing_indicator.start(context, agent_name)

            if is_human and subagent_name and context.message_id:
                try:
                    reaction = SUBAGENT_REACTION_EMOJI
                    await self._get_im_client(context).add_reaction(
                        context,
                        context.message_id,
                        reaction,
                    )
                except Exception as err:
                    logger.debug(f"Failed to add subagent reaction: {err}")
                # Keep 👀 alive; the agent will remove it on result/error
                # via the normal ack_reaction lifecycle. Previously 👀 was
                # removed here immediately, leaving no processing indicator
                # for the entire duration of the subagent run.

            # Process file attachments if present
            processed_files = None
            attachment_errors: List[str] = []
            if context.files:
                processed_files, attachment_errors = await self._process_file_attachments(context, working_path)
                if processed_files:
                    logger.info(f"Processed {len(processed_files)} file attachments for message")

            audio_transcripts = await self._transcribe_audio_attachments(context, processed_files or [])
            if audio_transcripts:
                message = append_audio_transcripts_to_message(message, audio_transcripts)
                await self._echo_audio_transcripts_if_enabled(context, audio_transcripts)

            message = await self._prepend_message_metadata(context, message, include_user_info=is_human)

            message = self._append_attachment_errors(message, attachment_errors)

            if vibe_agent:
                spec = dict(context.platform_specific or {})
                spec["resolved_vibe_agent"] = {
                    "id": vibe_agent.id,
                    "name": vibe_agent.name,
                    "backend": vibe_agent.backend,
                }
                context.platform_specific = spec

            request = self._build_agent_request(
                context=context,
                message=message,
                working_path=working_path,
                base_session_id=base_session_id,
                composite_session_id=composite_key,
                session_key=session_key,
                subagent_name=subagent_name,
                subagent_key=matched_prefix,
                subagent_model=subagent_model,
                subagent_reasoning_effort=subagent_reasoning_effort,
                vibe_agent_id=vibe_agent.id if vibe_agent else None,
                vibe_agent_name=vibe_agent.name if vibe_agent else None,
                vibe_agent_backend=vibe_agent.backend if vibe_agent else None,
                vibe_agent_model=effective_model,
                vibe_agent_reasoning_effort=effective_reasoning_effort,
                vibe_agent_system_prompt=vibe_agent.system_prompt if vibe_agent else None,
                processing_indicator=processing_indicator,
                files=processed_files,
            )
            if processing_indicator is not None:
                self.controller.processing_indicator.apply_to_request(request, processing_indicator)
            try:
                await self.controller.agent_service.handle_message(agent_name, request)
                agent_dispatched = True
                # Back-fill the human prompt's session_id now that dispatch has bound
                # the turn's session. IM inbound is mirrored scope-keyed BEFORE the
                # session PK exists (mirror_inbound runs above, pre-dispatch); the PK
                # now lives on platform_specific['agent_session_id'] — the same field
                # the agent reply uses — so a session's transcript stays complete.
                if is_human and context.platform and context.platform != "avibe" and context.message_id:
                    bound_session_id = (context.platform_specific or {}).get("agent_session_id")
                    if bound_session_id:
                        from core.message_mirror import link_inbound_message_session

                        link_inbound_message_session(
                            platform=context.platform,
                            native_message_id=context.message_id,
                            session_id=str(bound_session_id),
                        )
            except KeyError:
                await self._handle_missing_agent(context, agent_name)
                # Synchronous terminal failure (no agent dispatched). Settle the
                # turn through the OUTBOUND status chokepoint: an empty terminal
                # error result turns the dot red + releases the SSE waiter (the
                # missing-agent message was already shown above). No separate latch.
                await self.controller.emit_agent_message(
                    context,
                    "result",
                    "",
                    is_error=True,
                    output=terminal_output_for(request),
                )
                # Clean up reaction on error
                await self._remove_ack_reaction(context, request)
                return f"agent '{agent_name}' is not available"
            finally:
                if request.ack_message_id:
                    await self._delete_ack(context.channel_id, request)
            return None
        except Exception as e:
            logger.error(f"Error processing user message: {e}", exc_info=True)
            # Clean up reaction on any exception
            try:
                # Use the request once it exists; otherwise finish any indicator
                # selected during pre-dispatch context preparation.
                if request is not None:
                    await self._remove_ack_reaction(context, request)
                elif processing_indicator is not None:
                    await self.controller.processing_indicator.finish(
                        processing_indicator
                    )
            except Exception as cleanup_err:
                logger.debug(f"Failed to clean up reaction on error: {cleanup_err}")
            error_text = self.formatter.format_error(self._t("error.processMessageFailed", error=str(e)))
            await self._get_im_client(context).send_message(context, error_text)
            # Surface the failure into the live web-Chat SSE stream first...
            await self._stream_terminal_error(context, error_text)
            # ...then settle the failed turn through the OUTBOUND status chokepoint:
            # an empty terminal error result turns the dot red + releases the SSE
            # waiter (the visible error was sent + streamed above). No separate latch.
            await self.controller.emit_agent_message(
                context,
                "result",
                "",
                is_error=True,
                output=(
                    terminal_output_for(request)
                    if request is not None
                    else terminal_turn_output()
                ),
            )
            return str(e)
        finally:
            if not agent_dispatched:
                # Synchronous completion — no async agent reply is coming, so
                # release any live streaming SSE waiter for this turn now
                # instead of holding it open until the dispatch safety
                # timeout. No-op for non-streaming (IM/CLI) turns.
                mark_complete = getattr(self.controller, "mark_turn_complete", None)
                if callable(mark_complete):
                    mark_complete(context)

    @staticmethod
    def _build_agent_request(**kwargs: Any) -> AgentRequest:
        try:
            signature = inspect.signature(AgentRequest)
        except (TypeError, ValueError):
            return AgentRequest(**kwargs)
        accepted = {name for name in signature.parameters if name != "self"}
        return AgentRequest(**{key: value for key, value in kwargs.items() if key in accepted})

    async def _transcribe_audio_attachments(
        self,
        context: MessageContext,
        files: List[FileAttachment],
    ) -> List[AudioTranscript]:
        asr_service = getattr(self.controller, "audio_asr_service", None)
        if not files or asr_service is None:
            return []
        refresh_config = getattr(self.controller, "_refresh_config_from_disk", None)
        if callable(refresh_config):
            refresh_config()
        try:
            return await asr_service.transcribe_attachments(files)
        except Exception as err:
            logger.warning(
                "Audio ASR augmentation failed for channel=%s message=%s: %s",
                context.channel_id,
                context.message_id,
                err,
            )
            return []

    async def _echo_audio_transcripts_if_enabled(
        self,
        context: MessageContext,
        transcripts: List[AudioTranscript],
    ) -> None:
        if not transcripts:
            return
        audio_asr_config = getattr(self.config, "audio_asr", None)
        if not getattr(audio_asr_config, "echo_transcript", True):
            return
        echo = format_audio_transcript_echo(
            transcripts,
            single_label=self._t("audio.transcriptEchoSingle"),
            multiple_label=self._t("audio.transcriptEchoMultiple"),
        )
        if not echo:
            return
        try:
            await self._get_im_client(context).send_message(context, echo)
        except Exception as err:
            logger.debug("Failed to echo audio transcript: %s", err, exc_info=True)

    @staticmethod
    def _sanitize_identity(value: str) -> str:
        """Strip control chars and delimiters that could break the [name<id>] format."""
        token = (value or "").replace("\n", " ").replace("\r", " ").strip()
        token = token.replace("[", "(").replace("]", ")").replace("<", "(").replace(">", ")")
        return token[:80] or "unknown"

    async def _prepend_user_info(self, context: MessageContext, message: str) -> str:
        """Prepend user identity as [username<user_id>] to the message."""
        user_info_line = await self._build_user_info_line(context)
        return f"{user_info_line}\n{message}"

    async def _prepend_message_metadata(
        self,
        context: MessageContext,
        message: str,
        *,
        include_user_info: bool,
    ) -> str:
        """Prepend configured per-turn metadata lines to the agent message."""
        metadata_lines: list[str] = []
        if getattr(self.config, "include_time_info", True):
            metadata_lines.append(self._build_current_time_line())
        if include_user_info and getattr(self.config, "include_user_info", True):
            metadata_lines.append(await self._build_user_info_line(context))

        if not metadata_lines:
            return message
        return "\n".join([*metadata_lines, message])

    @staticmethod
    def _build_current_time_line(now: datetime | None = None) -> str:
        """Return the current local time with seconds and UTC offset."""
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.astimezone()
        offset = current.strftime("%z")
        if len(offset) == 5:
            offset = f"{offset[:3]}:{offset[3:]}"
        return f"[Current Time: {current.strftime('%Y-%m-%d %H:%M:%S')} UTC{offset}]"

    async def _build_user_info_line(self, context: MessageContext) -> str:
        """Return user identity as [username<user_id>]."""
        try:
            user_info = await self._get_im_client(context).get_user_info(context.user_id)
            raw_name = self._resolve_user_display_name(user_info, context.user_id)
        except Exception as e:
            logger.debug(f"Failed to fetch user info for {context.user_id}: {e}")
            raw_name = context.user_id
        name = self._sanitize_identity(raw_name)
        uid = self._sanitize_identity(context.user_id)
        return f"[{name}<{uid}>]"

    @staticmethod
    def _get_control_message(context: MessageContext, message: str) -> str:
        payload = context.platform_specific or {}
        control_text = payload.get("control_text")
        if isinstance(control_text, str):
            return control_text
        return message

    async def handle_callback_query(self, context: MessageContext, callback_data: str):
        """Route callback queries to appropriate handlers"""
        try:
            logger.info(f"handle_callback_query called with data: {callback_data} for user {context.user_id}")
            im_client = self._get_im_client(context)

            settings_handler = self.controller.settings_handler
            command_handlers = self.controller.command_handler

            # Route based on callback data
            # Note: admin permission for protected callbacks is enforced by
            # the centralized auth pipeline (core.auth.check_auth) in IM
            # entry points before reaching this handler.
            if callback_data.startswith("toggle_msg_"):
                # Toggle message type visibility
                msg_type = callback_data.replace("toggle_msg_", "")
                await settings_handler.handle_toggle_message_type(context, msg_type)
            elif callback_data.startswith("toggle_"):
                # Legacy toggle handler (if any)
                setting_type = callback_data.replace("toggle_", "")
                handler = getattr(settings_handler, "handle_toggle_setting", None)
                if handler:
                    await handler(context, setting_type)

            elif callback_data == "info_msg_types":
                logger.info(f"Handling info_msg_types callback for user {context.user_id}")
                await settings_handler.handle_info_message_types(context)

            elif callback_data == "info_how_it_works":
                await settings_handler.handle_info_how_it_works(context)

            elif callback_data == "cmd_cwd":
                await command_handlers.handle_cwd(context)

            elif callback_data == "cmd_change_cwd":
                await command_handlers.handle_change_cwd_modal(context)

            elif callback_data in {"cmd_new", "cmd_clear"}:
                await command_handlers.handle_new(context)

            elif callback_data == "cmd_resume":
                await command_handlers.handle_resume(context)

            elif callback_data.startswith("auth_setup:"):
                await self.controller.agent_auth_service.handle_setup_callback(context, callback_data)

            elif callback_data == "cmd_settings":
                await settings_handler.handle_settings(context)

            elif callback_data == "cmd_routing":
                await settings_handler.handle_routing(context)

            elif callback_data.startswith("vibe_update_now"):
                # Discord update button handler
                target_version = None
                if ":" in callback_data:
                    target_version = callback_data.split(":", 1)[1] or None
                if hasattr(self.controller, "update_checker"):
                    await self.controller.update_checker.handle_update_button_click(context, target_version)
                else:
                    await im_client.send_message(
                        context,
                        self.formatter.format_warning(self._t("error.updateUnavailable")),
                    )

            elif callback_data.startswith("info_") and callback_data != "info_msg_types":
                # Generic info handler
                info_type = callback_data.replace("info_", "")
                info_text = self.formatter.format_info_message(
                    title=self._t("info.genericTitle", topic=info_type),
                    emoji="ℹ️",
                    footer=self._t("info.genericFooter"),
                )
                await im_client.send_message(context, info_text)

            elif callback_data.startswith("resume_session:"):
                # Feishu resume button: resume_session:{agent}:{session_id}
                parts = callback_data.split(":", 2)
                agent = parts[1] if len(parts) > 1 else None
                session_id = parts[2] if len(parts) > 2 else None
                await self.controller.session_handler.handle_resume_session_submission(
                    user_id=context.user_id,
                    channel_id=context.channel_id,
                    thread_id=context.thread_id,
                    agent=agent,
                    session_id=session_id,
                    is_dm=(context.platform_specific or {}).get("is_dm", False),
                    platform=context.platform or (context.platform_specific or {}).get("platform"),
                )

            elif callback_data.startswith("opencode_question:"):
                logger.info("Ignoring legacy OpenCode question callback because the question tool is disabled")

            elif callback_data.startswith("claude_question:"):
                if not self.session_handler:
                    raise RuntimeError("Session handler not initialized")

                base_session_id, working_path, composite_key = self.session_handler.get_session_info(context)
                session_key = self._get_session_key(context)
                request = AgentRequest(
                    context=context,
                    message=callback_data,
                    working_path=working_path,
                    base_session_id=base_session_id,
                    composite_session_id=composite_key,
                    session_key=session_key,
                )
                await self.controller.agent_service.handle_message("claude", request)

            elif callback_data.startswith("quick_reply:"):
                # Quick-reply button: treat the button text as a new user message
                reply_text = callback_data[len("quick_reply:") :]
                if reply_text:
                    # Remove buttons from the original message card.
                    remove_target_message_id = context.message_id
                    platform_payload_raw = context.platform_specific or {}
                    platform_payload = platform_payload_raw if isinstance(platform_payload_raw, dict) else {}
                    can_remove_via_interaction = bool(platform_payload.get("interaction"))
                    if not remove_target_message_id:
                        event_payload = platform_payload.get("event")
                        event_payload = event_payload if isinstance(event_payload, dict) else {}
                        event_context = event_payload.get("context")
                        event_context = event_context if isinstance(event_context, dict) else {}
                        event_open_message_id = (
                            event_payload.get("open_message_id") if isinstance(event_payload, dict) else ""
                        )
                        remove_target_message_id = (
                            platform_payload.get("message_id")
                            or platform_payload.get("open_message_id")
                            or event_context.get("open_message_id")
                            or event_open_message_id
                            or ""
                        )
                    try:
                        if remove_target_message_id or can_remove_via_interaction:
                            await im_client.remove_inline_keyboard(context, remove_target_message_id or "")
                        else:
                            logger.debug("Skip quick-reply keyboard removal: message id unavailable")
                    except Exception as err:
                        logger.debug(f"Failed to remove quick-reply buttons: {err}")

                    # Echo the selected quick reply as a bot message.
                    quick_reply_echo_id = None
                    try:
                        quick_reply_echo = self._t("message.quickReplyNote", text=reply_text)
                        quick_reply_echo_id = await im_client.send_message(
                            self.controller.processing_indicator.target_context(context),
                            quick_reply_echo,
                        )
                    except Exception as err:
                        logger.debug(f"Failed to send quick-reply echo message: {err}")

                    # Dispatch as a normal user message with message_id=None to
                    # bypass platform event dedup.  The echo message remains
                    # available as the processing-indicator reaction target.
                    reply_payload = dict(context.platform_specific or {})
                    if quick_reply_echo_id:
                        reply_payload["processing_indicator_message_id"] = quick_reply_echo_id
                    context_for_reply = MessageContext(
                        user_id=context.user_id,
                        channel_id=context.channel_id,
                        platform=context.platform or (context.platform_specific or {}).get("platform"),
                        thread_id=context.thread_id,
                        message_id=None,
                        platform_specific=reply_payload or None,
                    )
                    await self.handle_user_message(context_for_reply, reply_text)

            else:
                logger.warning(f"Unknown callback data: {callback_data}")
                await im_client.send_message(
                    context,
                    self.formatter.format_warning(self._t("error.unknownAction", action=callback_data)),
                )

        except Exception as e:
            logger.error(f"Error handling callback query: {e}", exc_info=True)
            await self._get_im_client(context).send_message(
                context,
                self.formatter.format_error(self._t("error.processActionFailed", error=str(e))),
            )

    async def _handle_inline_stop(self, context: MessageContext) -> bool:
        """Route inline 'stop' messages to the active agent."""
        try:
            if not self.session_handler:
                raise RuntimeError("Session handler not initialized")

            base_session_id, working_path, composite_key = self.session_handler.get_session_info(context)
            session_key = self._get_session_key(context)
            agent_name = self.controller.resolve_agent_for_context(context)
            request = AgentRequest(
                context=context,
                message="stop",
                working_path=working_path,
                base_session_id=base_session_id,
                composite_session_id=composite_key,
                session_key=session_key,
            )
            try:
                handled = await self.controller.agent_service.handle_stop(agent_name, request)
            except KeyError:
                await self._handle_missing_agent(context, agent_name)
                return False
            if not handled:
                await self._get_im_client(context).send_message(context, f"ℹ️ {self._t('command.stop.noActiveSession')}")
            return handled
        except Exception as e:
            logger.error(f"Error handling inline stop: {e}", exc_info=True)
            return False

    async def _handle_missing_agent(self, context: MessageContext, agent_name: str):
        """Notify user when a requested agent backend is unavailable."""
        target = agent_name or self.controller.agent_service.default_agent
        backend = self._missing_agent_backend(context, target)
        display_backend = display_name_for_backend(backend) if backend else str(target)
        hint_key = f"error.agentNotConfiguredHint.{backend}" if backend else "error.agentNotConfiguredHint.generic"
        hint = self._t(hint_key)
        msg = f"❌ {self._t('error.agentNotConfigured', agent=target, backend=display_backend, hint=hint)}"
        await self._get_im_client(context).send_message(context, msg)
        await self._stream_terminal_error(context, msg)

    def _missing_agent_backend(self, context: MessageContext, target: str) -> Optional[str]:
        payload = context.platform_specific or {}
        resolved = payload.get("resolved_vibe_agent")
        if isinstance(resolved, dict) and is_agent_backend(str(resolved.get("backend") or "")):
            return str(resolved["backend"])
        run_target = payload.get("agent_run_target") or payload.get("agent_session_target")
        if isinstance(run_target, dict) and is_agent_backend(str(run_target.get("agent_backend") or "")):
            return str(run_target["agent_backend"])
        if is_agent_backend(str(target)):
            return str(target)
        try:
            agent = self.controller.vibe_agent_store.get(str(target))
        except Exception:
            agent = None
        backend = getattr(agent, "backend", None)
        return str(backend) if is_agent_backend(str(backend)) else None

    async def _stream_terminal_error(self, context: MessageContext, text: str) -> None:
        """Surface a synchronous, no-agent-dispatched failure (missing backend,
        a pre-dispatch exception) into the web Chat so the browser shows it
        instead of silently ending the turn with only the user's prompt visible.

        The default Chat send path is now fire-and-forget and renders only
        durable ``message.new`` rows, so we PERSIST the failure as a row (it
        surfaces over the session stream + the inbox). We still forward it to any
        live legacy ``?stream=1`` sink via ``_stream_chunk`` (no-op otherwise).
        """
        try:
            from core.message_mirror import persist_agent_message

            # Persisted as ``notify`` → renders as a status box, not an answer;
            # publishes message.new so the async send path surfaces it.
            persist_agent_message(context, "notify", text)
        except Exception:
            logger.debug("failed to persist terminal error row", exc_info=True)
        try:
            from core.message_dispatcher import _stream_chunk

            await _stream_chunk(self.controller, context, text=text, message_id=None, kind="error")
        except Exception:
            logger.debug("failed to stream terminal error chunk", exc_info=True)

    async def _delete_ack(self, channel_id: str, request: AgentRequest):
        """Delete acknowledgement message if it still exists."""
        await self.controller.processing_indicator.delete_ack_message(request, channel_id=channel_id)

    async def _remove_ack_reaction(self, context: MessageContext, request: AgentRequest):
        """Remove acknowledgement reaction / typing indicator if it still exists."""
        await self.controller.processing_indicator.finish(request)

    async def _process_file_attachments(
        self, context: MessageContext, working_path: str
    ) -> Tuple[Optional[List[FileAttachment]], List[str]]:
        """Download and process file attachments from the message.

        All files (including images) are saved to ~/.vibe_remote/attachments/{channel_id}/
        to avoid polluting the working directory (which is often a git repo).
        The agent can then use Read tools to access them.

        Args:
            context: Message context with file attachments
            working_path: Working directory path (not used for storage, kept for API compat)

        Returns:
            Tuple of processed attachments and download error messages
        """
        import os
        import time
        from config.paths import get_attachments_dir
        from modules.im.base import FileAttachment, FileDownloadResult

        if not context.files:
            return None, []

        # Create channel-specific attachments directory
        # Path: ~/.vibe_remote/attachments/{channel_id}/
        attachments_dir = get_attachments_dir() / context.channel_id
        attachments_dir.mkdir(parents=True, exist_ok=True)

        processed = []
        errors: List[str] = []
        for attachment in context.files:
            if not isinstance(attachment, FileAttachment):
                continue

            # Already on local disk (e.g. an avibe workbench upload, saved by the
            # UI server before dispatch) — there is nothing to download, so pass
            # it straight through to the agent turn. IM attachments arrive with a
            # ``url`` and no ``local_path`` and fall through to the download path.
            if attachment.local_path and os.path.isfile(attachment.local_path):
                if attachment.size is None:
                    try:
                        attachment.size = os.path.getsize(attachment.local_path)
                    except OSError:
                        pass
                processed.append(attachment)
                continue

            try:
                im_client = self._get_im_client(context)
                # Download the file content. Some platforms receive a thin
                # attachment event first and resolve the actual URL from
                # platform metadata such as a Slack file id.
                can_download = hasattr(im_client, "download_file_to_path") or hasattr(im_client, "download_file")
                if can_download:
                    # Platform-agnostic download info dict
                    file_info = {
                        "url": attachment.url,
                        "name": attachment.name,
                        "size": attachment.size,
                        "platform": context.platform,
                    }
                    if attachment.url:
                        file_info["url_private_download"] = attachment.url  # Slack compat
                    attachment_data = getattr(attachment, "__dict__", {})
                    for key, value in attachment_data.items():
                        if key in {"name", "mimetype", "url", "content", "local_path", "size"}:
                            continue
                        file_info[key] = value
                    timestamp = int(time.time())
                    safe_name = self._sanitize_filename(attachment.name)
                    filename = f"{timestamp}_{safe_name}"
                    local_path = attachments_dir / filename
                    temp_path = attachments_dir / f"{filename}.part"
                    content = None
                    detected_sample = None
                    content_size = None

                    if hasattr(im_client, "download_file_to_path"):
                        self._cleanup_partial_attachment(temp_path)
                        result = await im_client.download_file_to_path(file_info, str(temp_path))
                        if not isinstance(result, FileDownloadResult):
                            result = FileDownloadResult(bool(result), None if result else "Download failed")

                        if result.success:
                            os.replace(temp_path, local_path)
                            content_size = local_path.stat().st_size
                            with open(local_path, "rb") as file_obj:
                                detected_sample = file_obj.read(AUDIO_SIGNATURE_SAMPLE_BYTES)
                        else:
                            self._cleanup_partial_attachment(temp_path)
                            error_text = result.error or "Download failed"
                            logger.warning("Failed to download file %s: %s", attachment.name, error_text)
                            errors.append(f"Attachment '{attachment.name}' could not be downloaded: {error_text}")
                    else:
                        content = await im_client.download_file(file_info)
                        if content:
                            with open(local_path, "wb") as f:
                                f.write(content)
                            content_size = len(content)
                            detected_sample = content[:AUDIO_SIGNATURE_SAMPLE_BYTES]
                        else:
                            logger.warning("Failed to download file %s: download returned no content", attachment.name)
                            errors.append(
                                f"Attachment '{attachment.name}' could not be downloaded: Download returned no content"
                            )

                    if content is not None or content_size is not None:
                        # Detect actual MIME type from magic bytes for media
                        # (some platforms don't provide accurate MIME, e.g. Feishu and Slack)
                        detected = self._detect_image_mime(detected_sample or b"")
                        if not detected:
                            detected = detect_audio_mime_from_sample(detected_sample or b"")
                        if detected:
                            attachment.mimetype = detected[0]
                            # Fix filename extension to match actual type
                            ext = detected[1]
                            base = os.path.splitext(attachment.name)[0]
                            attachment.name = f"{base}{ext}"

                        attachment.local_path = str(local_path)
                        attachment.size = content_size

                        # Determine file type for logging
                        is_image = (attachment.mimetype or "").startswith("image/")
                        file_type = "image" if is_image else "file"

                        logger.info(f"Saved {file_type} '{attachment.name}' ({content_size} bytes) to '{local_path}'")

                        processed.append(attachment)
                    else:
                        logger.warning(f"Failed to download file: {attachment.name}")
                else:
                    logger.warning(f"Cannot download file: {attachment.name} (no URL or download method)")
                    errors.append(f"Attachment '{attachment.name}' could not be downloaded: No URL or download method")

            except Exception as e:
                self._cleanup_partial_attachment(locals().get("temp_path"))
                logger.error(f"Error processing file attachment {attachment.name}: {e}")
                errors.append(f"Attachment '{attachment.name}' could not be downloaded: {e}")
                continue

        return (processed if processed else None), errors

    @staticmethod
    def _cleanup_partial_attachment(path) -> None:
        if not path:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception as err:
            logger.debug("Failed to remove partial attachment %s: %s", path, err)

    @staticmethod
    def _append_attachment_errors(message: str, errors: List[str]) -> str:
        if not errors:
            return message

        error_block = "\n".join(["[Attachment Download Errors]", *[f"- {error}" for error in errors]])
        if not message or not message.strip():
            return error_block
        return f"{message}\n\n{error_block}"

    def _detect_image_mime(self, data: bytes) -> Optional[tuple]:
        """Detect image MIME type from magic bytes.

        Returns:
            (mimetype, extension) tuple if recognized image, else None.
        """
        if len(data) < 12:
            return None
        if data[:3] == b"\xff\xd8\xff":
            return ("image/jpeg", ".jpg")
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ("image/png", ".png")
        if data[:4] == b"GIF8":
            return ("image/gif", ".gif")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ("image/webp", ".webp")
        if data[:2] == b"BM":
            return ("image/bmp", ".bmp")
        return None

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to be safe for filesystem.

        Args:
            filename: Original filename

        Returns:
            Sanitized filename safe for filesystem
        """
        import re

        # Remove or replace dangerous characters
        # Keep alphanumeric, dots, hyphens, underscores
        safe = re.sub(r"[^\w\-.]", "_", filename)
        # Prevent directory traversal
        safe = safe.replace("..", "_")
        # Limit length
        if len(safe) > 200:
            base, ext = safe.rsplit(".", 1) if "." in safe else (safe, "")
            safe = base[:195] + ("." + ext if ext else "")
        return safe or "unnamed_file"
