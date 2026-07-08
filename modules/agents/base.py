"""Abstract agent interfaces and shared dataclasses."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from modules.im import MessageContext
from modules.im.base import FileAttachment
from core.reply_enhancer import strip_silent_blocks

logger = logging.getLogger(__name__)

AGENT_RUNTIME_TURN_KEY = "agent_runtime_turn_key"
AGENT_RUNTIME_TURN_TOKEN = "agent_runtime_turn_token"
AGENT_TURN_TOKEN = "turn_token"


@dataclass
class AgentRequest:
    """Normalized agent invocation request."""

    context: MessageContext
    message: str
    working_path: str
    base_session_id: str
    composite_session_id: str
    session_key: str
    ack_message_id: Optional[str] = None
    subagent_name: Optional[str] = None
    subagent_key: Optional[str] = None
    subagent_model: Optional[str] = None
    subagent_reasoning_effort: Optional[str] = None
    vibe_agent_id: Optional[str] = None
    vibe_agent_name: Optional[str] = None
    vibe_agent_backend: Optional[str] = None
    vibe_agent_model: Optional[str] = None
    vibe_agent_reasoning_effort: Optional[str] = None
    vibe_agent_system_prompt: Optional[str] = None
    last_agent_message: Optional[str] = None
    last_agent_message_parse_mode: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)
    # Reaction ack: emoji added to user's message, to be removed when result is sent
    processing_indicator: Optional[Any] = None
    ack_reaction_message_id: Optional[str] = None
    ack_reaction_emoji: Optional[str] = None
    typing_indicator_active: bool = False
    typing_indicator_task: Optional[Any] = None
    # File attachments (downloaded or with URLs for download)
    files: Optional[List[FileAttachment]] = None
    # Internal stop diagnostics. Backend adapters set this when ``handle_stop``
    # returns False so Workbench can distinguish a stale missing runtime from an
    # interrupt refusal/failure.
    stop_failure_reason: Optional[str] = None


@dataclass
class AgentMessage:
    """Normalized message emitted by an agent implementation."""

    text: str
    message_type: str = "assistant"
    parse_mode: str = "markdown"
    metadata: Optional[Dict[str, Any]] = None


class BaseAgent(ABC):
    """Abstract base class for all agent implementations."""

    name: str

    def __init__(self, controller):
        self.controller = controller
        self.config = controller.config
        self.im_client = controller.im_client
        self.settings_manager = controller.settings_manager
        self.sessions = (
            getattr(controller, "sessions", None)
            or getattr(controller.settings_manager, "sessions", None)
            or controller.settings_manager
        )

    def _calculate_duration_ms(self, started_at: Optional[float]) -> int:
        if not started_at:
            return 0
        elapsed = time.monotonic() - started_at
        return max(0, int(elapsed * 1000))

    def _get_im_client(self, context: MessageContext):
        getter = getattr(self.controller, "get_im_client_for_context", None)
        if callable(getter):
            return getter(context)
        return self.im_client

    def _get_formatter(self, context: MessageContext):
        return getattr(self._get_im_client(context), "formatter", self.im_client.formatter)

    def runtime_turn_key(self, request: AgentRequest) -> str:
        """Return the backend runtime identity that must run one turn at a time."""
        payload = getattr(request.context, "platform_specific", None) or {}
        backend_key = str(payload.get("backend_composite_session_id") or "").strip()
        return backend_key or request.composite_session_id

    def runtime_turn_keys(self) -> set[str]:
        """Return all currently known runtime identities for this backend."""
        return set()

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        """Return runtime identities scoped to a persisted Avibe settings key."""
        return set()

    def backend_alive(self, context: Any) -> Optional[bool]:
        """Best-effort liveness of this backend for the given turn context.

        Returns ``True`` (alive), ``False`` (definitively gone), or ``None``
        (unknown — the default). Callers MUST treat ``None`` as alive so an
        unknown state never triggers a false "backend not responding" warning.
        The concise status bubble uses this to flip its footer to ⚠️.
        """
        return None

    def mark_runtime_turn_started(self, context: Any) -> None:
        """Mark the current gated turn as accepted by the backend runtime.

        AgentService uses this to distinguish a startup-window stop (backend has
        not accepted the turn yet) from a stale stop after the backend lost an
        accepted turn.
        """
        service = getattr(self.controller, "agent_service", None)
        mark_started = getattr(service, "mark_runtime_turn_started", None)
        if callable(mark_started):
            mark_started(context)

    def ensure_agent_session_id(
        self,
        request: AgentRequest,
        *,
        session_anchor: Optional[str] = None,
    ) -> Optional[str]:
        """Ensure the Vibe-owned public session id exists on the request context."""
        # avibe: pin the reserved workbench row id BEFORE any pre-bind ensure can
        # mint a hidden row and overwrite it. Without this, a setup/start failure
        # between this ensure and the later native bind would persist the terminal
        # notify under the hidden id and the open Chat would never see it (Codex P2).
        reserved_id = self._reserved_agent_session_id(request.context)
        if reserved_id:
            self._pin_agent_session_id(request.context, reserved_id)
            return reserved_id
        anchor = session_anchor or request.base_session_id
        sessions = getattr(self, "sessions", None)
        ensure = getattr(sessions, "ensure_agent_session_id", None)
        if callable(ensure):
            kwargs = {}
            resolved_target = (request.context.platform_specific or {}).get("agent_run_target")
            if isinstance(resolved_target, dict) and resolved_target.get("workdir"):
                kwargs["workdir"] = str(resolved_target["workdir"])
            if request.vibe_agent_id is not None:
                kwargs["vibe_agent_id"] = request.vibe_agent_id
            if request.vibe_agent_name is not None:
                kwargs["vibe_agent_name"] = request.vibe_agent_name
            agent_session_id = ensure(
                request.session_key,
                self.name,
                anchor,
                **kwargs,
            )
        else:
            getter = getattr(sessions, "get_agent_session_row_id", None)
            agent_session_id = (
                getter(request.session_key, anchor, self.name)
                if callable(getter)
                else None
            )
        if not agent_session_id:
            return None
        payload = dict(request.context.platform_specific or {})
        payload["agent_session_id"] = agent_session_id
        request.context.platform_specific = payload
        return agent_session_id

    @staticmethod
    def _reserved_agent_session_id(context: Any) -> Optional[str]:
        """The open Chat session's ``agent_sessions`` PK reserved for an avibe turn.

        avibe dispatch stamps ``platform_specific['agent_session_target']['id']``;
        IM/CLI turns have no target. Used to keep Claude/Codex replies attributed to
        the open Chat session instead of a freshly-minted hidden row (Codex P1)."""
        payload = getattr(context, "platform_specific", None) or {}
        target = payload.get("agent_session_target")
        if isinstance(target, dict) and target.get("id"):
            reserved = str(target["id"]).strip()
            return reserved or None
        return None

    @staticmethod
    def _reserved_native_session_id(context: Any, backend: Optional[str] = None) -> Optional[str]:
        """The backend-native session id last bound to the RESERVED workbench row.

        avibe dispatch carries it in
        ``platform_specific['agent_session_target']['native_session_id']`` (read
        from the ``agent_sessions`` row by its PK). Resuming from THIS — rather
        than the ``(session_key, anchor)`` projection — keeps the resume READ on
        the same key as the by-PK bind WRITE (``_bind_reserved_workbench_session``),
        so a controller restart resumes the SAME native session instead of forking
        a fresh one and losing context. Empty until the first turn captures a
        native; ``None`` for IM/CLI turns (no reserved target). Mirrors
        ``_reserved_agent_session_id``.

        ``backend``: when given, only return the native if the reserved row's
        ``agent_backend`` matches — after a header backend switch the row still
        carries the previous backend's native, and handing e.g. a Claude id to
        Codex would fail to resume; in that case return ``None`` so the newly
        selected backend starts its own first thread for this Chat."""
        payload = getattr(context, "platform_specific", None) or {}
        target = payload.get("agent_session_target")
        if not isinstance(target, dict):
            return None
        native = str(target.get("native_session_id") or "").strip()
        if not native:
            return None
        if backend:
            target_backend = str(target.get("agent_backend") or "").strip()
            if target_backend and target_backend != backend:
                return None
        return native

    @staticmethod
    def _pin_agent_session_id(context: Any, agent_session_id: str) -> None:
        payload = dict(getattr(context, "platform_specific", None) or {})
        payload["agent_session_id"] = agent_session_id
        context.platform_specific = payload

    @staticmethod
    def _uses_namespaced_backend_session(
        context: Any,
        *,
        subagent_name: Optional[str] = None,
    ) -> bool:
        if subagent_name:
            return True
        payload = getattr(context, "platform_specific", None) or {}
        return bool(payload.get("routing_subagent"))

    def _bind_reserved_workbench_session(
        self,
        context: Any,
        native_session_id: Any,
        *,
        working_path: Optional[str] = None,
        vibe_agent_id: Optional[str] = None,
        vibe_agent_name: Optional[str] = None,
        vibe_agent_backend: Optional[str] = None,
    ) -> Optional[str]:
        """Bind the backend-native id to the RESERVED workbench session row, by id.

        Claude/Codex must bind the native session to the reserved workbench row
        (like OpenCode's ``bind_agent_session_by_id``) instead of letting the
        generic ``bind_agent_session`` mint a fresh row and overwrite
        ``agent_session_id`` — otherwise ``persist_agent_message`` would publish
        ``message.new`` under the new hidden id and the reply would never reach the
        open Chat page (Codex P1). Returns the reserved id when this is an avibe
        turn (so the caller skips its normal binder), else ``None``.
        """
        if self._uses_namespaced_backend_session(context):
            return None
        reserved_id = self._reserved_agent_session_id(context)
        if not reserved_id:
            return None
        payload = getattr(context, "platform_specific", None) or {}
        resolved = payload.get("resolved_vibe_agent")
        if isinstance(resolved, dict):
            vibe_agent_id = vibe_agent_id if vibe_agent_id is not None else resolved.get("id")
            vibe_agent_name = vibe_agent_name if vibe_agent_name is not None else resolved.get("name")
            vibe_agent_backend = vibe_agent_backend if vibe_agent_backend is not None else resolved.get("backend")
        sessions = getattr(self, "sessions", None)
        bind_by_id = getattr(sessions, "bind_agent_session_by_id", None)
        bound: Optional[str] = None
        if callable(bind_by_id):
            try:
                resolved_target = payload.get("agent_run_target")
                resolved_workdir = (
                    resolved_target.get("workdir")
                    if isinstance(resolved_target, dict)
                    else None
                )
                # Positional (session_id, native_session_id) — the SessionsFacade
                # binds by a positional ``agent_session_id``, NOT a keyword, so a
                # ``session_id=`` call would TypeError and silently skip recording
                # the native id (Codex P2). Mirrors the OpenCode call.
                bound = bind_by_id(
                    reserved_id,
                    native_session_id,
                    workdir=resolved_workdir or working_path,
                    vibe_agent_id=vibe_agent_id,
                    vibe_agent_name=vibe_agent_name,
                    vibe_agent_backend=vibe_agent_backend,
                )
            except Exception:
                logger.debug("bind_agent_session_by_id failed; keeping reserved id", exc_info=True)
        # Pin ``agent_session_id`` to the reserved row even if the by-id bind
        # couldn't record the native id — the publish target is what matters for
        # the open Chat page.
        new_id = bound or reserved_id
        self._pin_agent_session_id(context, new_id)
        return new_id

    def _maybe_backfill_session_title(
        self,
        request: AgentRequest | None,
        native_session_id: Any,
        *,
        retry_delay_seconds: float | None = None,
    ) -> None:
        """Best-effort backfill of an empty Vibe session title from the backend."""

        if request is None or not native_session_id:
            return
        agent_session_id = self._reserved_agent_session_id(request.context) or self._session_id_from_context(
            request.context
        )
        if not agent_session_id:
            return
        working_path = getattr(request, "working_path", "") or ""
        if not working_path:
            return

        async def _run(delay: float | None = None) -> None:
            if delay:
                await asyncio.sleep(delay)
            updated = self._backfill_session_title_once(
                agent_session_id=agent_session_id,
                native_session_id=str(native_session_id),
                working_path=working_path,
                fallback_first_user_message=getattr(request, "message", "") or "",
            )
            if updated is None and delay is None and retry_delay_seconds:
                asyncio.create_task(_run(retry_delay_seconds))

        asyncio.create_task(_run())

    @staticmethod
    def _session_id_from_context(context: Any) -> Optional[str]:
        payload = getattr(context, "platform_specific", None) or {}
        sid = payload.get("agent_session_id")
        return str(sid).strip() if sid else None

    def _backfill_session_title_once(
        self,
        *,
        agent_session_id: str,
        native_session_id: str,
        working_path: str,
        fallback_first_user_message: str = "",
    ) -> Optional[dict[str, Any]]:
        try:
            from core.session_titles import backfill_agent_session_title

            return backfill_agent_session_title(
                agent_session_id=agent_session_id,
                backend=self.name,
                native_session_id=native_session_id,
                working_path=working_path,
                fallback_first_user_message=fallback_first_user_message,
            )
        except Exception:
            logger.debug(
                "session-title: backfill failed for agent=%s session=%s native=%s",
                self.name,
                agent_session_id,
                native_session_id,
                exc_info=True,
            )
            return None

    def bind_agent_session_id(
        self,
        request: AgentRequest,
        native_session_id: Any,
        *,
        session_anchor: Optional[str] = None,
    ) -> Optional[str]:
        """Bind a backend-native session id to the existing Vibe session row."""
        anchor = session_anchor or request.base_session_id
        reserved_id = self._reserved_agent_session_id(request.context)
        use_backend_anchor = self._uses_namespaced_backend_session(
            request.context,
            subagent_name=getattr(request, "subagent_name", None),
        )
        if not use_backend_anchor:
            # avibe: bind to the reserved workbench row by id (mirrors OpenCode) so
            # the reply publishes under the open Chat session, not a new hidden row
            # (P1). Namespaced backend subagents are the exception: their native
            # threads must be stored under their own anchor, while the publish
            # target remains the open Chat row.
            reserved = self._bind_reserved_workbench_session(
                request.context,
                native_session_id,
                working_path=getattr(request, "working_path", None),
                vibe_agent_id=getattr(request, "vibe_agent_id", None),
                vibe_agent_name=getattr(request, "vibe_agent_name", None),
                vibe_agent_backend=getattr(request, "vibe_agent_backend", None),
            )
            if reserved:
                return reserved
        sessions = getattr(self, "sessions", None)
        binder = getattr(sessions, "bind_agent_session", None)
        if callable(binder):
            kwargs = {}
            resolved_target = (request.context.platform_specific or {}).get("agent_run_target")
            if isinstance(resolved_target, dict) and resolved_target.get("workdir"):
                kwargs["workdir"] = str(resolved_target["workdir"])
            if request.vibe_agent_id is not None:
                kwargs["vibe_agent_id"] = request.vibe_agent_id
            if request.vibe_agent_name is not None:
                kwargs["vibe_agent_name"] = request.vibe_agent_name
            agent_session_id = binder(
                request.session_key,
                self.name,
                anchor,
                native_session_id,
                **kwargs,
            )
        else:
            setter = getattr(sessions, "set_agent_session_mapping", None)
            if callable(setter):
                setter(request.session_key, self.name, anchor, native_session_id)
            agent_session_id = None
        if agent_session_id:
            payload = dict(request.context.platform_specific or {})
            payload["agent_session_id"] = reserved_id if use_backend_anchor and reserved_id else agent_session_id
            request.context.platform_specific = payload
            return agent_session_id
        ensured_id = self.ensure_agent_session_id(request, session_anchor=anchor)
        if use_backend_anchor and reserved_id:
            self._pin_agent_session_id(request.context, reserved_id)
        return ensured_id

    async def _remove_ack_reaction(self, request: AgentRequest) -> None:
        """Remove the acknowledgement reaction / typing indicator.

        Called after sending result message or on terminal error to clean up
        the 👀 reaction.  This is the **single** implementation — subclasses
        should NOT override it.  The guard (check-then-clear) is idempotent so
        calling it more than once is harmless.
        """
        service = getattr(self.controller, "processing_indicator", None)
        if service is None:
            return
        await service.finish(request)

    async def emit_result_message(
        self,
        context: MessageContext,
        result_text: Optional[str],
        subtype: str = "success",
        duration_ms: Optional[int] = None,
        started_at: Optional[float] = None,
        parse_mode: str = "markdown",
        suffix: Optional[str] = None,
        request: Optional[AgentRequest] = None,
    ) -> None:
        # An error result subtype (e.g. Claude's ``error_max_turns`` /
        # ``error_during_execution``) is a FAILED turn. Carry that on the terminal
        # ``result`` emit via ``is_error`` so the outbound chokepoint flips the dot
        # red — no separate latch.
        is_error = (subtype or "").startswith("error")

        show_duration = getattr(self.config, "show_duration", True)
        if duration_ms is None:
            duration_ms = self._calculate_duration_ms(started_at)

        raw_result = result_text or ""
        raw_suffix = suffix or ""
        visible_result = strip_silent_blocks(raw_result)
        visible_suffix = strip_silent_blocks(raw_suffix) if raw_suffix else None
        has_silent_directive = "<silent" in raw_result.lower() or "<silent" in raw_suffix.lower()

        if has_silent_directive and not visible_result.strip() and not (visible_suffix or "").strip():
            await self.controller.emit_agent_message(context, "result", "", parse_mode=parse_mode, is_error=is_error)
            if request:
                await self._remove_ack_reaction(request)
            return

        # When show_duration is disabled, skip the entire result line
        # unless there is actual result_text or suffix to deliver.
        if not show_duration:
            parts = []
            if visible_result and visible_result.strip():
                parts.append(visible_result)
            if visible_suffix:
                parts.append(visible_suffix)
            if parts:
                formatted = "\n".join(parts)
                await self.controller.emit_agent_message(context, "result", formatted, parse_mode=parse_mode, is_error=is_error)
            else:
                # No visible text (show_duration off + empty result/suffix) is still
                # a TERMINAL turn: settle it through the OUTBOUND status chokepoint
                # (empty result → dot idle / failed AND releases the web-Chat stream
                # waiter), mirroring the silent-directive path above — otherwise the
                # dot stays green and the stream hangs until the 600s timeout (Codex P2).
                await self.controller.emit_agent_message(context, "result", "", parse_mode=parse_mode, is_error=is_error)
        else:
            token_field = ""
            get_token_field = getattr(self.controller, "session_token_field", None)
            if callable(get_token_field):
                try:
                    token_field = get_token_field(context) or ""
                except Exception:
                    token_field = ""
            # Duration + token usage ride as a de-emphasized grey subtext footer
            # (the same channel as the concise status footer), NOT a prominent head
            # line. The body carries only the actual answer + suffix.
            result_footer = None
            if duration_ms > 0 or token_field:
                result_footer = self._get_formatter(context).format_result_footer(
                    subtype or "",
                    duration_ms,
                    token_field=token_field,
                )
            parts = []
            if visible_result and visible_result.strip():
                parts.append(visible_result)
            if visible_suffix:
                parts.append(visible_suffix)
            body = "\n".join(parts)
            # Footer-only completion (show_duration on, no visible result/suffix):
            # promote the footnote to the visible body so a duration/token-only turn
            # is still delivered/persisted/streamed. An empty body would otherwise be
            # treated as a silent terminal result and the footnote would be lost.
            if not body.strip() and result_footer:
                body = result_footer
                result_footer = None
            await self.controller.emit_agent_message(
                context,
                "result",
                body,
                parse_mode=parse_mode,
                is_error=is_error,
                result_footer=result_footer,
            )

        # Remove ack reaction after result is sent
        if request:
            await self._remove_ack_reaction(request)

    @abstractmethod
    async def handle_message(self, request: AgentRequest) -> None:
        """Process a user message routed to this agent."""

    async def clear_sessions(self, session_key: str) -> int:
        """Clear session state for a given session scope key. Returns cleared count."""
        return 0

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        """Return runtime turn gate keys owned by the session scope."""
        return set()

    async def prepare_resume_binding(
        self,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        """Prepare backend runtime before binding a resumed session."""
        return None

    async def handle_stop(self, request: AgentRequest) -> bool:
        """Attempt to interrupt an in-flight task. Returns True if handled."""
        return False
