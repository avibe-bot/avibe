"""Session management handlers for Claude SDK sessions"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Dict, Any, Tuple
from uuid import uuid4
from modules.im import MessageContext
from modules.claude_sdk_compat import (
    CLAUDE_SDK_HOOKS_AVAILABLE,
    CLAUDE_SDK_MAX_BUFFER_SIZE,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    is_claude_sdk_buffer_error,
)
from modules.agents.native_sessions.base import build_resume_preview
from modules.agents.claude_process_reaper import (
    AVIBE_CLAUDE_PROCESS_OWNER_ENV,
    AVIBE_CLAUDE_SESSION_OWNER,
    get_claude_client_pid,
    get_claude_client_returncode,
    get_claude_client_stderr_tail,
    claude_process_exit_reason,
    claude_process_exit_reason_i18n,
    register_claude_owned_process,
    reap_duplicate_claude_resume_processes,
    reap_orphaned_claude_processes,
)
from config.v2_config import (
    DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS,
    DEFAULT_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER,
)
from core.agent_tool_policy import (
    ALWAYS_SESSION_ONLY_TOOL_NAMES,
    check_tool_call,
    native_background_tools_allowed,
    session_only_background_tool_names,
)
from core.avibe_cloud import avibe_cloud_url_available
from core.agent_session_context import resolve_context_agent_session_target
from core.caller_context import caller_env_for_platform_payload
from core.message_context import build_thread_session_anchor, resolve_context_thread_id
from core.resource_governance import governor_from_controller
from core.runtime_activation import RuntimeActivationIdentity
from core.services.session_fork import pending_native_fork_source
from core.system_prompt_injection import (
    build_system_prompt_injection,
    get_enabled_agents_for_prompt,
    memory_cli_prompt_admitted,
)
from vibe import backend_model_catalog

from .base import BaseHandler

logger = logging.getLogger(__name__)

# A Claude CLI process the service terminated on purpose surfaces as SIGTERM
# (-15) or the SIGKILL escalation (-9). The SDK may report it as a returncode
# or only as text on the transport error, so both shapes are recognized. The
# text arrives in two shapes: the SDK transport reader raises "Command failed
# with exit code -9", while write failures raise "Cannot write to terminated
# process (exit code: -9)".
CLAUDE_TEARDOWN_RETURNCODES = frozenset({-9, -15})
CLAUDE_TEARDOWN_EXIT_PATTERN = re.compile(r"exit code:?\s*(-9|-15)\b")
# Teardown intent is dropped as soon as a new client takes the key; this bound
# only limits how long a stale record can survive when that never happens.
CLAUDE_INTENTIONAL_TEARDOWN_TTL_SECONDS = 120.0

if TYPE_CHECKING:
    from core.runtime_ownership import RuntimeResourceTarget
    from modules.agents.model_hub import ModelHubLaunch

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


class _ClaudeReceiverCleanupRequired(RuntimeError):
    """Signal that a dead generation must be retried after receiver cleanup."""

    def __init__(self, composite_key: str):
        self.composite_key = composite_key
        super().__init__(f"Claude receiver cleanup is still pending for {composite_key}")


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
        self.claude_runtime_generation_locks = getattr(
            controller,
            "claude_runtime_generation_locks",
            {},
        )
        self.claude_intentional_teardowns = getattr(
            controller,
            "claude_intentional_teardowns",
            {},
        )
        controller.session_last_activity = self.session_last_activity
        controller.session_turn_started = self.session_turn_started
        controller.claude_active_sessions = self.active_sessions
        controller.claude_system_prompts = self.claude_system_prompts
        controller.claude_session_creates = self.claude_session_creates
        controller.claude_runtime_generation_locks = self.claude_runtime_generation_locks
        controller.claude_intentional_teardowns = self.claude_intentional_teardowns

    @staticmethod
    def _cached_claude_subagent_model(
        explicit_model: Optional[str],
        model_hub_launch: "ModelHubLaunch",
    ) -> Optional[str]:
        if model_hub_launch.channel in {"native_cli", "hub"}:
            return model_hub_launch.runtime_model
        return explicit_model

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

    def mark_session_turn_started(self, composite_key: str) -> None:
        """Record exact native acceptance as the fresh progress baseline."""

        if not composite_key:
            return
        now = time.monotonic()
        self.active_sessions.add(composite_key)
        self.session_turn_started[composite_key] = now
        self.session_last_activity[composite_key] = now

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

    def _live_claude_client_pids(self) -> tuple[set[int], bool]:
        """Claude CLI pids owned by registered clients, and whether that set is whole.

        The duplicate reaper matches on the native ``--resume`` id, which is
        reused across reconnects and therefore cannot distinguish a leaked
        generation from a live client for the same session. Membership in
        ``claude_sessions`` is the authoritative ownership signal, so these
        pids are never eligible for a duplicate reap.

        A registered client whose pid cannot be resolved is still an owner — it
        just cannot be named — so the second element reports whether every
        owner is accounted for. ``reap_orphaned_claude_sessions`` already draws
        this distinction with ``owner_set_complete``; without it a partial set
        reads as the whole ownership picture and a live replacement resuming
        this native id can be selected by the process-table scan.
        """
        pids: set[int] = set()
        complete = True
        for client in list(self.claude_sessions.values()):
            pid = get_claude_client_pid(client)
            if pid:
                pids.add(pid)
            else:
                complete = False
        return pids, complete

    def _mark_claude_teardown_intentional(self, composite_key: str, client) -> None:
        """Record that this generation is being torn down by the service."""
        if client is not None:
            setattr(client, "_vibe_intentional_teardown", True)
        if not composite_key:
            return
        now = time.monotonic()
        # Keys that never get a replacement client would otherwise keep their
        # record forever; drop expired ones on the way in.
        for key, started_at in list(self.claude_intentional_teardowns.items()):
            if now - started_at > CLAUDE_INTENTIONAL_TEARDOWN_TTL_SECONDS:
                self.claude_intentional_teardowns.pop(key, None)
        self.claude_intentional_teardowns[composite_key] = now

    def _clear_claude_teardown_intent(self, composite_key: str) -> None:
        """Drop the teardown record once a new generation owns the key.

        A fresh client must never inherit the previous generation's teardown
        marker, otherwise its own failures would be silently swallowed.

        The key-level record is therefore the *replacement's* view, not the
        torn-down generation's: an in-flight query from the old generation can
        still reach ``handle_session_error`` after this runs. That case is
        carried by the per-client ``_vibe_intentional_teardown`` attribute
        instead, which is why callers pass the exact client whose failure they
        observed rather than letting the handler re-read ``claude_sessions``.
        """
        if composite_key:
            self.claude_intentional_teardowns.pop(composite_key, None)

    def _is_intentional_teardown_signal(
        self,
        composite_key: str,
        error: Exception,
        returncode: Optional[int],
        client=None,
    ) -> bool:
        """Was this failure our own SIGTERM/SIGKILL against a torn-down client?

        Cleanup escalates SIGTERM to SIGKILL, so the SDK reports ``exit code
        -15``/``-9`` for a process the service killed on purpose. Reporting
        that as a session error makes a deliberate teardown look like a backend
        crash.
        """
        if not getattr(client, "_vibe_intentional_teardown", False):
            started_at = self.claude_intentional_teardowns.get(composite_key)
            if started_at is None:
                return False
            if time.monotonic() - started_at > CLAUDE_INTENTIONAL_TEARDOWN_TTL_SECONDS:
                self.claude_intentional_teardowns.pop(composite_key, None)
                return False
        if returncode is not None:
            return returncode in CLAUDE_TEARDOWN_RETURNCODES
        return bool(CLAUDE_TEARDOWN_EXIT_PATTERN.search(str(error)))

    def claude_teardown_is_intentional(
        self,
        composite_key: str,
        error: Exception,
        *,
        client=None,
    ) -> bool:
        """Public probe: is this failure a teardown the service performed itself?

        Callers need the answer *before* they record backend-health evidence.
        ``record_model_hub_native_failure`` converts the pending attempt into a
        failed one, so classifying only afterwards lets operational cleanup
        settle a Model Hub source as unhealthy for a process nobody reported a
        fault in.
        """
        resolved = client if client is not None else self.claude_sessions.get(composite_key)
        return self._is_intentional_teardown_signal(
            composite_key,
            error,
            get_claude_client_returncode(resolved),
            resolved,
        )

    async def _wait_for_claude_session_idle(self, composite_key: str) -> None:
        while composite_key in self.active_sessions:
            await asyncio.sleep(0.05)

    async def _wait_for_claude_receiver_cleanup(self, composite_key: str) -> None:
        """Wait for the receiver task to finish its post-error cleanup."""
        receiver_task = self.receiver_tasks.get(composite_key)
        if receiver_task is None or receiver_task is asyncio.current_task():
            return
        try:
            await asyncio.shield(receiver_task)
        except asyncio.CancelledError:
            if receiver_task.cancelled():
                return
            raise
        except Exception:
            # The receiver already reported its failure; cleanup must still retire
            # the cached client and drain the task without masking that failure.
            logger.debug(
                "Claude receiver task ended with an error while waiting for cleanup: %s",
                composite_key,
                exc_info=True,
            )

    def bind_claude_runtime_session(
        self,
        client: ClaudeSDKClient,
        base_session_id: str,
        composite_key: str,
        native_session_id: Optional[str] = None,
        *,
        working_path: str,
        fallback_session_key: str,
        agent_session_id: Optional[str],
    ) -> None:
        """Attach the resolved Claude runtime keys to the connected client."""
        setattr(client, "_vibe_runtime_base_session_id", base_session_id)
        setattr(client, "_vibe_runtime_session_key", composite_key)
        setattr(client, "_vibe_runtime_workdir", working_path)
        setattr(client, "_vibe_runtime_fallback_session_key", fallback_session_key)
        setattr(client, "_vibe_agent_session_id", str(agent_session_id or "").strip())
        self._attach_claude_runtime_activation(client, composite_key)
        if native_session_id:
            setattr(client, "_vibe_native_session_id", native_session_id)
        register_claude_owned_process(
            client,
            native_session_id=native_session_id,
            owner=AVIBE_CLAUDE_SESSION_OWNER,
        )

    def _runtime_activation_registry(self):
        return getattr(self.controller, "runtime_activation", None)

    @staticmethod
    def _claude_runtime_activation_identity(
        client: ClaudeSDKClient | None,
    ) -> RuntimeActivationIdentity | None:
        identity = getattr(client, "_vibe_runtime_activation_identity", None)
        return identity if isinstance(identity, RuntimeActivationIdentity) else None

    def _attach_claude_runtime_activation(
        self,
        client: ClaudeSDKClient,
        composite_key: str,
    ) -> RuntimeActivationIdentity | None:
        registry = self._runtime_activation_registry()
        if registry is None:
            return None
        existing = self._claude_runtime_activation_identity(client)
        if existing is not None and registry.is_current(existing):
            return existing
        identity = registry.attach("claude", composite_key)
        setattr(client, "_vibe_runtime_activation_identity", identity)
        return identity

    def _retire_claude_runtime_activation(
        self,
        composite_key: str,
        client: ClaudeSDKClient,
        final_predicate,
    ) -> bool:
        registry = self._runtime_activation_registry()
        if registry is None:
            return bool(final_predicate())
        identity = self._claude_runtime_activation_identity(client)
        if identity is None:
            identity = self._attach_claude_runtime_activation(client, composite_key)
        if identity is None:
            return False
        return bool(registry.retire_if_current(identity, final_predicate))

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

    @staticmethod
    def _claude_git_path_state(working_path: str) -> str:
        from core.git_runtime import prepend_vendored_git_to_path

        env: dict[str, str] = {}
        prepend_vendored_git_to_path(
            env,
            base_env=os.environ,
            working_dir=working_path,
        )
        return env["PATH"] if "PATH" in env else os.environ.get("PATH", "")

    async def _evict_terminated_cached_claude_session(
        self,
        composite_key: str,
        client: ClaudeSDKClient,
    ) -> bool:
        returncode = get_claude_client_returncode(client)
        if returncode is None:
            return False

        reason = claude_process_exit_reason(returncode)
        stderr_tail = get_claude_client_stderr_tail(client)
        diagnostic = f"\nClaude stderr tail:\n{stderr_tail}" if stderr_tail else ""
        logger.warning(
            "Recreating cached Claude SDK client for %s because its process terminated (%s)%s",
            composite_key,
            reason,
            diagnostic,
        )
        receiver_task = self.receiver_tasks.get(composite_key)
        if (
            receiver_task is not None
            and receiver_task is not asyncio.current_task()
            and not receiver_task.done()
        ):
            # This method runs under the generation lock. Let the receiver
            # release that lock through its normal error cleanup before waiting
            # for the task, otherwise both sides wait on one another.
            raise _ClaudeReceiverCleanupRequired(composite_key)
        await self._wait_for_claude_receiver_cleanup(composite_key)
        await self._wait_for_claude_session_idle(composite_key)
        await self._cleanup_session_locked(
            composite_key,
            # A dead cached client may have been launched through Model Hub even
            # when the current turn resolves to a different channel. Retire the
            # cached generation's process credential before recreating it.
            retire_model_hub_scope=True,
        )
        return True

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
        model_hub_launch: "ModelHubLaunch",
    ) -> ClaudeSDKClient | None:
        client = self.claude_sessions.get(composite_key)
        if client is None:
            return None
        if await self._evict_terminated_cached_claude_session(
            composite_key,
            client,
        ):
            return None
        if getattr(client, "_vibe_model_hub_fingerprint", "direct") != model_hub_launch.fingerprint:
            logger.info("Recreating cached Claude SDK client because Model Hub channel changed")
            await self._wait_for_claude_session_idle(composite_key)
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
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
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
            return None

        caller_env = self._caller_env_for_context(context)
        if getattr(client, "_vibe_caller_env", {}) != caller_env:
            logger.info(
                "Recreating cached Claude SDK client for %s because caller context env changed",
                composite_key,
            )
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
            return None
        git_path_state = self._claude_git_path_state(working_path)
        if getattr(client, "_vibe_git_path_state", None) != git_path_state:
            logger.info(
                "Recreating cached Claude SDK client for %s because Git PATH changed",
                composite_key,
            )
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
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
            working_path=working_path,
            fallback_session_key=session_key,
            agent_session_id=self.ensure_agent_session_id(
                context,
                session_key=session_key,
                agent_name="claude",
                session_anchor=base_session_id,
                working_path=working_path,
            ),
        )
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
        desired_model: Optional[str],
        effective_agent: str,
        agent_system_prompt: Optional[str],
        model_hub_launch: "ModelHubLaunch",
    ) -> ClaudeSDKClient | None:
        client = self.claude_sessions.get(composite_key)
        if client is None:
            return None
        if await self._evict_terminated_cached_claude_session(
            composite_key,
            client,
        ):
            return None
        if getattr(client, "_vibe_model_hub_fingerprint", "direct") != model_hub_launch.fingerprint:
            logger.info("Recreating cached Claude subagent SDK client because Model Hub channel changed")
            await self._wait_for_claude_session_idle(composite_key)
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
            return None
        self.ensure_agent_session_id(
            context,
            session_key=session_key,
            agent_name="claude",
            session_anchor=base_session_id,
        )
        next_agent_system_prompt = agent_system_prompt
        if next_agent_system_prompt is None:
            agent_data = self._load_agent_file(effective_agent, working_path)
            next_agent_system_prompt = agent_data.get("prompt") if agent_data else None
        next_system_prompt = self._build_claude_system_prompt(
            context=context,
            session_key=session_key,
            agent_name="claude",
            session_anchor=base_session_id,
            agent_system_prompt=next_agent_system_prompt,
        )
        if self.claude_system_prompts.get(composite_key) != next_system_prompt:
            logger.info(
                "Recreating cached Claude subagent SDK client for %s because avibe system prompt changed",
                composite_key,
            )
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
            return None
        caller_env = self._caller_env_for_context(context)
        if getattr(client, "_vibe_caller_env", {}) != caller_env:
            logger.info(
                "Recreating cached Claude subagent SDK client for %s because caller context env changed",
                composite_key,
            )
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
            return None
        git_path_state = self._claude_git_path_state(working_path)
        if getattr(client, "_vibe_git_path_state", None) != git_path_state:
            logger.info(
                "Recreating cached Claude subagent SDK client for %s because Git PATH changed",
                composite_key,
            )
            await self._cleanup_session_locked(
                composite_key,
                retire_model_hub_scope=model_hub_launch.channel == "direct",
            )
            return None
        if desired_model:
            try:
                await self._set_claude_model_if_needed(client, desired_model)
            except Exception as e:
                logger.warning(f"Failed to update model on cached Claude subagent session: {e}")
        logger.info(
            "Using Claude subagent session for %s at %s (model_override=%s)",
            base_session_id,
            working_path,
            desired_model,
        )
        self.bind_claude_runtime_session(
            client,
            base_session_id,
            composite_key,
            native_session_id,
            working_path=working_path,
            fallback_session_key=session_key,
            agent_session_id=self.ensure_agent_session_id(
                context,
                session_key=session_key,
                agent_name="claude",
                session_anchor=base_session_id,
                working_path=working_path,
            ),
        )
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
        # Retire the key-level teardown record as soon as a replacement starts
        # being built, not once it registers. A new generation that dies with
        # ``-9`` inside ``connect()`` never reaches registration, so the caller
        # reports it with ``client=None`` — and a marker still standing from the
        # previous teardown would classify that genuine crash as our own
        # cleanup, suppressing both the IM notification and the durable Web Chat
        # error row. The old generation keeps its own coverage through the
        # per-client ``_vibe_intentional_teardown`` attribute.
        self._clear_claude_teardown_intent(composite_key)
        return future

    def _untrack_claude_session_create(self, composite_key: str, future: asyncio.Future) -> None:
        if self.claude_session_creates.get(composite_key) is future:
            self.claude_session_creates.pop(composite_key, None)

    def get_base_session_id(self, context: MessageContext, source: str = "human") -> str:
        """Get base session ID based on platform and context (without path)"""
        platform = self._get_context_platform(context)
        session_target = resolve_context_agent_session_target(context)
        if session_target:
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
            resolved_thread_id = resolve_context_thread_id(context)
            base_id = resolved_thread_id or context.thread_id
            if platform == "telegram" and base_id:
                return build_thread_session_anchor(platform, context.channel_id, base_id)
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
        """Native session id bound to the selected persisted row (by PK).

        This includes explicit Workbench targets and rows selected for IM turns.
        Only returns the native when the row's backend is Claude; after a backend
        switch, the previous backend's native id must not be resumed."""
        target = resolve_context_agent_session_target(context)
        if not target:
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

    def _caller_env_for_context(self, context: MessageContext) -> dict[str, str]:
        """Caller identity AND creation origin for the Agent subprocess env.

        The typed context is passed alongside the platform payload because an
        IM-created Harness definition is created by an Agent run executing
        ``vibe task add`` — this env is the only channel between the conversation that
        asked for it and the ``created_by.caller`` row that records where it came from.
        Dropping the ids here is what left the failure ladder's owner-DM rung dormant
        and the notice unable to name its origin.

        The platform is resolved through ``_get_context_platform`` rather than off the
        context alone, so the captured origin agrees with the platform every other
        session-scoped decision in this handler is made with (the Slack adapter sets
        neither ``context.platform`` nor a payload ``platform``).

        SESSION-STABLE ONLY, and that is not a shortcut. This env is baked into a Claude
        SDK client at spawn and is also the value compared to decide whether a cached
        client may be reused, so ordinary IM author/message fields are dropped. A trusted
        remote Workbench ACL snapshot is retained: changing remote identity or revision
        must recreate the client rather than let one user's resource authority leak into
        another user's turn. ``CallerContext.session_stable`` documents both cases.
        """

        return caller_env_for_platform_payload(
            getattr(context, "platform_specific", None),
            message=context,
            fallback_platform=self._get_context_platform(context),
            session_stable_only=True,
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

    @staticmethod
    def _is_reserved_session_anchor(context: MessageContext, base_session_id: str) -> bool:
        """True when this anchor names a reserved, durable Agent Session row.

        ``clear_source`` is decided upstream by ``_build_delivery_alias_strategy`` from
        ``session_target.thread_id is None``. That is a proxy for "this is a throwaway
        channel-level anchor that the delivered message should replace". A durable
        ``--create-session`` definition anchor satisfies the same proxy:
        ``thread_id_from_session_anchor`` splits the anchor at ``:``, so
        ``<platform>_<channel_id>:definition_<uuid>`` reduces to ``<platform>_<channel_id>``
        and the derived thread id equals the channel id, which the function reports as
        ``None``. The proxy therefore cannot tell a provisional anchor from a durable one.

        The clear is a HARD delete of the ``agent_sessions`` row, while the definition keeps
        its now-dangling ``run_definitions.session_id``. Every later fire then dies at
        dispatch with ``agent session id not found``, silently, at ``last_status=failed``
        with no result text, and the definition never recovers on its own.

        Decide from the same authority ``get_base_session_id`` used to mint the anchor: a
        context bound to a reserved Agent Session row is never provisional. Reading the
        reserved row keeps this a fact check rather than a second anchor-format parser that
        would drift from the minting side in ``_session_anchor_with_suffix``.
        """
        reserved = resolve_context_agent_session_target(context)
        if not reserved:
            return False
        reserved_anchor = str(reserved.get("session_anchor") or "").strip()
        return bool(reserved_anchor) and reserved_anchor == str(base_session_id or "").strip()

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
        clear_source = bool(strategy.get("clear_source", False)) and not self._is_reserved_session_anchor(
            context, source_base_session_id
        )
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

    async def _guard_session_only_background_tools(
        self,
        input_data: Dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> Dict[str, Any]:
        """PreToolUse hook governing backend-native, session-only background work.

        Runs in-process, so it needs no file in the user's `~/.claude`. The deny
        reason names the durable `vibe ...` equivalent, which lets the agent
        self-correct within the same turn instead of just failing.

        An advisory outcome deliberately omits `permissionDecision`: injecting
        context must not double as an approval, or this hook would override a
        permission hook the user configured for the same tool.
        """
        try:
            tool_name = str(input_data.get("tool_name") or "")
            tool_input = input_data.get("tool_input") or {}
            decision = check_tool_call(tool_name, tool_input)
            if decision.allowed:
                if not decision.advice:
                    return {}
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": decision.advice,
                    }
                }
            logger.info(
                "Blocking session-only background tool %s; redirecting to the Avibe Harness",
                tool_name,
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.reason,
                }
            }
        except Exception:
            # A guard that raises must never take the turn down with it.
            logger.exception("Session-only background tool guard failed; allowing the call")
            return {}

    def _build_claude_tool_policy_hooks(self) -> Optional[Dict[str, Any]]:
        """Hook config enforcing the shared tool policy, or None when unavailable."""
        if not CLAUDE_SDK_HOOKS_AVAILABLE or HookMatcher is None:
            return None
        if native_background_tools_allowed():
            return None
        matcher = "|".join(session_only_background_tool_names())
        return {
            "PreToolUse": [
                HookMatcher(
                    matcher=matcher,
                    hooks=[self._guard_session_only_background_tools],
                )
            ]
        }

    def _claude_disallowed_tools(self, hooks: Optional[Dict[str, Any]]) -> list:
        """Disallowed tool names for this launch.

        The hook is the precise enforcement path because it can read arguments and
        return an actionable reason. Only when hooks are unavailable does the
        coarse name-level deny list stand in, and then only for the tools that are
        session-only under every input.
        """
        disallowed = list(CLAUDE_REMOTE_DISALLOWED_TOOLS)
        if hooks is None and not native_background_tools_allowed():
            disallowed.extend(
                name for name in ALWAYS_SESSION_ONLY_TOOL_NAMES if name not in disallowed
            )
        return disallowed

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

    def _claude_runtime_generation_lock(self, composite_key: str) -> asyncio.Lock:
        return self.claude_runtime_generation_locks.setdefault(
            composite_key,
            asyncio.Lock(),
        )

    def _claude_runtime_generation_key(
        self,
        context: MessageContext,
        subagent_name: Optional[str],
    ) -> str:
        payload = context.platform_specific or {}
        turn_source = str(payload.get("turn_source") or "human")
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        working_path = self.get_working_path(context)
        if not base_session_id:
            base_session_id, working_path, _ = self.get_session_info(
                context,
                source=turn_source,
            )
        settings_key = self._get_settings_key(context)
        routing = self._get_settings_manager(context).get_channel_routing(settings_key)
        effective_agent = subagent_name or (routing.claude_agent if routing else None)
        if effective_agent:
            base_session_id = f"{base_session_id}:{effective_agent}"
        return f"{base_session_id}:{working_path}"

    async def get_or_create_claude_session(
        self,
        context: MessageContext,
        subagent_name: Optional[str] = None,
        subagent_model: Optional[str] = None,
        subagent_reasoning_effort: Optional[str] = None,
        agent_system_prompt: Optional[str] = None,
    ) -> ClaudeSDKClient:
        """Resolve or create one Claude runtime generation under its exact lock."""

        composite_key = self._claude_runtime_generation_key(context, subagent_name)
        while True:
            try:
                async with self._claude_runtime_generation_lock(composite_key):
                    return await self._get_or_create_claude_session_locked(
                        context,
                        subagent_name=subagent_name,
                        subagent_model=subagent_model,
                        subagent_reasoning_effort=subagent_reasoning_effort,
                        agent_system_prompt=agent_system_prompt,
                    )
            except _ClaudeReceiverCleanupRequired as retry:
                # Receiver error handling may need the same generation lock. Wait
                # only after the lock has been released, then retry resolution.
                await self._wait_for_claude_receiver_cleanup(retry.composite_key)

    async def _get_or_create_claude_session_locked(
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

        # Priority: subagent params > channel config > Agent model.
        # Note: agent frontmatter model is applied later after loading agent file
        effective_agent = subagent_name or (routing.claude_agent if routing else None)
        # Store explicit model override (not including default yet)
        from config.v2_settings import routing_model_for_backend, routing_reasoning_effort_for_backend

        explicit_model = subagent_model or routing_model_for_backend(routing, "claude")
        explicit_effort = subagent_reasoning_effort or routing_reasoning_effort_for_backend(routing, "claude")
        session_target = resolve_context_agent_session_target(context)
        if session_target:
            explicit_model = subagent_model or session_target.get("model") or explicit_model
            explicit_effort = subagent_reasoning_effort or session_target.get("reasoning_effort") or explicit_effort

        launch_model = explicit_model
        if not launch_model and effective_agent:
            launch_agent_data = self._load_agent_file(effective_agent, working_path)
            configured_agent_model = launch_agent_data.get("model") if launch_agent_data else None
            if configured_agent_model and configured_agent_model.lower() not in ("inherit", ""):
                launch_model = configured_agent_model
        cached_base = (
            f"{base_session_id}:{effective_agent}"
            if effective_agent
            else None
        )
        cached_key = (
            f"{cached_base}:{working_path}"
            if cached_base is not None
            else None
        )
        from modules.agents.model_hub import bind_launch, resolve_model_hub_launch

        model_hub_launch = await resolve_model_hub_launch(
            self.controller,
            "claude",
            launch_model or "",
            process_scope=cached_key or composite_key,
        )
        bind_launch(context, model_hub_launch)
        runtime_model = model_hub_launch.runtime_model or launch_model
        cached_subagent_model = self._cached_claude_subagent_model(explicit_model, model_hub_launch)

        if not effective_agent:
            # Claude SDK model changes are control requests; only send one when
            # the effective model actually changes.
            current_model = runtime_model
            client = await self._reuse_cached_claude_session_if_available(
                composite_key=composite_key,
                base_session_id=base_session_id,
                working_path=working_path,
                context=context,
                session_key=session_key,
                stored_claude_session_id=stored_claude_session_id,
                current_model=current_model,
                agent_system_prompt=None,
                model_hub_launch=model_hub_launch,
            )
            if client is not None:
                return client

        if effective_agent:
            assert cached_base is not None and cached_key is not None
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
                desired_model=cached_subagent_model,
                effective_agent=effective_agent,
                agent_system_prompt=agent_system_prompt,
                model_hub_launch=model_hub_launch,
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
                    desired_model=cached_subagent_model,
                    effective_agent=effective_agent,
                    agent_system_prompt=agent_system_prompt,
                    model_hub_launch=model_hub_launch,
                )
            else:
                client = await self._reuse_cached_claude_session_if_available(
                    composite_key=composite_key,
                    base_session_id=base_session_id,
                    working_path=working_path,
                    context=context,
                    session_key=session_key,
                    stored_claude_session_id=stored_claude_session_id,
                    current_model=runtime_model,
                    agent_system_prompt=None,
                    model_hub_launch=model_hub_launch,
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

        # The routed Vibe Agent model is materialized into ``explicit_model``.
        effective_model = explicit_model or agent_model
        from modules.agents.model_hub import (
            build_claude_hub_env,
            claude_setting_sources_for_launch,
            launch_for_context,
        )

        model_hub_launch = launch_for_context(context)
        runtime_model = (
            model_hub_launch.runtime_model
            if model_hub_launch is not None and model_hub_launch.backend == "claude"
            else effective_model
        )
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

        # Echo native input frames so the long-lived receiver can correlate
        # accepted steering with Claude's actual input consumption.
        extra_args: Dict[str, str | None] = {"replay-user-messages": None}
        if runtime_model:
            extra_args["model"] = runtime_model

        claude_stderr_lines: list[str] = []

        def _capture_claude_stderr(line: str) -> None:
            text = (line or "").strip()
            if not text:
                return
            claude_stderr_lines.append(text)
            if len(claude_stderr_lines) > 40:
                del claude_stderr_lines[:-40]
            logger.warning("Claude CLI stderr for %s: %s", composite_key, text)

        # V2Config-driven Anthropic env composition, centralised so the
        # control-channel client (``agent_auth_service``) cannot drift
        # away from this site's auth_mode handling.
        from vibe.claude_config import (
            CLAUDE_MEMORY_DISABLED_SETTINGS,
            build_claude_subprocess_env,
        )
        from core.git_runtime import prepend_vendored_git_to_path

        claude_env = build_claude_subprocess_env(getattr(self.config, "claude", None))
        if model_hub_launch is not None:
            claude_env = build_claude_hub_env(claude_env, model_hub_launch)
        claude_env.update(self._caller_env_for_context(context))
        prepend_vendored_git_to_path(
            claude_env,
            base_env=os.environ,
            working_dir=working_path,
        )
        git_path_state = claude_env["PATH"] if "PATH" in claude_env else os.environ.get("PATH", "")
        claude_env[AVIBE_CLAUDE_PROCESS_OWNER_ENV] = AVIBE_CLAUDE_SESSION_OWNER
        if self._should_mark_claude_isolated_env():
            claude_env["IS_SANDBOX"] = "1"
            logger.info("Detected Claude bypassPermissions running as root; marking Claude subprocess as isolated")

        tool_policy_hooks = self._build_claude_tool_policy_hooks()

        option_kwargs: Dict[str, Any] = {
            "permission_mode": CLAUDE_REMOTE_PERMISSION_MODE,
            "cwd": working_path,
            "system_prompt": final_system_prompt,
            "resume": stored_claude_session_id if stored_claude_session_id else None,
            "fork_session": bool(fork_session and stored_claude_session_id),
            "extra_args": extra_args,
            "settings": CLAUDE_MEMORY_DISABLED_SETTINGS,
            "setting_sources": claude_setting_sources_for_launch(model_hub_launch),
            "sandbox": CLAUDE_REMOTE_SANDBOX,
            # Disable interactive-only Claude Code tools that remote IM sessions
            # cannot answer programmatically, plus any session-only background
            # tool the hook path cannot cover.
            "disallowed_tools": self._claude_disallowed_tools(tool_policy_hooks),
            "env": claude_env,  # Pass Anthropic/Claude env vars
            "stderr": _capture_claude_stderr,
            "max_buffer_size": CLAUDE_SDK_MAX_BUFFER_SIZE,
            "can_use_tool": self._allow_claude_bypass_tool,
        }
        if tool_policy_hooks:
            option_kwargs["hooks"] = tool_policy_hooks
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
        setattr(client, "_vibe_stderr_lines", claude_stderr_lines)
        setattr(client, "_vibe_caller_env", self._caller_env_for_context(context))
        setattr(client, "_vibe_git_path_state", git_path_state)
        setattr(
            client,
            "_vibe_model_hub_fingerprint",
            model_hub_launch.fingerprint if model_hub_launch is not None else "direct",
        )

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
            router = getattr(self.controller, "model_hub_runtime", None)
            record_failure = getattr(router, "record_native_failure", None)
            if callable(record_failure):
                try:
                    await record_failure(context, str(exc))
                except Exception:
                    logger.warning(
                        "Failed to record Model Hub Claude startup failure",
                        exc_info=True,
                    )
            terminal_turn_id = str(
                (getattr(context, "platform_specific", None) or {}).get(
                    "turn_token"
                )
                or ""
            ).strip()
            self._retire_model_hub_process_scope(
                composite_key,
                terminal_turn_id=terminal_turn_id or None,
            )
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

        self.claude_system_prompts[composite_key] = final_system_prompt
        setattr(client, "_vibe_current_model", effective_model)
        self.bind_claude_runtime_session(
            client,
            base_session_id,
            composite_key,
            None if fork_session else stored_claude_session_id,
            working_path=working_path,
            fallback_session_key=session_key,
            agent_session_id=self.ensure_agent_session_id(
                context,
                session_key=session_key,
                agent_name="claude",
                session_anchor=base_session_id,
                working_path=working_path,
            ),
        )
        self.claude_sessions[composite_key] = client
        # Normally already cleared when the create was tracked; kept as the
        # backstop for any path that builds a client without going through
        # ``_track_claude_session_create``.
        self._clear_claude_teardown_intent(composite_key)
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

        # Resolve admission once: it associates or clears this turn's Memory CLI
        # session scope as a side effect, so a second call per turn would repeat
        # that write.
        memory_cli_admitted = memory_cli_prompt_admitted(self.controller, context)

        system_prompt_injection = build_system_prompt_injection(
            include_quick_replies=quick_replies_on and platform != "wechat",
            include_show_pages=getattr(self.config, "show_pages_prompt", True),
            include_memory_cli=memory_cli_admitted,
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

    def _retire_model_hub_process_scope(
        self,
        composite_key: str,
        *,
        terminal_turn_id: Optional[str] = None,
    ) -> None:
        router = getattr(self.controller, "model_hub_runtime", None)
        retire = getattr(router, "retire_process_scope", None)
        if callable(retire):
            if terminal_turn_id is None:
                retire("claude", composite_key)
            else:
                retire(
                    "claude",
                    composite_key,
                    terminal_turn_id=terminal_turn_id,
                )

    async def cleanup_session(
        self,
        composite_key: str,
        *,
        current_receiver_task=None,
        retire_model_hub_scope: bool = True,
        activation_retired: bool = False,
        expected_client=None,
    ):
        """Clean up one Claude generation under the same lock used by creation.

        ``expected_client`` names the generation the caller means to retire.
        Cleanup resolves the composite key again under the lock, so a caller
        acting on a client that has since been replaced would otherwise tear
        down the healthy replacement instead.
        """

        async with self._claude_runtime_generation_lock(composite_key):
            await self._cleanup_session_locked(
                composite_key,
                current_receiver_task=current_receiver_task,
                retire_model_hub_scope=retire_model_hub_scope,
                activation_retired=activation_retired,
                expected_client=expected_client,
            )

    async def _cleanup_session_locked(
        self,
        composite_key: str,
        *,
        current_receiver_task=None,
        retire_model_hub_scope: bool = True,
        activation_retired: bool = False,
        expected_client=None,
    ):
        """Clean up a specific session by composite key"""
        client = self.claude_sessions.get(composite_key)
        if expected_client is not None and client is not expected_client:
            # The named generation is gone: either already retired, or replaced
            # by a client that owns the key now. Containing a stale teardown
            # must not become a second teardown.
            logger.info(
                "Skipping Claude cleanup for session %s: the named generation no longer owns the key",
                composite_key,
            )
            return
        activation_retired = activation_retired or bool(
            getattr(client, "_vibe_runtime_activation_retired", False)
        )
        if client is not None and not activation_retired:
            if not self._retire_claude_runtime_activation(
                composite_key,
                client,
                lambda: self.claude_sessions.get(composite_key) is client,
            ):
                return
        receiver_task = self.receiver_tasks.pop(composite_key, None)
        client = self.claude_sessions.pop(composite_key, None)
        if client is not None and retire_model_hub_scope:
            self._retire_model_hub_process_scope(composite_key)
        cleanup_from_receiver = receiver_task is not None and receiver_task is current_receiver_task
        native_session_id = getattr(client, "_vibe_native_session_id", None)
        keep_pid = get_claude_client_pid(client)
        self.clear_session_tracking(composite_key)
        if client is not None or receiver_task is not None:
            # Only a generation that actually existed can produce the signal the
            # marker explains. Recording one for an already-empty key leaves a
            # 120s window in which the NEXT client's genuine ``-9`` — say, dying
            # inside ``connect()`` before registration can clear the record — is
            # suppressed as though the service had killed it.
            self._mark_claude_teardown_intentional(composite_key, client)

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
            # ``disconnect()`` on a hung client can block for seconds, so the
            # owner set is re-read here rather than before it: by now another
            # client may legitimately own a process resuming the same native
            # session id, and it must not be reaped as a duplicate.
            await self._reap_duplicate_resume_processes(
                composite_key,
                native_session_id,
                keep_pid=keep_pid if cleanup_from_receiver else None,
            )

    async def _reap_duplicate_resume_processes(
        self,
        composite_key: str,
        native_session_id: Optional[str],
        *,
        keep_pid: Optional[int],
    ) -> None:
        """Reap leftover processes for one native session id, ownership-first.

        Generation locks are per composite key, so another key can be starting
        a client for the same native session id right now. Between
        ``connect()`` and registration that client owns a live subprocess that
        ``claude_sessions`` cannot yet report, and ``keep_pid`` is ``None`` on
        every cleanup that does not run from the receiver task — so the reap is
        deferred rather than run against an incomplete owner set. The same
        applies when a registered client's pid cannot be resolved. The periodic
        orphan reaper, which reconciles against the persisted ownership
        registry, remains the backstop for anything left behind.
        """
        if not native_session_id:
            return
        creates_in_flight = [key for key in self.claude_session_creates if key != composite_key]
        if creates_in_flight:
            logger.info(
                "Deferring duplicate Claude reap for session %s: %d client create(s) in flight",
                composite_key,
                len(creates_in_flight),
            )
            return
        owned_pids, owner_set_complete = self._live_claude_client_pids()
        if not owner_set_complete:
            logger.info(
                "Deferring duplicate Claude reap for session %s: a registered client's pid is unresolved",
                composite_key,
            )
            return
        await reap_duplicate_claude_resume_processes(
            native_session_id,
            keep_pid=keep_pid,
            exclude_pids=owned_pids,
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

    def _claude_runtime_ownership_target(
        self,
        composite_key: str,
        client: ClaudeSDKClient,
        *,
        known_activity_keys: tuple[str, ...] | None = None,
        known_route_keys: tuple[str, ...] | None = None,
    ) -> "RuntimeResourceTarget | None":
        from core.runtime_ownership import (
            RuntimeResourceTarget,
            RuntimeSessionBinding,
        )

        base_session_id = str(
            getattr(client, "_vibe_runtime_base_session_id", "") or ""
        ).strip()
        workdir = str(getattr(client, "_vibe_runtime_workdir", "") or "").strip()
        fallback_session_key = str(
            getattr(client, "_vibe_runtime_fallback_session_key", "") or ""
        ).strip()
        agent_session_id = str(
            getattr(client, "_vibe_agent_session_id", "") or ""
        ).strip()
        runtime_key = str(
            getattr(client, "_vibe_runtime_session_key", "") or ""
        ).strip()
        if (
            not base_session_id
            or not agent_session_id
            or not workdir
            or not fallback_session_key
            or runtime_key != composite_key
        ):
            logger.error(
                "Claude runtime ownership mapping is incomplete for resource=%s",
                composite_key,
            )
            return None
        if known_activity_keys is None or known_route_keys is None:
            known_activity_keys, known_route_keys = (
                self._claude_runtime_ownership_known_keys()
            )
        return RuntimeResourceTarget(
            backend="claude",
            resource_key=composite_key,
            bindings=(
                RuntimeSessionBinding(
                    session_id=agent_session_id,
                    session_anchor=base_session_id,
                    workdir=workdir,
                    activity_runtime_keys=(runtime_key,),
                    fallback_route_keys=(fallback_session_key,),
                ),
            ),
            known_activity_runtime_keys=known_activity_keys,
            known_fallback_route_keys=known_route_keys,
        )

    def _claude_runtime_ownership_known_keys(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        clients = tuple(self.claude_sessions.values())
        return (
            tuple(
                sorted(
                    {
                        str(
                            getattr(
                                candidate,
                                "_vibe_runtime_session_key",
                                "",
                            )
                            or ""
                        )
                        for candidate in clients
                        if getattr(candidate, "_vibe_runtime_session_key", None)
                    }
                )
            ),
            tuple(
                sorted(
                    {
                        str(
                            getattr(
                                candidate,
                                "_vibe_runtime_fallback_session_key",
                                "",
                            )
                            or ""
                        )
                        for candidate in clients
                        if getattr(
                            candidate,
                            "_vibe_runtime_fallback_session_key",
                            None,
                        )
                    }
                )
            ),
        )

    def _claude_runtime_ownership_targets(
        self,
        items: tuple[tuple[str, ClaudeSDKClient], ...] | None = None,
    ) -> dict[str, "RuntimeResourceTarget"]:
        selected = items if items is not None else tuple(self.claude_sessions.items())
        known_activity_keys, known_route_keys = (
            self._claude_runtime_ownership_known_keys()
        )
        targets = {}
        for composite_key, client in selected:
            target = self._claude_runtime_ownership_target(
                composite_key,
                client,
                known_activity_keys=known_activity_keys,
                known_route_keys=known_route_keys,
            )
            if target is not None:
                targets[composite_key] = target
        return targets

    def _claude_runtime_ownership_snapshot(
        self,
        composite_key: str,
        client: ClaudeSDKClient,
    ):
        from core.runtime_ownership import wake_runtime_ownership

        target = self._claude_runtime_ownership_target(composite_key, client)
        provider = getattr(self.controller, "runtime_ownership", None)
        snapshot = getattr(provider, "snapshot", None)
        if target is None or not callable(snapshot):
            logger.error(
                "Claude runtime ownership provider unavailable for resource=%s",
                composite_key,
            )
            return None
        result = snapshot(target)
        wake_runtime_ownership(self.controller, result)
        return result

    def runtime_ownership_snapshots(self) -> tuple[Any, ...] | None:
        from core.runtime_ownership import wake_runtime_ownership

        items = tuple(self.claude_sessions.items())
        targets_by_key = self._claude_runtime_ownership_targets(items)
        if len(targets_by_key) != len(items):
            return None
        provider = getattr(self.controller, "runtime_ownership", None)
        snapshot_many = getattr(provider, "snapshot_many", None)
        if not callable(snapshot_many):
            logger.error("Claude runtime ownership batch provider unavailable")
            return None
        results = tuple(snapshot_many(tuple(targets_by_key.values())))
        if len(results) != len(targets_by_key):
            logger.error("Claude runtime ownership batch returned incomplete results")
            return None
        for result in results:
            wake_runtime_ownership(self.controller, result)
        return results

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
        from core.runtime_ownership import SessionRuntimeDisposition

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

        ownership_items = tuple(
            (composite_key, client)
            for composite_key in self.session_last_activity
            if (client := self.claude_sessions.get(composite_key)) is not None
        )
        targets_by_key = self._claude_runtime_ownership_targets(ownership_items)
        provider = getattr(self.controller, "runtime_ownership", None)
        snapshot_many = getattr(provider, "snapshot_many", None)
        ownership_by_key = {}
        if targets_by_key and callable(snapshot_many):
            from core.runtime_ownership import wake_runtime_ownership

            results = await asyncio.to_thread(
                snapshot_many,
                tuple(targets_by_key.values()),
            )
            ownership_by_key = {
                result.resource_key: result for result in results
            }
            if len(ownership_by_key) != len(targets_by_key):
                logger.error(
                    "Claude runtime ownership batch returned incomplete results"
                )
                ownership_by_key = {}
            else:
                for result in results:
                    wake_runtime_ownership(self.controller, result)
        elif targets_by_key:
            logger.error("Claude runtime ownership batch provider unavailable")

        for composite_key, last_activity in list(self.session_last_activity.items()):
            client = self.claude_sessions.get(composite_key)
            if client is None:
                self.session_last_activity.pop(composite_key, None)
                self.session_turn_started.pop(composite_key, None)
                self.active_sessions.discard(composite_key)
                continue
            ownership = ownership_by_key.get(composite_key)
            if ownership is None:
                continue
            idle_for = now - last_activity
            if composite_key in self.active_sessions:
                # Stuck-active backstop: only evict once well past the cap.
                if stuck_threshold is not None and idle_for >= stuck_threshold:
                    if ownership.disposition in {
                        SessionRuntimeDisposition.TRANSITIONING,
                        SessionRuntimeDisposition.UNKNOWN,
                    }:
                        continue
                    expired.append((composite_key, idle_for))
                continue
            if not ownership.blocks_reclamation and idle_for >= idle_timeout:
                expired.append((composite_key, idle_for))

        evicted = 0
        for composite_key, idle_for in expired:
            lock = self._claude_runtime_generation_lock(composite_key)
            async with lock:
                client = self.claude_sessions.get(composite_key)
                if client is None:
                    self.session_last_activity.pop(composite_key, None)
                    self.session_turn_started.pop(composite_key, None)
                    self.active_sessions.discard(composite_key)
                    continue
                target = self._claude_runtime_ownership_targets(
                    ((composite_key, client),)
                ).get(composite_key)
                snapshot = getattr(provider, "snapshot", None)
                if target is None or not callable(snapshot):
                    continue
                registry = self._runtime_activation_registry()
                reservation = None
                if registry is not None:
                    identity = self._claude_runtime_activation_identity(client)
                    if identity is None:
                        identity = self._attach_claude_runtime_activation(
                            client,
                            composite_key,
                        )
                    if identity is None:
                        continue
                    reservation = registry.reserve_retirement(identity)
                    if reservation is None:
                        continue
                retired = False
                final: dict[str, Any] = {}
                try:
                    ownership = await asyncio.to_thread(snapshot, target)
                    from core.runtime_ownership import wake_runtime_ownership

                    wake_runtime_ownership(self.controller, ownership)
                    current_last_activity = self.session_last_activity.get(
                        composite_key
                    )
                    if (
                        self.claude_sessions.get(composite_key) is client
                        and current_last_activity is not None
                    ):
                        recheck_idle = time.monotonic() - current_last_activity
                        final["idle_for"] = recheck_idle
                        is_active = composite_key in self.active_sessions
                        final["active"] = is_active
                        if is_active:
                            allowed = bool(
                                stuck_threshold is not None
                                and recheck_idle >= stuck_threshold
                                and ownership.disposition
                                not in {
                                    SessionRuntimeDisposition.TRANSITIONING,
                                    SessionRuntimeDisposition.UNKNOWN,
                                }
                            )
                        else:
                            allowed = bool(
                                not ownership.blocks_reclamation
                                and recheck_idle >= idle_timeout
                            )
                    else:
                        allowed = False
                    if reservation is None:
                        retired = allowed
                    else:
                        retired = bool(
                            registry.finish_retirement(
                                reservation,
                                retire=allowed,
                            )
                            and allowed
                        )
                        reservation = None
                finally:
                    if reservation is not None:
                        registry.finish_retirement(reservation, retire=False)
                if not retired:
                    continue
                setattr(client, "_vibe_runtime_activation_retired", True)
                recheck_idle = float(final.get("idle_for", idle_for))
                if final.get("active"):
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
                    agent_service = getattr(self.controller, "agent_service", None)
                    claude_agent = (
                        getattr(agent_service, "agents", {}).get("claude")
                        if agent_service
                        else None
                    )
                    force_cleanup = getattr(
                        claude_agent,
                        "force_cleanup_stuck_active_session",
                        None,
                    )
                    if callable(force_cleanup):
                        await force_cleanup(
                            composite_key,
                            runtime_lock_held=True,
                        )
                    else:
                        await self._cleanup_session_locked(
                            composite_key,
                            activation_retired=True,
                        )
                else:
                    logger.info(
                        "Evicting idle Claude session %s after %.1fs idle",
                        composite_key,
                        recheck_idle,
                    )
                    await self._cleanup_session_locked(
                        composite_key,
                        activation_retired=True,
                    )
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

    async def handle_session_error(
        self,
        composite_key: str,
        context: MessageContext,
        error: Exception,
        *,
        client=None,
    ) -> bool:
        """Handle session-related errors.

        Returns ``True`` when the failure was contained (already explained to
        the user, or deliberately silenced), so callers can skip persisting a
        durable failure notification for it.

        ``client`` is the exact client whose failure the caller observed. It
        matters when a replacement has already registered under the same
        composite key: the torn-down client was popped from ``claude_sessions``
        and only it carries the teardown marker, so re-reading the map here
        would classify a delayed old-generation ``-9`` against the healthy
        replacement and report it as a genuine failure.
        """
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
            return False

        if client is None:
            client = self.claude_sessions.get(composite_key)
        returncode = get_claude_client_returncode(client)
        if self._is_intentional_teardown_signal(composite_key, error, returncode, client):
            # The service killed this process itself (idle eviction, duplicate
            # reap, shutdown). Surfacing it would report a deliberate teardown
            # as an unexplained backend failure.
            logger.warning(
                "Claude session %s ended on a service-initiated teardown signal: %s",
                composite_key,
                error_msg,
            )
            await self.cleanup_session(
                composite_key,
                current_receiver_task=asyncio.current_task(),
                # This failure may belong to a generation a replacement has
                # already taken over from; retire that exact client or nothing.
                expected_client=client,
            )
            return True
        if returncode is not None:
            reason_key, reason_values = claude_process_exit_reason_i18n(returncode)
            reason = self._t(reason_key, **reason_values)
            diagnostic = self.claude_error_diagnostic(composite_key, error)
            logger.error(
                "Claude process for session %s terminated (%s): %s",
                composite_key,
                reason,
                diagnostic,
            )
            # Same generation guard as the contained branch above: this branch is
            # reached off the CALLER's client, which a replacement may already
            # have superseded. Reporting a stale crash is right; retiring the
            # live client that replaced it is not.
            await self.cleanup_session(
                composite_key,
                current_receiver_task=asyncio.current_task(),
                expected_client=client,
            )
            await self._get_im_client(context).send_message(
                context,
                self._get_formatter(context).format_error(
                    self._t("error.claudeProcessTerminated", reason=reason)
                ),
            )
            return False
        if "read() called while another coroutine" in error_msg:
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
        return False

    def claude_error_diagnostic(self, composite_key: str, error: Exception) -> str:
        """Add process state and captured stderr to a Claude failure diagnostic."""
        diagnostic = str(error)
        client = self.claude_sessions.get(composite_key)
        returncode = get_claude_client_returncode(client)
        if returncode is not None:
            diagnostic = f"{diagnostic}\nClaude process terminated: {claude_process_exit_reason(returncode)}"
        stderr_tail = get_claude_client_stderr_tail(client)
        if stderr_tail:
            diagnostic = f"{diagnostic}\nClaude stderr tail:\n{stderr_tail}"
        return diagnostic

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
                setattr(client, "_vibe_agent_session_id", str(agent_session_id or ""))
        return agent_session_id

    def ensure_agent_session_id(
        self,
        context: MessageContext,
        *,
        session_key: str,
        agent_name: str,
        session_anchor: str,
        working_path: Optional[str] = None,
        vibe_agent_id: Optional[str] = None,
        vibe_agent_name: Optional[str] = None,
    ) -> Optional[str]:
        # avibe: pin the reserved workbench row id before any hidden-row creation
        # (mirrors BaseAgent.ensure_agent_session_id) so a pre-bind setup/query
        # failure persists the terminal notify under the OPEN Chat session rather
        # than a freshly-minted hidden row the page never sees (Codex P2).
        target = resolve_context_agent_session_target(context)
        if target and target.get("id"):
            reserved_id = str(target["id"]).strip()
            if reserved_id:
                payload = dict(context.platform_specific or {})
                payload["agent_session_id"] = reserved_id
                context.platform_specific = payload
                return reserved_id
        ensure = getattr(self.sessions, "ensure_agent_session_id", None)
        if callable(ensure):
            ensure_kwargs: dict[str, Any] = {}
            if working_path is not None:
                ensure_kwargs["workdir"] = working_path
            if vibe_agent_id is not None:
                ensure_kwargs["vibe_agent_id"] = vibe_agent_id
            if vibe_agent_name is not None:
                ensure_kwargs["vibe_agent_name"] = vibe_agent_name
            agent_session_id = ensure(
                session_key,
                agent_name,
                session_anchor,
                **ensure_kwargs,
            )
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
