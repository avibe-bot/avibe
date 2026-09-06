"""Message routing and Agent communication handlers"""

import asyncio
import logging
import inspect
from typing import Any, List, Optional, Tuple

from core.audio_asr import (
    AUDIO_SIGNATURE_SAMPLE_BYTES,
    AudioTranscript,
    append_audio_transcripts_to_message,
    detect_audio_mime_from_sample,
    format_audio_transcript_echo,
)
from core.agent_input import AgentInputMetadata
from core.backend_failure import emit_backend_failure
from core.message_output import HARNESS_PROMPT_ECHO_SPEC_KEY
from core.memory_adapter import TurnAccepted, snapshot_memory_files
from core.message_context import (
    resolve_context_thread_id,
)
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    set_dispatch_phase,
)
from modules.agents.base import AgentRequest
from modules.agents.catalog import display_name_for_backend, is_agent_backend
from modules.im import MessageContext
from modules.im.base import FileAttachment

from .base import BaseHandler

logger = logging.getLogger(__name__)

SUBAGENT_REACTION_EMOJI = "🤖"


def memory_turn_event(
    context: MessageContext,
    text: str,
    session_id: str,
    lifecycle_snapshot: object,
    attachment_lease: object = None,
    sender_name: str | None = None,
) -> TurnAccepted:
    """Close one live message context into immutable host-owned facts."""

    payload = (
        context.platform_specific
        if isinstance(context.platform_specific, dict)
        else {}
    )
    platform = context.platform or payload.get("platform")
    user_id = payload.get("author_id") if platform == "avibe" else context.user_id
    return TurnAccepted(
        platform=platform,
        user_id=user_id,
        message_id=context.message_id,
        session_id=session_id,
        text=text,
        files=snapshot_memory_files(context.files),
        is_dm=payload.get("is_dm") is True,
        is_ordinary_text=context.is_original_human_text,
        is_ordinary_attachment=context.is_original_human_attachment,
        lifecycle_snapshot=lifecycle_snapshot,
        attachment_lease=attachment_lease,
        sender_name=sender_name,
    )


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

    async def handle_user_message(
        self,
        context: MessageContext,
        message: str,
        *,
        lifecycle_snapshot: object | None = None,
    ):
        """Process regular human-originated messages and route to configured agent."""
        await self._handle_turn(
            context,
            message,
            source=self.TURN_SOURCE_HUMAN,
            lifecycle_snapshot=lifecycle_snapshot,
        )

    async def handle_scheduled_message(
        self,
        context: MessageContext,
        message: str,
        parsed_session_key=None,
        *,
        lifecycle_snapshot: object | None = None,
    ):
        """Process a scheduler-originated turn through the shared turn pipeline."""
        if parsed_session_key is not None:
            payload = dict(context.platform_specific or {})
            payload["parsed_session_key"] = parsed_session_key
            context.platform_specific = payload
        return await self._handle_turn(
            context,
            message,
            source=self.TURN_SOURCE_SCHEDULED,
            lifecycle_snapshot=lifecycle_snapshot,
        )

    async def _prepare_turn_context(self, context: MessageContext, source: str) -> MessageContext:
        payload = dict(context.platform_specific or {})
        payload["turn_source"] = source
        context.platform_specific = payload
        prepared = await self._get_im_client(context).prepare_turn_context(context, source)
        prepared_payload = dict(prepared.platform_specific or {})
        prepared_payload["turn_source"] = source
        prepared.platform_specific = prepared_payload
        return prepared

    @staticmethod
    def _processed_message_dedup_keys(
        context: MessageContext,
        thread_id: str,
        message_id: str,
    ) -> tuple[str, str, str]:
        """Namespace native event ids before claiming the cross-platform dedup record."""

        payload = context.platform_specific if isinstance(context.platform_specific, dict) else {}
        platform = context.platform or payload.get("platform")
        if not isinstance(platform, str) or not platform:
            return str(context.channel_id), thread_id, message_id
        prefix = f"im:{platform}:"
        return f"{prefix}{context.channel_id}", f"{prefix}{thread_id}", f"{prefix}{message_id}"

    def _claimed_before_dedup_namespacing(
        self,
        dedup_keys: tuple[str, str, str],
        legacy_keys: tuple[str, str, str],
    ) -> bool:
        """Honor dedup rows a previous version wrote without the platform prefix.

        A runtime that processed an IM event before namespaced keys shipped stored
        the raw native ids. When the platform redelivers such an event after the
        upgrade — a Slack event that ran but was never acked, a Socket Mode
        reconnect — the namespaced lookup misses and the turn would be dispatched
        a second time. This check is read-only: the claim below always uses the
        namespaced key, so legacy rows are neither written nor migrated. They age
        out with the existing per-thread dedup retention.
        """

        if dedup_keys == legacy_keys:
            return False
        checker = getattr(self.sessions, "is_message_already_processed", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(*legacy_keys))
        except Exception:
            logger.debug(
                "Legacy dedup lookup failed; continuing with the namespaced claim",
                exc_info=True,
            )
            return False

    async def _handle_turn(
        self,
        context: MessageContext,
        message: str,
        *,
        source: str,
        lifecycle_snapshot: object | None = None,
    ) -> Optional[str]:
        """Shared turn-processing pipeline used by both human and scheduled turns."""
        processing_indicator = None
        request: AgentRequest | None = None
        dispatch_evidence = set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        # Tracks whether we actually dispatched an agent turn (whose reply
        # streams in asynchronously). If we leave this method WITHOUT having
        # dispatched — early returns, missing/disabled backend, errors — no
        # async result is coming, so the ``finally`` releases any streaming SSE
        # waiter for this turn instead of leaving it open until the timeout.
        agent_dispatched = False
        attachment_lease = None
        try:
            is_human = source == self.TURN_SOURCE_HUMAN
            durable_delivery_owned = bool(
                (context.platform_specific or {}).get("delivery_ids")
            )
            delivery_manager = getattr(self.controller, "session_turns", None)
            durable_ingress_enabled = bool(
                is_human
                and callable(getattr(delivery_manager, "deliver", None))
            )
            control_message = self._get_control_message(context, message) if is_human else message

            # Record user activity for auto-update idle detection
            if is_human and hasattr(self.controller, "update_checker"):
                self.controller.update_checker.record_activity()

            if (
                durable_ingress_enabled
                and not durable_delivery_owned
                and self._is_duplicate_human_delivery(context)
            ):
                return None

            # If message is empty AND no files attached (e.g., user just @mentioned bot without text),
            # trigger the /start command instead of sending empty message to agent
            has_files = bool(context.files)
            if (not message or not message.strip()) and not has_files:
                if is_human:
                    await self.controller.command_handler.handle_start(context, "")
                return None

            if (
                is_human
                and not durable_delivery_owned
                and not durable_ingress_enabled
            ):
                if not self._claim_native_human_event(context):
                    return None

            if is_human and not durable_delivery_owned and not has_files:
                maybe_consume_setup_reply = getattr(self.controller.agent_auth_service, "maybe_consume_setup_reply", None)
                if callable(maybe_consume_setup_reply):
                    kwargs = {}
                    if durable_ingress_enabled:
                        kwargs["claim_native_event"] = lambda: self._claim_native_human_event(
                            context
                        )
                    consumed = await maybe_consume_setup_reply(
                        context,
                        control_message,
                        **kwargs,
                    )
                    if consumed:
                        return None

            # Skip automatic cleanup; receiver tasks are retained until shutdown

            # Allow "stop" shortcut inside Slack threads
            active_thread_id = resolve_context_thread_id(context) or context.thread_id
            if (
                is_human
                and not durable_delivery_owned
                and active_thread_id
                and control_message.strip().lower() in ["stop", "/stop"]
            ):
                if durable_ingress_enabled and not self._claim_native_human_event(
                    context
                ):
                    return None
                if await self._handle_inline_stop(context):
                    return None

            if not self.session_handler:
                raise RuntimeError("Session handler not initialized")

            context = await self._prepare_turn_context(context, source)
            set_dispatch_phase(
                context,
                DISPATCH_PHASE_PREWRITE,
                evidence=dispatch_evidence,
            )

            # Durable Deliveries materialize their Message only after exact native
            # acceptance. Legacy direct turns still mirror at this boundary.
            if not durable_delivery_owned and not durable_ingress_enabled:
                if source == self.TURN_SOURCE_HUMAN:
                    from core.message_mirror import mirror_inbound

                    mirror_inbound(context, control_message)
                else:
                    from core.message_mirror import mirror_harness_inbound

                    mirror_harness_inbound(context, message)

            base_session_id, working_path, composite_key = self.session_handler.get_session_info(context, source=source)
            capture_session_id = base_session_id
            capture_lifecycle_snapshot: object | None = None
            capture_sender_name = None
            if is_human:
                sender_name_for_context = getattr(self.controller, "memory_sender_name_for_context", None)
                if callable(sender_name_for_context):
                    capture_sender_name = await sender_name_for_context(context)
                capture_lifecycle_snapshot = lifecycle_snapshot
                if capture_lifecycle_snapshot is None:
                    snapshot = getattr(
                        getattr(self.controller, "session_turns", None),
                        "snapshot_session_lifecycle",
                        None,
                    )
                    capture_lifecycle_snapshot = (
                        snapshot(capture_session_id) if callable(snapshot) else 0
                    )
            lifecycle_snapshot = None
            payload = dict(context.platform_specific or {})
            payload["turn_source"] = source
            payload["turn_base_session_id"] = base_session_id
            payload["scheduled_anchor_required"] = self.session_handler.should_allocate_scheduled_anchor(
                context, source=source
            )
            context.platform_specific = payload

            # Text-only turns keep the original early capture path. Attachment
            # turns defer only until the shared materializer has produced a
            # descriptor-backed lease.
            if is_human and not context.files:
                self.controller.memory_adapter.offer(
                    memory_turn_event(
                        context,
                        control_message,
                        capture_session_id,
                        capture_lifecycle_snapshot,
                        sender_name=capture_sender_name,
                    )
                )
                capture_lifecycle_snapshot = None

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
            requested_vibe_agent_id = platform_payload.get("vibe_agent_id")
            session_target = platform_payload.get("agent_session_target")
            scope_agent_name = getattr(routing, "agent_name", None) if routing else None
            durable_agent_identity = bool(requested_vibe_agent_id or scope_agent_name)
            if not requested_vibe_agent_id and isinstance(session_target, dict):
                requested_vibe_agent_id = session_target.get("agent_id")
            if not requested_vibe_agent and isinstance(session_target, dict):
                requested_vibe_agent = session_target.get("agent_name")
            if isinstance(session_target, dict) and (
                session_target.get("agent_id") or session_target.get("agent_name")
            ):
                durable_agent_identity = True
            if not requested_vibe_agent:
                requested_vibe_agent = resolved_target.get("agent_name")
            if not requested_vibe_agent_id:
                requested_vibe_agent_id = resolved_target.get("agent_id")
            if resolved_target and (
                resolved_target.get("agent_id") or resolved_target.get("agent_name")
            ):
                durable_agent_identity = True
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
                    existing_agent_id = existing_thread.get("agent_id")
                    existing_agent_name = existing_thread.get("agent_name")
                    requested_vibe_agent_id = existing_agent_id or requested_vibe_agent_id
                    requested_vibe_agent = existing_agent_name or requested_vibe_agent
                    if existing_agent_id or existing_agent_name:
                        durable_agent_identity = True
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
            if (requested_vibe_agent_id or requested_vibe_agent) and callable(resolve_vibe_agent):
                resolve_kwargs = {
                    "override_agent_name": requested_vibe_agent,
                    "required": durable_agent_identity,
                }
                if requested_vibe_agent_id:
                    resolve_kwargs["override_agent_id"] = requested_vibe_agent_id
                vibe_agent = resolve_vibe_agent(context, **resolve_kwargs)
            elif callable(resolve_vibe_agent) and (
                durable_agent_identity or not session_agent_backend
            ):
                vibe_agent = resolve_vibe_agent(context, required=durable_agent_identity)
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
            materialized_agent_identity = bool(
                vibe_agent
                and isinstance(session_target, dict)
                and not session_target.get("agent_id")
                and not session_target.get("agent_name")
            )
            # A session may pin a setting to NOTHING on purpose. The cascade above
            # cannot express that: every `or` reads NULL as "inherit", which is the
            # correct reading for the whole existing table and the WRONG one for a
            # preserved `create_once` rebind, whose whole point is that the session
            # it replaced pinned no model and must keep pinning none (D3). Only
            # sessions carrying the explicit-override marker are re-read here, so
            # the global meaning of NULL is untouched -- reinterpreting it would
            # silently re-route every session that is merely inheriting.
            explicit_overrides: set[str] = set()
            if isinstance(session_target, dict):
                from storage.session_reclaim import explicit_override_names

                explicit_overrides = explicit_override_names(session_target.get("metadata"))
            if "model" in explicit_overrides:
                effective_model = session_target.get("model")
            if "reasoning_effort" in explicit_overrides:
                effective_reasoning_effort = session_target.get("reasoning_effort")
            # Materialize the resolved route into EMPTY Workbench session
            # columns at turn start. A session created on an inherited default
            # carries NULLs (dispatch resolves the live Agent default); without
            # pinning, the chat header shows an Agent with no model / effort
            # after the first message. Scheduled IM turns can carry the same
            # target projection, so the platform gate is essential: their model
            # semantics remain owned by channel routing.
            if (
                context.platform == "avibe"
                and isinstance(session_target, dict)
                and session_target.get("id")
                and (
                    materialized_agent_identity
                    or (effective_model and not session_target.get("model"))
                    or (effective_reasoning_effort and not session_target.get("reasoning_effort"))
                )
            ):
                materialize = getattr(self.sessions, "materialize_agent_session_route", None)
                if callable(materialize):
                    try:
                        materialize(
                            str(session_target["id"]),
                            agent_id=vibe_agent.id if materialized_agent_identity else None,
                            agent_name=vibe_agent.name if materialized_agent_identity else None,
                            model=effective_model,
                            reasoning_effort=effective_reasoning_effort,
                            expected_route={
                                "agent_id": session_target.get("agent_id"),
                                "agent_name": session_target.get("agent_name"),
                                "agent_backend": session_target.get("agent_backend"),
                                "agent_variant": session_target.get("agent_variant"),
                                "model": session_target.get("model"),
                                "reasoning_effort": session_target.get("reasoning_effort"),
                                "explicit_overrides": sorted(explicit_overrides),
                            },
                        )
                    except Exception:
                        logger.debug("Session route materialization failed; dispatch continues", exc_info=True)

            matched_prefix = None
            subagent_message = None
            subagent_name = None
            subagent_model = None
            subagent_reasoning_effort = None
            delivery_context = platform_payload.get("delivery_admission_context")
            restored_route = (
                delivery_context.get("message_handler_route")
                if durable_delivery_owned and isinstance(delivery_context, dict)
                else None
            )
            restored_route = restored_route if isinstance(restored_route, dict) else None

            if durable_delivery_owned:
                self._restore_reaction_target(context, delivery_context)

            if restored_route is not None:
                base_session_id = str(
                    restored_route.get("base_session_id") or base_session_id
                )
                composite_key = str(
                    restored_route.get("composite_session_id") or composite_key
                )
                subagent_name = restored_route.get("subagent_name") or None
                matched_prefix = restored_route.get("subagent_key") or None
                subagent_model = restored_route.get("subagent_model") or None
                subagent_reasoning_effort = (
                    restored_route.get("subagent_reasoning_effort") or None
                )
                if restored_route.get("routing_subagent"):
                    spec = dict(context.platform_specific or {})
                    spec["routing_subagent"] = subagent_name
                    context.platform_specific = spec
            elif agent_name in ["opencode", "claude", "codex"]:
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

            if restored_route is None:
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

            from core.message_priority import delivery_intent_for_trigger

            delivery_intent = delivery_intent_for_trigger("im")
            if (
                restored_route is None
                and agent_name == "opencode"
                and subagent_name
                and subagent_message is not None
            ):
                # OpenCode subagents share the main Session anchor. They need a
                # fresh Turn so the persisted route can select the requested
                # native subagent instead of text-steering the active main Turn.
                delivery_intent = "queue"

            if agent_name in {"claude", "codex"} and subagent_name:
                spec = dict(context.platform_specific or {})
                spec["backend_base_session_id"] = base_session_id
                spec["backend_composite_session_id"] = composite_key
                context.platform_specific = spec

            # Resolve remote attachments before admission so a queued Delivery
            # owns stable local media references and can survive a restart.
            processed_files = None
            attachment_errors: List[str] = []
            downloaded_attachment_paths: list[str] = []
            if context.files:
                existing_local_paths = {
                    str(attachment.local_path)
                    for attachment in context.files
                    if isinstance(attachment, FileAttachment)
                    and attachment.local_path
                }
                try:
                    attachment_batch = await self._materialize_file_attachments(
                        context,
                        working_path,
                    )
                except Exception:
                    if is_human:
                        self.controller.memory_adapter.offer(
                            memory_turn_event(
                                context,
                                control_message,
                                capture_session_id,
                                capture_lifecycle_snapshot,
                                sender_name=capture_sender_name,
                            )
                        )
                    raise
                attachment_lease = attachment_batch.lease
                processed_files = list(attachment_batch.attachments) or None
                attachment_errors = list(attachment_batch.display_errors)
                if processed_files:
                    downloaded_attachment_paths = [
                        str(attachment.local_path)
                        for attachment in processed_files
                        if isinstance(attachment, FileAttachment)
                        and attachment.local_path
                        and str(attachment.local_path) not in existing_local_paths
                    ]
                    logger.info(
                        "Processed %s file attachments for message",
                        len(processed_files),
                    )

            if is_human and context.files:
                self.controller.memory_adapter.offer(
                    memory_turn_event(
                        context,
                        control_message,
                        capture_session_id,
                        capture_lifecycle_snapshot,
                        attachment_lease,
                        sender_name=capture_sender_name,
                    )
                )
                capture_lifecycle_snapshot = None

            if durable_ingress_enabled and not durable_delivery_owned:
                admitted = await self._admit_human_delivery(
                    manager=delivery_manager,
                    context=context,
                    dispatch_text=self._append_attachment_errors(
                        message,
                        attachment_errors,
                    ),
                    display_text=control_message,
                    processed_files=processed_files or [],
                    session_key=session_key,
                    agent_name=agent_name,
                    session_anchor=base_session_id,
                    working_path=working_path,
                    vibe_agent=vibe_agent,
                    delivery_intent=delivery_intent,
                    downloaded_attachment_paths=downloaded_attachment_paths,
                    attachment_lease=attachment_lease,
                    admission_context={
                        # The reaction target is not always the sender's own
                        # message (a quick reply reacts on its bot echo), and it
                        # cannot be rebuilt from the Delivery snapshot.
                        "processing_indicator_message_id": self._reaction_target(
                            context
                        ),
                        "message_handler_route": {
                            "base_session_id": base_session_id,
                            "composite_session_id": composite_key,
                            "subagent_name": subagent_name,
                            "subagent_key": matched_prefix,
                            "subagent_model": subagent_model,
                            "subagent_reasoning_effort": subagent_reasoning_effort,
                            "routing_subagent": bool(
                                (context.platform_specific or {}).get(
                                    "routing_subagent"
                                )
                            ),
                        }
                    },
                )
                if admitted:
                    attachment_lease = None
                    return None

            if attachment_lease is not None:
                attachment_lease.adopt()
                attachment_lease.release()
                attachment_lease = None

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

            # A background task's prompt is only visible in the Workbench transcript
            # (the ``harness`` Message row); stage it so the IM conversation gets the
            # question the following reply answers. Staged here because this point is
            # past every ``suppress_delivery`` resolution (the ``agent_run_target`` and
            # thread-anchor branches settle it only after ``get_session_info``) and
            # covers the durable Delivery path too, which skips the mirror branch
            # above; the send itself happens at the real turn start, once the runtime
            # gate is held (see ``_stage_harness_prompt_echo``).
            # ``control_message`` rather than ``message``: subagent routing above
            # strips a matched ``name:`` prefix off ``message``, and the echo must show
            # the prompt as it was stored (which is what the Workbench row shows too),
            # including the prefix that names the requested subagent.
            if source != self.TURN_SOURCE_HUMAN:
                self._stage_harness_prompt_echo(context, control_message)

            user_message = self._get_user_message(context, message)
            audio_transcripts = await self._transcribe_audio_attachments(context, processed_files or [])
            if audio_transcripts:
                message = append_audio_transcripts_to_message(message, audio_transcripts)
                user_message = append_audio_transcripts_to_message(user_message, audio_transcripts)
                await self._echo_audio_transcripts_if_enabled(context, audio_transcripts)

            message = self._append_attachment_errors(message, attachment_errors)
            input_metadata = await self.prepare_input_metadata(context, human=is_human)

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
                user_message=user_message,
                input_metadata=input_metadata,
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
                vibe_agent_model_explicit="model" in explicit_overrides,
                vibe_agent_reasoning_effort_explicit="reasoning_effort" in explicit_overrides,
                vibe_agent_system_prompt=vibe_agent.system_prompt if vibe_agent else None,
                processing_indicator=processing_indicator,
                files=processed_files,
            )
            request.failure_handler = lambda error: self._emit_agent_dispatch_failure(
                context,
                request,
                error,
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
                if (
                    is_human
                    and not durable_delivery_owned
                    and context.platform
                    and context.platform != "avibe"
                    and context.message_id
                ):
                    bound_session_id = (context.platform_specific or {}).get("agent_session_id")
                    if bound_session_id:
                        from core.message_mirror import link_inbound_message_session

                        link_inbound_message_session(
                            platform=context.platform,
                            native_message_id=context.message_id,
                            session_id=str(bound_session_id),
                        )
            except KeyError:
                if request.failure_handled:
                    raise
                await self._handle_missing_agent(
                    context,
                    agent_name,
                    request=request,
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
            if not bool(getattr(request, "failure_handled", False)):
                await self._emit_agent_dispatch_failure(context, request, e)
            return str(e)
        finally:
            if attachment_lease is not None:
                attachment_lease.release()
            if not agent_dispatched:
                # Synchronous completion — no async agent reply is coming, so
                # release any live streaming SSE waiter for this turn now
                # instead of holding it open until the dispatch safety
                # timeout. No-op for non-streaming (IM/CLI) turns.
                mark_complete = getattr(self.controller, "mark_turn_complete", None)
                if callable(mark_complete):
                    mark_complete(context)

    async def _emit_agent_dispatch_failure(
        self,
        context: MessageContext,
        request: AgentRequest | None,
        error: BaseException,
    ) -> None:
        """Report one backend dispatch failure through the shared live boundary."""

        error_text = self.formatter.format_error(
            self._t("error.processMessageFailed", error=str(error))
        )
        await emit_backend_failure(
            self.controller,
            context,
            str(getattr(request, "vibe_agent_backend", None) or "agent"),
            error_text,
            display_text=error_text,
            request=request,
        )

    async def _admit_human_delivery(
        self,
        *,
        manager: Any,
        context: MessageContext,
        dispatch_text: str,
        display_text: str,
        processed_files: List[FileAttachment],
        session_key: str,
        agent_name: str,
        session_anchor: str,
        working_path: str,
        vibe_agent: Any,
        delivery_intent: str,
        downloaded_attachment_paths: List[str],
        admission_context: dict[str, Any],
        attachment_lease: Any = None,
    ) -> bool:
        """Transfer one IM input to its durable owner before native work."""

        session_id = self.session_handler.ensure_agent_session_id(
            context,
            session_key=session_key,
            agent_name=agent_name,
            session_anchor=session_anchor,
            working_path=working_path,
            vibe_agent_id=getattr(vibe_agent, "id", None),
            vibe_agent_name=getattr(vibe_agent, "name", None),
        )
        if not session_id:
            raise RuntimeError("Could not reserve the Agent Session before delivery")

        from core.message_priority import priority_for_delivery_intent
        from core.session_turns import DeliveryRequest
        from storage import message_deliveries, media_service, messages_service
        from storage.agent_session_rows import reserve_write_lock
        from storage.db import get_cached_sqlite_engine

        priority = priority_for_delivery_intent(delivery_intent)
        author_name = await self._input_user_name(context)
        scope_id = None
        request = None
        duplicate_delivery_id = None
        try:
            with get_cached_sqlite_engine().begin() as conn:
                from core.message_mirror import _scope_id_for_session

                reserve_write_lock(conn)
                scope_id = _scope_id_for_session(conn, session_id)
                platform = str(context.platform or "")
                native_message_id = str(context.message_id or "").strip()
                if native_message_id and messages_service.native_message_exists(
                    conn,
                    platform=platform,
                    scope_id=scope_id,
                    native_message_id=native_message_id,
                ):
                    duplicate_delivery_id = ""
                elif native_message_id:
                    existing = message_deliveries.get_delivery_by_native_identity(
                        conn,
                        platform=platform,
                        native_message_id=native_message_id,
                        scope_id=scope_id,
                        session_id=session_id,
                        normalize_legacy=True,
                    )
                    if existing is not None:
                        duplicate_delivery_id = str(existing["id"])

                if duplicate_delivery_id is None:
                    attachment_refs: list[dict[str, Any]] = []
                    for attachment in processed_files:
                        if not attachment.local_path:
                            continue
                        token = media_service.register(
                            conn,
                            scope_id=scope_id,
                            session_id=session_id,
                            kind=(
                                "image"
                                if str(attachment.mimetype or "").startswith("image/")
                                else "file"
                            ),
                            source="im_inbound",
                            local_path=attachment.local_path,
                            file_name=attachment.name,
                            content_type=attachment.mimetype,
                        )
                        attachment_refs.append(
                            {
                                "token": token,
                                "name": attachment.name,
                                "mimetype": attachment.mimetype,
                                "size": attachment.size,
                            }
                        )

                    if not message_deliveries.has_substantive_input(
                        dispatch_text,
                        has_attachments=bool(attachment_refs),
                    ):
                        raise ValueError(
                            "Message contains no deliverable text or attachment"
                        )

                    content = {"text": display_text}
                    if attachment_refs:
                        content["attachments"] = attachment_refs
                    request = DeliveryRequest(
                        session_id=session_id,
                        priority=priority,
                        content=dispatch_text,
                        has_content=True,
                        delivery_id=message_deliveries.new_delivery_id(),
                        scope_id=scope_id,
                        platform=platform,
                        source="user",
                        author="user",
                        message_type="user",
                        author_id=str(context.user_id or "").strip() or None,
                        author_name=author_name,
                        display_text=display_text,
                        content_json=content,
                        admission_context=admission_context,
                        native_message_id=native_message_id or None,
                        parent_native_message_id=(
                            str(context.thread_id or "").strip() or None
                        ),
                        message_kind=(
                            "original"
                            if context.is_original_human_text is True
                            or context.is_original_human_attachment is True
                            else context.message_kind
                        ),
                    )
                    reserved = manager.reserve_delivery(conn, request)
                    if str(reserved["id"]) != request.delivery_id:
                        raise RuntimeError(
                            "native Delivery appeared after writer reservation"
                        )
        except Exception:
            if attachment_lease is not None:
                attachment_lease.release()
            else:
                self._cleanup_unowned_attachment_paths(downloaded_attachment_paths)
            raise

        if duplicate_delivery_id is not None:
            if attachment_lease is not None:
                attachment_lease.release()
            else:
                self._cleanup_unowned_attachment_paths(downloaded_attachment_paths)
            if duplicate_delivery_id:
                payload = dict(context.platform_specific or {})
                payload["delivery_id"] = duplicate_delivery_id
                context.platform_specific = payload
            return True

        if request is None:
            raise RuntimeError("Delivery reservation did not produce a request")
        if attachment_lease is not None:
            attachment_lease.adopt()
            attachment_lease.release()
        result = await manager.deliver(request, context=context)
        payload = dict(context.platform_specific or {})
        payload["delivery_id"] = result.delivery_id
        context.platform_specific = payload
        if result.state in {
            "queued",
            "pending_steer",
            "steering",
            "reconciling_steer",
        }:
            from core.inbox_events import bus

            bus.publish("queue.updated", {"session_id": session_id})
        # This is the only place that knows an input's admission outcome: a
        # Delivery that did not start its own turn returns here and the caller
        # stops, so without a receipt the user sees nothing at all for every
        # message sent while a turn is running.
        await self._ack_delivery_admission(context, result)
        return True

    @staticmethod
    def _reaction_target(context: MessageContext) -> Optional[str]:
        """The message this input's reactions belong on, when it is not its own.

        A quick-reply callback is dispatched with ``message_id=None`` (to bypass
        platform event dedup) and reacts on its bot echo instead. The echo id
        only exists in this process, so it has to travel with the Delivery.
        """

        target = (context.platform_specific or {}).get(
            "processing_indicator_message_id"
        )
        return str(target) if target else None

    @staticmethod
    def _restore_reaction_target(
        context: MessageContext,
        delivery_context: Any,
    ) -> None:
        """Put a Delivery's reaction target back on its rehydrated context.

        Durable hydration restores only the native message id, so without this a
        promoted quick-reply Delivery computes a different receipt key: its 👌
        would never be cleared and its own indicator would target the synthetic
        delivery id instead of the echo.
        """

        if not isinstance(delivery_context, dict):
            return
        target = delivery_context.get("processing_indicator_message_id")
        if not target:
            return
        spec = dict(context.platform_specific or {})
        spec["processing_indicator_message_id"] = str(target)
        context.platform_specific = spec

    async def _ack_delivery_admission(self, context: MessageContext, result: Any) -> None:
        """Report one admission outcome back to the sender, best effort."""

        indicator = getattr(self.controller, "processing_indicator", None)
        ack = getattr(indicator, "ack_delivery_state", None)
        if not callable(ack):
            return
        try:
            await ack(
                context,
                state=str(getattr(result, "state", "") or ""),
                admission=str(getattr(result, "admission", "") or ""),
            )
        except Exception as err:
            logger.debug("Failed to acknowledge delivery admission: %s", err)

    @staticmethod
    def _cleanup_unowned_attachment_paths(paths: List[str]) -> None:
        from pathlib import Path

        for path in dict.fromkeys(str(value) for value in paths if value):
            try:
                Path(path).unlink(missing_ok=True)
            except Exception as err:
                logger.warning("Failed to remove unowned attachment %s: %s", path, err)

    def _is_duplicate_human_delivery(self, context: MessageContext) -> bool:
        """Reject a retried native event before pre-admission side effects."""

        if self._native_human_event_processed(context):
            return True
        platform = str(context.platform or "").strip()
        native_message_id = str(context.message_id or "").strip()
        if not platform or not native_message_id:
            return False
        try:
            from storage import message_deliveries, messages_service
            from storage.db import get_cached_sqlite_engine
            from core.message_mirror import scope_id_for_context

            scope_id = scope_id_for_context(context)

            with get_cached_sqlite_engine().connect() as conn:
                if messages_service.native_message_exists(
                    conn,
                    platform=platform,
                    scope_id=scope_id,
                    native_message_id=native_message_id,
                ):
                    return True
                return bool(
                    message_deliveries.get_delivery_by_native_identity(
                        conn,
                        platform=platform,
                        native_message_id=native_message_id,
                        scope_id=scope_id,
                    )
                    is not None
                )
        except Exception:
            logger.exception(
                "Could not preflight native Message dedupe for %s:%s",
                platform,
                native_message_id,
            )
            return False

    def _native_human_event_processed(self, context: MessageContext) -> bool:
        message_ts = context.message_id
        thread_ts = context.thread_id or context.message_id
        if not message_ts or not thread_ts:
            return False
        legacy_keys = (str(context.channel_id), str(thread_ts), str(message_ts))
        dedup_keys = self._processed_message_dedup_keys(context, str(thread_ts), str(message_ts))
        checker = getattr(self.sessions, "has_processed_message", None)
        if callable(checker):
            if checker(*dedup_keys):
                return True
            return self._claimed_before_dedup_namespacing(dedup_keys, legacy_keys)
        checker = getattr(self.sessions, "is_message_already_processed", None)
        if not callable(checker):
            return False
        if checker(*dedup_keys):
            return True
        return self._claimed_before_dedup_namespacing(dedup_keys, legacy_keys)

    def _claim_native_human_event(self, context: MessageContext) -> bool:
        """Fence a native control input that intentionally creates no Delivery."""

        message_ts = context.message_id
        thread_ts = context.thread_id or context.message_id
        if not message_ts or not thread_ts:
            return True
        legacy_keys = (str(context.channel_id), str(thread_ts), str(message_ts))
        dedup_keys = self._processed_message_dedup_keys(context, str(thread_ts), str(message_ts))
        if self._claimed_before_dedup_namespacing(dedup_keys, legacy_keys):
            return False
        try_record = getattr(self.sessions, "try_record_processed_message", None)
        if callable(try_record):
            recorded = try_record(*dedup_keys)
        else:
            recorded = not self.sessions.is_message_already_processed(*dedup_keys)
            if recorded:
                self.sessions.record_processed_message(
                    *dedup_keys,
                )
        if not recorded:
            logger.info(
                "Skipping already processed message: channel=%s, thread=%s, message=%s",
                context.channel_id,
                thread_ts,
                message_ts,
            )
        return bool(recorded)

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

    def _stage_harness_prompt_echo(self, context: MessageContext, message: str) -> None:
        """Stage the Harness prompt for the outward echo at turn start.

        Staged instead of sent here: ``AgentService.handle_message`` blocks on the
        runtime turn gate before this turn really starts, so posting now would
        announce a queued task's prompt while another turn is still working — and
        would leave that prompt behind if the queued turn is cancelled.
        ``AgentService._begin_turn_status`` emits it once the gate is held, right
        before the status bubble, so the channel still reads trigger -> work ->
        result. The dispatcher still owns every gate (platform,
        ``suppress_delivery``, trigger kind, the ``runtime.harness_prompt_echo``
        switch) and the delivery target.
        """
        if not (message or "").strip():
            return
        spec = dict(context.platform_specific or {})
        spec[HARNESS_PROMPT_ECHO_SPEC_KEY] = message
        context.platform_specific = spec

    async def prepare_input_metadata(
        self, context: MessageContext, *, human: bool
    ) -> AgentInputMetadata:
        """Resolve stable sender facts without rendering execution context."""
        return AgentInputMetadata(
            user_id=context.user_id if human else None,
            user_name=await self._input_user_name(context) if human else None,
            source_session_id=self._source_session_id(context) or None,
        )

    @staticmethod
    def _source_session_id(context: MessageContext) -> str:
        """Return the Agent Session that authored this Harness input, if known."""
        payload = context.platform_specific or {}
        source_session_id = str(payload.get("source_session_id") or "").strip()
        if source_session_id:
            return source_session_id
        if str(payload.get("source_kind") or "").strip() == "agent":
            return str(payload.get("source_actor") or "").strip()
        return ""

    async def _input_user_name(self, context: MessageContext) -> str:
        payload = context.platform_specific or {}
        if payload.get("author_name") and payload.get("author_id") == context.user_id:
            return str(payload["author_name"])
        try:
            user_info = await self._get_im_client(context).get_user_info(context.user_id)
            return self._resolve_user_display_name(user_info, context.user_id)
        except Exception as e:
            logger.debug(f"Failed to fetch user info for {context.user_id}: {e}")
            return context.user_id

    @staticmethod
    def _delivery_user_text(context: MessageContext) -> str | None:
        payload = context.platform_specific or {}
        content = payload.get("message_content")
        if payload.get("delivery_ids") and isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
        return None

    @staticmethod
    def _get_control_message(context: MessageContext, message: str) -> str:
        original = MessageHandler._delivery_user_text(context)
        if original is not None:
            return original
        payload = context.platform_specific or {}
        control_text = payload.get("control_text")
        if isinstance(control_text, str):
            return control_text
        return message

    @staticmethod
    def _get_user_message(context: MessageContext, message: str) -> str:
        original = MessageHandler._delivery_user_text(context)
        if original is not None:
            return original
        payload = context.platform_specific or {}
        normalized_user_text = payload.get("normalized_user_text")
        if isinstance(normalized_user_text, str):
            return normalized_user_text
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
                    user_message="",
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
            manager = getattr(self.controller, "session_turns", None)
            cancel = getattr(manager, "cancel", None)
            session_id = str(
                (context.platform_specific or {}).get("agent_session_id") or ""
            ).strip()
            if not session_id:
                getter = getattr(self.sessions, "get_agent_session_row_id", None)
                if callable(getter):
                    session_id = str(
                        getter(session_key, base_session_id, agent_name) or ""
                    ).strip()
            if session_id and callable(cancel):
                result = await cancel(session_id)
                handled = bool(isinstance(result, dict) and result.get("ok"))
                if not handled:
                    await self._get_im_client(context).send_message(
                        context,
                        f"ℹ️ {self._t('command.stop.noActiveSession')}",
                    )
                return handled
            request = AgentRequest(
                context=context,
                message="stop",
                user_message="",
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

    async def _handle_missing_agent(
        self,
        context: MessageContext,
        agent_name: str,
        *,
        request: AgentRequest | None = None,
    ) -> None:
        """Notify and, for a dispatched Turn, settle a missing Agent failure."""
        target = agent_name or self.controller.agent_service.default_agent
        backend = self._missing_agent_backend(context, target)
        display_backend = display_name_for_backend(backend) if backend else str(target)
        hint_key = f"error.agentNotConfiguredHint.{backend}" if backend else "error.agentNotConfiguredHint.generic"
        hint = self._t(hint_key)
        msg = f"❌ {self._t('error.agentNotConfigured', agent=target, backend=display_backend, hint=hint)}"
        if request is not None:
            await emit_backend_failure(
                self.controller,
                context,
                backend or str(target),
                msg,
                display_text=msg,
                request=request,
            )
            return
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

    async def _stream_terminal_error(
        self,
        context: MessageContext,
        text: str,
    ) -> None:
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
        """Materialize native files once and preserve ordinary Agent ownership."""

        batch = await self._materialize_file_attachments(context, working_path)
        batch.lease.adopt()
        batch.lease.release()
        processed = list(batch.attachments)
        return (processed if processed else None), list(batch.display_errors)

    async def _materialize_file_attachments(
        self,
        context: MessageContext,
        working_path: str,
    ):
        """Return an untransferred lease so admission decides final ownership."""

        from config.paths import get_attachments_dir
        from core.handlers.inbound_attachments import InboundAttachmentMaterializer

        if not context.files:
            raise ValueError("attachment materialization requires input files")
        batch = await InboundAttachmentMaterializer(
            attachments_root=get_attachments_dir(),
        ).materialize(
            context,
            self._get_im_client(context),
            language=self._get_lang(),
        )
        return batch

    @staticmethod
    def _cleanup_partial_attachment(path) -> None:
        if not path:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception as err:
            logger.debug("Failed to remove partial attachment %s: %s", path, err)

    def _append_attachment_errors(self, message: str, errors: List[str]) -> str:
        if not errors:
            return message

        error_block = "\n".join(
            [
                f"[{self._t('error.attachmentDownload.title')}]",
                *[f"- {error}" for error in errors],
            ]
        )
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
