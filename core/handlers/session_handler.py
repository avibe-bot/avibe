"""Session management handlers for Claude SDK sessions"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from uuid import uuid4
from modules.im import MessageContext
from modules.claude_sdk_compat import (
    CLAUDE_SDK_MAX_BUFFER_SIZE,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    is_claude_sdk_buffer_error,
)
from modules.agents.native_sessions.base import build_resume_preview
from modules.agents.claude_process_reaper import (
    AVIBE_CLAUDE_PROCESS_OWNER_ENV,
    AVIBE_CLAUDE_SESSION_OWNER,
    get_claude_client_pid,
    register_claude_owned_process,
    reap_duplicate_claude_resume_processes,
    reap_orphaned_claude_processes,
)
from config.v2_config import (
    DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS,
    DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER,
)
from core.avibe_cloud import avibe_cloud_url_available
from core.caller_context import caller_env_for_platform_payload
from core.resource_governance import governor_from_controller
from core.services.session_fork import pending_native_fork_source
from core.system_prompt_injection import build_system_prompt_injection, get_enabled_agents_for_prompt
from vibe import backend_model_catalog

from .base import BaseHandler

logger = logging.getLogger(__name__)

CLAUDE_NO_CONVERSATION_RE = re.compile(r"No conversation found with session ID:\s*(\S+)")
CLAUDE_REMOTE_DISALLOWED_TOOLS = ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]
CLAUDE_REMOTE_PERMISSION_MODE = "bypassPermissions"
CLAUDE_REMOTE_SANDBOX = {"enabled": False}


class ClaudeSessionNotFoundError(RuntimeError):
    """Claude Code could not resume a persisted session in the current cwd."""

    def __init__(self, session_id: str, working_path: str, stderr: str = ""):
        self.session_id = session_id
        self.working_path = working_path
        self.stderr = stderr
        super().__init__(
            f"Claude Code session not found in current working directory: {session_id} ({working_path})"
        )


class SessionHandler(BaseHandler):
    """Handles all session-related operations"""

    def __init__(self, controller):
        """Initialize with reference to main controller"""
        super().__init__(controller)
        self.session_manager = controller.session_manager
        self.claude_sessions = controller.claude_sessions
        self.receiver_tasks = controller.receiver_tasks
        self.stored_session_mappings = controller.stored_session_mappings
        self.session_last_activity = getattr(controller, "session_last_activity", {})
        self.session_turn_started = getattr(controller, "session_turn_started", {})
        self.active_sessions = getattr(controller, "claude_active_sessions", set())
        self.claude_system_prompts = getattr(controller, "claude_system_prompts", {})
        self.claude_session_creates = getattr(controller, "claude_session_creates", {})
        controller.session_last_activity = self.session_last_activity
        controller.session_turn_started = self.session_turn_started
        controller.claude_active_sessions = self.active_sessions
        controller.claude_system_prompts = self.claude_system_prompts
        controller.claude_session_creates = self.claude_session_creates

    def touch_session_activity(self, composite_key: str) -> None:
        if composite_key:
            self.session_last_activity[composite_key] = time.monotonic()

    def mark_session_active(self, composite_key: str) -> None:
        if not composite_key:
            return
        # Stamp the turn-start baseline only on the idle→active transition, so a
        # second queued request on an already-active session does not reset the
        # "busy for" clock (it stays anchored to the first in-flight request).
        if composite_key not in self.active_sessions:
            self.session_turn_started[composite_key] = time.monotonic()
        self.active_sessions.add(composite_key)
        self.touch_session_activity(composite_key)

    def mark_session_idle(self, composite_key: str) -> None:
        if not composite_key:
            return
        self.active_sessions.discard(composite_key)
        self.session_turn_started.pop(composite_key, None)
        if composite_key in self.claude_sessions:
            self.touch_session_activity(composite_key)

    def clear_session_tracking(self, composite_key: str) -> None:
        if not composite_key:
            return
        self.active_sessions.discard(composite_key)
        self.session_last_activity.pop(composite_key, None)
        self.session_turn_started.pop(composite_key, None)
        self.claude_system_prompts.pop(composite_key, None)

    def bind_claude_runtime_session(
        self,
        client: ClaudeSDKClient,
        base_session_id: str,
        composite_key: str,
        native_session_id: Optional[str] = None,
    ) -> None:
        """Attach the resolved Claude runtime keys to the connected client."""
        setattr(client, "_vibe_runtime_base_session_id", base_session_id)
        setattr(client, "_vibe_runtime_session_key", composite_key)
        if native_session_id:
            setattr(client, "_vibe_native_session_id", native_session_id)
        register_claude_owned_process(
            client,
            native_session_id=native_session_id,
            owner=AVIBE_CLAUDE_SESSION_OWNER,
        )

    async def _set_claude_model_if_needed(self, client: ClaudeSDKClient, desired_model: Optional[str]) -> None:
        unknown = object()
        current_model = getattr(client, "_vibe_current_model", unknown)
        if current_model is not unknown and current_model == desired_model:
            return

        if current_model is unknown and desired_model is None:
            setattr(client, "_vibe_current_model", None)
            return

        set_model = getattr(client, "set_model", None)
        if not callable(set_model):
            logger.warning("Claude SDK client does not support model switching")
            return

        await set_model(desired_model)
        setattr(client, "_vibe_current_model", desired_model)

    async def _reuse_cached_claude_session_if_available(
        self,
        *,
        composite_key: str,
        base_session_id: str,
        working_path: str,
        context: MessageContext,
        session_key: str,
        stored_claude_session_id: Optional[str],
        current_model: Optional[str],
        agent_system_prompt: Optional[str],
    ) -> ClaudeSDKClient | None:
        client = self.claude_sessions.get(composite_key)
        if client is None:
            return None

        next_system_prompt = self._build_claude_system_prompt(
            context=context,
            session_key=session_key,
            agent_name="claude",
            session_anchor=base_session_id,
            agent_system_prompt=agent_system_prompt,
        )
        cached_system_prompt = self.claude_system_prompts.get(composite_key)
        if cached_system_prompt != next_system_prompt:
            logger.info(
                "Recreating cached Claude SDK client for %s because avibe system prompt changed",
                composite_key,
            )
            await self.cleanup_session(composite_key)
            return None

        caller_env = caller_env_for_platform_payload(getattr(context, "platform_specific", None))
        if getattr(client, "_vibe_caller_env", {}) != caller_env:
            logger.info(
                "Recreating cached Claude SDK client for %s because caller context env changed",
                composite_key,
            )
            await self.cleanup_session(composite_key)
            return None

        try:
            await self._set_claude_model_if_needed(client, current_model)
        except Exception as e:
            logger.warning(f"Failed to update model on cached Claude session: {e}")
        logger.info(
            f"Using existing Claude SDK client for {base_session_id} at {working_path} (model={current_model})"
        )
        self.bind_claude_runtime_session(
            client,
            base_session_id,
            composite_key,
            stored_claude_session_id,
        )
        self.touch_session_activity(composite_key)
        return client

    async def _reuse_cached_claude_subagent_session_if_available(
        self,
        *,
        composite_key: str,
        base_session_id: str,
        working_path: str,
        context: MessageContext,
        session_key: str,
        native_session_id: Optional[str],
        explicit_model: Optional[str],
    ) -> ClaudeSDKClient | None:
        client = self.claude_sessions.get(composite_key)
        if client is None:
            return None
        self.ensure_agent_session_id(
            context,
            session_key=session_key,
            agent_name="claude",
            session_anchor=base_session_id,
        )
        caller_env = caller_env_for_platform_payload(getattr(context, "platform_specific", None))
        if getattr(client, "_vibe_caller_env", {}) != caller_env:
            logger.info(
                "Recreating cached Claude subagent SDK client for %s because caller context env changed",
                composite_key,
            )
            await self.cleanup_session(composite_key)
            return None
        if explicit_model:
            try:
                await self._set_claude_model_if_needed(client, explicit_model)
            except Exception as e:
                logger.warning(f"Failed to update model on cached Claude subagent session: {e}")
        logger.info(
            "Using Claude subagent session for %s at %s (model_override=%s)",
            base_session_id,
            working_path,
            explicit_model,
        )
        self.bind_claude_runtime_session(
            client,
            base_session_id,
            composite_key,
            native_session_id,
        )
        self.touch_session_activity(composite_key)
        return client

    async def _wait_for_claude_session_create(self, composite_key: str) -> ClaudeSDKClient | None:
        while True:
            future = self.claude_session_creates.get(composite_key)
            if future is None:
                return None
            logger.info("Waiting for in-flight Claude SDK client create for %s", composite_key)
            client = await asyncio.shield(future)
            if client is not None:
                return client
            client = self.claude_sessions.get(composite_key)
            if client is not None:
                return client
            if self.claude_session_creates.get(composite_key) is future:
                return None

    def _track_claude_session_create(self, composite_key: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self.claude_session_creates[composite_key] = future
        return future

    def _untrack_claude_session_create(self, composite_key: str, future: asyncio.Future) -> None:
        if self.claude_session_creates.get(composite_key) is future:
            self.claude_session_creates.pop(composite_key, None)

    def get_base_session_id(self, context: MessageContext, source: str = "human") -> str:
        """Get base session ID based on platform and context (without path)"""
        platform = self._get_context_platform(context)
        payload = context.platform_specific or {}
        session_target = payload.get("agent_session_target")
        if isinstance(session_target, dict):
            reserved_anchor = str(session_target.get("session_anchor") or "").strip()
            if reserved_anchor:
                return reserved_anchor
        is_dm = bool((context.platform_specific or {}).get("is_dm", False))
        if self.should_allocate_scheduled_anchor(context, source=source):
            return f"{platform}_scheduled-{uuid4().hex}"
        if is_dm:
            use_dm_threads = self._supports_threaded_session(context, is_dm=True)

            if use_dm_threads:
                base_id = context.thread_id or context.message_id or context.channel_id or context.user_id
            else:
                base_id = context.channel_id or context.user_id
        else:
            base_id = context.thread_id
            if not base_id:
                use_message_id = True
                getter = getattr(self.controller, "get_im_client_for_context", None)
                if callable(getter):
                    try:
                        im_client = getter(context)
                    except AttributeError:
                        im_client = getattr(self.controller, "im_client", None)
                else:
                    im_client = getattr(self.controller, "im_client", None)
                if im_client and hasattr(im_client, "should_use_message_id_for_channel_session"):
                    use_message_id = bool(im_client.should_use_message_id_for_channel_session(context))
                base_id = context.message_id if use_message_id and context.message_id else context.channel_id
        return f"{platform}_{base_id}"

    @staticmethod
    def _reserved_native_session_id(context: MessageContext) -> Optional[str]:
        """Native session id bound to the RESERVED workbench row (by PK).

        avibe dispatch carries it in
        ``platform_specific['agent_session_target']['native_session_id']`` (read
        from the ``agent_sessions`` row). Resuming from this keeps the resume READ
        on the same key as the by-PK bind WRITE, so a restart resumes the same
        native session instead of forking a fresh one. ``None`` for IM/CLI turns
        or before the first native is captured. Only returns the native when the
        reserved row's ``agent_backend`` is Claude — after a header backend switch
        the row still carries the previous backend's native, which Claude can't
        resume. Mirrors ``BaseAgent._reserved_native_session_id``."""
        payload = getattr(context, "platform_specific", None) or {}
        target = payload.get("agent_session_target")
        if not isinstance(target, dict):
            return None
        native = str(target.get("native_session_id") or "").strip()
        if not native:
            return None
        target_backend = str(target.get("agent_backend") or "").strip()
        if target_backend and target_backend != "claude":
            return None
        return native

    def _get_context_platform(self, context: MessageContext) -> str:
        return (
            context.platform
            or (context.platform_specific or {}).get("platform")
            or getattr(self.config, "platform", "slack")
        )

    def should_allocate_scheduled_anchor(self, context: MessageContext, source: str = "human") -> bool:
        if source != "scheduled" or context.thread_id:
            return False
        is_dm = bool((context.platform_specific or {}).get("is_dm", False))
        if not self._supports_threaded_session(context, is_dm=is_dm):
            return False
        if is_dm:
            return True

        im_client = self._get_im_client(context)
        use_message_id = getattr(im_client, "should_use_message_id_for_channel_session", lambda _context=None: True)
        return bool(use_message_id(context))

    def build_message_anchor_base(self, context: MessageContext, message_id: str) -> str:
        return f"{self._get_context_platform(context)}_{message_id}"

    def alias_session_base(
        self,
        context: MessageContext,
        *,
        source_base_session_id: str,
        alias_base_session_id: str,
        target_session_key: Optional[str] = None,
        source_session_key: Optional[str] = None,
        clear_source: bool = False,
    ) -> bool:
        if not source_base_session_id or not alias_base_session_id:
            return False
        resolved_source_key = source_session_key or self._get_session_key(context)
        resolved_target_key = target_session_key or resolved_source_key
        if resolved_target_key == resolved_source_key:
            changed = self.sessions.alias_session_base(
                resolved_target_key,
                source_base_session_id,
                alias_base_session_id,
            )
        else:
            changed = self.sessions.alias_session_base_across_scopes(
                resolved_source_key,
                resolved_target_key,
                source_base_session_id,
                alias_base_session_id,
            )
        cleared = 0
        if clear_source and source_base_session_id != alias_base_session_id:
            cleared = self.sessions.clear_session_base(resolved_source_key, source_base_session_id)
        return bool(changed or cleared)

    def finalize_scheduled_delivery(self, context: MessageContext, sent_message_id: Optional[str]) -> None:
        payload = context.platform_specific or {}
        if payload.get("turn_source") != "scheduled":
            return
        source_base_session_id = payload.get("turn_base_session_id") or ""
        strategy = payload.get("scheduled_delivery_alias") or {}
        mode = strategy.get("mode") or "none"
        if not source_base_session_id or mode == "none":
            return

        alias_base_session_id: Optional[str] = None
        if mode == "sent_message":
            if not sent_message_id:
                return
            alias_base_session_id = self.build_message_anchor_base(context, sent_message_id)
        elif mode == "fixed_base":
            alias_base_session_id = strategy.get("base_session_id")
        if not alias_base_session_id:
            return

        target_session_key = strategy.get("session_key") or self._get_session_key(context)
        clear_source = bool(strategy.get("clear_source", False))
        self.alias_session_base(
            context,
            source_base_session_id=source_base_session_id,
            alias_base_session_id=alias_base_session_id,
            target_session_key=target_session_key,
            clear_source=clear_source,
        )

        if mode == "sent_message" and sent_message_id:
            platform = self._get_context_platform(context)
            if platform in {"slack", "lark"}:
                delivery_channel_id = payload.get("delivery_override", {}).get("channel_id") or context.channel_id
                self.sessions.mark_thread_active("scheduled", delivery_channel_id, sent_message_id)

    def _supports_threaded_session(self, context: MessageContext, *, is_dm: bool) -> bool:
        getter = getattr(self.controller, "get_im_client_for_context", None)
        if callable(getter):
            try:
                im_client = getter(context)
            except AttributeError:
                im_client = getattr(self.controller, "im_client", None)
        else:
            im_client = getattr(self.controller, "im_client", None)

        if im_client is None:
            return False
        if is_dm:
            return bool(getattr(im_client, "should_use_thread_for_dm_session", lambda: False)())
        return bool(getattr(im_client, "should_use_thread_for_reply", lambda: False)())

    def get_working_path(self, context: MessageContext) -> str:
        """Get working directory - delegate to controller's get_cwd"""
        return self.controller.get_cwd(context)

    def _running_as_root(self) -> bool:
        geteuid = getattr(os, "geteuid", None)
        return bool(geteuid and geteuid() == 0)

    def _should_mark_claude_isolated_env(self) -> bool:
        if os.environ.get("IS_SANDBOX"):
            return False
        return self._running_as_root()

    async def _allow_claude_bypass_tool(self, tool_name: str, tool_input: Dict[str, Any], context: Any):
        logger.info("Auto-approving Claude tool permission request in avibe bypass mode: %s", tool_name)
        return PermissionResultAllow()

    def _get_claude_cli_path_override(self) -> Optional[str]:
        cli_path = getattr(getattr(self.config, "claude", None), "cli_path", None)
        if cli_path is None:
            return None

        normalized = str(cli_path).strip()
        if not normalized:
            return None

        if normalized == "claude":
            return None

        return os.path.expanduser(normalized)

    def _load_agent_file(self, agent_name: str, working_path: str) -> Optional[Dict[str, Any]]:
        """Load an agent file and return its parsed content.

        Searches for agent file in:
        1. Project agents: <working_path>/.claude/agents/<agent_name>.md
        2. Global agents: ~/.claude/agents/<agent_name>.md

        Returns:
            Dict with keys: name, description, prompt, tools, model
            or None if not found/parse error.
        """
        from pathlib import Path
        from vibe.api import parse_claude_agent_file

        # Search paths (project first, then global)
        search_paths = [
            Path(working_path) / ".claude" / "agents" / f"{agent_name}.md",
            Path.home() / ".claude" / "agents" / f"{agent_name}.md",
        ]

        for agent_path in search_paths:
            if agent_path.exists() and agent_path.is_file():
                parsed = parse_claude_agent_file(str(agent_path))
                if parsed:
                    return parsed
                else:
                    logger.warning(f"Failed to parse agent file: {agent_path}")

        logger.warning(f"Agent file not found for '{agent_name}' in {search_paths}")
        return None

    def get_session_info(self, context: MessageContext, source: str = "human") -> Tuple[str, str, str]:
        """Get session info: base_session_id, working_path, and composite_key"""
        base_session_id = self.get_base_session_id(context, source=source)
        resolve_target = getattr(self.controller, "resolve_agent_run_target", None)
        if callable(resolve_target):
            target = resolve_target(context, base_session_id=base_session_id, source=source)
            working_path = target.workdir
        else:
            working_path = self.get_working_path(context)
        # Create composite key for internal storage
        composite_key = f"{base_session_id}:{working_path}"
        return base_session_id, working_path, composite_key

    async def _prepare_resume_context(
        self,
        context: MessageContext,
        host_message_ts: Optional[str],
        is_dm: bool,
    ) -> MessageContext:
        im_client = self._get_im_client(context)
        prepare = getattr(im_client, "prepare_resume_context", None)
        if not callable(prepare):
            return context
        try:
            prepared = await prepare(context, host_message_ts=host_message_ts, is_dm=is_dm)
        except Exception as exc:
            logger.warning("Failed to prepare resume context for %s: %s", context.platform, exc)
            return context
        return prepared if isinstance(prepared, MessageContext) else context

    def _supports_resume_threading(self, context: MessageContext, *, is_dm: bool) -> bool:
        im_client = self._get_im_client(context)
        if is_dm:
            return bool(getattr(im_client, "should_use_thread_for_dm_session", lambda: False)())
        uses_thread_replies = bool(getattr(im_client, "should_use_thread_for_reply", lambda: False)())
        if not uses_thread_replies:
            return False
        if context.thread_id:
            return True
        uses_message_anchor = bool(
            getattr(im_client, "should_use_message_id_for_channel_session", lambda _context=None: True)(context)
        )
        return uses_message_anchor

    def _build_resume_confirmation(
        self,
        *,
        agent_label: str,
        session_id: str,
        preview: str = "",
    ) -> str:
        lines = [f"✅ {self._t('success.sessionResumed', agent=agent_label, sessionId=session_id)}"]
        if preview:
            lines.extend(["", preview])
        return "\n".join(lines)

    def _build_resume_followup(
        self,
        context: MessageContext,
        *,
        is_dm: bool,
    ) -> str:
        lines: list[str] = []
        platform = context.platform or self.config.platform
        if context.thread_id:
            if platform == "discord":
                lines.append(self._t("success.sessionResumedContinueDiscordThread"))
            elif platform == "lark":
                lines.append(self._t("success.sessionResumedContinueFeishuThread"))
            else:
                lines.append(self._t("success.sessionResumedContinueThread"))
            if not is_dm:
                lines.append(self._t("success.sessionResumedThreadFreshTip"))
        else:
            lines.append(self._t("success.sessionResumedContinueDirect"))
        return "\n".join(line for line in lines if line)

    def _get_resume_preview(
        self,
        context: MessageContext,
        *,
        agent: str,
        session_id: str,
    ) -> str:
        service_getter = getattr(self.controller, "get_native_session_service", None)
        if callable(service_getter):
            native_session_service = service_getter()
        else:
            native_session_service = getattr(self.controller, "native_session_service", None)
        if native_session_service is None:
            return ""
        try:
            working_path = self.get_working_path(context)
            item = native_session_service.get_session(working_path, agent, session_id)
        except Exception as exc:
            logger.warning("Failed to resolve resume preview for %s session %s: %s", agent, session_id, exc)
            return ""
        if item is None:
            return ""
        return build_resume_preview(item.last_agent_message or item.last_agent_tail)

    async def get_or_create_claude_session(
        self,
        context: MessageContext,
        subagent_name: Optional[str] = None,
        subagent_model: Optional[str] = None,
        subagent_reasoning_effort: Optional[str] = None,
        agent_system_prompt: Optional[str] = None,
    ) -> ClaudeSDKClient:
        """Get existing Claude session or create a new one"""
        payload = context.platform_specific or {}
        turn_source = str(payload.get("turn_source") or "human")
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        working_path = self.get_working_path(context)
        if base_session_id:
            composite_key = f"{base_session_id}:{working_path}"
        else:
            base_session_id, working_path, composite_key = self.get_session_info(context, source=turn_source)

        settings_key = self._get_settings_key(context)
        session_key = self._get_session_key(context)
        # Resume the native session bound to the RESERVED workbench row (by PK).
        # The bind WRITE (_bind_reserved_workbench_session) records the native on
        # that row by id; the resume READ must read it back from there, because the
        # (session_key, anchor) projection drifts for avibe (its scope/anchor differ
        # from where the native was bound) and a restart would otherwise fork a fresh
        # session and lose context. Skip it for ANY subagent — explicit (its own
        # session resolved below) OR a routing-default subagent (its namespaced base
        # has its own session) — else the first subagent turn after the subagent is
        # enabled would resume the MAIN transcript under the subagent. IM/CLI turns
        # carry no reserved target, so this is a no-op for them.
        routing_subagent = (getattr(context, "platform_specific", None) or {}).get("routing_subagent")
        stored_claude_session_id = self.sessions.get_claude_session_id(session_key, base_session_id)
        if not subagent_name and not routing_subagent:
            stored_claude_session_id = self._reserved_native_session_id(context) or stored_claude_session_id
        fork_source_claude_session_id: Optional[str] = None
        if not stored_claude_session_id and not subagent_name and not routing_subagent:
            fork_source_claude_session_id = pending_native_fork_source(context, "claude")

        # Read routing overrides via get_channel_routing which correctly
        # resolves DM users from the users store (not the stale channels store).
        routing = self._get_settings_manager(context).get_channel_routing(settings_key)

        # Priority: subagent params > channel config > agent frontmatter > global default
        # Note: agent frontmatter model is applied later after loading agent file
        effective_agent = subagent_name or (routing.claude_agent if routing else None)
        # Store explicit model override (not including default yet)
        from config.v2_settings import routing_model_for_backend, routing_reasoning_effort_for_backend

        explicit_model = subagent_model or routing_model_for_backend(routing, "claude")
        explicit_effort = subagent_reasoning_effort or routing_reasoning_effort_for_backend(routing, "claude")
        session_target = payload.get("agent_session_target")
        if isinstance(session_target, dict):
            explicit_model = subagent_model or session_target.get("model") or explicit_model
            explicit_effort = subagent_reasoning_effort or session_target.get("reasoning_effort") or explicit_effort

        if not effective_agent:
            # Claude SDK model changes are control requests; only send one when
            # the effective model actually changes.
            current_model = explicit_model or self.config.claude.default_model
            client = await self._reuse_cached_claude_session_if_available(
                composite_key=composite_key,
                base_session_id=base_session_id,
                working_path=working_path,
                context=context,
                session_key=session_key,
                stored_claude_session_id=stored_claude_session_id,
                current_model=current_model,
                agent_system_prompt=None,
            )
            if client is not None:
                return client

        if effective_agent:
            cached_base = f"{base_session_id}:{effective_agent}"
            cached_key = f"{cached_base}:{working_path}"
            cached_session_id = self.sessions.get_agent_session_id(
                session_key,
                cached_base,
                agent_name="claude",
            )
            client = await self._reuse_cached_claude_subagent_session_if_available(
                composite_key=cached_key,
                base_session_id=cached_base,
                working_path=working_path,
                context=context,
                session_key=session_key,
                native_session_id=cached_session_id,
                explicit_model=explicit_model,
            )
            if client is not None:
                return client
            # Always use agent-specific key when effective_agent is set
            # This ensures session continuity even on first use
            composite_key = cached_key
            base_session_id = cached_base
            if cached_session_id:
                stored_claude_session_id = cached_session_id
            else:
                stored_claude_session_id = None

        waiting_client = await self._wait_for_claude_session_create(composite_key)
        if waiting_client is not None:
            if effective_agent:
                client = await self._reuse_cached_claude_subagent_session_if_available(
                    composite_key=composite_key,
                    base_session_id=base_session_id,
                    working_path=working_path,
                    context=context,
                    session_key=session_key,
                    native_session_id=stored_claude_session_id,
                    explicit_model=explicit_model,
                )
            else:
                client = await self._reuse_cached_claude_session_if_available(
                    composite_key=composite_key,
                    base_session_id=base_session_id,
                    working_path=working_path,
                    context=context,
                    session_key=session_key,
                    stored_claude_session_id=stored_claude_session_id,
                    current_model=explicit_model or self.config.claude.default_model,
                    agent_system_prompt=None,
                )
            if client is not None:
                return client

        create_future = self._track_claude_session_create(composite_key)
        try:
            client = await self._create_claude_session(
                context=context,
                composite_key=composite_key,
                base_session_id=base_session_id,
                working_path=working_path,
                session_key=session_key,
                stored_claude_session_id=fork_source_claude_session_id or stored_claude_session_id,
                effective_agent=effective_agent,
                explicit_model=explicit_model,
                explicit_effort=explicit_effort,
                agent_system_prompt=agent_system_prompt,
                fork_session=bool(fork_source_claude_session_id),
            )
            if not create_future.done():
                create_future.set_result(client)
            return client
        except asyncio.CancelledError:
            if not create_future.done():
                create_future.set_result(None)
            raise
        except Exception:
            if not create_future.done():
                create_future.set_result(None)
            raise
        finally:
            self._untrack_claude_session_create(composite_key, create_future)

    async def _create_claude_session(
        self,
        *,
        context: MessageContext,
        composite_key: str,
        base_session_id: str,
        working_path: str,
        session_key: str,
        stored_claude_session_id: Optional[str],
        effective_agent: Optional[str],
        explicit_model: Optional[str],
        explicit_effort: Optional[str],
        agent_system_prompt: Optional[str],
        fork_session: bool = False,
    ) -> ClaudeSDKClient:

        # Ensure working directory exists
        if not os.path.exists(working_path):
            try:
                os.makedirs(working_path, exist_ok=True)
                logger.info(f"Created working directory: {working_path}")
            except Exception as e:
                logger.error(f"Failed to create working directory {working_path}: {e}")
                working_path = os.getcwd()

        # Build system prompt from agent file if subagent is specified
        # Claude Code has a bug where ~/.claude/agents/*.md files are not auto-discovered
        # See: https://github.com/anthropics/claude-code/issues/11205
        # Workaround: read the agent file and use its content as system_prompt
        agent_allowed_tools: Optional[list] = None
        agent_model: Optional[str] = None
        if effective_agent and agent_system_prompt is None:
            agent_data = self._load_agent_file(effective_agent, working_path)
            if agent_data:
                agent_system_prompt = agent_data.get("prompt")
                agent_allowed_tools = agent_data.get("tools")
                agent_model = agent_data.get("model")
                logger.info(f"Loaded agent '{effective_agent}' system prompt ({len(agent_system_prompt or '')} chars)")
                if agent_allowed_tools:
                    logger.info(f"  Agent allowed tools: {agent_allowed_tools}")
                if agent_model:
                    logger.info(f"  Agent model from frontmatter: {agent_model}")
            else:
                logger.warning(f"Could not load agent file for '{effective_agent}'")

        # Filter out special values that aren't actual model names
        if agent_model and agent_model.lower() in ("inherit", ""):
            agent_model = None

        # Determine final model: explicit override > agent frontmatter > global default
        effective_model = explicit_model or agent_model or self.config.claude.default_model
        from modules.agents.opencode.utils import normalize_claude_reasoning_effort

        effective_effort = normalize_claude_reasoning_effort(
            effective_model,
            explicit_effort,
            backend_model_catalog.catalog_reasoning_efforts_for_model("claude", effective_model),
        )

        # Determine final system prompt: agent prompt takes precedence over config.
        # Always append avibe system prompt injection so transport
        # capabilities remain available; reply_enhancements only controls
        # quick-reply button instructions.
        final_system_prompt = self._build_claude_system_prompt(
            context,
            session_key=session_key,
            agent_name="claude",
            session_anchor=base_session_id,
            agent_system_prompt=agent_system_prompt,
        )

        # Create extra_args for CLI passthrough (fallback for model)
        extra_args: Dict[str, str | None] = {}
        if effective_model:
            extra_args["model"] = effective_model

        claude_stderr_lines: list[str] = []

        def _capture_claude_stderr(line: str) -> None:
            text = (line or "").strip()
            if not text:
                return
            claude_stderr_lines.append(text)
            if len(claude_stderr_lines) > 40:
                del claude_stderr_lines[:-40]

        # V2Config-driven Anthropic env composition, centralised so the
        # control-channel client (``agent_auth_service``) cannot drift
        # away from this site's auth_mode handling.
        from vibe.claude_config import build_claude_subprocess_env

        claude_env = build_claude_subprocess_env(getattr(self.config, "claude", None))
        claude_env.update(caller_env_for_platform_payload(getattr(context, "platform_specific", None)))
        claude_env[AVIBE_CLAUDE_PROCESS_OWNER_ENV] = AVIBE_CLAUDE_SESSION_OWNER
        if self._should_mark_claude_isolated_env():
            claude_env["IS_SANDBOX"] = "1"
            logger.info("Detected Claude bypassPermissions running as root; marking Claude subprocess as isolated")

        option_kwargs: Dict[str, Any] = {
            "permission_mode": CLAUDE_REMOTE_PERMISSION_MODE,
            "cwd": working_path,
            "system_prompt": final_system_prompt,
            "resume": stored_claude_session_id if stored_claude_session_id else None,
            "fork_session": bool(fork_session and stored_claude_session_id),
            "extra_args": extra_args,
            "setting_sources": ["user", "project", "local"],  # Load all setting sources (user, project CLAUDE.md, local overrides)
            "sandbox": CLAUDE_REMOTE_SANDBOX,
            # Disable interactive-only Claude Code tools that remote IM sessions
            # cannot answer programmatically.
            "disallowed_tools": CLAUDE_REMOTE_DISALLOWED_TOOLS,
            "env": claude_env,  # Pass Anthropic/Claude env vars
            "stderr": _capture_claude_stderr,
            "max_buffer_size": CLAUDE_SDK_MAX_BUFFER_SIZE,
            "can_use_tool": self._allow_claude_bypass_tool,
        }
        cli_path_override = self._get_claude_cli_path_override()
        if cli_path_override:
            option_kwargs["cli_path"] = cli_path_override
        if effective_effort:
            option_kwargs["effort"] = effective_effort
        # Only set allowed_tools if agent file specifies tools.
        # Omitting the field keeps SDK default tool behavior.
        if agent_allowed_tools:
            option_kwargs["allowed_tools"] = agent_allowed_tools

        options = ClaudeAgentOptions(**option_kwargs)

        # Log session creation details
        logger.info(f"Creating Claude client for {base_session_id} at {working_path}")
        logger.info(f"  Working directory: {working_path}")
        logger.info(f"  Resume session ID: {stored_claude_session_id}")
        logger.info(f"  Options.resume: {options.resume}")
        logger.info(f"  Options.fork_session: {getattr(options, 'fork_session', False)}")
        if effective_agent:
            logger.info(f"  Subagent: {effective_agent}")
        if effective_model:
            logger.info(f"  Model: {effective_model}")
        if effective_effort:
            logger.info(f"  Effort: {effective_effort}")

        # Log if we're resuming a session
        if stored_claude_session_id:
            logger.info(f"Attempting to resume Claude session {stored_claude_session_id}")
        else:
            logger.info(f"Creating new Claude session")

        # Create new Claude client
        client = ClaudeSDKClient(options=options)
        setattr(client, "_vibe_caller_env", caller_env_for_platform_payload(getattr(context, "platform_specific", None)))

        # Log the actual options being used
        logger.info("ClaudeAgentOptions details:")
        logger.info(f"  - permission_mode: {options.permission_mode}")
        logger.info(f"  - cwd: {options.cwd}")
        logger.info(f"  - system_prompt: {options.system_prompt}")
        logger.info(f"  - resume: {options.resume}")
        logger.info(f"  - continue_conversation: {options.continue_conversation}")
        logger.info(f"  - cli_path: {options.cli_path}")
        if effective_agent:
            logger.info(f"  - subagent: {effective_agent}")

        # Connect the client
        try:
            await client.connect()
            governor_from_controller(self.controller).apply_to_pid(
                get_claude_client_pid(client),
                label="claude",
            )
        except Exception as exc:
            stderr_text = "\n".join(claude_stderr_lines)
            match = CLAUDE_NO_CONVERSATION_RE.search(stderr_text) or CLAUDE_NO_CONVERSATION_RE.search(str(exc))
            if match:
                # FAIL LOUD: a session bound to a native id that no longer resumes
                # (cwd changed, expired, or gone) surfaces the error rather than
                # silently starting a fresh session — silent recovery hides the
                # context loss and strands the user in an empty conversation
                # (product decision: no silent fallbacks). The persisted mapping is
                # kept so resuming in the correct cwd still works.
                raise ClaudeSessionNotFoundError(
                    session_id=match.group(1),
                    working_path=str(working_path),
                    stderr=stderr_text,
                ) from exc
            raise

        self.claude_sessions[composite_key] = client
        self.claude_system_prompts[composite_key] = final_system_prompt
        setattr(client, "_vibe_current_model", effective_model)
        self.bind_claude_runtime_session(
            client,
            base_session_id,
            composite_key,
            None if fork_session else stored_claude_session_id,
        )
        self.touch_session_activity(composite_key)
        logger.info(f"Created new Claude SDK client for {base_session_id} at {working_path}")

        return client

    def _build_claude_system_prompt(
        self,
        context: MessageContext,
        *,
        session_key: str,
        agent_name: str,
        session_anchor: str,
        agent_system_prompt: Optional[str],
    ) -> str | Dict[str, str]:
        base_prompt = agent_system_prompt or self.config.claude.system_prompt
        quick_replies_on = getattr(self.config, "reply_enhancements", True)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.config.platform

        self.ensure_agent_session_id(
            context,
            session_key=session_key,
            agent_name=agent_name,
            session_anchor=session_anchor,
        )

        system_prompt_injection = build_system_prompt_injection(
            include_quick_replies=quick_replies_on and platform != "wechat",
            include_show_pages=getattr(self.config, "show_pages_prompt", True),
            avibe_cloud_connected=avibe_cloud_url_available(self.config),
            context=context,
            fallback_platform=platform,
            enabled_agents=get_enabled_agents_for_prompt(self.controller),
            current_agent_backend="claude",
        )

        if base_prompt:
            return f"{base_prompt}\n\n{system_prompt_injection}"
        return {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt_injection,
        }

    async def _prepare_backend_for_resume(
        self,
        agent: str,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        """Let the backend prepare scoped runtime state before a resume bind."""
        agent_service = getattr(self.controller, "agent_service", None)
        backend = getattr(agent_service, "agents", {}).get(agent) if agent_service else None
        prepare = getattr(backend, "prepare_resume_binding", None)
        if callable(prepare):
            logger.info("Preparing %s runtime before resuming session %s", agent, base_session_id)
            await prepare(
                base_session_id=base_session_id,
                session_key=session_key,
                working_path=working_path,
            )

    async def handle_resume_session_submission(
        self,
        user_id: str,
        channel_id: Optional[str],
        thread_id: Optional[str],
        agent: Optional[str],
        session_id: Optional[str],
        host_message_ts: Optional[str] = None,
        is_dm: bool = False,
        platform: Optional[str] = None,
    ) -> None:
        """Bind a provided session_id to the current thread for the chosen agent."""
        from modules.settings_manager import ChannelRouting

        try:
            if not agent or not session_id:
                raise ValueError("Agent and session ID are required to resume.")

            if getattr(self.controller, "agent_service", None):
                available_agents = set(self.controller.agent_service.agents.keys())
                if agent not in available_agents:
                    raise ValueError(f"Agent '{agent}' is not enabled.")

            reuse_thread = True
            if host_message_ts and thread_id and thread_id == host_message_ts:
                reuse_thread = False

            target_thread = thread_id if reuse_thread else None

            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id or user_id,
                platform=platform or self.config.platform,
                thread_id=target_thread or None,
                message_id=host_message_ts or None,
                platform_specific={"is_dm": is_dm},
            )
            thread_capable = self._supports_resume_threading(context, is_dm=is_dm)

            settings_key = self._get_settings_key(context)
            session_key = self._get_session_key(context)
            settings_manager = self._get_settings_manager(context)
            current_routing = settings_manager.get_channel_routing(settings_key)
            preserve_scope_overrides = bool(
                current_routing and self._routing_matches_backend(current_routing, agent)
            )

            routing = ChannelRouting(
                agent_name=agent,
                model=current_routing.model if preserve_scope_overrides else None,
                reasoning_effort=current_routing.reasoning_effort if preserve_scope_overrides else None,
                opencode_agent=current_routing.opencode_agent if current_routing else None,
                claude_agent=current_routing.claude_agent if current_routing else None,
                codex_agent=current_routing.codex_agent if current_routing else None,
            )
            settings_manager.set_channel_routing(settings_key, routing)

            agent_label = agent.capitalize()
            preview = self._get_resume_preview(context, agent=agent, session_id=session_id)
            confirmation = self._build_resume_confirmation(
                agent_label=agent_label,
                session_id=session_id,
                preview=preview,
            )

            initial_context = context
            if thread_capable and not target_thread:
                initial_context = MessageContext(
                    user_id=context.user_id,
                    channel_id=context.channel_id,
                    platform=context.platform,
                    thread_id=None,
                    message_id=context.message_id,
                    platform_specific=context.platform_specific,
                    files=context.files,
                )

            confirmation_ts = await self._get_im_client(initial_context).send_message(
                initial_context, confirmation, parse_mode="markdown"
            )

            followup_context = context
            if thread_capable and not target_thread:
                anchor_context = MessageContext(
                    user_id=context.user_id,
                    channel_id=context.channel_id,
                    platform=context.platform,
                    thread_id=None,
                    message_id=confirmation_ts,
                    platform_specific=context.platform_specific,
                    files=context.files,
                )
                followup_context = await self._prepare_resume_context(anchor_context, confirmation_ts, is_dm)

            followup = self._build_resume_followup(followup_context, is_dm=is_dm)
            if followup:
                await self._get_im_client(followup_context).send_message(
                    followup_context,
                    followup,
                    parse_mode="markdown",
                )

            mapped_thread = followup_context.thread_id or confirmation_ts
            if thread_capable:
                mapping_context = MessageContext(
                    user_id=user_id,
                    channel_id=followup_context.channel_id,
                    platform=followup_context.platform,
                    thread_id=mapped_thread,
                    message_id=confirmation_ts,
                    platform_specific={"is_dm": is_dm},
                )
            else:
                mapping_context = MessageContext(
                    user_id=user_id,
                    channel_id=followup_context.channel_id,
                    platform=followup_context.platform,
                    thread_id=None,
                    message_id=None,
                    platform_specific={"is_dm": is_dm},
                )
            base_session_id = self.get_base_session_id(mapping_context)
            working_path = self.get_working_path(mapping_context)

            await self._prepare_backend_for_resume(
                agent,
                base_session_id=base_session_id,
                session_key=session_key,
                working_path=working_path,
            )

            # The anchor is the bare base for every backend. OpenCode no longer
            # folds working_path into the key (the cwd is a per-request param that
            # lives on the ``workdir`` column, not part of the thread identity), so
            # this writer must match the bare-anchor read path in
            # OpenCodeSessionManager.get_or_create_session_id — otherwise a resumed
            # OpenCode session is written under ``base:/cwd`` but the next message
            # looks up ``base`` and forks a different session.
            mapping_key = base_session_id

            # Resume creates a FRESH session record, never mutates an existing one:
            # clear any prior binding at this anchor first so the bind below INSERTs
            # a new row (new PK) bound to the user-selected native, instead of
            # UPDATE-ing the current row's native_session_id — which the write-once
            # guard would (correctly) drop, silently leaving the thread on its old
            # conversation (Codex P2). A no-op when the anchor is a brand-new
            # confirmation message (channel/DM resume).
            #
            # A thread is ONE session per (scope, anchor). If this anchor already
            # holds a row pinned to a DIFFERENT backend (e.g. a Feishu resume button
            # fired inside an existing thread, which bypasses the scope-only command
            # guard), clear that row too — otherwise the bind below collides with the
            # (scope_id, session_anchor) unique invariant and resume fails after
            # channel routing was already updated (Codex P2).
            finder = getattr(self.sessions, "find_session_for_anchor", None)
            if callable(finder):
                try:
                    prior = finder(session_key, mapping_key)
                except Exception:
                    prior = None
                prior_agent = str((prior or {}).get("agent_variant") or (prior or {}).get("agent_backend") or "")
                if prior_agent and prior_agent != agent:
                    self.sessions.remove_agent_session(session_key, prior_agent, mapping_key)
            self.sessions.remove_agent_session(session_key, agent, mapping_key)
            self.sessions.set_agent_session_mapping(session_key, agent, mapping_key, session_id)
            self.sessions.mark_thread_active(user_id, context.channel_id, mapped_thread)
        except Exception as e:
            logger.error(f"Error resuming session: {e}", exc_info=True)
            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id or user_id,
                platform=platform or self.config.platform,
                thread_id=thread_id or None,
                platform_specific={"is_dm": is_dm},
            )
            await self._get_im_client(context).send_message(
                context,
                f"❌ {self._t('error.resumeSubmitFailed', error=str(e))}",
            )

    def _routing_matches_backend(self, routing, backend: str) -> bool:
        agent_name = getattr(routing, "agent_name", None)
        if not agent_name:
            return False
        if str(agent_name) == str(backend):
            return True
        store = getattr(self.controller, "vibe_agent_store", None)
        if store is None:
            return False
        try:
            agent = store.get(str(agent_name))
        except Exception:
            return False
        return bool(agent and getattr(agent, "backend", None) == backend)

    async def cleanup_session(self, composite_key: str, *, current_receiver_task=None):
        """Clean up a specific session by composite key"""
        receiver_task = self.receiver_tasks.pop(composite_key, None)
        client = self.claude_sessions.pop(composite_key, None)
        cleanup_from_receiver = receiver_task is not None and receiver_task is current_receiver_task
        native_session_id = getattr(client, "_vibe_native_session_id", None)
        keep_pid = get_claude_client_pid(client)
        self.clear_session_tracking(composite_key)

        try:
            # Close the SDK client first so its receive stream can finish normally.
            # Cancelling the receiver first can leave the SDK's anyio cancel scope
            # retrying cancellation on every event-loop tick.
            if client is not None:
                if cleanup_from_receiver:
                    self._disconnect_client_after_receiver(client, composite_key, receiver_task)
                else:
                    await self._disconnect_client(client, composite_key)
        finally:
            if not cleanup_from_receiver:
                await self._stop_receiver_task(receiver_task, composite_key)
            await reap_duplicate_claude_resume_processes(
                native_session_id,
                keep_pid=keep_pid if cleanup_from_receiver else None,
                cli_path=self._get_claude_cli_path_override(),
                logger=logger,
            )

    async def _disconnect_client(self, client, composite_key: str) -> None:
        try:
            await client.disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting Claude session {composite_key}: {e}")
        logger.info(f"Cleaned up Claude session {composite_key}")

    def _disconnect_client_after_receiver(self, client, composite_key: str, receiver_task) -> None:
        async def _run() -> None:
            if receiver_task is not None:
                try:
                    await receiver_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning("Claude receiver ended with error before deferred disconnect: %s", e)
            await self._disconnect_client(client, composite_key)

        asyncio.create_task(_run())

    async def _stop_receiver_task(self, receiver_task, composite_key: str) -> None:
        if receiver_task is None:
            return
        receiver_result_retrieved = False
        if not receiver_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(receiver_task), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                pass
            except Exception as e:
                receiver_result_retrieved = True
                logger.warning("Claude receiver ended with error during cleanup: %s", e)
        if receiver_task.done() and not receiver_result_retrieved:
            self._drain_receiver_task_exception(receiver_task)
        if not receiver_task.done():
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        logger.info(f"Cancelled receiver task for session {composite_key}")

    @staticmethod
    def _drain_receiver_task_exception(receiver_task) -> None:
        try:
            exc = receiver_task.exception()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Error reading Claude receiver cleanup result: %s", e)
            return
        if exc is not None:
            logger.warning("Claude receiver ended with error during cleanup: %s", exc)

    async def evict_idle_sessions(
        self,
        idle_timeout: float,
        stuck_active_multiplier: float = DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER,
        stuck_active_floor_seconds: float = DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS,
    ) -> int:
        """Disconnect Claude sessions that have been idle beyond the timeout.

        A session is normally exempt from eviction while it is flagged
        ``active`` (a turn is in flight). That veto is **not** absolute: if the
        receiver coroutine never releases the flag (e.g. it stays alive but
        blocked on ``receive_messages`` with no stream EOF), the session would
        otherwise be pinned forever and its ``claude`` subprocess would survive
        until the next service restart. As an independent backstop, a session
        that is ``active`` but whose ``last_activity`` is older than
        ``max(idle_timeout * stuck_active_multiplier,
        stuck_active_floor_seconds)`` is force-evicted regardless of why the
        flag was not cleared. A genuine in-flight turn keeps touching
        ``last_activity`` via assistant/tool messages, so it normally stays well
        under this cap. Pass ``stuck_active_multiplier <= 0`` to disable the
        backstop. Caveat: a real turn whose single tool call runs silently for
        longer than the cap is indistinguishable from a stuck session and would
        be force-evicted — see ``DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER``.
        """
        if idle_timeout <= 0:
            return 0

        stuck_threshold = None
        if stuck_active_multiplier > 0:
            stuck_threshold = max(
                idle_timeout * stuck_active_multiplier,
                max(0.0, stuck_active_floor_seconds),
            )

        now = time.monotonic()
        expired: list[tuple[str, float]] = []

        for composite_key, last_activity in list(self.session_last_activity.items()):
            if composite_key not in self.claude_sessions:
                self.session_last_activity.pop(composite_key, None)
                self.session_turn_started.pop(composite_key, None)
                self.active_sessions.discard(composite_key)
                continue
            idle_for = now - last_activity
            if composite_key in self.active_sessions:
                # Stuck-active backstop: only evict once well past the cap.
                if stuck_threshold is not None and idle_for >= stuck_threshold:
                    expired.append((composite_key, idle_for))
                continue
            if idle_for >= idle_timeout:
                expired.append((composite_key, idle_for))

        evicted = 0
        for composite_key, idle_for in expired:
            current_last_activity = self.session_last_activity.get(composite_key)
            if composite_key not in self.claude_sessions:
                self.session_last_activity.pop(composite_key, None)
                self.session_turn_started.pop(composite_key, None)
                self.active_sessions.discard(composite_key)
                continue
            if current_last_activity is None:
                continue
            # Re-derive the decision from current state: a session may have been
            # touched or (de)activated between the two passes.
            recheck_idle = time.monotonic() - current_last_activity
            if composite_key in self.active_sessions:
                if stuck_threshold is None or recheck_idle < stuck_threshold:
                    continue
                logger.warning(
                    "Force-evicting stuck-active Claude session %s after %.1fs idle "
                    "(>= stuck-active threshold %.1fs; multiplier=%s idle_timeout=%ss); "
                    "receiver never released the active flag",
                    composite_key,
                    recheck_idle,
                    stuck_threshold,
                    stuck_active_multiplier,
                    idle_timeout,
                )
            else:
                if recheck_idle < idle_timeout:
                    continue
                logger.info("Evicting idle Claude session %s after %.1fs idle", composite_key, recheck_idle)
            if composite_key in self.active_sessions:
                agent_service = getattr(self.controller, "agent_service", None)
                claude_agent = getattr(agent_service, "agents", {}).get("claude") if agent_service else None
                force_cleanup = getattr(claude_agent, "force_cleanup_stuck_active_session", None)
                if callable(force_cleanup):
                    await force_cleanup(composite_key)
                else:
                    await self.cleanup_session(composite_key)
            else:
                await self.cleanup_session(composite_key)
            evicted += 1

        return evicted

    async def reap_orphaned_claude_sessions(self) -> int:
        """Reap leaked ``claude`` subprocesses not owned by any tracked session.

        Defense-in-depth backstop for the idle-eviction path: even if a session
        slips out of tracking (or a previous service instance left a child
        reparented to init), the resident ``claude`` subprocess is reconciled
        against the set of currently-tracked sessions and terminated when it has
        no owner. See ``reap_orphaned_claude_processes`` for the safety guards.
        """
        owned_pids: set[int] = set()
        tracked_resume_ids: dict[str, int] = {}
        owner_set_complete = True
        for client in list(self.claude_sessions.values()):
            pid = get_claude_client_pid(client)
            if not pid:
                # A tracked client whose pid we cannot resolve means the owner
                # set is incomplete: its live process would look ownerless to
                # the in-tree sweep. Disable that sweep this round.
                owner_set_complete = False
                continue
            owned_pids.add(pid)
            native_session_id = getattr(client, "_vibe_native_session_id", None)
            if native_session_id:
                tracked_resume_ids[str(native_session_id)] = pid
        # A session create in flight has spawned a subprocess (connect()) that is
        # not yet in claude_sessions; the in-tree sweep must not touch it.
        creates_in_flight = bool(self.claude_session_creates)
        exclude_pids: set[int] = set()
        watch_service = getattr(self.controller, "watch_service", None)
        active_watch_pids = getattr(watch_service, "active_process_pids", None)
        if callable(active_watch_pids):
            exclude_pids.update(active_watch_pids())
        auth_service = getattr(self.controller, "agent_auth_service", None)
        active_auth_pids = getattr(auth_service, "active_claude_auth_client_pids", None)
        if callable(active_auth_pids):
            exclude_pids.update(active_auth_pids())
        auth_pid_unknown = getattr(auth_service, "has_active_claude_auth_client_with_unknown_pid", None)
        auth_client_pid_unknown = bool(auth_pid_unknown()) if callable(auth_pid_unknown) else False
        # Let unexpected errors surface to the caller (``periodic_cleanup``
        # logs them at error level); ``reap_orphaned_claude_processes`` already
        # absorbs the expected ``ps``-read failure internally.
        return await reap_orphaned_claude_processes(
            owned_pids=owned_pids,
            tracked_resume_ids=tracked_resume_ids,
            cli_path=self._get_claude_cli_path_override(),
            logger=logger,
            reap_in_tree=owner_set_complete and not creates_in_flight and not auth_client_pid_unknown,
            exclude_pids=exclude_pids,
        )

    async def handle_session_error(self, composite_key: str, context: MessageContext, error: Exception):
        """Handle session-related errors"""
        error_msg = str(error)

        # Check for specific error types
        if isinstance(error, ClaudeSessionNotFoundError):
            logger.warning(
                "Claude session %s not found for current working directory %s; keeping persisted mapping unchanged",
                error.session_id,
                error.working_path,
            )
            await self._get_im_client(context).send_message(
                context,
                self._get_formatter(context).format_error(
                    self._t(
                        "error.claudeSessionNotFound",
                        sessionId=error.session_id,
                        path=error.working_path,
                    )
                ),
            )
        elif "read() called while another coroutine" in error_msg:
            logger.error(f"Session {composite_key} has concurrent read error - cleaning up")
            await self.cleanup_session(composite_key, current_receiver_task=asyncio.current_task())

            # Notify user and suggest retry
            await self._get_im_client(context).send_message(
                context,
                self._get_formatter(context).format_error(self._t("error.sessionReset")),
            )
        elif (
            "Session is broken" in error_msg
            or "Connection closed" in error_msg
            or "Connection lost" in error_msg
            # Claude Agent SDK raises this when one stdio JSON message exceeds
            # its line buffer; keep the match scoped to that transport fatal.
            or is_claude_sdk_buffer_error(error)
        ):
            logger.error(f"Session {composite_key} is broken - cleaning up")
            await self.cleanup_session(composite_key, current_receiver_task=asyncio.current_task())

            # Notify user
            await self._get_im_client(context).send_message(
                context,
                self._get_formatter(context).format_error(self._t("error.sessionConnectionLost")),
            )
        else:
            # Generic error handling
            logger.error(f"Error in session {composite_key}: {error}")
            await self._get_im_client(context).send_message(
                context,
                self._get_formatter(context).format_error(self._t("error.sessionGeneric", error=error_msg)),
            )

    def capture_session_id(
        self,
        base_session_id: str,
        claude_session_id: str,
        session_key: str,
        *,
        working_path: Optional[str] = None,
    ):
        """Capture and store Claude session ID mapping"""
        agent_session_id = self.bind_agent_session_id(
            session_key=session_key,
            agent_name="claude",
            session_anchor=base_session_id,
            native_session_id=claude_session_id,
            working_path=working_path,
        )
        logger.info(f"Captured Claude session_id: {claude_session_id} for {base_session_id}")
        composite_key = f"{base_session_id}:{working_path}" if working_path else None
        if composite_key:
            client = self.claude_sessions.get(composite_key)
            if client is not None:
                setattr(client, "_vibe_native_session_id", claude_session_id)
        return agent_session_id

    def ensure_agent_session_id(
        self,
        context: MessageContext,
        *,
        session_key: str,
        agent_name: str,
        session_anchor: str,
    ) -> Optional[str]:
        # avibe: pin the reserved workbench row id before any hidden-row creation
        # (mirrors BaseAgent.ensure_agent_session_id) so a pre-bind setup/query
        # failure persists the terminal notify under the OPEN Chat session rather
        # than a freshly-minted hidden row the page never sees (Codex P2).
        target = (getattr(context, "platform_specific", None) or {}).get("agent_session_target")
        if isinstance(target, dict) and target.get("id"):
            reserved_id = str(target["id"]).strip()
            if reserved_id:
                payload = dict(context.platform_specific or {})
                payload["agent_session_id"] = reserved_id
                context.platform_specific = payload
                return reserved_id
        ensure = getattr(self.sessions, "ensure_agent_session_id", None)
        if callable(ensure):
            agent_session_id = ensure(session_key, agent_name, session_anchor)
        else:
            getter = getattr(self.sessions, "get_agent_session_row_id", None)
            agent_session_id = getter(session_key, session_anchor, agent_name) if callable(getter) else None
        if not agent_session_id:
            return None
        payload = dict(context.platform_specific or {})
        payload["agent_session_id"] = agent_session_id
        context.platform_specific = payload
        return agent_session_id

    def bind_agent_session_id(
        self,
        *,
        session_key: str,
        agent_name: str,
        session_anchor: str,
        native_session_id: str,
        working_path: Optional[str] = None,
    ) -> Optional[str]:
        binder = getattr(self.sessions, "bind_agent_session", None)
        if callable(binder):
            return binder(
                session_key,
                agent_name,
                session_anchor,
                native_session_id,
                workdir=working_path,
            )
        self.sessions.set_agent_session_mapping(session_key, agent_name, session_anchor, native_session_id)
        getter = getattr(self.sessions, "get_agent_session_row_id", None)
        return getter(session_key, session_anchor, agent_name) if callable(getter) else None

    def attach_agent_session_id(
        self,
        context: MessageContext,
        *,
        session_key: str,
        agent_name: str,
        session_anchor: str,
    ) -> Optional[str]:
        return self.ensure_agent_session_id(
            context,
            session_key=session_key,
            agent_name=agent_name,
            session_anchor=session_anchor,
        )

    def restore_session_mappings(self):
        """Restore session mappings from settings on startup"""
        logger.info("Initializing session mappings from saved settings...")

        session_state = self.sessions.get_all_session_mappings()

        restored_count = 0
        for user_id, agent_map in session_state.items():
            claude_map = agent_map.get("claude", {}) if isinstance(agent_map, dict) else {}
            for thread_id, claude_session_id in claude_map.items():
                if isinstance(claude_session_id, str):
                    logger.info(f"  - {thread_id} -> {claude_session_id} (user {user_id})")
                    restored_count += 1

        logger.info(f"Session restoration complete. Restored {restored_count} session mappings.")
